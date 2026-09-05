#!/usr/bin/env python3
"""
PP Investments - Foreclosure & Auction Watch Scraper v7
Authenticated realforeclose.com scraping (AJAX login + per-case detail)
ported from scrape-live.ts. Falls back to calendar-only when no creds.

Sites:
  realforeclose.com ×9  → requests + Session (cookie jar, optional login)
  auction.com           → Playwright (stealth, optional login)
  hubzu.com             → Playwright (stealth, optional login)
  xome.com              → Playwright (stealth, optional login)
  hudhomestore.gov      → requests (public API)
  govdeals.com          → Playwright (stealth)

Safety: if total listings == 0, existing feed.json is preserved.
"""

import json, os, time, logging, hashlib, re
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ppwatch")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CRM_WEBHOOK_URL      = os.getenv("CRM_WEBHOOK_URL",      "")
CRM_API_KEY          = os.getenv("CRM_API_KEY",          "")
CRM_LOCATION_ID      = "KoyfEHXBmxbD69hWgYyJ"

REALFORECLOSE_USER   = os.getenv("REALFORECLOSE_USER",   "")
REALFORECLOSE_PASS   = os.getenv("REALFORECLOSE_PASS",   "")
HAS_RF_CREDS         = bool(REALFORECLOSE_USER and REALFORECLOSE_PASS)

AUCTION_COM_EMAIL    = os.getenv("AUCTION_COM_EMAIL",    "")
AUCTION_COM_PASS     = os.getenv("AUCTION_COM_PASS",     "")
HUBZU_EMAIL          = os.getenv("HUBZU_EMAIL",          "")
HUBZU_PASS           = os.getenv("HUBZU_PASS",           "")
XOME_EMAIL           = os.getenv("XOME_EMAIL",           "")
XOME_PASS            = os.getenv("XOME_PASS",            "")
PROXY_URL            = os.getenv("PROXY_URL",            "")
HUD_API_KEY          = os.getenv("HUD_API_KEY",          "")

FEED_PATH            = Path(os.getenv("FEED_OUTPUT_PATH", "docs/feed.json"))
SEEN_PATH            = Path("scraper/.seen.json")
MIN_LISTINGS_TO_OVERWRITE = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
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

