from fastapi import FastAPI
from pydantic import BaseModel, HttpUrl
from typing import Optional, List

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
import os

GOOGLE_CSE_API_KEY = "AIzaSyC3OsQjBX6CFbs-V-PNDjC4iaDB0nWWNOk"
GOOGLE_CSE_CX = "a2c879beabb934cb6"

app = FastAPI(
    title="Ayna Brand Harvester",
    description="Given a brand/company + optional URLs, return good product/hero images for outreach.",
    version="3.0.0",
)

# ---------- MODELS ----------

class EnrichBrandRequest(BaseModel):
    company_name: Optional[str] = None
    website_url: Optional[HttpUrl] = None
    sku_url: Optional[HttpUrl] = None


class EnrichBrandResponse(BaseModel):
    company_name: Optional[str] = None
    resolved_website_url: Optional[HttpUrl] = None

    # Primary “chosen” (for backwards compatibility)
    chosen_product_url: Optional[HttpUrl] = None
    chosen_image_url: Optional[HttpUrl] = None
    candidate_image_urls: List[HttpUrl] = []

    # Website-derived image(s)
    website_product_url: Optional[HttpUrl] = None
    website_image_url: Optional[HttpUrl] = None
    website_candidate_image_urls: List[HttpUrl] = []

    # Marketplace-derived image(s)
    marketplace_product_url: Optional[HttpUrl] = None
    marketplace_image_url: Optional[HttpUrl] = None
    marketplace_candidate_image_urls: List[HttpUrl] = []

    notes: Optional[str] = None


# ---------- HTML PARSING ----------

def extract_image_urls_from_html(html: str, base_url: str) -> List[str]:
    """
    Robust image extractor for fashion / product pages.

    - Reads og:image / twitter:image meta tags
    - Handles <img src>, data-src, data-original, data-img, data-lazy
    - Handles <source srcset> inside <picture>
    - Handles inline CSS background-image
    - Normalises to absolute URLs
    - Filters out obvious junk (icons, logos, sprites, chevrons, banners)
    """
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []

    def add_url(url: str):
        if not url:
            return

        url = url.strip()

        # Protocol-relative -> assume https
        if url.startswith("//"):
            url = "https:" + url

        # Make absolute if relative
        url = urljoin(base_url, url)

        lower = url.lower()

        # Keep only likely image URLs (allow query params)
        if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".avif"]):
            return

        # Filter obvious UI junk
        bad_tokens = [
            "sprite", "icon", "logo", "placeholder",
            "chevron", "arrow", "banner", "nav", "favicon"
        ]
        if any(tok in lower for tok in bad_tokens):
            return

        # Myntra-specific: drop constant UI assets
        if "myntra" in base_url.lower() or "myntra" in lower:
            if "constant.myntassets.com/web/assets/img" in lower:
                return

        if url not in urls:
            urls.append(url)

    # 1) META TAGS: og:image, twitter:image, etc.
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name")
        if not prop:
            continue
        prop = prop.lower()
        if prop in ("og:image", "twitter:image", "twitter:image:src"):
            content = meta.get("content")
            if content:
                add_url(content)

    # 2) <img> tags with various lazy-load attributes
    for img in soup.find_all("img"):
        for attr in ["src", "data-src", "data-original", "data-img", "data-lazy"]:
            if img.has_attr(attr):
                add_url(img.get(attr))

    # 3) <source srcset="..."> inside <picture>
    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if not srcset:
            continue
        for item in srcset.split(","):
            candidate = item.strip().split(" ")[0]
            add_url(candidate)

    # 4) Inline CSS background-image / background
    for tag in soup.find_all(style=True):
        style = tag["style"]
        match = re.search(r'url\(["\']?(.*?)["\']?\)', style)
        if match:
            add_url(match.group(1))
    # 5) Myntra-specific fallback: scan for CDN URLs in raw HTML
    lower_html = html.lower()
    if "myntra" in base_url.lower() or "myntra" in lower_html:
        cdn_pattern = re.compile(
            r"https://assets\.myntassets\.com/[^\s\"')]+?\.(?:jpg|jpeg|png|webp|avif)",
            re.IGNORECASE,
        )
        for match in cdn_pattern.findall(html):
            add_url(match)
    return urls


# ---------- HELPERS ----------

