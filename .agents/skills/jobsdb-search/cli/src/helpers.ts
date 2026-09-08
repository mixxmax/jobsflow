// Data source: JobsDB Hong Kong REST search API (Seek group "HK-Main" market).
// Endpoint: https://hk.jobsdb.com/api/jobsearch/v5/search
// No authentication required — verified live. Returns a flat JSON envelope
// ({ data: [...], totalCount, ... }). We reshape it into the portal-skill
// contract's result fields. See url-reference.md for the full schema.

import { existsSync, readFileSync } from "node:fs"
import { requestSignal, retryDelayMs } from "../../../_shared/http-policy.ts"

export const SEARCH_BASE = "https://hk.jobsdb.com/api/jobsearch/v5/search"
export const SITE_KEY = "HK-Main"
export const JOB_URL_PREFIX = "https://hk.jobsdb.com/job/"

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
}

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

function envInt(name: string, fallback: number, min = 0, max = 60_000): number {
  const parsed = Number(process.env[name])
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(parsed)))
}

function requestTimeoutMs(): number {
  return envInt("JOBSFLOW_SCAN_REQUEST_TIMEOUT_MS", 15_000, 1_000, 60_000)
}

function requestMaxRetries(): number {
  return envInt("JOBSFLOW_SCAN_MAX_RETRIES", 6, 0, 6)
}

function retryCapMs(): number {
  return envInt("JOBSFLOW_SCAN_MAX_RETRY_DELAY_MS", 30_000, 0, 30_000)
}

/**
 * Read the optional listing-API cookie bridge.
 *
 * This function is intentionally kept separate from the detail command.  A
 * cookie copied out of a browser is never a substitute for the browser-bound
 * Cloudflare session used by JobsFlow's full-JD path.  The only consumer is
 * `searchGet` when its caller explicitly permits the listing compatibility
 * bridge.
 */
function jobsdbCookieHeader(): string {
  const fromEnv = (process.env.JOBSDB_COOKIE || "").trim()
  if (fromEnv) return fromEnv
  const root = (process.env.JOBSEARCH_ROOT || "").trim()
  const defaultPrivateFile = process.env.HOME
    ? `${process.env.HOME}/.config/jobsearch/jobsdb_browser_cookies.txt`
    : ""
  const legacyPrivateFile = root
    ? `${root}/02_Tracker/portal_state/jobsdb_browser_cookies.txt`
    : ""
  const fromFile = (
    process.env.JOBSDB_COOKIE_FILE ||
    (defaultPrivateFile && existsSync(defaultPrivateFile)
      ? defaultPrivateFile
      : // Compatibility read for a pre-2026.08 runtime. New writes always use
        // the home-directory secret path above; this fallback can be removed
        // after existing private instances complete one successful recovery.
        legacyPrivateFile)
  ).trim()
  if (!fromFile) return ""
  try {
    return readFileSync(fromFile, "utf8").trim()
  } catch {
    return ""
  }
}

/**
 * GET the JobsDB search JSON with exponential backoff on 429/5xx.
 * Returns the parsed envelope. Throws on connection failure or non-retryable error.
 */
export interface SearchGetOptions {
  /**
   * Whether the legacy cookie header may be sent to the search API.  This is
   * deliberately opt-in: a new caller (or a new model) must not accidentally
   * turn a browser cookie bridge into an authentication mechanism.  Detail
   * always passes `false`; only the listing command passes `true` explicitly.
   */
  includeSearchCookie?: boolean
}