# Approximate county center coords for map display (lat, lng)
COUNTY_GEO = {
    "Bay FL":        (30.24, -85.66),
    "Gulf FL":       (29.92, -85.19),
    "Walton FL":     (30.55, -86.17),
    "Okaloosa FL":   (30.57, -86.52),
    "Washington FL": (30.61, -85.66),
    "Holmes FL":     (30.87, -85.81),
    "Jackson FL":    (30.83, -85.18),
    "Calhoun FL":    (30.41, -85.16),
    "Escambia FL":   (30.60, -87.34),
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
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
    isNew:          bool  = True
    isLiveScrape:   bool  = False
    parcelId:       Optional[str] = None
    lat:            Optional[float] = None
    lng:            Optional[float] = None

    def to_dict(self):
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None or k in
                ("bidPrice", "beds", "baths", "acreage", "imageUrl", "parcelId")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _is_calendar_marker(prop) -> bool:
    """True if entry is a county-level date placeholder with no real data."""
    return (
        bool(re.match(r".+ County Foreclosure Auction — \d{2}/\d{2}/\d{4}$", prop.address))
        and not prop.bidPrice
    )


def get_r(url, *, session=None, timeout=20, **kw) -> Optional[requests.Response]:
    try:
        fn = session.get if session else requests.get
        r  = fn(url, headers=HEADERS, timeout=timeout, **kw)
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


# ===========================================================================
# realforeclose.com — authenticated scraper
# Ported from scrape-live.ts (AJAX login + cookie jar + per-case parsing)
# ===========================================================================

def _rf_make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def _rf_login(session: requests.Session, site: str) -> bool:
    """
    Authenticate against a realforeclose portal using the AJAX login endpoint.
    Mirrors the TypeScript logic in scrape-live.ts exactly.
    """
    base = f"https://{site}"
    # Prime the session (sets initial cookies)
    try:
        session.get(f"{base}/index.cfm?zaction=USER&zmethod=LOGIN", timeout=15)
    except Exception:
        pass

    try:
        r = session.post(
            f"{base}/index.cfm",
            data={
                "ZACTION":  "AJAX",
                "ZMETHOD":  "LOGIN",
                "func":     "LOGIN",
                "USERNAME": REALFORECLOSE_USER,
                "USERPASS": REALFORECLOSE_PASS,
            },
            headers={
                **HEADERS,
                "Accept":        "application/json, text/javascript, */*",
                "Content-Type":  "application/x-www-form-urlencoded",
                "Referer":       f"{base}/index.cfm?zaction=USER&zmethod=LOGIN",
            },
            timeout=15,
        )
        ok = '"isOk":"YES"' in r.text or '"isOk": "YES"' in r.text
        log.info("%s login: %s", site, "OK" if ok else "FAILED")
        return ok
    except Exception as e:
        log.warning("%s login error: %s", site, e)
        return False

def _rf_extract_dates(html: str) -> list:
    """Pull unique MM/DD/YYYY auction dates from a realforeclose calendar page."""
    matches = re.findall(r'AuctionDate=(\d{2}/\d{2}/\d{4})', html)
    seen, out = set(), []
    for d in matches:
        if d not in seen:
            seen.add(d); out.append(d)
    return out

def _to_iso(mmddyyyy: str) -> str:
    m, d, y = mmddyyyy.split("/")
    return f"{y}-{m}-{d}T10:00:00-05:00"

def _rf_parse_cases(html: str, site: str) -> list:
    """
    Parse per-case rows from an authenticated PREVIEW page.
    Extracts AuctionID, address, bid, case number, parcel ID.
    Mirrors parseCases() in scrape-live.ts.
    """
    cases = []
    ids   = list(dict.fromkeys(re.findall(r'AuctionID=([^&"\'>\s]+)', html)))
    if not ids:
        return cases

    for i, aid in enumerate(ids):
        start = html.find(f"AuctionID={aid}")
        end   = html.find(f"AuctionID={ids[i+1]}") if i + 1 < len(ids) else len(html)
        block = html[start:end]
        soup  = BeautifulSoup(block, "lxml")
        text  = soup.get_text(" ", strip=True)

        # Address: first "123 Street St ..." pattern
        addr_m = re.search(
            r'\b\d+\s+[\w.\-]+\s+'
            r'(?:St|Street|Dr|Drive|Ave|Avenue|Blvd|Boulevard|Rd|Road|'
            r'Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway|Pl|Place|'
            r'Ter|Terrace|Cir|Circle)\b[^,\n]*',
            text, re.I
        )
        address = addr_m.group(0).strip() if addr_m else f"Auction ID {aid}"

        # Bid / judgment amount
        dollar_m = re.search(r'\$[\d,]+(?:\.\d{2})?', text)
        bid = int(re.sub(r'[^\d]', '', dollar_m.group(0))) if dollar_m else 0

        # Case number
        case_m = (
            re.search(r'Case\s*(?:No\.?|#)\s*:?\s*([A-Z0-9\-]+)', text, re.I) or
            re.search(r'\b(\d{2,4}-[A-Z]{2}-\d{3,6})\b', text)
        )
        case_num = case_m.group(1) if case_m else aid

        # Parcel ID
        parcel_m = re.search(
            r'(?:Parcel|PIN|Folio)\s*#?\s*:?\s*([A-Z0-9\-]+)', text, re.I
        )
        parcel_id = parcel_m.group(1) if parcel_m else None

        cases.append({
            "id":          f"{site}-case-{aid}",
            "address":     address,
            "bidPrice":    bid,
            "caseNumber":  case_num,
            "parcelId":    parcel_id,
            "propertyUrl": f"https://{site}/index.cfm?zaction=AUCTION&zmethod=DETAILS&AuctionID={aid}",
            "notes":       text[:400],
        })
    return cases



def _rf_enrich_case(session: requests.Session, site: str, auction_id: str) -> dict:
    """Fetch the DETAILS page to recover address/bid/parcel when PREVIEW parse missed them."""
    url = f"https://{site}/index.cfm?zaction=AUCTION&zmethod=DETAILS&AuctionID={auction_id}"
    r   = get_r(url, session=session, timeout=20)
    if not r:
        return {}
    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text(" ", strip=True)
    addr_m   = re.search(
        r'\b\d+\s+[\w.\-]+\s+'
        r'(?:St|Street|Dr|Drive|Ave|Avenue|Blvd|Boulevard|Rd|Road|'
        r'Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway|Pl|Place|'
        r'Ter|Terrace|Cir|Circle)\b[^,\n]*',
        text, re.I
    )
    dollar_m = re.search(r'\$[\d,]+(?:\.\d{2})?', text)
    parcel_m = re.search(r'(?:Parcel|PIN|Folio)\s*#?\s*:?\s*([A-Z0-9\-]+)', text, re.I)
    return {
        "address":   addr_m.group(0).strip()                       if addr_m   else None,
        "bidPrice":  int(re.sub(r"[^\d]", "", dollar_m.group(0))) if dollar_m else None,
        "parcelId":  parcel_m.group(1)                             if parcel_m else None,
        "detailUrl": url,
    }


def parse_realforeclose(slug: str, county: str) -> list:
    """
    Scrape one realforeclose.com county portal.
    - Always fetches the public auction calendar (upcoming dates).
    - If REALFORECLOSE_USER/PASS are set: logs in via AJAX, then pulls
      per-case detail (address, bid, case #, parcel ID) for each date.
    - Falls back to a date-only entry when unauthenticated or no cases found.
    """
    base    = f"https://{slug}.realforeclose.com"
    results = []
    coords  = COUNTY_GEO.get(county)

    # One session per portal to maintain cookies across requests
    session = _rf_make_session()

    # Fetch the calendar
    r = get_r(f"{base}/index.cfm?zaction=AUCTION&zmethod=PREVIEW", session=session)
    if not r:
        return results

    all_dates = _rf_extract_dates(r.text)
    today_ms  = datetime.now(timezone.utc).timestamp() * 1000
    upcoming  = sorted(
        [d for d in all_dates if _date_ms(d) >= today_ms - 86_400_000],
        key=_date_ms
    )
    log.info("realforeclose/%s: %d upcoming dates%s",
             slug, len(upcoming), " (will authenticate)" if HAS_RF_CREDS else "")

    # Attempt login once per portal
    authed = _rf_login(session, f"{slug}.realforeclose.com") if HAS_RF_CREDS else False

    for d in upcoming:
        iso_date    = _to_iso(d)
        preview_url = f"{base}/index.cfm?zaction=AUCTION&zmethod=PREVIEW&AuctionDate={d}"

        if authed:
            date_r = get_r(preview_url, session=session)
            cases  = _rf_parse_cases(date_r.text, f"{slug}.realforeclose.com") if date_r else []
            if cases:
                # Enrich cases whose address fell back to "Auction ID XXX"
                for c in cases:
                    if c["address"].startswith("Auction ID "):
                        enriched = _rf_enrich_case(session, f"{slug}.realforeclose.com", c["auctionId"])
                        if enriched.get("address"):  c["address"]    = enriched["address"]
                        if enriched.get("bidPrice"): c["bidPrice"]   = enriched["bidPrice"]
                        if enriched.get("parcelId"): c["parcelId"]   = enriched["parcelId"]
                        if enriched.get("detailUrl"):c["propertyUrl"]= enriched["detailUrl"]
                # Drop cases still lacking both address and bid — pure empty scaffolding
                cases = [c for c in cases if not (c["address"].startswith("Auction ID ") and not c["bidPrice"])]
            if cases:
                for c in cases:
                    results.append(Property(
                        id            = listing_id(c["id"]),
                        address       = c["address"],
                        county        = county,
                        type          = "Single Family",
                        beds          = None,
                        baths         = None,
                        acreage       = None,
                        bidPrice      = c["bidPrice"] or None,
                        auctionDate   = iso_date,
                        auctionSite   = f"{slug}.realforeclose.com",
                        caseNumber    = c["caseNumber"],
                        propertyUrl   = c["propertyUrl"],
                        imageUrl      = None,
                        notes         = f"Live case — {county} auction {d}. {c['notes'][:200]}",
                        listingFoundAt = now_iso(),
                        isLiveScrape  = True,
                        parcelId      = c["parcelId"],
                        lat           = coords[0] if coords else None,
                        lng           = coords[1] if coords else None,
                    ))
                log.info("realforeclose/%s %s: %d case(s)", slug, d, len(cases))
                continue  # next date

        # Fallback: date-only calendar entry
        results.append(Property(
            id            = listing_id(f"{slug}-cal-{d}"),
            address       = f"{county.replace(' FL','')} County Foreclosure Auction — {d}",
            county        = county,
            type          = "Foreclosure",
            beds          = None,
            baths         = None,
            acreage       = None,
            bidPrice      = None,
            auctionDate   = iso_date,
            auctionSite   = f"{slug}.realforeclose.com",
            caseNumber    = f"Sale date {d}",
            propertyUrl   = preview_url,
            imageUrl      = None,
            notes         = (
                "Live auction date from county portal. No cases published yet — "
                "check back closer to the sale date." if authed else
                "Live auction date from county portal calendar. Add realforeclose.com "
                "credentials (REALFORECLOSE_USER / REALFORECLOSE_PASS) to GitHub Secrets "
                "to unlock per-case addresses and bids."
            ),
            listingFoundAt = now_iso(),
            isLiveScrape  = True,
            lat           = coords[0] if coords else None,
            lng           = coords[1] if coords else None,
        ))
        log.info("realforeclose/%s %s: date-only (calendar)", slug, d)

    return results


def _date_ms(mmddyyyy: str) -> float:
    from datetime import datetime
    try:
        m, d, y = mmddyyyy.split("/")
        return datetime(int(y), int(m), int(d)).timestamp() * 1000
    except Exception:
        return 0.0


# ===========================================================================
# Playwright-based parsers (bot-protected sites)
# ===========================================================================

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import stealth_sync
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    log.warning("Playwright not installed — browser-based parsers disabled")


@contextmanager
def browser_page():
    if not PLAYWRIGHT_AVAILABLE:
        yield None
        return
    launch_opts = {"headless": True}
    ctx_opts    = {}
    if PROXY_URL:
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


def browser_get_html(url, *, wait_selector=None, login_fn=None, timeout=30000):
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
            page.wait_for_timeout(2000)
            return page.content()
        except Exception as e:
            log.warning("browser_get_html %s -> %s", url, e)
            return None


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
    except Exception as e:
        log.warning("auction.com login: %s", e)

def parse_auction_com() -> list:
    results = []
    # Try JSON API first
    r = get_r("https://www.auction.com/api/property/search",
              params={"state": "FL",
                      "county": "Bay,Gulf,Walton,Okaloosa,Washington",
                      "propertyType": "SFR,MFR,LND,COM",
                      "listingType": "AUCTION,BIN",
                      "pageSize": 50, "page": 1})
    if r:
        try:
            for p in r.json().get("properties", r.json().get("results", [])):
                addr = p.get("address", {})
                full = addr.get("fullAddress", "") if isinstance(addr, dict) else str(addr)
                slug = p.get("slug") or p.get("propertyId", "")
                results.append(Property(
                    id=listing_id(str(slug)), address=full,
                    county=p.get("county","FL"), type=p.get("propertyType","Single Family"),
                    beds=p.get("bedrooms"), baths=p.get("bathrooms"),
                    acreage=p.get("lotSizeAcres"),
                    bidPrice=p.get("openingBid") or p.get("currentBid"),
                    auctionDate=p.get("auctionDate"),
                    auctionSite="auction.com", caseNumber=str(p.get("caseNumber", slug)),
                    propertyUrl=f"https://www.auction.com/residential/{slug}/",
                    imageUrl=p.get("photoUrl") or p.get("primaryPhoto"),
                    notes=str(p.get("description",""))[:300],
                    listingFoundAt=now_iso(),
                ))
            if results:
                log.info("auction.com (API) -> %d", len(results)); return results
        except Exception as e:
            log.warning("auction.com API: %s", e)

    if not PLAYWRIGHT_AVAILABLE:
        return results
    html = browser_get_html(
        "https://www.auction.com/foreclosure/real-estate/fl/"
        "?state=FL&county=Bay%2CGulf%2CWalton%2COkaloosa%2CWashington",
        wait_selector="[data-testid='property-card'], .propertyCard",
        login_fn=_auction_login if AUCTION_COM_EMAIL else None,
    )
    if not html:
        return results
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select("[data-testid='property-card'], .propertyCard, .property-card"):
        try:
            addr  = card.select_one(".address, [data-testid='address']")
            price = card.select_one(".price, [data-testid='price']")
            link  = card.select_one("a[href*='/residential/']") or card.select_one("a")
            if not addr: continue
            href = link["href"] if link else ""
            url  = href if href.startswith("http") else "https://www.auction.com" + href
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ",strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=clean_price(price.get_text()) if price else None,
                auctionDate=None, auctionSite="auction.com",
                caseNumber=listing_id(url), propertyUrl=url,
                imageUrl=None, notes="Via auction.com (browser)",
                listingFoundAt=now_iso(),
            ))
        except Exception: pass
    log.info("auction.com (browser) -> %d", len(results))
    return results


