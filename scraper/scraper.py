#!/usr/bin/env python3
"""
PP Investments - Foreclosure & Auction Watch Scraper
Polls realforeclose.com counties + auction.com / hubzu / xome /
homepath / HUD / GovDeals every 4-6 h.
Writes docs/feed.json  (served via GitHub Pages for the watch app)
Fires CRM Workflow-2 webhook per matched watcher (per-contact alert).

Deploy: GitHub Actions (.github/workflows/scrape.yml), VPS cron,
        n8n, Make.com, or AWS Lambda (README has all four).
"""

import json, os, time, logging, hashlib
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
CRM_WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "")
CRM_API_KEY     = os.getenv("CRM_API_KEY",    "")
CRM_LOCATION_ID = "KoyfEHXBmxbD69hWgYyJ"
HUD_API_KEY     = os.getenv("HUD_API_KEY",    "")
GOVDEALS_KEY    = os.getenv("GOVDEALS_API_KEY","")

FEED_PATH  = Path(os.getenv("FEED_OUTPUT_PATH", "docs/feed.json"))
SEEN_PATH  = Path("scraper/.seen.json")

# Minimum listings before we overwrite the existing feed.
# If the scraper returns fewer than this, keep the existing feed.json intact.
MIN_LISTINGS_TO_OVERWRITE = 1

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
    ("bay",         "Bay FL"),
    ("gulf",        "Gulf FL"),
    ("walton",      "Walton FL"),
    ("okaloosa",    "Okaloosa FL"),
    ("washington",  "Washington FL"),
    ("holmes",      "Holmes FL"),
    ("jackson",     "Jackson FL"),
    ("calhoun",     "Calhoun FL"),
    ("escambia",    "Escambia FL"),
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


# -- Seen-listings deduplication ----------------------------------------------
def load_seen() -> set:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()

def save_seen(seen: set):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2))

def listing_id(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:12]


# -- HTTP helpers -------------------------------------------------------------
def get(url, *, timeout=20, **kw) -> Optional[requests.Response]:
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
# SITE PARSERS
# ============================================================================

def parse_realforeclose(slug: str, county: str) -> list:
    """
    realforeclose.com - Judicial foreclosure sales.
    The site renders a calendar/list view; we grab the upcoming-sales page.
    """
    base    = f"https://{slug}.realforeclose.com"
    results = []

    # Try the main auction listing page
    for path in [
        "/index.cfm?zaction=AUCTION&zmethod=preview",
        "/index.cfm?zaction=AUCTION&zmethod=PREVIEW&AUCTIONDATE=",
        "/",
    ]:
        r = get(f"{base}{path}")
        if r:
            break
    if not r:
        return results

    soup = BeautifulSoup(r.text, "lxml")

    # realforeclose uses a ColdFusion page; listings are in table rows
    # with class AUCTION_ITEM or inside a #PUBLIC_AUCTION_RESULTS table
    selectors = [
        "table#PUBLIC_AUCTION_RESULTS tr",
        ".AUCTION_ITEM",
        "tr.altRow",
        "tr.altRow2",
        "tr[class*='Row']",
        "table.dataTable tr",
        "#auction_list tr",
        "tbody tr",
    ]

    rows = []
    for sel in selectors:
        rows = soup.select(sel)
        if len(rows) > 1:  # >1 because first row is often a header
            log.info("realforeclose/%s: matched selector '%s' -> %d rows", slug, sel, len(rows))
            break

    now_iso = datetime.now(timezone.utc).isoformat()

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        # Skip header rows
        if row.find("th") and not row.find("td"):
            continue

        text_cells = [c.get_text(" ", strip=True) for c in cells]
        full_text  = " | ".join(text_cells)

        # Try to extract case number (looks like YYYY-CA-NNNNNN)
        import re
        case_match = re.search(r'\d{4}-[A-Z]{2}-\d+', full_text)
        case_num   = case_match.group(0) if case_match else listing_id(full_text)

        # Try to extract a bid/judgment amount ($NNN,NNN)
        price_match = re.search(r'\$[\d,]+\.?\d*', full_text)
        bid_int = None
        if price_match:
            clean = re.sub(r'[^\d]', '', price_match.group(0))
            bid_int = int(clean) if clean else None

        # Try to find a link
        link = row.find("a", href=True)
        if link:
            href = link["href"]
            prop_url = href if href.startswith("http") else base + "/" + href.lstrip("/")
        else:
            prop_url = f"{base}/index.cfm?zaction=AUCTION&zmethod=preview"

        # Best-effort address from cells
        # Typically: date | case# | address | bid | plaintiff/defendant
        addr = ""
        for cell_text in text_cells:
            # Address-like: contains a number and a street keyword
            if re.search(r'\d+\s+\w+\s+(St|Ave|Rd|Dr|Blvd|Ln|Way|Ct|Pkwy|Hwy|Pl)', cell_text, re.I):
                addr = cell_text
                break
        if not addr:
            # Fall back to longest cell
            addr = max(text_cells, key=len) if text_cells else f"Property in {county}"

        # Auction date: first cell that looks like a date
        auction_date = None
        for cell_text in text_cells:
            date_match = re.search(r'\d{1,2}/\d{1,2}/\d{4}', cell_text)
            if date_match:
                auction_date = date_match.group(0)
                break

        if not addr or len(addr) < 5:
            continue

        results.append(Property(
            id            = listing_id(case_num),
            address       = addr,
            county        = county,
            type          = "Single Family",
            beds          = None,
            baths         = None,
            acreage       = None,
            bidPrice      = bid_int,
            auctionDate   = auction_date,
            auctionSite   = f"{slug}.realforeclose.com",
            caseNumber    = case_num,
            propertyUrl   = prop_url,
            imageUrl      = None,
            notes         = f"Judicial foreclosure - {county}. Sold as-is. {full_text[:200]}",
            listingFoundAt = now_iso,
        ))

    log.info("realforeclose/%s -> %d listings", slug, len(results))
    return results


