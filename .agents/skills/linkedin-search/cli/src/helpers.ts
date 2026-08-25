// Data source: LinkedIn public "jobs-guest" endpoints. No authentication required.
// Search returns an HTML list of job cards; detail returns a single job's HTML.
// We parse both with regex (the markup is shallow and stable; a full DOM parser
// is unnecessary and node-html-parser has known nesting bugs on LinkedIn cards).

import { requestSignal, retryDelayMs } from "../../../_shared/http-policy.ts"

export const SEARCH_URL =
  "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
export const DETAIL_URL =
  "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting"

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
}

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

function envInt(name: string, fallback: number, min = 0, max = 60_000): number {
  const parsed = Number(process.env[name])
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(parsed)))
}

function requestTimeoutMs(): number {
  return envInt(
    "JOBSFLOW_SCAN_REQUEST_TIMEOUT_MS",
    15_000,
    1_000,
    60_000,
  )
}

function requestMaxRetries(): number {
  return envInt("JOBSFLOW_SCAN_MAX_RETRIES", 6, 0, 6)
}

function retryCapMs(): number {
  return envInt("JOBSFLOW_SCAN_MAX_RETRY_DELAY_MS", 30_000, 0, 30_000)
}

/**
 * Some VPN/DNS paths (esp. utun + polluted resolvers) map www.linkedin.com to
 * unreachable Azure-CN style A records (e.g. 52.131.*). TLS then dies with
 * Bun's "unknown certificate verification error" / curl SSL_ERROR_SYSCALL
 * and zero peer cert bytes.
 *
 * Fix: resolve LinkedIn hosts via DoH (Cloudflare), skip poisoned prefixes,
 * connect to a good Cloudflare edge IP with SNI + Host still = linkedin.com.
 */
const LI_HOST_RE = /(^|\.)linkedin\.com$/i
/** A-record prefixes seen for poisoned / unusable LinkedIn answers on bad DNS. */
const POISONED_IP_PREFIXES = ["52.131.", "52.130.", "42.120.", "42.121.", "0.0.0."]
const dohIpCache = new Map<string, { ip: string; exp: number }>()

function isPoisonedIp(ip: string): boolean {
  return POISONED_IP_PREFIXES.some((p) => ip.startsWith(p))
}

async function resolveHostViaDoh(host: string): Promise<string | null> {
  const cached = dohIpCache.get(host)
  if (cached && cached.exp > Date.now()) return cached.ip

  const endpoints = [
    `https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(host)}&type=A`,
    `https://dns.google/resolve?name=${encodeURIComponent(host)}&type=A`,
  ]
  for (const ep of endpoints) {
    try {
      const r = await fetch(ep, {
        headers: { Accept: "application/dns-json" },
        signal: requestSignal(requestTimeoutMs()),
        // DoH endpoints are not LinkedIn — use normal system DNS
      })
      if (!r.ok) continue
      const j = (await r.json()) as {
        Answer?: Array<{ type: number; data: string }>
      }
      const ips = (j.Answer || [])
        .filter((a) => a.type === 1 && a.data && !isPoisonedIp(a.data))
        .map((a) => a.data)
      if (ips.length) {
        const ip = ips[0]
        dohIpCache.set(host, { ip, exp: Date.now() + 5 * 60_000 })
        return ip
      }
    } catch {
      // try next DoH endpoint
    }
  }
  return null
}

type FetchInit = RequestInit & { tls?: { serverName?: string } }

/**
 * Build fetch URL + init. For LinkedIn hosts, prefer DoH IP + SNI so we do not
 * depend on the OS/VPN resolver.
 */
async function fetchInitFor(url: string): Promise<{ target: string; init: FetchInit }> {
  const u = new URL(url)
  const host = u.hostname
  const headers: Record<string, string> = {
    "User-Agent": UA,
    Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
  }
  const init: FetchInit = { headers, redirect: "follow" }

  if (LI_HOST_RE.test(host)) {
    const ip = await resolveHostViaDoh(host)
    if (ip) {
      u.hostname = ip
      headers.Host = host
      init.tls = { serverName: host }
      return { target: u.toString(), init }
    }
  }
  return { target: url, init }
}