# --- hubzu.com ---------------------------------------------------------------
def _hubzu_login(page):
    if not HUBZU_EMAIL: return
    try:
        page.goto("https://www.hubzu.com/login", timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.fill("input[name='username'], input[type='email']", HUBZU_EMAIL)
        page.fill("input[name='password'], input[type='password']", HUBZU_PASS)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
    except Exception as e:
        log.warning("hubzu login: %s", e)

def parse_hubzu() -> list:
    results = []
    if not PLAYWRIGHT_AVAILABLE:
        return results
    html = browser_get_html(
        "https://www.hubzu.com/search?state=FL&county=Bay,Gulf,Walton,Okaloosa,Washington",
        wait_selector=".listing-card, [class*='propertyCard']",
        login_fn=_hubzu_login if HUBZU_EMAIL else None,
    )
    if not html: return results
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select(".property-listing, .listing-card, [class*='propertyCard']"):
        try:
            addr  = card.select_one(".property-address, .address, [class*='address']")
            price = card.select_one(".listing-price, .price, [class*='price']")
            link  = card.select_one("a[href*='/property/']") or card.select_one("a")
            if not addr: continue
            href = link["href"] if link else ""
            url  = href if href.startswith("http") else "https://www.hubzu.com" + href
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ",strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=clean_price(price.get_text()) if price else None,
                auctionDate=None, auctionSite="hubzu.com",
                caseNumber=listing_id(url), propertyUrl=url,
                imageUrl=None, notes="Via Hubzu - bank-owned REO (browser)",
                listingFoundAt=now_iso(),
            ))
        except Exception: pass
    log.info("hubzu (browser) -> %d", len(results))
    return results