def find_candidate_product_or_catalog_url(html: str, base_url: str) -> Optional[str]:
    """
    Given a homepage or landing page HTML, try to find
    one 'product-like' or 'catalog-like' URL.

    Heuristics:
    - Look at <a href="..."> links
    - Prefer links containing 'product', 'products', 'shop', 'collection',
      'catalog', 'buy', 'p/', 'dp/'
    - Return the first match, resolved to absolute URL
    """
    soup = BeautifulSoup(html, "lxml")

    # Keywords that suggest product or catalog pages
    good_tokens = [
        "/product", "/products", "/shop", "/collection", "/collections",
        "/catalog", "/buy", "/p/", "/dp/", "/store"
    ]

    # Basic blacklist for homepage anchors / login / cart etc.
    bad_tokens = [
        "#", "login", "signin", "sign-in", "account",
        "cart", "wishlist", "help", "faq", "contact", "about"
    ]

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href_lower = href.lower()

        # Skip purely anchor or obviously non-product stuff
        if any(bad in href_lower for bad in bad_tokens):
            continue

        # Build absolute URL
        full_url = urljoin(base_url, href)

        # Check if this looks like a product/catalog URL
        if any(tok in full_url.lower() for tok in good_tokens):
            return full_url

    # If nothing matched, fallback: None (caller will handle)
    return None