/** Fetch HTML with exponential backoff on 429/5xx. Returns "" on a 404. */
export async function htmlFetch(url: string): Promise<string> {
  const maxRetries = requestMaxRetries()
  let delay = 500
  let lastErr: unknown
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const { target, init } = await fetchInitFor(url)
      init.signal = requestSignal(requestTimeoutMs())
      const response = await fetch(target, init)
      if (response.status === 429 || response.status >= 500) {
        if (attempt === maxRetries) {
          throw new Error(`Request failed: ${response.status} ${response.statusText}`)
        }
        const jitter = Math.floor(Math.random() * 500)
        await new Promise((r) =>
          setTimeout(r, Math.min(retryCapMs(), retryDelayMs(response, delay + jitter))),
        )
        delay = Math.min(delay * 2, 8000)
        continue
      }
      if (response.status === 404) return ""
      if (!response.ok) {
        throw new Error(`Request failed: ${response.status} ${response.statusText}`)
      }
      return response.text()
    } catch (e) {
      lastErr = e
      const msg = e instanceof Error ? e.message : String(e)
      // Cert / connect failures: drop DoH cache once and retry (IP may have gone stale)
      if (
        /certificate|CERT|SSL|TLS|ECONN|timed out|Connection/i.test(msg) &&
        attempt < maxRetries
      ) {
        try {
          const host = new URL(url).hostname
          dohIpCache.delete(host)
        } catch {
          /* ignore */
        }
        const jitter = Math.floor(Math.random() * 500)
        await new Promise((r) => setTimeout(r, Math.min(retryCapMs(), delay + jitter)))
        delay = Math.min(delay * 2, 8000)
        continue
      }
      throw e
    }
  }
  throw lastErr instanceof Error
    ? lastErr
    : new Error("Request failed after max retries")
}

export interface JobCard {
  id: string
  title: string
  company: string | null
  companyUrl: string | null
  location: string | null
  date: string | null
  url: string
}

export interface JobDetail extends JobCard {
  description: string | null
  seniority: string | null
  employmentType: string | null
  jobFunction: string | null
  industries: string | null
  applyUrl: string | null
}

/**
 * Convert a Unicode code point to a string. Uses `fromCodePoint` (not
 * `fromCharCode`) so supplementary-plane code points (e.g. emoji, U+1F600)
 * decode correctly, and drops out-of-range values instead of throwing.
 */
function numericEntity(cp: number): string {
  return cp >= 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : ""
}