# --- xome.com ----------------------------------------------------------------
def parse_xome() -> list:
    results = []
    r = get_r("https://www.xome.com/api/search/auction",
              params={"state":"FL","county":"Bay,Gulf,Walton,Okaloosa","pageSize":50})
    if r:
        try:
            for p in r.json().get("listings", r.json().get("results", [])):
                slug = p.get("slug") or p.get("id","")
                results.append(Property(
                    id=listing_id(str(slug)), address=p.get("address",""),
                    county=p.get("county","FL"), type=p.get("propertyType","Single Family"),
                    beds=p.get("beds"), baths=p.get("baths"),
                    acreage=p.get("lotSizeAcres"),
                    bidPrice=p.get("listPrice") or p.get("openingBid"),
                    auctionDate=p.get("auctionDate"),
                    auctionSite="xome.com", caseNumber=str(slug),
                    propertyUrl=f"https://www.xome.com/homes-for-sale/{slug}",
                    imageUrl=p.get("primaryPhotoUrl"),
                    notes=str(p.get("description",""))[:300],
                    listingFoundAt=now_iso(),
                ))
            if results:
                log.info("xome (API) -> %d", len(results)); return results
        except Exception as e:
            log.warning("xome API: %s", e)
    if not PLAYWRIGHT_AVAILABLE: return results
    html = browser_get_html(
        "https://www.xome.com/real-estate-auctions/for-sale?state=FL"
        "&county=Bay,Gulf,Walton,Okaloosa,Washington",
        wait_selector="[class*='listing'], [class*='property-card']",
    )
    if not html: return results
    soup = BeautifulSoup(html,"lxml")
    for card in soup.select("[class*='ListingCard'],[class*='PropertyCard'],.listing-card"):
        try:
            addr  = card.select_one("[class*='address']")
            price = card.select_one("[class*='price']")
            link  = card.select_one("a")
            if not addr: continue
            href = link["href"] if link else ""
            url  = href if href.startswith("http") else "https://www.xome.com"+href
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ",strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=clean_price(price.get_text()) if price else None,
                auctionDate=None, auctionSite="xome.com",
                caseNumber=listing_id(url), propertyUrl=url,
                imageUrl=None, notes="Via Xome (browser)",
                listingFoundAt=now_iso(),
            ))
        except Exception: pass
    log.info("xome (browser) -> %d", len(results))
    return results


