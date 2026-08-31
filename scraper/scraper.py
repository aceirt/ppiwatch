#!/usr/bin/env python3
"""
PP Investments - Foreclosure & Auction Watch Scraper v3
- realforeclose.com (9 FL counties)  → requests + BeautifulSoup
- auction.com                         → Playwright (stealth, optional login)
- hubzu.com                           → Playwright (stealth, optional login)
- xome.com                            → Playwright (stealth, optional login)
- hudhomestore.gov                    → requests (public API)
- govdeals.com                        → Playwright (stealth)

Safety: if total listings == 0, existing feed.json is preserved.
"""

import json, os, time, logging, hashlib, re
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ppwatch")

# -- Config -------------------------------------------------------------------
CRM_WEBHOOK_URL   = os.getenv("CRM_WEBHOOK_URL",   "")
CRM_API_KEY       = os.getenv("CRM_API_KEY",       "")
CRM_LOCATION_ID   = "KoyfEHXBmxbD69hWgYyJ"

AUCTION_COM_EMAIL = os.getenv("AUCTION_COM_EMAIL", "")
AUCTION_COM_PASS  = os.getenv("AUCTION_COM_PASS",  "")
HUBZU_EMAIL       = os.getenv("HUBZU_EMAIL",       "")
HUBZU_PASS        = os.getenv("HUBZU_PASS",        "")
XOME_EMAIL        = os.getenv("XOME_EMAIL",        "")
XOME_PASS         = os.getenv("XOME_PASS",         "")
PROXY_URL         = os.getenv("PROXY_URL",         "")   # e.g. http://user:pass@host:port
HUD_API_KEY       = os.getenv("HUD_API_KEY",       "")
GOVDEALS_KEY      = os.getenv("GOVDEALS_API_KEY",  "")

FEED_PATH = Path(os.getenv("FEED_OUTPUT_PATH", "docs/feed.json"))
SEEN_PATH = Path("scraper/.seen.json")
MIN_LISTINGS_TO_OVERWRITE = 1   # keep existing feed if scraper returns fewer

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REALFORECLOSE_COUNTIES = [
    ("bay",        "Bay FL"),
    ("gulf",       "Gulf FL"),
    ("walton",     "Walton FL"),
    ("okaloosa",   "Okaloosa FL"),
    ("washington", "Washington FL"),
    ("holmes",     "Holmes FL"),
    ("jackson",    "Jackson FL"),
    ("calhoun",    "Calhoun FL"),
    ("escambia",   "Escambia FL"),
]

# -- Data model ---------------------------------------------------------------
@dataclass
class Property:
    id:             str
    address:        str
    county:         str
    type:           str
    beds:           Optional[int]
    baths:          Optional[int]
    acreage:        Optional[float]
    bidPrice:       Optional[int]
    auctionDate:    Optional[str]
    auctionSite:    str
    caseNumber:     str
    propertyUrl:    str
    imageUrl:       Optional[str]
    notes:          str
    listingFoundAt: str
    isNew:          bool = True

    def to_dict(self):
        return asdict(self)


# -- Helpers ------------------------------------------------------------------
def load_seen() -> set:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()

def save_seen(seen: set):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2))

def listing_id(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:12]

