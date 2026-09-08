import {
  SEARCH_BASE,
  SITE_KEY,
  searchGet,
  toResult,
  jobageToDateRange,
  type JobResult,
  type SearchEnvelope,
} from "../helpers.js"

export interface SearchOpts {
  query?: string
  jobage: number // 0/9999 = all; else 7/14/30
  page: number
  limit?: number
  format: "json" | "table" | "plain"
}

export interface SearchPayload {
  meta: { count: number; page: number; total: number | null }
  results: JobResult[]
}

function buildParams(opts: SearchOpts): Record<string, string> {
  const params: Record<string, string> = {
    siteKey: SITE_KEY,
    page: String(opts.page),
    pageSize: String(opts.limit && opts.limit > 0 ? Math.min(opts.limit, 100) : 30),
  }
  if (opts.query) params.keywords = opts.query
  const dr = jobageToDateRange(opts.jobage)
  if (dr) {
    params.daterange = dr
    params.sortmode = "ListedDate"
  }
  return params
}

function renderTable(cards: JobResult[]): string {
  if (cards.length === 0) return "No results."
  const rows = cards.map((c) => {
    const title = (c.title || "").slice(0, 40).padEnd(40)
    const company = (c.company || "—").slice(0, 26).padEnd(26)
    const loc = (c.location || "—").slice(0, 22).padEnd(22)
    const date = (c.date || "—").slice(0, 10)
    return `${c.id.padEnd(10)} ${title} ${company} ${loc} ${date}`
  })
  const header =
    "ID".padEnd(10) + " " + "TITLE".padEnd(40) + " " + "COMPANY".padEnd(26) + " " + "LOCATION".padEnd(22) + " DATE"
  return [header, "-".repeat(header.length), ...rows].join("\n")
}

/** Fetch and normalize one search without writing to stdout. */
export async function searchData(opts: SearchOpts): Promise<SearchPayload> {
  const params = buildParams(opts)
  // The optional cookie bridge is permitted only for the listing API.  Keep
  // this explicit so future callers cannot inherit browser credentials by
  // accident; detail.ts deliberately passes `false`.
  const env: SearchEnvelope = await searchGet(params, { includeSearchCookie: true })
  let cards = (env.data || []).map(toResult)
  if (opts.limit !== undefined && opts.limit >= 0) cards = cards.slice(0, opts.limit)

  return {
    meta: { count: cards.length, page: opts.page, total: env.totalCount ?? null },
    results: cards,
  }
}

export async function runSearch(opts: SearchOpts): Promise<number> {
  try {
    const payload = await searchData(opts)

    if (opts.format === "table") {
      process.stdout.write(renderTable(payload.results) + "\n")
    } else if (opts.format === "plain") {
      process.stdout.write(
        payload.results
          .map(
            (c) =>
              `${c.title}\n  ${c.company || "—"} · ${c.location || "—"} · ${c.date || "—"}\n  id: ${c.id}\n  ${c.url}`,
          )
          .join("\n\n") + "\n",
      )
    } else {
      process.stdout.write(
        JSON.stringify(payload, null, 2) + "\n",
      )
    }
    return 0
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    process.stderr.write(JSON.stringify({ error: msg, code: "SEARCH_FAILED" }) + "\n")
    return 1
  }
}