# --- hudhomestore.gov --------------------------------------------------------
def parse_hud() -> list:
    results = []
    kw = {"headers":{**HEADERS,"X-API-KEY":HUD_API_KEY}} if HUD_API_KEY else {}
    r  = get_r("https://www.hudhomestore.gov/HUDApi/ListingSearch",
               params={"stateCode":"FL","county":"Bay","pageSize":50}, **kw)
    if not r: return results
    try:
        for p in r.json().get("properties", r.json().get("results",[])):
            pid = str(p.get("listingId", p.get("caseNumber","")))
            results.append(Property(
                id=listing_id(pid),
                address=f"{p.get('streetAddr','')} {p.get('city','')} FL {p.get('zip','')}".strip(),
                county=f"{p.get('county','FL')} FL",
                type=p.get("propType","Single Family"),
                beds=p.get("bedroom"), baths=p.get("bath"), acreage=None,
                bidPrice=int(p.get("listPrice",0) or 0) or None,
                auctionDate=p.get("listingDate"),
                auctionSite="hudhomestore.com", caseNumber=pid,
                propertyUrl=f"https://www.hudhomestore.gov/Listing/PropertySearch.aspx?sState=FL&sCaseNumber={pid}",
                imageUrl=None,
                notes=f"HUD FHA foreclosure. Case #{pid}.",
                listingFoundAt=now_iso(),
            ))
    except Exception as e:
        log.warning("HUD: %s", e)
    log.info("HUD -> %d", len(results))
    return results


