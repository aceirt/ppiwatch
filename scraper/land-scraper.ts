// land-scraper.ts
// Scrapes land.com and landwatch.com for FL Panhandle county listings.
// Requires ZENROWS_API_KEY (set as GitHub repo secret).
// Called by scrape-live.ts via scrapeAllLand().

import * as cheerio from 'cheerio';

const ZENROWS_KEY = process.env.ZENROWS_API_KEY;

// 10 FL Panhandle counties — search slugs + approximate center coords for map pins
const PANHANDLE_COUNTIES = [
  { name: 'Bay',         landSlug: 'bay-county',          lwSlug: 'bay-county',           lat: 30.2969, lng: -85.6669 },
  { name: 'Gulf',        landSlug: 'gulf-county',          lwSlug: 'gulf-county',          lat: 29.9249, lng: -85.1749 },
  { name: 'Walton',      landSlug: 'walton-county',        lwSlug: 'walton-county',        lat: 30.5903, lng: -86.1766 },
  { name: 'Okaloosa',    landSlug: 'okaloosa-county',      lwSlug: 'okaloosa-county',      lat: 30.7196, lng: -86.5249 },
  { name: 'Escambia',    landSlug: 'escambia-county',      lwSlug: 'escambia-county',      lat: 30.6390, lng: -87.3414 },
  { name: 'Santa Rosa',  landSlug: 'santa-rosa-county',    lwSlug: 'santa-rosa-county',    lat: 30.6901, lng: -86.9533 },
  { name: 'Holmes',      landSlug: 'holmes-county',        lwSlug: 'holmes-county',        lat: 30.8682, lng: -85.8131 },
  { name: 'Washington',  landSlug: 'washington-county',    lwSlug: 'washington-county',    lat: 30.6082, lng: -85.6586 },
  { name: 'Jackson',     landSlug: 'jackson-county',       lwSlug: 'jackson-county',       lat: 30.7818, lng: -85.2111 },
  { name: 'Calhoun',     landSlug: 'calhoun-county',       lwSlug: 'calhoun-county',       lat: 30.4063, lng: -85.1936 },
] as const;

export interface LandListing {
  id: string;
  county: string;
  address: string;
  price: number;
  acreage: number | null;
  sourceUrl: string;
  saleType: string;
  bidPrice: number;
  isLiveScrape: boolean;
  lat: number;
  lng: number;
  source: 'land.com' | 'landwatch.com';
}

// Fetch HTML through ZenRows with JS rendering (bypasses anti-bot 403)
async function zenFetch(url: string): Promise<string | null> {
  if (!ZENROWS_KEY) return null;
  const apiUrl =
    `https://api.zenrows.com/v1/` +
    `?apikey=${ZENROWS_KEY}` +
    `&url=${encodeURIComponent(url)}` +
    `&js_render=true` +
    `&wait=2500`;
  try {
    const res = await fetch(apiUrl, { signal: AbortSignal.timeout(35_000) });
    if (!res.ok) {
      console.warn(`[land-scraper] ZenRows ${res.status} for ${url}`);
      return null;
    }
    return await res.text();
  } catch (err) {
    console.warn(`[land-scraper] Timeout/error fetching ${url}:`, err);
    return null;
  }
}

// '$1,250,000' → 1250000  |  '$450K' → 450000
function parsePrice(raw: string): number {
  const cleaned = raw.replace(/,/g, '');
  const m = cleaned.match(/\$(\d+(?:\.\d+)?)[Kk]?/);
  if (!m) return 0;
  const val = parseFloat(m[1]);
  return /[Kk]/.test(raw) ? Math.round(val * 1000) : Math.round(val);
}

// '12.5 acres' → 12.5  |  '200 ac.' → 200
function parseAcreage(raw: string): number | null {
  const m = raw.match(/([\d,]+(?:\.\d+)?)\s*(?:acres?|ac\.?)/i);
  return m ? parseFloat(m[1].replace(/,/g, '')) : null;
}

// Build a short, stable dedup key from a URL's last two path segments
function urlKey(url: string): string {
  return url.split('/').filter(Boolean).slice(-2).join('/');
}

// Build a short ID from URL (safe for JSON keys)
function urlId(prefix: string, url: string): string {
  const encoded = Buffer.from(url).toString('base64').replace(/[+/=]/g, '').slice(0, 10);
  return `${prefix}-${encoded}`;
}