export async function searchGet(
  params: Record<string, string>,
  options: SearchGetOptions = {},
): Promise<SearchEnvelope> {
  const url = `${SEARCH_BASE}?${new URLSearchParams(params).toString()}`
  const maxRetries = requestMaxRetries()
  let delay = 500
  // Fail closed.  Cookie forwarding is a narrowly-scoped compatibility
  // bridge for the listing API, never the default and never a detail path.
  const cookie = options.includeSearchCookie === true ? jobsdbCookieHeader() : ""
  const headers: Record<string, string> = { "User-Agent": UA, Accept: "application/json" }
  if (cookie) headers.Cookie = cookie

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    let response: Response
    try {
      response = await fetch(url, {
        headers,
        redirect: "follow",
        signal: requestSignal(requestTimeoutMs()),
      })
    } catch (e) {
      throw new Error(
        `could not reach JobsDB search API (${e instanceof Error ? e.message : String(e)})`,
      )
    }

    if (response.status === 429 || response.status >= 500) {
      if (attempt === maxRetries) {
        throw new Error(`JobsDB request failed: ${response.status} ${response.statusText}`)
      }
      await sleep(
        Math.min(
          retryCapMs(),
          retryDelayMs(response, delay + Math.floor(Math.random() * 500)),
        ),
      )
      delay = Math.min(delay * 2, 8000)
      continue
    }
    if (response.status === 404) {
      throw new Error("JobsDB search endpoint returned 404 (endpoint may have changed)")
    }
    if (!response.ok) {
      throw new Error(`JobsDB request failed: ${response.status} ${response.statusText}`)
    }
    return (await response.json()) as SearchEnvelope
  }
  throw new Error("JobsDB request failed after retries")
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

// --- Wire shapes (only the fields this skill reads) ---

export interface JobRaw {
  id: string
  title?: string
  companyName?: string | null
  teaser?: string | null
  bulletPoints?: string[] | null
  listingDate?: string | null
  listingDateDisplay?: string | null
  salaryLabel?: string | null
  locations?: { label?: string; countryCode?: string; seoHierarchy?: unknown[] }[] | null
  classifications?: {
    classification?: { id?: string; description?: string }
    subclassification?: { id?: string; description?: string }
  }[] | null
  workTypes?: string[] | null
  workArrangements?:
    | { id?: string; label?: { text?: string } }[]
    | { data?: { id?: string; label?: { text?: string } }[] }
    | null
  roleId?: string | null
  advertiser?: { id?: string; description?: string } | null
  employer?: { id?: string; name?: string; companyUrl?: string } | null
  displayType?: string | null
}

export interface SearchEnvelope {
  data: JobRaw[]
  totalCount?: number
  info?: Record<string, unknown>
  sortModes?: { isActive: boolean; name: string; value: string }[]
  solMetadata?: Record<string, unknown>
  facets?: Record<string, unknown>
  searchParams?: Record<string, string>
}

/** Reshape a raw job into the portal-skill contract search-result shape. */
export interface JobResult {
  id: string
  title: string
  company: string | null
  location: string | null
  date: string | null
  url: string
  // Permitted superset fields (extra context for fit scoring):
  teaser: string | null
  salary: string | null
  classification: string | null
  workTypes: string[]
  workArrangements: string[]
}

export function toResult(j: JobRaw): JobResult {
  const location = j.locations && j.locations.length ? j.locations[0].label ?? null : null
  const classification =
    j.classifications && j.classifications.length
      ? j.classifications[0].classification?.description ?? null
      : null
  const waRaw = j.workArrangements
  const waArr = Array.isArray(waRaw)
    ? waRaw
    : waRaw && "data" in waRaw && Array.isArray(waRaw.data)
      ? waRaw.data
      : []
  const workArrangements = waArr.map((w) => w.label?.text).filter((s): s is string => !!s)
  return {
    id: j.id,
    title: j.title || "(untitled)",
    company: j.companyName || null,
    location,
    date: j.listingDate ?? null,
    url: `${JOB_URL_PREFIX}${j.id}`,
    teaser: j.teaser || null,
    salary: j.salaryLabel && j.salaryLabel.trim() ? j.salaryLabel.trim() : null,
    classification,
    workTypes: j.workTypes ?? [],
    workArrangements,
  }
}

/** Map a job-age in days to JobsDB's daterange param value. */
export function jobageToDateRange(days: number): string | null {
  if (!days || days <= 0 || days >= 9999) return null
  if (days <= 7) return "7"
  if (days <= 14) return "14"
  if (days <= 30) return "30"
  return null
}

/** Extract a numeric job id from an id or a /job/<id> URL. */
export function normalizeId(input: string): string | null {
  const trimmed = input.trim()
  // Bare id (digits).
  if (/^\d+$/.test(trimmed)) return trimmed
  // A /job/<id> URL or /job/<id>/<slug> URL.
  const m = trimmed.match(/\/job\/(\d+)/)
  if (m) return m[1]
  return null
}