# --- govdeals.com ------------------------------------------------------------
def parse_govdeals() -> list:
    results = []
    if not PLAYWRIGHT_AVAILABLE:
        return results
    html = browser_get_html(
        "https://www.govdeals.com/index.cfm?fa=Main.AdvSearchResultsNew"
        "&searchPg=1&category=0058&state=FL",
        wait_selector=".itemListing, .listing-item",
    )
    if not html: return results
    soup = BeautifulSoup(html,"lxml")
    for item in soup.select(".itemListing,.listing-item,[class*='item-card']"):
        try:
            title = item.select_one(".itemTitle,.item-title,h3,h2")
            price = item.select_one(".currentBid,.current-bid,.price,[class*='bid']")
            link  = item.select_one("a[href*='itemno'],a[href*='item'],a")
            if not title: continue
            href = link["href"] if link else ""
            url  = href if href.startswith("http") else "https://www.govdeals.com"+href
            results.append(Property(
                id=listing_id(url), address=title.get_text(" ",strip=True),
                county="FL", type="Mixed Use",
                beds=None, baths=None, acreage=None,
                bidPrice=clean_price(price.get_text()) if price else None,
                auctionDate=None, auctionSite="govdeals.com",
                caseNumber=listing_id(url), propertyUrl=url,
                imageUrl=None, notes="Government surplus / tax deed sale.",
                listingFoundAt=now_iso(),
            ))
        except Exception: pass
    log.info("govdeals -> %d", len(results))
    return results


# ===========================================================================
# CRM integration
# ===========================================================================

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
        return [c for c in r.json().get("contacts",[]) if _field(c,"watch_active") == "Active"]
    except Exception as e:
        log.warning("CRM fetch: %s", e)
        return []

def _field(contact, key):
    for f in contact.get("customFields",[]):
        fkey  = f.get("fieldKey","")
        fname = f.get("name","").lower().replace(" ","_")
        if fkey.endswith(key) or fname == key:
            return str(f.get("value",""))
    return ""

def matches_watcher(prop: Property, watcher: dict) -> bool:
    counties = [c.strip() for c in _field(watcher,"watch_counties").split(",") if c.strip()]
    types    = [t.strip() for t in _field(watcher,"watch_property_types").split(",") if t.strip()]
    sites    = [s.strip() for s in _field(watcher,"watch_sites_to_monitor").split(",") if s.strip()]
    inc_kw   = [k.strip().lower() for k in _field(watcher,"watch_keywords_include").split(",") if k.strip()]
    exc_kw   = [k.strip().lower() for k in _field(watcher,"watch_keywords_exclude").split(",") if k.strip()]
    try:
        min_p = float(_field(watcher,"watch_min_price") or 0)
        max_p = float(_field(watcher,"watch_max_price") or 1e12)
        min_a = float(_field(watcher,"watch_min_acreage") or 0)
        min_b = int(_field(watcher,"watch_min_bedrooms") or 0)
    except ValueError:
        min_p,max_p,min_a,min_b = 0,1e12,0,0
    text = (prop.address+" "+prop.notes).lower()
    if counties and prop.county not in counties:       return False
    if types    and prop.type    not in types:         return False
    if sites    and prop.auctionSite not in sites:     return False
    if prop.bidPrice and prop.bidPrice < min_p:        return False
    if prop.bidPrice and prop.bidPrice > max_p:        return False
    if prop.acreage  and prop.acreage  < min_a:        return False
    if prop.beds     and prop.beds     < min_b:        return False
    if inc_kw and not any(k in text for k in inc_kw): return False
    if exc_kw and     any(k in text for k in exc_kw): return False
    return True