def search_marketplace_product_url(company_name: str) -> Optional[str]:
    """
    Use Google Custom Search to find a marketplace product page for the brand.
    Priority: Myntra -> Ajio -> NykaaFashion.
    Returns the first product-ish URL, or None.
    """
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        print("CSE config missing, skipping marketplace search.")
        return None

    base_url = "https://www.googleapis.com/customsearch/v1"

    marketplace_sites = [
        "myntra.com",
        "ajio.com",
        "nykaafashion.com",
    ]

    # Heuristic: product pages usually contain these tokens
    product_tokens = ["/buy", "/p/", "/dp/", "/product", "/products"]

    for site in marketplace_sites:
        q = f'"{company_name}" site:{site}'

        try:
            resp = requests.get(
                base_url,
                params={
                    "key": GOOGLE_CSE_API_KEY,
                    "cx": GOOGLE_CSE_CX,
                    "q": q,
                    "num": 5,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            items = data.get("items", [])
            for item in items:
                link = item.get("link") or ""
                lower = link.lower()
                if any(tok in lower for tok in product_tokens):
                    return link  # first product-ish URL

            # If no “product-ish” URL, but we got results, fallback to first link:
            if items:
                return items[0].get("link")

        except Exception as e:
            print(f"CSE error for site {site}: {e}")

    return None


# --- NEW: fashion-biased website search ---

FASHION_KEYWORDS = [
    "clothing", "clothes", "apparel", "fashion", "wear", "streetwear",
    "garments", "tshirts", "t-shirt", "denim", "jeans", "kurta", "saree",
    "boutique", "label", "brand", "lookbook", "collection", "model", "studio"
]

BAD_WEBSITE_HOSTS = [
    "myntra.com",
    "ajio.com",
    "nykaafashion.com",
    "instagram.com",
    "facebook.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "pinterest.com",
    "amazon.in",
    "amazon.com",
    "flipkart.com",
]


def _looks_like_fashion_site(item: dict) -> bool:
    """Heuristic filter to keep only fashion/garment-ish sites."""
    link = (item.get("link") or "").lower()
    title = (item.get("title") or "").lower()
    snippet = (item.get("snippet") or "").lower()

    # Drop obvious marketplaces / socials
    if any(bad in link for bad in BAD_WEBSITE_HOSTS):
        return False

    text = " ".join([title, snippet])
    return any(kw in text for kw in FASHION_KEYWORDS)


def search_brand_website_url(company_name: str) -> Optional[str]:
    """
    Use Google Custom Search to find the brand's main website,
    biased towards fashion/garments/labels/model studios.
    """
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        print("CSE config missing, skipping brand website search.")
        return None

    base_url = "https://www.googleapis.com/customsearch/v1"

    # Bias query towards apparel/fashion context
    q = f'"{company_name}" (clothing OR apparel OR fashion OR wear OR streetwear OR label)'

    try:
        resp = requests.get(
            base_url,
            params={
                "key": GOOGLE_CSE_API_KEY,
                "cx": GOOGLE_CSE_CX,
                "q": q,
                "num": 10,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])

        if not items:
            return None

        # 1) Prefer items that *look* like fashion/garment sites
        fashion_items = [it for it in items if _looks_like_fashion_site(it)]
        if fashion_items:
            return fashion_items[0].get("link")

        # 2) Fallback: first non-bad host
        for it in items:
            link = (it.get("link") or "").lower()
            if not any(bad in link for bad in BAD_WEBSITE_HOSTS):
                return it.get("link")

        # 3) Last resort: first result
        return items[0].get("link")

    except Exception as e:
        print(f"CSE error while searching brand website for '{company_name}': {e}")
        return None


# ---------- MAIN ENDPOINT ----------

@app.post("/enrich-brand", response_model=EnrichBrandResponse)
def enrich_brand(payload: EnrichBrandRequest):
    """
    v3.0 behavior:

    1) If sku_url is provided:
        -> Scrape SKU page, return images (same as before).

    2) Elif website_url is provided (optionally with company_name):
        -> Scrape website_url for images.
        -> If company_name present, ALSO search marketplace and scrape that.
        -> Decide primary chosen image (prefer marketplace, else website).

    3) Elif ONLY company_name is provided:
        -> (A) Try to find brand website via CSE and scrape it.
        -> (B) Try to find marketplace product via CSE and scrape it.
        -> Decide primary chosen image (prefer marketplace, else website).
        -> If both fail, fallback to dummy image.

    4) Else (no company_name, no website_url, no sku_url):
        -> Dummy response.
    """

    # --- CASE 1: SKU URL PRESENT (same as before) ---
    if payload.sku_url:
        try:
            resp = requests.get(
                str(payload.sku_url),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
                timeout=10,
            )
            resp.raise_for_status()

            image_urls = extract_image_urls_from_html(resp.text, str(payload.sku_url))

            if not image_urls:
                dummy_image_url = "https://picsum.photos/800/1200"
                return EnrichBrandResponse(
                    company_name=payload.company_name,
                    resolved_website_url=payload.website_url,
                    chosen_product_url=payload.sku_url,
                    chosen_image_url=dummy_image_url,
                    candidate_image_urls=[dummy_image_url],
                    website_product_url=payload.sku_url,
                    website_image_url=dummy_image_url,
                    website_candidate_image_urls=[dummy_image_url],
                    notes="No images found on sku_url; returned dummy image.",
                )

            chosen_image_url = image_urls[0]

            return EnrichBrandResponse(
                company_name=payload.company_name,
                resolved_website_url=payload.website_url,
                chosen_product_url=payload.sku_url,
                chosen_image_url=chosen_image_url,
                candidate_image_urls=image_urls,
                website_product_url=payload.sku_url,
                website_image_url=chosen_image_url,
                website_candidate_image_urls=image_urls,
                notes=f"Found {len(image_urls)} image(s) from sku_url. Using first candidate as hero.",
            )

        except Exception as e:
            dummy_image_url = "https://picsum.photos/800/1200"
            return EnrichBrandResponse(
                company_name=payload.company_name,
                resolved_website_url=payload.website_url,
                chosen_product_url=payload.sku_url,
                chosen_image_url=dummy_image_url,
                candidate_image_urls=[dummy_image_url],
                website_product_url=payload.sku_url,
                website_image_url=dummy_image_url,
                website_candidate_image_urls=[dummy_image_url],
                notes=f"Error fetching/parsing sku_url: {e}",
            )

    # Helper to scrape a given website URL (homepage or product page)
    def scrape_website_url(url: str):
        image_urls: List[str] = []
        hero_url: Optional[str] = None
        try:
            print(f"[website] Fetching {url}")
            resp = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=10,
            )
            print(f"[website] Status={resp.status_code}")
            resp.raise_for_status()
            image_urls = extract_image_urls_from_html(resp.text, url)
            print(f"[website] Found {len(image_urls)} image candidates")
            if image_urls:
                hero_url = image_urls[0]
        except Exception as e:
            print(f"Error fetching/parsing website_url={url}: {e}")
        return hero_url, image_urls

    # Helper to scrape marketplace using your existing CSE function
    def scrape_marketplace(company_name: str):
        marketplace_product_url = search_marketplace_product_url(company_name)
        marketplace_image_urls: List[str] = []
        marketplace_hero_url: Optional[str] = None

        if not marketplace_product_url:
            print(f"[marketplace] No product URL found for {company_name}")
            return None, None, []

        try:
            print(f"[marketplace] Fetching {marketplace_product_url}")
            m_resp = requests.get(
                marketplace_product_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=10,
            )
            print(f"[marketplace] Status={m_resp.status_code}")
            m_resp.raise_for_status()

            marketplace_image_urls = extract_image_urls_from_html(
                m_resp.text, marketplace_product_url
            )
            print(f"[marketplace] Found {len(marketplace_image_urls)} image candidates")

            if marketplace_image_urls:
                marketplace_hero_url = marketplace_image_urls[0]
        except Exception as e:
            print(f"[marketplace] Error fetching marketplace product page: {e}")

        return marketplace_product_url, marketplace_hero_url, marketplace_image_urls

    # --- CASE 2: WEBSITE URL PRESENT (website + marketplace) ---
    if payload.website_url:
        website_hero_url, website_image_urls = scrape_website_url(str(payload.website_url))

        marketplace_product_url = None
        marketplace_hero_url = None
        marketplace_image_urls: List[str] = []

        if payload.company_name:
            marketplace_product_url, marketplace_hero_url, marketplace_image_urls = scrape_marketplace(
                payload.company_name
            )

        # Decide a primary
        if marketplace_hero_url:
            primary_product_url = marketplace_product_url
            primary_image_url = marketplace_hero_url
            primary_candidates = marketplace_image_urls
        elif website_hero_url:
            primary_product_url = str(payload.website_url)
            primary_image_url = website_hero_url
            primary_candidates = website_image_urls
        else:
            primary_product_url = str(payload.website_url)
            primary_image_url = "https://picsum.photos/800/1200"
            primary_candidates = [primary_image_url]

        notes_parts = []
        if website_hero_url:
            notes_parts.append(f"Website image OK ({len(website_image_urls)} candidates).")
        else:
            notes_parts.append("Website image missing or failed.")
        if marketplace_hero_url:
            notes_parts.append(
                f"Marketplace image OK from {marketplace_product_url} ({len(marketplace_image_urls)} candidates)."
            )
        else:
            if payload.company_name:
                notes_parts.append("Marketplace image missing or failed.")
            else:
                notes_parts.append("Marketplace search skipped (no company_name).")

        return EnrichBrandResponse(
            company_name=payload.company_name,
            resolved_website_url=payload.website_url,
            chosen_product_url=primary_product_url,
            chosen_image_url=primary_image_url,
            candidate_image_urls=primary_candidates,
            website_product_url=str(payload.website_url),
            website_image_url=website_hero_url,
            website_candidate_image_urls=website_image_urls,
            marketplace_product_url=marketplace_product_url,
            marketplace_image_url=marketplace_hero_url,
            marketplace_candidate_image_urls=marketplace_image_urls,
            notes=" | ".join(notes_parts),
        )

    # --- CASE 3: ONLY company_name (no website_url, no sku_url) ---
    if payload.company_name and not payload.website_url and not payload.sku_url:
        resolved_website_url: Optional[str] = None
        website_hero_url: Optional[str] = None
        website_image_urls: List[str] = []

        # 3A: Try to find brand website via CSE
        resolved_website_url = search_brand_website_url(payload.company_name)
        if resolved_website_url:
            website_hero_url, website_image_urls = scrape_website_url(resolved_website_url)

        # 3B: Marketplace images
        marketplace_product_url, marketplace_hero_url, marketplace_image_urls = scrape_marketplace(
            payload.company_name
        )

        # Decide primary
        if marketplace_hero_url:
            primary_product_url = marketplace_product_url
            primary_image_url = marketplace_hero_url
            primary_candidates = marketplace_image_urls
        elif website_hero_url:
            primary_product_url = resolved_website_url
            primary_image_url = website_hero_url
            primary_candidates = website_image_urls
        else:
            primary_product_url = resolved_website_url or "https://example.com/dummy-product"
            primary_image_url = "https://picsum.photos/800/1200"
            primary_candidates = [primary_image_url]

        notes_parts = []
        if resolved_website_url:
            if website_hero_url:
                notes_parts.append(
                    f"Brand website resolved to {resolved_website_url} ({len(website_image_urls)} image candidates)."
                )
            else:
                notes_parts.append(
                    f"Brand website resolved to {resolved_website_url} but no images found."
                )
        else:
            notes_parts.append("Brand website could not be resolved via CSE.")

        if marketplace_hero_url:
            notes_parts.append(
                f"Marketplace image OK from {marketplace_product_url} ({len(marketplace_image_urls)} candidates)."
            )
        else:
            notes_parts.append("Marketplace image missing or failed.")

        return EnrichBrandResponse(
            company_name=payload.company_name,
            resolved_website_url=resolved_website_url,
            chosen_product_url=primary_product_url,
            chosen_image_url=primary_image_url,
            candidate_image_urls=primary_candidates,
            website_product_url=resolved_website_url,
            website_image_url=website_hero_url,
            website_candidate_image_urls=website_image_urls,
            marketplace_product_url=marketplace_product_url,
            marketplace_image_url=marketplace_hero_url,
            marketplace_candidate_image_urls=marketplace_image_urls,
            notes=" | ".join(notes_parts),
        )

    # --- CASE 4: NOTHING PROVIDED (fallback dummy) ---
    dummy_product_url = payload.website_url or "https://example.com/dummy-product"
    dummy_image_url = "https://picsum.photos/800/1200"

    return EnrichBrandResponse(
        company_name=payload.company_name,
        resolved_website_url=payload.website_url,
        chosen_product_url=dummy_product_url,
        chosen_image_url=dummy_image_url,
        candidate_image_urls=[dummy_image_url],
        website_product_url=dummy_product_url,
        website_image_url=dummy_image_url,
        website_candidate_image_urls=[dummy_image_url],
        notes="sku_url and website_url not provided; using dummy response for now.",
    )