// ─── land.com ────────────────────────────────────────────────────────────────

async function scrapeLandCom(
  county: (typeof PANHANDLE_COUNTIES)[number]
): Promise<LandListing[]> {
  const url = `https://www.land.com/land/Florida/${county.landSlug}/`;
  console.log(`[land-scraper] Fetching land.com ${county.name} …`);
  const html = await zenFetch(url);
  if (!html) return [];

  const $ = cheerio.load(html);
  const results: LandListing[] = [];

  // Cards link to /property/NNN-slug/
  $('a[href*="/property/"]').each((_, el) => {
    const href = $(el).attr('href') ?? '';
    const fullUrl = href.startsWith('http') ? href : `https://www.land.com${href}`;
    const text = $(el).text();

    const price = parsePrice(text);
    const acreage = parseAcreage(text);
    if (price === 0 && acreage === null) return; // Skip nav links / ads

    const lines = text.split(/\n/).map(s => s.trim()).filter(Boolean);
    const addressLine =
      lines.find(l => /FL|Florida|,/.test(l) && l.length > 8) ??
      `${county.name} County, FL`;

    results.push({
      id:          urlId(`land-${county.name.toLowerCase().replace(/ /g, '-')}`, fullUrl),
      county:      county.name,
      address:     addressLine,
      price,
      acreage,
      sourceUrl:   fullUrl,
      saleType:    'Land Sale (Non-Auction)',
      bidPrice:    price,
      isLiveScrape: true,
      lat:         county.lat,
      lng:         county.lng,
      source:      'land.com',
    });
  });

  console.log(`[land-scraper]   land.com ${county.name}: ${results.length} listings`);
  return results;
}

// ─── landwatch.com ───────────────────────────────────────────────────────────

async function scrapeLandWatch(
  county: (typeof PANHANDLE_COUNTIES)[number]
): Promise<LandListing[]> {
  const url = `https://www.landwatch.com/florida-land-for-sale/${county.lwSlug}/`;
  console.log(`[land-scraper] Fetching landwatch.com ${county.name} …`);
  const html = await zenFetch(url);
  if (!html) return [];

  const $ = cheerio.load(html);
  const results: LandListing[] = [];

  // Cards link to /listings/NNN-slug/
  $('a[href*="/listings/"]').each((_, el) => {
    const href = $(el).attr('href') ?? '';
    const fullUrl = href.startsWith('http') ? href : `https://www.landwatch.com${href}`;
    const text = $(el).text();

    const price = parsePrice(text);
    const acreage = parseAcreage(text);
    if (price === 0 && acreage === null) return;

    const lines = text.split(/\n/).map(s => s.trim()).filter(Boolean);
    const addressLine =
      lines.find(l => /FL|Florida|,/.test(l) && l.length > 8) ??
      `${county.name} County, FL`;

    results.push({
      id:          urlId(`lw-${county.name.toLowerCase().replace(/ /g, '-')}`, fullUrl),
      county:      county.name,
      address:     addressLine,
      price,
      acreage,
      sourceUrl:   fullUrl,
      saleType:    'Land Sale (Non-Auction)',
      bidPrice:    price,
      isLiveScrape: true,
      lat:         county.lat,
      lng:         county.lng,
      source:      'landwatch.com',
    });
  });

  console.log(`[land-scraper]   landwatch.com ${county.name}: ${results.length} listings`);
  return results;
}

// ─── Main export ─────────────────────────────────────────────────────────────

export async function scrapeAllLand(): Promise<LandListing[]> {
  if (!ZENROWS_KEY) {
    console.log('[land-scraper] ZENROWS_API_KEY not set — land scrape skipped');
    return [];
  }

  const all: LandListing[] = [];
  const seen = new Set<string>();

  for (const county of PANHANDLE_COUNTIES) {
    // Stagger requests ~500 ms apart to avoid rate-limiting
    await new Promise(r => setTimeout(r, 500));

    const [fromLand, fromLW] = await Promise.all([
      scrapeLandCom(county),
      scrapeLandWatch(county),
    ]);

    for (const listing of [...fromLand, ...fromLW]) {
      const key = urlKey(listing.sourceUrl);
      if (!seen.has(key)) {
        seen.add(key);
        all.push(listing);
      }
    }
  }

  console.log(`[land-scraper] Done — ${all.length} unique land listings`);
  return all;
}