# CRM custom field IDs — match-alert fields (used by email template merge tags)
MATCH_ALERT_FIELD_ADDRESS   = "fVJxlefxB0xVhCVxvA3w"
MATCH_ALERT_FIELD_SUMMARY   = "RYEncSxTF2OSJGmVaZtu"
MATCH_ALERT_FIELD_WATCHLIST = "90UFlXz6MffRwNd5SYmJ"

# CRM custom field IDs — property alert fields (used by email template merge tags)
PROP_FIELD_ADDRESS   = "ui320qyOyGLYLu7r9n7f"
PROP_FIELD_TYPE      = "UEA4nnFoTcC5qHFRDHXB"
PROP_FIELD_COUNTY    = "7AlGQ5OYeGuF9ipF2UUR"
PROP_FIELD_DATE      = "8i6F4uqG3Zc1y6ShRAfX"
PROP_FIELD_BEDS      = "BZuuDdhYrENPRrvpprfq"
PROP_FIELD_BATHS     = "9PRHtFKhmC2uKZLiG486"
PROP_FIELD_PRICE     = "T3RC4uv0pThcy5SR8ZTj"
PROP_FIELD_SITE      = "kMKCRQ7dsUDBkcM8rLgZ"
PROP_FIELD_CASE      = "wZwhyn0FC9XgqSPOIL9I"
PROP_FIELD_ACREAGE   = "bXrxV4PVKE7FYv9Jg5ai"
PROP_FIELD_NOTES     = "UrtpGT74EKWQIezYtiaj"

# Workflow ID for the Property Alert Webhook workflow
PROPERTY_ALERT_WORKFLOW_ID  = "cd111f23-2915-45e1-85f6-00edf1bffcdd"


def _build_summary(prop: Property, watch_list_name: str) -> str:
    """Build a human-readable summary string for the match_alert_summary field."""
    parts = []
    parts.append(prop.type or "Property")
    parts.append(prop.county or "")
    if prop.beds and prop.baths:
        parts.append(f"{int(prop.beds)}BR/{int(prop.baths)}BA")
    elif prop.acreage:
        parts.append(f"{prop.acreage} acres")
    if prop.bidPrice:
        parts.append(f"Bid ${prop.bidPrice:,}")
    if prop.auctionDate:
        try:
            date_str = prop.auctionDate[:10]
        except Exception:
            date_str = str(prop.auctionDate)
        parts.append(f"Auction {date_str}")
    parts.append(f"on {prop.auctionSite}")
    return " | ".join(p for p in parts if p)