def clean_price(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None

def get_r(url, *, timeout=20, **kw) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning("GET %s -> %s", url, e)
        return None

def post_json(url, payload) -> bool:
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("POST %s -> %s", url, e)
        return False


# ============================================================================
# PLAYWRIGHT BROWSER HELPER
# ============================================================================

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import stealth_sync
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    log.warning("Playwright not installed — browser-based parsers will be skipped")


@contextmanager
def browser_page(headless=True):
    """
    Yields a stealthy Playwright page. Automatically closes on exit.
    Uses PROXY_URL env var if set (e.g. for residential proxies).
    """
    if not PLAYWRIGHT_AVAILABLE:
        yield None
        return

    launch_opts = {"headless": headless}
    ctx_opts    = {}

    if PROXY_URL:
        # Support http://user:pass@host:port format
        ctx_opts["proxy"] = {"server": PROXY_URL}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        ctx     = browser.new_context(
            **ctx_opts,
            viewport={"width": 1280, "height": 800},
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        page = ctx.new_page()
        stealth_sync(page)
        try:
            yield page
        finally:
            ctx.close()
            browser.close()


def browser_get_html(url: str, *, wait_selector: str = None,
                     login_fn=None, timeout: int = 30000) -> Optional[str]:
    """
    Navigate to url with a stealthy browser, optionally run login_fn(page)
    before navigating, wait for an optional CSS selector, return page HTML.
    """
    with browser_page() as page:
        if page is None:
            return None
        try:
            if login_fn:
                login_fn(page)
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:
                    pass
            page.wait_for_timeout(2000)   # let JS settle
            return page.content()
        except Exception as e:
            log.warning("browser_get_html %s -> %s", url, e)
            return None


# ============================================================================
# SITE PARSERS — requests-based
# ============================================================================

def parse_realforeclose(slug: str, county: str) -> list:
    base    = f"https://{slug}.realforeclose.com"
    results = []

    r = None
    for path in ["/index.cfm?zaction=AUCTION&zmethod=preview", "/"]:
        r = get_r(f"{base}{path}")
        if r:
            break
    if not r:
        return results

    soup = BeautifulSoup(r.text, "lxml")
    selectors = [
        "table#PUBLIC_AUCTION_RESULTS tr",
        ".AUCTION_ITEM", "tr.altRow", "tr.altRow2",
        "tr[class*='Row']", "table.dataTable tr",
        "#auction_list tr", "tbody tr",
    ]
    rows = []
    for sel in selectors:
        rows = soup.select(sel)
        if len(rows) > 1:
            break

    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2 or (row.find("th") and not row.find("td")):
            continue
        text_cells = [c.get_text(" ", strip=True) for c in cells]
        full_text  = " | ".join(text_cells)

        case_m   = re.search(r'\d{4}-[A-Z]{2}-\d+', full_text)
        case_num = case_m.group(0) if case_m else listing_id(full_text)
        price_m  = re.search(r'\$[\d,]+\.?\d*', full_text)
        bid_int  = clean_price(price_m.group(0)) if price_m else None

        link     = row.find("a", href=True)
        href     = link["href"] if link else ""
        prop_url = (href if href.startswith("http") else base + "/" + href.lstrip("/")) if href \
                   else f"{base}/index.cfm?zaction=AUCTION&zmethod=preview"

        addr = ""
        for ct in text_cells:
            if re.search(r'\d+\s+\w+\s+(St|Ave|Rd|Dr|Blvd|Ln|Way|Ct|Pkwy|Hwy|Pl)', ct, re.I):
                addr = ct
                break
        if not addr:
            addr = max(text_cells, key=len) if text_cells else f"Property in {county}"
        if not addr or len(addr) < 5:
            continue

        date_m       = re.search(r'\d{1,2}/\d{1,2}/\d{4}', full_text)
        auction_date = date_m.group(0) if date_m else None

        results.append(Property(
            id=listing_id(case_num), address=addr,
            county=county, type="Single Family",
            beds=None, baths=None, acreage=None,
            bidPrice=bid_int, auctionDate=auction_date,
            auctionSite=f"{slug}.realforeclose.com",
            caseNumber=case_num, propertyUrl=prop_url,
            imageUrl=None,
            notes=f"Judicial foreclosure - {county}. {full_text[:200]}",
            listingFoundAt=now_iso,
        ))

    log.info("realforeclose/%s -> %d listings", slug, len(results))
    return results


def parse_hud() -> list:
    results = []
    kw = {"headers": {**HEADERS, "X-API-KEY": HUD_API_KEY}} if HUD_API_KEY else {}
    r  = get_r(
        "https://www.hudhomestore.gov/HUDApi/ListingSearch",
        params={"stateCode": "FL", "county": "Bay", "pageSize": 50}, **kw
    )
    if not r:
        return results
    try:
        for p in r.json().get("properties", r.json().get("results", [])):
            pid = str(p.get("listingId", p.get("caseNumber", "")))
            results.append(Property(
                id=listing_id(pid),
                address=f"{p.get('streetAddr','')} {p.get('city','')} FL {p.get('zip','')}".strip(),
                county=f"{p.get('county','FL')} FL",
                type=p.get("propType", "Single Family"),
                beds=p.get("bedroom"), baths=p.get("bath"), acreage=None,
                bidPrice=int(p.get("listPrice", 0) or 0) or None,
                auctionDate=p.get("listingDate"),
                auctionSite="hudhomestore.com", caseNumber=pid,
                propertyUrl=f"https://www.hudhomestore.gov/Listing/PropertySearch.aspx?sState=FL&sCaseNumber={pid}",
                imageUrl=None,
                notes=f"HUD FHA foreclosure. Case #{pid}.",
                listingFoundAt=datetime.now(timezone.utc).isoformat(),
            ))
    except Exception as e:
        log.warning("HUD: %s", e)
    log.info("HUD -> %d listings", len(results))
    return results


# ============================================================================
# SITE PARSERS — Playwright-based (bot-protected sites)
# ============================================================================

# --- auction.com -------------------------------------------------------------

def _auction_login(page):
    if not AUCTION_COM_EMAIL:
        return
    try:
        page.goto("https://www.auction.com/signin/", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.fill("input[name='email'], input[type='email']", AUCTION_COM_EMAIL)
        page.fill("input[name='password'], input[type='password']", AUCTION_COM_PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        log.info("auction.com login submitted")
    except Exception as e:
        log.warning("auction.com login: %s", e)


def parse_auction_com() -> list:
    results = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # Try JSON API first (no auth needed for basic search)
    r = get_r(
        "https://www.auction.com/api/property/search",
        params={"state": "FL",
                "county": "Bay,Gulf,Walton,Okaloosa,Washington",
                "propertyType": "SFR,MFR,LND,COM",
                "listingType": "AUCTION,BIN",
                "pageSize": 50, "page": 1},
    )
    if r:
        try:
            data = r.json()
            for p in data.get("properties", data.get("results", [])):
                addr = p.get("address", {})
                full_addr = addr.get("fullAddress", "") if isinstance(addr, dict) else str(addr)
                slug = p.get("slug") or p.get("propertyId", "")
                results.append(Property(
                    id=listing_id(str(slug)), address=full_addr,
                    county=p.get("county", "FL"), type=p.get("propertyType", "Single Family"),
                    beds=p.get("bedrooms"), baths=p.get("bathrooms"),
                    acreage=p.get("lotSizeAcres"),
                    bidPrice=p.get("openingBid") or p.get("currentBid"),
                    auctionDate=p.get("auctionDate"),
                    auctionSite="auction.com", caseNumber=str(p.get("caseNumber", slug)),
                    propertyUrl=f"https://www.auction.com/residential/{slug}/",
                    imageUrl=p.get("photoUrl") or p.get("primaryPhoto"),
                    notes=str(p.get("description", ""))[:300],
                    listingFoundAt=now_iso,
                ))
            if results:
                log.info("auction.com (API) -> %d listings", len(results))
                return results
        except Exception as e:
            log.warning("auction.com API: %s", e)

    # Fallback: Playwright browser
    if not PLAYWRIGHT_AVAILABLE:
        log.warning("auction.com: Playwright not available, skipping")
        return results

    log.info("auction.com: trying browser scrape%s",
             " (with login)" if AUCTION_COM_EMAIL else "")
    html = browser_get_html(
        "https://www.auction.com/foreclosure/real-estate/fl/?state=FL"
        "&county=Bay%2CGulf%2CWalton%2COkaloosa%2CWashington",
        wait_selector="[data-testid='property-card'], .propertyCard",
        login_fn=_auction_login if AUCTION_COM_EMAIL else None,
    )
    if not html:
        return results

    soup = BeautifulSoup(html, "lxml")
    for card in soup.select(
        "[data-testid='property-card'], .propertyCard, "
        ".property-card, [class*='PropertyCard']"
    ):
        try:
            addr  = card.select_one(".address, [data-testid='address'], [class*='address']")
            price = card.select_one(".price, [data-testid='price'], [class*='price']")
            link  = card.select_one("a[href*='/residential/']") or card.select_one("a")
            if not addr:
                continue
            href    = link["href"] if link else ""
            url     = href if href.startswith("http") else "https://www.auction.com" + href
            bid_int = clean_price(price.get_text(strip=True)) if price else None
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ", strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=bid_int, auctionDate=None,
                auctionSite="auction.com", caseNumber=listing_id(url),
                propertyUrl=url, imageUrl=None,
                notes="Via auction.com (browser)",
                listingFoundAt=now_iso,
            ))
        except Exception as e:
            log.debug("auction.com card: %s", e)

    log.info("auction.com (browser) -> %d listings", len(results))
    return results


# --- hubzu.com ---------------------------------------------------------------

def _hubzu_login(page):
    if not HUBZU_EMAIL:
        return
    try:
        page.goto("https://www.hubzu.com/login", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.fill("input[name='username'], input[type='email']", HUBZU_EMAIL)
        page.fill("input[name='password'], input[type='password']", HUBZU_PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        log.info("hubzu login submitted")
    except Exception as e:
        log.warning("hubzu login: %s", e)


def parse_hubzu() -> list:
    results = []
    now_iso = datetime.now(timezone.utc).isoformat()

    if not PLAYWRIGHT_AVAILABLE:
        log.warning("hubzu: Playwright not available, skipping")
        return results

    log.info("hubzu: browser scrape%s", " (with login)" if HUBZU_EMAIL else "")
    html = browser_get_html(
        "https://www.hubzu.com/search?state=FL"
        "&county=Bay,Gulf,Walton,Okaloosa,Washington",
        wait_selector=".listing-card, [class*='propertyCard'], [class*='listing']",
        login_fn=_hubzu_login if HUBZU_EMAIL else None,
    )
    if not html:
        return results

    soup = BeautifulSoup(html, "lxml")
    for card in soup.select(
        ".property-listing, .listing-card, [class*='propertyCard'], "
        "[class*='PropertyCard'], [class*='listing-item']"
    ):
        try:
            addr  = card.select_one(".property-address, .address, [class*='address']")
            price = card.select_one(".listing-price, .price, .bid-price, [class*='price']")
            link  = card.select_one("a[href*='/property/'], a[href*='/real-estate/']") \
                    or card.select_one("a")
            if not addr:
                continue
            href    = link["href"] if link else ""
            url     = href if href.startswith("http") else "https://www.hubzu.com" + href
            bid_int = clean_price(price.get_text(strip=True)) if price else None
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ", strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=bid_int, auctionDate=None,
                auctionSite="hubzu.com", caseNumber=listing_id(url),
                propertyUrl=url, imageUrl=None,
                notes="Via Hubzu - bank-owned REO (browser).",
                listingFoundAt=now_iso,
            ))
        except Exception as e:
            log.debug("hubzu card: %s", e)

    log.info("hubzu (browser) -> %d listings", len(results))
    return results


# --- xome.com ----------------------------------------------------------------

def _xome_login(page):
    if not XOME_EMAIL:
        return
    try:
        page.goto("https://www.xome.com/login", timeout=20000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.fill("input[type='email']", XOME_EMAIL)
        page.fill("input[type='password']", XOME_PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        log.info("xome login submitted")
    except Exception as e:
        log.warning("xome login: %s", e)


def parse_xome() -> list:
    results = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # Try JSON API first
    r = get_r(
        "https://www.xome.com/api/search/auction",
        params={"state": "FL", "county": "Bay,Gulf,Walton,Okaloosa", "pageSize": 50}
    )
    if r:
        try:
            data = r.json()
            for p in data.get("listings", data.get("results", [])):
                slug = p.get("slug") or p.get("id", "")
                results.append(Property(
                    id=listing_id(str(slug)), address=p.get("address", ""),
                    county=p.get("county", "FL"), type=p.get("propertyType", "Single Family"),
                    beds=p.get("beds"), baths=p.get("baths"),
                    acreage=p.get("lotSizeAcres"),
                    bidPrice=p.get("listPrice") or p.get("openingBid"),
                    auctionDate=p.get("auctionDate"),
                    auctionSite="xome.com", caseNumber=str(slug),
                    propertyUrl=f"https://www.xome.com/homes-for-sale/{slug}",
                    imageUrl=p.get("primaryPhotoUrl"),
                    notes=str(p.get("description", ""))[:300],
                    listingFoundAt=now_iso,
                ))
            if results:
                log.info("xome (API) -> %d listings", len(results))
                return results
        except Exception as e:
            log.warning("xome API: %s", e)

    if not PLAYWRIGHT_AVAILABLE:
        log.warning("xome: Playwright not available, skipping")
        return results

    log.info("xome: browser scrape%s", " (with login)" if XOME_EMAIL else "")
    html = browser_get_html(
        "https://www.xome.com/real-estate-auctions/for-sale?state=FL"
        "&county=Bay,Gulf,Walton,Okaloosa,Washington",
        wait_selector="[class*='listing'], [class*='property-card']",
        login_fn=_xome_login if XOME_EMAIL else None,
    )
    if not html:
        return results

    soup = BeautifulSoup(html, "lxml")
    for card in soup.select(
        "[class*='ListingCard'], [class*='PropertyCard'], "
        ".listing-card, [class*='property-item']"
    ):
        try:
            addr  = card.select_one("[class*='address'], [class*='Address']")
            price = card.select_one("[class*='price'], [class*='Price']")
            link  = card.select_one("a") 
            if not addr:
                continue
            href = link["href"] if link else ""
            url  = href if href.startswith("http") else "https://www.xome.com" + href
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ", strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=clean_price(price.get_text()) if price else None,
                auctionDate=None,
                auctionSite="xome.com", caseNumber=listing_id(url),
                propertyUrl=url, imageUrl=None,
                notes="Via Xome (browser).",
                listingFoundAt=now_iso,
            ))
        except Exception as e:
            log.debug("xome card: %s", e)

    log.info("xome (browser) -> %d listings", len(results))
    return results


# --- govdeals.com ------------------------------------------------------------

def parse_govdeals() -> list:
    results = []
    now_iso = datetime.now(timezone.utc).isoformat()

    if not PLAYWRIGHT_AVAILABLE:
        # Try plain requests as fallback
        r = get_r(
            "https://www.govdeals.com/index.cfm?fa=Main.AdvSearchResultsNew"
            "&searchPg=1&category=0058&state=FL"
        )
        if not r:
            return results
        soup = BeautifulSoup(r.text, "lxml")
    else:
        log.info("govdeals: browser scrape")
        html = browser_get_html(
            "https://www.govdeals.com/index.cfm?fa=Main.AdvSearchResultsNew"
            "&searchPg=1&category=0058&state=FL",
            wait_selector=".itemListing, .listing-item, [class*='listing']",
        )
        if not html:
            return results
        soup = BeautifulSoup(html, "lxml")

    for item in soup.select(".itemListing, .listing-item, [class*='item-card']"):
        try:
            title = item.select_one(".itemTitle, .item-title, h3, h2")
            price = item.select_one(".currentBid, .current-bid, .price, [class*='bid']")
            link  = item.select_one("a[href*='itemno'], a[href*='item'], a")
            if not title:
                continue
            href    = link["href"] if link else ""
            url     = href if href.startswith("http") else "https://www.govdeals.com" + href
            bid_int = clean_price(price.get_text()) if price else None
            results.append(Property(
                id=listing_id(url), address=title.get_text(" ", strip=True),
                county="FL", type="Mixed Use",
                beds=None, baths=None, acreage=None,
                bidPrice=bid_int, auctionDate=None,
                auctionSite="govdeals.com", caseNumber=listing_id(url),
                propertyUrl=url, imageUrl=None,
                notes="Government surplus / tax deed sale.",
                listingFoundAt=now_iso,
            ))
        except Exception:
            pass

    log.info("govdeals -> %d listings", len(results))
    return results


# ============================================================================
# CRM INTEGRATION
# ============================================================================

def fetch_active_watchers() -> list:
    if not CRM_API_KEY:
        log.warning("CRM_API_KEY not set — skipping watcher alerts")
        return []
    try:
        r = requests.get(
            "https://services.leadconnectorhq.com/contacts/",
            headers={"Authorization": f"Bearer {CRM_API_KEY}", "Version": "2021-07-28"},
            params={"locationId": CRM_LOCATION_ID, "tags": "Foreclosure Watcher", "limit": 100},
            timeout=15,
        )
        r.raise_for_status()
        contacts = r.json().get("contacts", [])
        return [c for c in contacts if _field(c, "watch_active") == "Active"]
    except Exception as e:
        log.warning("CRM fetch watchers: %s", e)
        return []


def _field(contact: dict, key: str) -> str:
    for f in contact.get("customFields", []):
        fkey  = f.get("fieldKey", "")
        fname = f.get("name", "").lower().replace(" ", "_")
        if fkey.endswith(key) or fname == key:
            return str(f.get("value", ""))
    return ""


def matches_watcher(prop: Property, watcher: dict) -> bool:
    counties = [c.strip() for c in _field(watcher, "watch_counties").split(",") if c.strip()]
    types    = [t.strip() for t in _field(watcher, "watch_property_types").split(",") if t.strip()]
    sites    = [s.strip() for s in _field(watcher, "watch_sites_to_monitor").split(",") if s.strip()]
    inc_kw   = [k.strip().lower() for k in _field(watcher, "watch_keywords_include").split(",") if k.strip()]
    exc_kw   = [k.strip().lower() for k in _field(watcher, "watch_keywords_exclude").split(",") if k.strip()]
    try:
        min_price = float(_field(watcher, "watch_min_price") or 0)
        max_price = float(_field(watcher, "watch_max_price") or 1e12)
        min_acres = float(_field(watcher, "watch_min_acreage") or 0)
        min_beds  = int(_field(watcher, "watch_min_bedrooms") or 0)
    except ValueError:
        min_price, max_price, min_acres, min_beds = 0, 1e12, 0, 0
    text = (prop.address + " " + prop.notes).lower()
    if counties and prop.county not in counties:       return False
    if types    and prop.type    not in types:         return False
    if sites    and prop.auctionSite not in sites:     return False
    if prop.bidPrice and prop.bidPrice < min_price:    return False
    if prop.bidPrice and prop.bidPrice > max_price:    return False
    if prop.acreage  and prop.acreage  < min_acres:    return False
    if prop.beds     and prop.beds     < min_beds:     return False
    if inc_kw and not any(k in text for k in inc_kw): return False
    if exc_kw and     any(k in text for k in exc_kw): return False
    return True


def fire_crm_alert(prop: Property, watcher: dict):
    if not CRM_WEBHOOK_URL:
        return
    payload = {
        "contactId":      watcher.get("id"),
        "watchListName":  _field(watcher, "watch_list_name"),
        "alertFrequency": _field(watcher, "watch_alert_frequency") or "Immediate",
        "watcher": {
            "name":     watcher.get("contactName", ""),
            "email":    watcher.get("email", ""),
            "ccEmails": [e.strip() for e in _field(watcher, "watch_cc_emails").split(",") if e.strip()],
            "phone":    watcher.get("phone", ""),
        },
        "property": {
            "address":        prop.address,
            "type":           prop.type,
            "county":         prop.county,
            "bidPrice":       prop.bidPrice,
            "acreage":        prop.acreage,
            "beds":           prop.beds,
            "baths":          prop.baths,
            "auctionDate":    prop.auctionDate,
            "auctionSite":    prop.auctionSite,
            "caseNumber":     prop.caseNumber,
            "propertyUrl":    prop.propertyUrl,
            "imageUrl":       prop.imageUrl,
            "notes":          prop.notes,
            "listingFoundAt": prop.listingFoundAt,
            "isNew":          prop.isNew,
        },
    }
    ok = post_json(CRM_WEBHOOK_URL, payload)
    log.info("CRM alert [%s] -> %s | %s",
             "OK" if ok else "FAIL", watcher.get("email", "?"), prop.address)


# ============================================================================
# FEED WRITER — with empty-result safety guard
# ============================================================================

def write_feed(listings: list):
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    if len(listings) < MIN_LISTINGS_TO_OVERWRITE:
        log.warning(
            "Scraper returned %d listings (threshold: %d) — "
            "preserving existing feed.json",
            len(listings), MIN_LISTINGS_TO_OVERWRITE
        )
        return
    feed = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source":    "PP Investments Foreclosure Watch Scraper",
        "count":     len(listings),
        "listings":  [p.to_dict() for p in listings],
    }
    FEED_PATH.write_text(json.dumps(feed, indent=2, default=str))
    log.info("Wrote %s (%d listings)", FEED_PATH, len(listings))


# ============================================================================
# ORCHESTRATOR
# ============================================================================

def run_once():
    log.info("=== PP Investments Scraper v3 — run started ===")
    log.info("Playwright available: %s", PLAYWRIGHT_AVAILABLE)

    seen     = load_seen()
    all_new  = []
    all_kept = []
    watchers = fetch_active_watchers()
    log.info("Active watchers: %d", len(watchers))

    parsers = (
        [lambda s=s, c=c: parse_realforeclose(s, c) for s, c in REALFORECLOSE_COUNTIES]
        + [parse_auction_com, parse_hubzu, parse_xome, parse_hud, parse_govdeals]
    )

    for fn in parsers:
        try:
            listings = fn()
        except Exception as e:
            log.error("Parser %s error: %s", getattr(fn, "__name__", "?"), e)
            listings = []

        for prop in listings:
            all_kept.append(prop)
            if prop.id not in seen:
                prop.isNew = True
                all_new.append(prop)
                seen.add(prop.id)
                for w in watchers:
                    if matches_watcher(prop, w):
                        fire_crm_alert(prop, w)
                        time.sleep(0.3)
            else:
                prop.isNew = False

        time.sleep(1)

    save_seen(seen)
    write_feed(all_kept)
    log.info("=== Done. %d new / %d total ===", len(all_new), len(all_kept))


if __name__ == "__main__":
    run_once()
