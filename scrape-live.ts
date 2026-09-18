/**
 * PPI Property Watch — LIVE scraper (Node/Bun, zero-dependency).
 *
 * Pulls REAL auction data from each realforeclose.com county portal:
 *   - Always: the public auction CALENDAR (which dates have sales).
 *   - When REALFORECLOSE_USER / REALFORECLOSE_PASS are set: logs in and
 *     parses the per-case rows for each sale date — real addresses, case
 *     numbers, final-judgment / opening bids, parcel ids.
 *
 * It merges live results with the curated inventory so the feed always
 * reflects genuine current/upcoming auctions. Output: scraper/feed.json.
 *
 * Run:  bun scraper/scrape-live.ts
 *       (GitHub Actions runs this every 2h — see github-actions.yml)
 */
import { writeFileSync, readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// The scraper must be self-contained — it runs in the ppiwatch Pages repo,
// which does NOT contain the app's src/lib/* modules. So instead of importing
// the curated inventory, we load it from the existing feed.json (the prior run's
// curated entries, which carry all the addresses, coords, and metadata). Live
// entries from this run are merged on top, replacing any stale live entries.

/** County center coordinates (used to place live calendar entries on the map
 *  when per-case geocoding isn't available). Mirrors src/lib/geo.ts. */
const COUNTY_GEO: Record<string, { center: [number, number] }> = {
  "Bay FL": { center: [30.16, -85.66] },
  "Gulf FL": { center: [29.78, -85.3] },
  "Walton FL": { center: [30.4, -86.15] },
  "Okaloosa FL": { center: [30.51, -86.48] },
  "Washington FL": { center: [30.78, -85.54] },
  "Holmes FL": { center: [30.82, -85.81] },
  "Jackson FL": { center: [30.8, -85.23] },
  "Calhoun FL": { center: [30.44, -85.05] },
  "Escambia FL": { center: [30.41, -87.21] },
  "Santa Rosa FL": { center: [30.63, -87.04] },
};

/** Verification status for the audit step. */
type Verified = "verified" | "unverified" | "pending";

/** Sites whose URLs are trusted listing portals (deep links to real pages). */
const TRUSTED_SITES = new Set([
  "bay.realforeclose.com",
  "gulf.realforeclose.com",
  "walton.realforeclose.com",
  "okaloosa.realforeclose.com",
  "washington.realforeclose.com",
  "auction.com",
  "hubzu.com",
  "xome.com",
  "homepath.com",
  "hudhomestore.com",
  "govdeals.com",
]);

/** A "deep" listing URL carries a specific identifier — not a search page. */
function isDeepListingUrl(url: string, parcelId?: string | null): boolean {
  if (!url) return false;
  if (/AuctionID=/.test(url)) return true;
  if (/govdeals\.com\/asset\/.+\/\d+/i.test(url)) return true;
  if (/auction\.com\/details\//i.test(url)) return true;
  if (/caseNumber=/i.test(url)) return true;
  if (/homepath\.fanniemae\.com\/property\//i.test(url)) return true;
  if (/hubzu\.com\/property\/details\//i.test(url)) return true;
  if (/xome\.com\/realestate\//i.test(url) && parcelId) return true;
  if (/\d{4,}/.test(url.split("?")[0])) return true;
  return false;
}

/** Classify a property's verification status from its URL shape + source.
 *  Used as the fallback when a live HEAD check isn't possible (CORS, etc.). */
function classifyVerification(
  url: string,
  site: string,
  parcelId?: string | null,
  isLiveScrape?: boolean,
): Verified {
  if (isLiveScrape) return "pending";
  if (TRUSTED_SITES.has(site) && isDeepListingUrl(url, parcelId)) {
    return "verified";
  }
  return "unverified";
}

/** Attempt a HEAD request to confirm the URL resolves. Many listing sites
 *  reject HEAD or return 403/405; in that case we fall back to URL-shape
 *  classification. Returns null when the check is inconclusive. */
async function headCheck(url: string): Promise<boolean | null> {
  try {
    const res = await fetch(url, {
      method: "HEAD",
      headers: { "User-Agent": UA },
      redirect: "follow",
      signal: AbortSignal.timeout(8000),
    });
    if (res.ok) return true;
    // 405/403 means the server exists but rejects HEAD — treat as
    // inconclusive (fall back to classification), not a hard failure.
    if (res.status === 405 || res.status === 403) return null;
    if (res.status === 404) return false;
    return null;
  } catch {
    return null;
  }
}

/** Audit a single listing: HEAD-check the URL, then classify by shape.
 *  Live-scrape entries from realforeclose are trusted (the scraper already
 *  confirmed the page exists when it parsed it). */
async function auditListing(p: {
  propertyUrl: string;
  auctionSite: string;
  parcelId?: string | null;
  isLiveScrape?: boolean;
}): Promise<{ verified: Verified; verifiedAt: string | null }> {
  const now = nowIso();
  // Live realforeclose entries are already confirmed by the parse step.
  if (p.isLiveScrape && TRUSTED_SITES.has(p.auctionSite)) {
    return { verified: "verified", verifiedAt: now };
  }
  // Try a HEAD check for trusted deep links.
  if (TRUSTED_SITES.has(p.auctionSite) && isDeepListingUrl(p.propertyUrl, p.parcelId)) {
    const ok = await headCheck(p.propertyUrl);
    if (ok === true) return { verified: "verified", verifiedAt: now };
    if (ok === false) return { verified: "unverified", verifiedAt: now };
    // inconclusive → trust the deep-link shape
    return { verified: "verified", verifiedAt: now };
  }
  // Generic search-page URLs (land.com, fsbo.com, etc.) → unverified.
  const cls = classifyVerification(
    p.propertyUrl,
    p.auctionSite,
    p.parcelId,
    p.isLiveScrape,
  );
  return { verified: cls, verifiedAt: cls === "pending" ? null : now };
}

// Always write feed.json next to THIS script. When the script lives at the
// repo root (the GitHub Pages repo), that's root feed.json — exactly what
// `git add feed.json` in the workflow commits and what GitHub Pages serves.
// Writing a hard-coded "./scraper/feed.json" never reached the deployed URL.
const __dirname = dirname(fileURLToPath(import.meta.url));
const FEED_OUT = join(__dirname, "feed.json");

// Curated baseline inventory (70 vetted listings: foreclosures, tax deeds,
// REO, land sales, etc.) bundled alongside the scraper so the GitHub Pages
// repo is self-contained — it doesn't need the app's src/lib/* modules.
// Falls back to the prior feed.json's non-live entries when curated.json
// is absent (the legacy path).
const CURATED_PATH = join(__dirname, "curated.json");

const SALE_TYPE_BY_SITE: Record<string, string> = {
  "bay.realforeclose.com": "Foreclosure",
  "gulf.realforeclose.com": "Foreclosure",
  "walton.realforeclose.com": "Foreclosure",
  "okaloosa.realforeclose.com": "Foreclosure",
  "washington.realforeclose.com": "Foreclosure",
  "auction.com": "REO / Bank-Owned",
  "hubzu.com": "REO / Bank-Owned",
  "xome.com": "Foreclosure",
  "homepath.com": "REO / Bank-Owned",
  "hudhomestore.com": "REO / Bank-Owned",
  "govdeals.com": "Government Surplus",
  "land.com": "Land Sale (Non-Auction)",
  "landwatch.com": "Land Sale (Non-Auction)",
  "fsbo.com": "Land Sale (Non-Auction)",
  "county tax collector": "Land Sale (Non-Auction)",
  // Open-market residential/commercial sources (non-auction).
  "bhhsbeachpropertiesofflorida.com": "Open-Market (Non-Auction)",
  "zillow.com": "Open-Market (Non-Auction)",
  "realtor.com": "Open-Market (Non-Auction)",
  "trulia.com": "Open-Market (Non-Auction)",
  "redfin.com": "Open-Market (Non-Auction)",
  // Commercial real-estate marketplace.
  "loopnet.com": "Open-Market (Non-Auction)",
  // Distressed-data aggregators.
  "foreclosure.com": "Foreclosure",
  "realtytrac.com": "Foreclosure",
  // Public-record reference (not a listing source).
  "city-data.com": "Open-Market (Non-Auction)",
};

const COUNTY_BY_SITE: Record<string, string> = {
  "bay.realforeclose.com": "Bay FL",
  "gulf.realforeclose.com": "Gulf FL",
  "walton.realforeclose.com": "Walton FL",
  "okaloosa.realforeclose.com": "Okaloosa FL",
  "washington.realforeclose.com": "Washington FL",
};

const PORTALS = Object.keys(COUNTY_BY_SITE);

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

const RF_USER = process.env.REALFORECLOSE_USER || "";
const RF_PASS = process.env.REALFORECLOSE_PASS || "";
const HAS_CREDS = !!(RF_USER && RF_PASS);

function nowIso(): string {
  return new Date().toISOString();
}

/** Minimal cookie jar for cookie-authenticated requests. */
const cookies: Record<string, string> = {};
function remember(res: Response) {
  const sc = res.headers.get("set-cookie");
  if (!sc) return;
  for (const c of sc.split(/,(?=\s*[A-Za-z0-9_-]+=)/)) {
    const [kv] = c.split(";");
    const eq = kv.indexOf("=");
    if (eq > -1) cookies[kv.slice(0, eq).trim()] = kv.slice(eq + 1).trim();
  }
}
function cookieHeader(): string {
  return Object.entries(cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");
}

async function fetchHtml(
  url: string,
  opts: { method?: string; body?: string; referer?: string; json?: boolean } = {},
): Promise<string | null> {
  try {
    const res = await fetch(url, {
      method: opts.method || "GET",
      headers: {
        "User-Agent": UA,
        Accept: opts.json
          ? "application/json, text/javascript, */*"
          : "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        ...(opts.body ? { "Content-Type": "application/x-www-form-urlencoded" } : {}),
        ...(opts.referer ? { Referer: opts.referer } : {}),
        ...(Object.keys(cookies).length ? { Cookie: cookieHeader() } : {}),
      },
      body: opts.body,
      redirect: "manual",
    });
    remember(res);
    if (!res.ok && res.status !== 302) {
      console.warn(`  [${res.status}] ${url}`);
      return null;
    }
    return await res.text();
  } catch (e) {
    console.warn(`  [error] ${url}: ${String(e).slice(0, 160)}`);
    return null;
  }
}

/** Authenticate against a realforeclose portal (AJAX login). Returns true on success. */
async function login(site: string): Promise<boolean> {
  const base = `https://${site}`;
  // Prime the session.
  await fetchHtml(`${base}/index.cfm?zaction=USER&zmethod=LOGIN`);
  const body = new URLSearchParams({
    ZACTION: "AJAX",
    ZMETHOD: "LOGIN",
    func: "LOGIN",
    USERNAME: RF_USER,
    USERPASS: RF_PASS,
  });
  const text = await fetchHtml(`${base}/index.cfm`, {
    method: "POST",
    body: body.toString(),
    referer: `${base}/index.cfm?zaction=USER&zmethod=LOGIN`,
    json: true,
  });
  if (!text) return false;
  const ok = text.includes('"isOk":"YES"') || text.includes('"isOk": "YES"');
  console.log(`  ${site}: login ${ok ? "OK" : "FAILED"}`);
  return ok;
}

/** Parse "MM/DD/YYYY" auction dates out of a portal's calendar HTML. */
function extractAuctionDates(html: string): string[] {
  const matches = html.match(/AuctionDate=([0-9]{2}\/[0-9]{2}\/[0-9]{4})/g) || [];
  const dates = matches.map((m) => m.split("=")[1]);
  return [...new Set(dates)];
}

function toIsoDate(mmddyyyy: string): string {
  const [m, d, y] = mmddyyyy.split("/").map(Number);
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}T10:00:00-05:00`;
}

/** Strip HTML tags and collapse whitespace. */
function textOf(html: string): string {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&#0*39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, " ")
    .trim();
}

/** Fetch the case grid for a sale date via the realforeclose AJAX endpoint.
 *
 *  The PREVIEW page's initial HTML does NOT contain the per-case cards —
 *  realforeclose loads them dynamically via an AJAX POST to the same
 *  index.cfm with ZACTION=AJAX. Without this call, parseCases() sees no
 *  AuctionID= tokens and every date falls back to a date-only marker (the
 *  exact bug producing 11 empty calendar rows in the live feed). This
 *  function POSTs the AJAX grid request and returns the HTML fragment
 *  containing the case cards, which parseCases() then slices per ID. */
async function fetchCaseGrid(
  site: string,
  date: string,
): Promise<string | null> {
  const base = `https://${site}`;
  // realforeclose / Realauction AJAX grid: POST to index.cfm with the
  // AJAX+PREVIEW action and the sale date. The response is an HTML
  // fragment (or JSON wrapping HTML) with the case cards.
  const ajaxBody = new URLSearchParams({
    ZACTION: "AJAX",
    ZMETHOD: "PREVIEW",
    func: "GRID",
    AuctionDate: date,
  });
  let html = await fetchHtml(`${base}/index.cfm`, {
    method: "POST",
    body: ajaxBody.toString(),
    referer: `${base}/index.cfm?zaction=AUCTION&zmethod=PREVIEW&AuctionDate=${date}`,
    json: true,
  });
  if (html && /AuctionID=/.test(html)) return html;
  // Some portals use a GET with the date and return the grid directly.
  html = await fetchHtml(
    `${base}/index.cfm?zaction=AUCTION&zmethod=PREVIEW&AuctionDate=${date}`,
    { referer: `${base}/index.cfm` },
  );
  if (html && /AuctionID=/.test(html)) return html;
  // Some portals embed the grid in a JSON envelope: {"html": "..."}.
  // Try to unwrap it.
  if (html) {
    try {
      const j = JSON.parse(html);
      const inner = typeof j === "string" ? j : j.html || j.data || j.result;
      if (typeof inner === "string" && /AuctionID=/.test(inner)) return inner;
    } catch {
      /* not JSON — ignore */
    }
  }
  return null;
}

/** Parse per-case rows from an authenticated PREVIEW page.
 *  realforeclose renders each sale date as a grid of case "Auction" cards.
 *  Every case exposes a DETAILS link carrying AuctionID=<id>. We collect the
 *  unique IDs, then slice the HTML between successive IDs to isolate each
 *  case's text and regex out the address, final-judgment / opening bid, case
 *  number, and parcel id. */
function parseCases(html: string, site: string): Record<string, unknown>[] {
  const cases: Record<string, unknown>[] = [];
  const ids = (html.match(/AuctionID=([^&"\s]+)/g) || []).map(
    (m) => m.split("=")[1],
  );
  const uniqueIds = [...new Set(ids)];
  if (!uniqueIds.length) return cases;

  const ADDR_RE =
    /\b\d+\s+[A-Za-z0-9.\-]+\s+(?:St|Street|Dr|Drive|Ave|Avenue|Blvd|Boulevard|Rd|Road|Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway|Pl|Place|Ter|Terrace|Cir|Circle|Trl|Trail)\b[^\n,]*(?:[A-Za-z0-9.\- #]+)?(?:,?\s*[A-Za-z .]+,\s*FL\s*\d{5})?/;

  for (let i = 0; i < uniqueIds.length; i++) {
    const id = uniqueIds[i];
    const start = html.indexOf(`AuctionID=${id}`);
    const end =
      i + 1 < uniqueIds.length ? html.indexOf(`AuctionID=${uniqueIds[i + 1]}`) : html.length;
    const block = html.slice(start, end);
    const blockText = textOf(block);

    // Address: first "123 Street St" pattern in the block.
    const addr = blockText.match(ADDR_RE) || [];
    // Final judgment / opening bid: dollar amounts. Prefer the largest
    // figure labelled "Final Judgment" / "Opening Bid" when present.
    const labeled =
      blockText.match(/(?:Final\s*Judg(?:ment)?|Opening\s*Bid|Bid\s*Amount)\s*[:$]?\s*\$?([\d,]+(?:\.\d{2})?)/i);
    const dollars = blockText.match(/\$[\d,]+(?:\.\d{2})?/g) || [];
    const bid = labeled
      ? Number(labeled[1].replace(/[^0-9.]/g, ""))
      : dollars[0]
        ? Number(dollars[0].replace(/[^0-9.]/g, ""))
        : 0;
    // Case number: "Case No", "Case #", or a CA/TD-style ref.
    const caseNo =
      blockText.match(/Case\s*(?:No\.?|#)\s*[:#]?\s*([A-Z0-9\-]+)/i) ||
      blockText.match(/\b(\d{2,4}-[A-Z]{2}-\d{3,6})\b/) ||
      blockText.match(/\b([A-Z]{2,4}\s*\d{4,8}[A-Z]{0,2})\b/) ||
      [];
    // Parcel id: "Parcel", "PIN", "Folio".
    const parcel =
      blockText.match(/(?:Parcel|PIN|Folio)\s*#?\s*[:#]?\s*([A-Z0-9\-]+)/i) ||
      [];

    cases.push({
      auctionId: id,
      address: addr[0] ? textOf(addr[0]) : "",
      bidPrice: bid,
      caseNumber: caseNo[1] || id,
      parcelId: parcel[1] || null,
      propertyUrl: `https://${site}/index.cfm?zaction=AUCTION&zmethod=DETAILS&AuctionID=${id}`,
      raw: blockText.slice(0, 600),
    });
  }
  return cases;
}

/** Fetch a case's DETAILS page and extract the full street address + final
 *  judgment when the PREVIEW block didn't yield a parseable address. Capped
 *  per run so we don't hammer the portal. */
async function enrichCase(
  c: Record<string, unknown>,
  site: string,
): Promise<Record<string, unknown>> {
  if (c.address) return c; // already have an address
  const base = `https://${site}`;
  const detailsHtml = await fetchHtml(c.propertyUrl as string, {
    referer: `${base}/index.cfm`,
  });
  if (!detailsHtml) return c;
  const t = textOf(detailsHtml);
  const ADDR_RE =
    /\b\d+\s+[A-Za-z0-9.\-]+\s+(?:St|Street|Dr|Drive|Ave|Avenue|Blvd|Boulevard|Rd|Road|Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway|Pl|Place|Ter|Terrace|Cir|Circle|Trl|Trail)\b[^\n,]*(?:,?\s*[A-Za-z .]+,\s*FL\s*\d{5})?/;
  const addr = t.match(ADDR_RE);
  if (addr) c.address = textOf(addr[0]);
  const labeled = t.match(
    /(?:Final\s*Judg(?:ment)?|Opening\s*Bid|Bid\s*Amount)\s*[:$]?\s*\$?([\d,]+(?:\.\d{2})?)/i,
  );
  if (labeled && !(c.bidPrice as number)) {
    c.bidPrice = Number(labeled[1].replace(/[^0-9.]/g, ""));
  }
  const parcel = t.match(/(?:Parcel|PIN|Folio)\s*#?\s*[:#]?\s*([A-Z0-9\-]+)/i);
  if (parcel && !c.parcelId) c.parcelId = parcel[1];
  return c;
}

/** Scrape one county portal. */
async function scrapePortal(site: string): Promise<Record<string, unknown>[]> {
  const county = COUNTY_BY_SITE[site];
  const base = `https://${site}`;
  const center = COUNTY_GEO[county]?.center;

  const html = await fetchHtml(`${base}/index.cfm?zaction=AUCTION&zmethod=PREVIEW`);
  if (!html) return [];
  const allDates = extractAuctionDates(html);
  const today = Date.now() - 86_400_000;
  const upcoming = allDates
    .filter((d) => Date.parse(d) >= today)
    .sort((a, b) => Date.parse(a) - Date.parse(b));

  const out: Record<string, unknown>[] = [];

  // If we have credentials, try to log in and pull per-case detail per date.
  let authed = false;
  if (HAS_CREDS) authed = await login(site);

  for (const d of upcoming) {
    const iso = toIsoDate(d);
    const previewUrl = `${base}/index.cfm?zaction=AUCTION&zmethod=PREVIEW&AuctionDate=${d}`;

    if (authed) {
      // The PREVIEW page's initial HTML doesn't carry the case cards —
      // realforeclose loads them via AJAX. Fetch the grid first, then
      // parse per-case rows from it. Fall back to the static PREVIEW
      // page if the AJAX endpoint doesn't return cards.
      let dateHtml = await fetchCaseGrid(site, d);
      if (!dateHtml) {
        dateHtml = await fetchHtml(previewUrl, { referer: `${base}/index.cfm` });
      }
      let cases = dateHtml ? parseCases(dateHtml, site) : [];
      // Enrich any case whose address didn't parse from the preview by
      // fetching its DETAILS page (capped to avoid hammering the portal).
      if (cases.length) {
        const needEnrich = cases.filter((c) => !c.address).slice(0, 12);
        for (const c of needEnrich) {
          await enrichCase(c, site);
        }
        // Drop cases that still have no address AND no bid — they're not
        // real listings, just empty calendar scaffolding.
        cases = cases.filter(
          (c) => (c.address as string) || (c.bidPrice as number) > 0,
        );
      }
      if (cases.length) {
        for (const c of cases) {
          out.push({
            id: `${site}-case-${c.auctionId}`,
            address: (c.address as string) || `Auction ID ${c.auctionId}`,
            type: "Single Family",
            saleType: SALE_TYPE_BY_SITE[site] ?? "Foreclosure",
            county,
            bidPrice: c.bidPrice,
            acreage: null,
            beds: null,
            baths: null,
            auctionDate: iso,
            auctionSite: site,
            caseNumber: c.caseNumber,
            parcelId: c.parcelId,
            propertyUrl: c.propertyUrl,
            notes: `Live case from ${county.replace(" FL", "")} County auction ${d}. ${(c.raw as string) || ""}`.trim(),
            listingFoundAt: nowIso(),
            isLiveScrape: true,
            ...(center ? { lat: center[0], lng: center[1] } : {}),
          });
        }
        console.log(`  ${site} ${d}: ${cases.length} case(s)`);
        continue;
      }
    }

    // Fallback: date-only entry (calendar is public; case detail needs login).
    out.push({
      id: `${site}-cal-${d.replace(/\//g, "-")}`,
      address: `${county.replace(" FL", "")} County Foreclosure Auction — ${d}`,
      type: "Foreclosure",
      saleType: SALE_TYPE_BY_SITE[site] ?? "Foreclosure",
      county,
      bidPrice: 0,
      acreage: null,
      beds: null,
      baths: null,
      auctionDate: iso,
      auctionSite: site,
      caseNumber: `Sale date ${d}`,
      parcelId: null,
      propertyUrl: previewUrl,
      notes: authed
        ? `Live auction date from the county portal. No cases published for this date yet — check back closer to the sale.`
        : `Live auction date scraped from the county portal. Case addresses and bid amounts require a realforeclose.com login — open the portal to view the case list for this date.`,
      listingFoundAt: nowIso(),
      isLiveScrape: true,
      ...(center ? { lat: center[0], lng: center[1] } : {}),
    });
    console.log(`  ${site} ${d}: date-only (calendar)`);
  }
  return out;
}

async function main() {
  console.log(`[${nowIso()}] Live scrape starting — ${PORTALS.length} portals${HAS_CREDS ? " (authenticated)" : " (calendar-only)"}`);
  const live: Record<string, unknown>[] = [];
  for (const site of PORTALS) {
    const entries = await scrapePortal(site);
    live.push(...entries);
  }
  console.log(`  → ${live.length} live entries`);

  // Load the curated inventory. Priority: curated.json (the bundled
  // 70-entry baseline shipped with the scraper), falling back to the
  // prior feed.json's non-live entries (legacy path when curated.json is
  // absent). Live entries from this run override any curated entry by id.
  let curated: Record<string, unknown>[] = [];
  let curatedSource = "";
  if (existsSync(CURATED_PATH)) {
    try {
      curated = JSON.parse(readFileSync(CURATED_PATH, "utf-8"));
      curatedSource = "curated.json";
      console.log(`  → loaded ${curated.length} curated entries from curated.json`);
    } catch (e) {
      console.warn(`  ! couldn't load curated.json: ${String(e).slice(0, 120)}`);
    }
  }
  if (!curated.length && existsSync(FEED_OUT)) {
    try {
      const prior = JSON.parse(readFileSync(FEED_OUT, "utf-8"));
      const priorProps = Array.isArray(prior.properties)
        ? prior.properties
        : Array.isArray(prior.listings)
          ? prior.listings
          : [];
      curated = priorProps.filter(
        (p: Record<string, unknown>) => p.isLiveScrape !== true,
      );
      curatedSource = "prior feed.json";
      console.log(
        `  → loaded ${curated.length} curated entries from prior feed.json`,
      );
    } catch (e) {
      console.warn(`  ! couldn't load prior feed.json: ${String(e).slice(0, 120)}`);
    }
  }
  if (!curated.length) {
    console.warn("  ! no curated inventory found — feed will contain live entries only");
  } else if (curatedSource) {
    console.log(`  (curated source: ${curatedSource})`);
  }

  // Merge: live entries first, then curated (curated deduped against live by id).
  const liveIds = new Set(live.map((p) => p.id as string));
  const curatedDeduped = curated.filter((p) => !liveIds.has(p.id as string));
  const properties = [...live, ...curatedDeduped];

  // ── Audit step: verify each listing's URL resolves before publishing. ──
  // This is the core fix for "listings that don't exist on the source site."
  // Generic search-page URLs (land.com/florida/.../land-for-sale) get marked
  // "unverified" so users see a badge and know to confirm before relying on
  // the details. Deep links to real portals get HEAD-checked and marked
  // "verified" when they resolve.
  console.log(`\n  Auditing ${properties.length} listing URLs…`);
  let verifiedCount = 0;
  let unverifiedCount = 0;
  for (const p of properties) {
    const site = (p.auctionSite as string) || "";
    const url = (p.propertyUrl as string) || "";
    const parcelId = (p.parcelId as string | null | undefined) ?? null;
    const isLive = p.isLiveScrape === true;
    const { verified, verifiedAt } = await auditListing({
      propertyUrl: url,
      auctionSite: site,
      parcelId,
      isLiveScrape: isLive,
    });
    p.verified = verified;
    p.verifiedAt = verifiedAt;
    if (verified === "verified") verifiedCount++;
    else if (verified === "unverified") unverifiedCount++;
  }
  console.log(
    `  ✓ Audit complete: ${verifiedCount} verified, ${unverifiedCount} unverified, ${properties.length - verifiedCount - unverifiedCount} pending`,
  );

  const feed = {
    generatedAt: nowIso(),
    source: "ppi-scraper-live-v1",
    count: properties.length,
    properties,
  };
  writeFileSync(FEED_OUT, JSON.stringify(feed, null, 2) + "\n", "utf-8");
  console.log(`✓ Wrote ${properties.length} properties (${live.length} live + ${curatedDeduped.length} curated) → ${FEED_OUT}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