def _update_contact_alert_fields(contact_id: str, prop: Property, watch_list_name: str) -> bool:
    """
    Step 1: Update ALL property + match-alert custom fields with fresh data
    so every email template merge tag renders the correct property details.
    Covers both {{contact.property_address}} and {{contact.match_alert_address}} families.
    """
    if not CRM_API_KEY:
        return False
    summary   = _build_summary(prop, watch_list_name)
    date_str  = (prop.auctionDate or "")[:10].replace("-", "/") if prop.auctionDate else ""
    price_str = f"${prop.bidPrice:,}" if prop.bidPrice else ""
    acreage_str = f"{prop.acreage} acres" if prop.acreage else ""
    fields = [
        # Property alert fields (used by Foreclosure Property Alert email template)
        {"id": PROP_FIELD_ADDRESS, "field_value": prop.address},
        {"id": PROP_FIELD_TYPE,    "field_value": prop.type or ""},
        {"id": PROP_FIELD_COUNTY,  "field_value": prop.county or ""},
        {"id": PROP_FIELD_DATE,    "field_value": date_str},
        {"id": PROP_FIELD_SITE,    "field_value": prop.auctionSite or ""},
        {"id": PROP_FIELD_CASE,    "field_value": prop.caseNumber or ""},
        {"id": PROP_FIELD_NOTES,   "field_value": prop.notes or ""},
        # Match-alert fields (secondary merge tags)
        {"id": MATCH_ALERT_FIELD_ADDRESS,   "field_value": prop.address},
        {"id": MATCH_ALERT_FIELD_SUMMARY,   "field_value": summary},
        {"id": MATCH_ALERT_FIELD_WATCHLIST, "field_value": watch_list_name},
    ]
    if prop.beds  is not None: fields.append({"id": PROP_FIELD_BEDS,    "field_value": str(int(prop.beds))})
    if prop.baths is not None: fields.append({"id": PROP_FIELD_BATHS,   "field_value": str(int(prop.baths))})
    if price_str:              fields.append({"id": PROP_FIELD_PRICE,   "field_value": price_str})
    if acreage_str:            fields.append({"id": PROP_FIELD_ACREAGE, "field_value": acreage_str})
    body = {"customFields": fields}
    try:
        r = requests.put(
            f"https://services.leadconnectorhq.com/contacts/{contact_id}",
            headers={"Authorization": f"Bearer {CRM_API_KEY}", "Version": "2021-07-28",
                     "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("CRM field update [%s]: %s", contact_id, e)
        return False


def _enroll_in_workflow(contact_id: str) -> bool:
    """
    Step 2: Enroll the contact directly in the Property Alert Webhook workflow.
    This reliably resolves the contact (no webhook contact-lookup issues).
    """
    if not CRM_API_KEY:
        return False
    try:
        r = requests.post(
            f"https://services.leadconnectorhq.com/contacts/{contact_id}/workflow/{PROPERTY_ALERT_WORKFLOW_ID}",
            headers={"Authorization": f"Bearer {CRM_API_KEY}", "Version": "2021-07-28",
                     "Content-Type": "application/json"},
            json={},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("succeeded", False)
    except Exception as e:
        log.warning("CRM workflow enroll [%s]: %s", contact_id, e)
        return False


def fire_crm_alert(prop: Property, watcher: dict):
    """
    Send a property match alert to a watcher contact.
    Two-step: update match_alert custom fields, then enroll in alert workflow.
    This ensures the email template always renders fresh property data.
    """
    if not CRM_API_KEY:
        log.warning("CRM_API_KEY not set — skipping alert for %s", watcher.get("email","?"))
        return

    contact_id     = watcher.get("id", "")
    watch_list_name = _field(watcher, "watch_list_name") or "My Watch List"
    email          = watcher.get("email", "?")

    # Step 1 — write fresh property data to the 3 match-alert custom fields
    fields_ok = _update_contact_alert_fields(contact_id, prop, watch_list_name)
    if not fields_ok:
        log.warning("CRM field update failed for %s — still attempting workflow enroll", email)

    # Step 2 — enroll contact in the Property Alert Webhook workflow
    time.sleep(0.5)   # brief pause so field update propagates before email renders
    workflow_ok = _enroll_in_workflow(contact_id)

    log.info("CRM alert [fields:%s workflow:%s] -> %s | %s",
             "OK" if fields_ok else "FAIL",
             "OK" if workflow_ok else "FAIL",
             email, prop.address)


# ===========================================================================
# Feed writer — with empty-result safety guard
# ===========================================================================

def write_feed(listings: list):
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Drop calendar-only date placeholders (render as broken "TBD" cards in the app)
    real_listings = [p for p in listings if not _is_calendar_marker(p)]
    dropped = len(listings) - len(real_listings)
    if dropped:
        log.info("Filtered %d calendar-only markers from feed", dropped)
    listings = real_listings
    if len(listings) < MIN_LISTINGS_TO_OVERWRITE:
        log.warning("Scraper returned %d listings (threshold %d) — preserving existing feed",
                    len(listings), MIN_LISTINGS_TO_OVERWRITE)
        return
    feed = {
        "generated": now_iso(),
        "source":    "PP Investments Foreclosure Watch Scraper v7",
        "count":     len(listings),
        "listings":  [p.to_dict() for p in listings],
    }
    FEED_PATH.write_text(json.dumps(feed, indent=2, default=str))
    live_count = sum(1 for p in listings if p.isLiveScrape)
    log.info("Wrote %s — %d total (%d live + %d curated)",
             FEED_PATH, len(listings), live_count, len(listings)-live_count)


# ===========================================================================
# Orchestrator
# ===========================================================================

def run_once():
    log.info("=== PP Investments Scraper v7 — run started ===")
    log.info("realforeclose auth: %s | Playwright: %s",
             "YES" if HAS_RF_CREDS else "NO",
             "YES" if PLAYWRIGHT_AVAILABLE else "NO")

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
            log.error("Parser %s error: %s", getattr(fn,"__name__","?"), e)
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