def parse_auction_com() -> list:
    results = []
    r = get(
        "https://www.auction.com/api/property/search",
        params={
            "state": "FL",
            "county": "Bay,Gulf,Walton,Okaloosa,Washington",
            "propertyType": "SFR,MFR,LND,COM",
            "listingType": "AUCTION,BIN",
            "pageSize": 50,
            "page": 1,
        }
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
                    listingFoundAt=datetime.now(timezone.utc).isoformat(),
                ))
            log.info("auction.com -> %d listings", len(results))
            return results
        except Exception as e:
            log.warning("auction.com json: %s", e)

    # Fallback HTML
    r = get("https://www.auction.com/foreclosure/real-estate/fl/")
    if not r:
        return results
    soup = BeautifulSoup(r.text, "lxml")
    for card in soup.select("[data-testid='property-card'], .propertyCard, .property-card"):
        try:
            addr  = card.select_one(".address, [data-testid='address']")
            price = card.select_one(".price, [data-testid='price']")
            link  = card.select_one("a[href*='/residential/']") or card.select_one("a")
            if not addr:
                continue
            href    = link["href"] if link else ""
            url     = href if href.startswith("http") else "https://www.auction.com" + href
            raw_p   = price.get_text(strip=True) if price else ""
            bid_int = int("".join(c for c in raw_p if c.isdigit())) if raw_p else None
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ", strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=bid_int, auctionDate=None,
                auctionSite="auction.com", caseNumber=listing_id(url),
                propertyUrl=url, imageUrl=None, notes="Via auction.com",
                listingFoundAt=datetime.now(timezone.utc).isoformat(),
            ))
        except Exception as e:
            log.debug("auction.com card: %s", e)
    log.info("auction.com (html) -> %d listings", len(results))
    return results


def parse_hubzu() -> list:
    results = []
    r = get("https://www.hubzu.com/search/?state=FL&county=Bay,Gulf,Walton,Okaloosa")
    if not r:
        return results
    soup = BeautifulSoup(r.text, "lxml")
    for card in soup.select(".property-listing, .listing-card, [class*='propertyCard']"):
        try:
            addr  = card.select_one(".property-address, .address")
            price = card.select_one(".listing-price, .price, .bid-price")
            link  = card.select_one("a[href*='/real-estate/']") or card.select_one("a")
            if not addr:
                continue
            href    = link["href"] if link else ""
            url     = href if href.startswith("http") else "https://www.hubzu.com" + href
            raw_p   = price.get_text(strip=True) if price else ""
            bid_int = int("".join(c for c in raw_p if c.isdigit())) if raw_p else None
            results.append(Property(
                id=listing_id(url), address=addr.get_text(" ", strip=True),
                county="FL", type="Single Family",
                beds=None, baths=None, acreage=None,
                bidPrice=bid_int, auctionDate=None,
                auctionSite="hubzu.com", caseNumber=listing_id(url),
                propertyUrl=url, imageUrl=None, notes="Via Hubzu - bank-owned REO.",
                listingFoundAt=datetime.now(timezone.utc).isoformat(),
            ))
        except Exception as e:
            log.debug("hubzu card: %s", e)
    log.info("hubzu -> %d listings", len(results))
    return results


def parse_xome() -> list:
    results = []
    r = get(
        "https://www.xome.com/api/search/auction",
        params={"state": "FL", "county": "Bay,Gulf,Walton,Okaloosa", "pageSize": 50}
    )
    if not r:
        return results
    try:
        for p in r.json().get("listings", r.json().get("results", [])):
            slug = p.get("slug") or p.get("id", "")
            results.append(Property(
                id=listing_id(str(slug)), address=p.get("address", ""),
                county=p.get("county", "FL"), type=p.get("propertyType", "Single Family"),
                beds=p.get("beds"), baths=p.get("baths"), acreage=p.get("lotSizeAcres"),
                bidPrice=p.get("listPrice") or p.get("openingBid"),
                auctionDate=p.get("auctionDate"),
                auctionSite="xome.com", caseNumber=str(slug),
                propertyUrl=f"https://www.xome.com/homes-for-sale/{slug}",
                imageUrl=p.get("primaryPhotoUrl"),
                notes=str(p.get("description", ""))[:300],
                listingFoundAt=datetime.now(timezone.utc).isoformat(),
            ))
    except Exception as e:
        log.warning("xome json: %s", e)
    log.info("xome -> %d listings", len(results))
    return results