function decodeHtmlEntities(text: string): string {
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    // Numeric character references: decimal (&#233;) and hexadecimal (&#xE9;).
    .replace(/&#(\d+);/g, (_, dec) => numericEntity(parseInt(dec, 10)))
    .replace(/&#[xX]([0-9a-fA-F]+);/g, (_, hex) => numericEntity(parseInt(hex, 16)))
    .replace(/&nbsp;/g, " ")
}

function stripTags(html: string): string {
  return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()
}

function clean(html: string): string {
  return decodeHtmlEntities(stripTags(html))
}

/** Parse the job ID out of a LinkedIn job-view URL or URN. */
function idFromUrl(url: string): string | null {
  const m = url.match(/-(\d{6,})(?:\?|$)/) || url.match(/(\d{6,})/)
  return m ? m[1] : null
}

/**
 * Parse the search response: a flat list of <li> job cards. We split on the
 * job-posting URN and parse each chunk independently so one malformed card
 * cannot break the rest.
 */
export function parseJobCards(html: string): JobCard[] {
  const results: JobCard[] = []
  const chunks = html.split(/data-entity-urn="urn:li:jobPosting:/).slice(1)

  for (const chunk of chunks) {
    const idMatch = chunk.match(/^(\d+)/)
    if (!idMatch) continue
    const id = idMatch[1]

    // Full link + title (title lives in the sr-only span or the <h3> title).
    const linkMatch = chunk.match(/class="base-card__full-link[^"]*"[^>]*href="([^"]+)"/i)
    const url = linkMatch ? decodeHtmlEntities(linkMatch[1]).split("?")[0] : ""

    let title: string | null = null
    const h3 = chunk.match(/class="base-search-card__title"[^>]*>([\s\S]*?)<\/h3>/i)
    if (h3) title = clean(h3[1])
    if (!title) {
      const sr = chunk.match(/class="sr-only"[^>]*>([\s\S]*?)<\/span>/i)
      if (sr) title = clean(sr[1])
    }
    if (!title) continue

    // Company (subtitle <h4> with optional inner <a>).
    let company: string | null = null
    let companyUrl: string | null = null
    const sub = chunk.match(/class="base-search-card__subtitle"[^>]*>([\s\S]*?)<\/h4>/i)
    if (sub) {
      const a = sub[1].match(/href="([^"]+)"/i)
      if (a) companyUrl = decodeHtmlEntities(a[1]).split("?")[0]
      company = clean(sub[1]) || null
    }

    // Location + date.
    const loc = chunk.match(/class="job-search-card__location"[^>]*>([\s\S]*?)<\/span>/i)
    const location = loc ? clean(loc[1]) || null : null
    const dt = chunk.match(/class="job-search-card__listdate[^"]*"[^>]*datetime="([^"]+)"/i)
    const date = dt ? dt[1] : null

    results.push({
      id,
      title,
      company,
      companyUrl,
      location,
      date,
      url: url || `https://www.linkedin.com/jobs/view/${id}`,
    })
  }

  return results
}

/** Parse the single-job detail page. */
export function parseJobDetail(html: string, id: string): JobDetail {
  const title = html.match(
    /class="(?:top-card-layout__title|topcard__title)[^"]*"[^>]*>([\s\S]*?)<\/h[12]>/i,
  )?.[1]
  const orgMatch = html.match(
    /class="topcard__org-name-link[^"]*"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/i,
  )
  const company = orgMatch ? clean(orgMatch[2]) || null : null
  const companyUrl = orgMatch ? decodeHtmlEntities(orgMatch[1]).split("?")[0] : null

  const locMatch = html.match(
    /class="topcard__flavor topcard__flavor--bullet"[^>]*>([\s\S]*?)<\/span>/i,
  )
  const location = locMatch ? clean(locMatch[1]) || null : null

  // Rich description block. Keep paragraph/line breaks as newlines.
  let description: string | null = null
  const desc = html.match(
    /class="(?:show-more-less-html__markup|description__text[^"]*)"[^>]*>([\s\S]*?)<\/div>/i,
  )
  if (desc) {
    const withBreaks = desc[1]
      .replace(/<\s*br\s*\/?>/gi, "\n")
      .replace(/<\/(p|li|ul|ol|div|h\d)>/gi, "\n")
    description = decodeHtmlEntities(stripTags(withBreaks)).replace(/\n{3,}/g, "\n\n").trim() || null
  }

  // Job-criteria items: subheader label -> text value.
  const criteria: Record<string, string> = {}
  const itemRe =
    /class="description__job-criteria-subheader"[^>]*>([\s\S]*?)<\/h3>[\s\S]*?class="description__job-criteria-text[^"]*"[^>]*>([\s\S]*?)<\/span>/gi
  let cm: RegExpExecArray | null
  while ((cm = itemRe.exec(html)) !== null) {
    criteria[clean(cm[1]).toLowerCase()] = clean(cm[2])
  }

  const applyMatch = html.match(/class="topcard__link[^"]*"[^>]*href="([^"]+)"/i)
  const applyUrl = applyMatch ? decodeHtmlEntities(applyMatch[1]).split("?")[0] : null

  return {
    id,
    title: title ? clean(title) : "(untitled)",
    company,
    companyUrl,
    location,
    date: null,
    url: `https://www.linkedin.com/jobs/view/${id}`,
    description,
    seniority: criteria["seniority level"] ?? null,
    employmentType: criteria["employment type"] ?? null,
    jobFunction: criteria["job function"] ?? null,
    industries: criteria["industries"] ?? null,
    applyUrl,
  }
}

/** Convert a job-age in days to LinkedIn's f_TPR seconds value. */
export function jobageToTPR(days: number): string | null {
  if (!days || days <= 0 || days >= 9999) return null
  return `r${days * 86400}`
}

/** Workplace-type flag: on-site=1, remote=2, hybrid=3. */
export function workTypeFlag(mode: string | undefined): string | null {
  switch ((mode || "").toLowerCase()) {
    case "remote":
      return "2"
    case "hybrid":
      return "3"
    case "onsite":
    case "on-site":
      return "1"
    default:
      return null
  }
}