def parse_hud() -> list:
    results = []
    kw = {"headers": {**HEADERS, "X-API-KEY": HUD_API_KEY}} if HUD_API_KEY else {}
    r  = get(
        "https://www.hudhomestore.gov/HUDApi/ListingSearch",
        params={"stateCode": "FL", "county": "Bay", "pageSize": 50},
        **kw
    )
    if not r:
        return results
    try:
        for p in r.json().get("properties", r.json().get("results", [])):
            prop_id = str(p.get("listingId", p.get("caseNumber", "")))
            results.append(Property(
                id=listing_id(prop_id),
                address=f"{p.get('streetAddr','')} {p.get('city','')} FL {p.get('zip','')}".strip(),
                county=f"{p.get('county','FL')} FL",
                type=p.get("propType", "Single Family"),
                beds=p.get("bedroom"), baths=p.get("bath"), acreage=None,
                bidPrice=int(p.get("listPrice", 0) or 0) or None,
                auctionDate=p.get("listingDate"),
                auctionSite="hudhomestore.com", caseNumber=prop_id,
                propertyUrl=f"https://www.hudhomestore.gov/Listing/PropertySearch.aspx?sState=FL&sCaseNumber={prop_id}",
                imageUrl=None,
                notes=f"HUD FHA foreclosure. Case #{prop_id}.",
                listingFoundAt=datetime.now(timezone.utc).isoformat(),
            ))
    except Exception as e:
        log.warning("HUD json: %s", e)
    log.info("HUD -> %d listings", len(results))
    return results


def parse_govdeals() -> list:
    results = []
    r = get(
        "https://www.govdeals.com/index.cfm?fa=Main.AdvSearchResultsNew"
        "&searchPg=1&category=0058&state=FL"
    )
    if not r:
        return results
    soup = BeautifulSoup(r.text, "lxml")
    for item in soup.select(".itemListing, .listing-item"):
        try:
            title = item.select_one(".itemTitle, .item-title, h3")
            price = item.select_one(".currentBid, .current-bid, .price")
            link  = item.select_one("a[href*='itemno'], a[href*='item']")
            if not title:
                continue
            href    = link["href"] if link else ""
            url     = href if href.startswith("http") else "https://www.govdeals.com" + href
            bid_int = int("".join(
                c for c in (price.get_text(strip=True) if price else "") if c.isdigit()
            )) or None
            results.append(Property(
                id=listing_id(url), address=title.get_text(" ", strip=True),
                county="FL", type="Mixed Use",
                beds=None, baths=None, acreage=None,
                bidPrice=bid_int, auctionDate=None,
                auctionSite="govdeals.com", caseNumber=listing_id(url),
                propertyUrl=url, imageUrl=None,
                notes="Government surplus / tax deed sale.",
                listingFoundAt=datetime.now(timezone.utc).isoformat(),
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
        log.warning("CRM_API_KEY not set - skipping watcher match & alerts")
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
    ok     = post_json(CRM_WEBHOOK_URL, payload)
    status = "OK" if ok else "FAIL"
    log.info("CRM alert [%s] -> %s | %s", status, watcher.get("email", "?"), prop.address)


# ============================================================================
# FEED WRITER  — with empty-result safety guard
# ============================================================================

def write_feed(listings: list):
    """
    Write docs/feed.json.
    SAFETY: if the scraper returns 0 listings (site down / blocked / no results),
    keep the existing feed.json rather than overwriting it with an empty array.
    This prevents the watch app from going blank due to a transient scrape failure.
    """
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)

    if len(listings) < MIN_LISTINGS_TO_OVERWRITE:
        log.warning(
            "Scraper returned %d listings (below threshold %d) — "
            "keeping existing feed.json to avoid blanking the app.",
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
    log.info("=== PP Investments Scraper run started ===")
    seen     = load_seen()
    all_new  = []
    all_kept = []
    watchers = fetch_active_watchers()
    log.info("Loaded %d active watchers from CRM", len(watchers))

    parsers = (
        [lambda s=s, c=c: parse_realforeclose(s, c) for s, c in REALFORECLOSE_COUNTIES]
        + [parse_auction_com, parse_hubzu, parse_xome, parse_hud, parse_govdeals]
    )

    for fn in parsers:
        try:
            listings = fn()
        except Exception as e:
            log.error("Parser error: %s", e)
            listings = []

        for prop in listings:
            all_kept.append(prop)
            if prop.id not in seen:
                prop.isNew = True
                all_new.append(prop)
                seen.add(prop.id)
                for watcher in watchers:
                    if matches_watcher(prop, watcher):
                        fire_crm_alert(prop, watcher)
                        time.sleep(0.3)
            else:
                prop.isNew = False

        time.sleep(1)

    save_seen(seen)
    write_feed(all_kept)
    log.info("=== Done. %d new / %d total ===", len(all_new), len(all_kept))


if __name__ == "__main__":
    run_once()
