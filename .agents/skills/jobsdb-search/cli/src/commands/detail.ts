import {
  searchGet,
  toResult,
  normalizeId,
  SITE_KEY,
  writeError,
  type JobResult,
} from "../helpers.js"

export interface DetailOpts {
  id: string // a numeric job id or a /job/<id> URL
  format: "json" | "plain"
  /** Explicit acknowledgement that this is teaser/structured lookup only. */
  teaserOnly: boolean
}

/**
 * JobsDB has no clean single-job REST endpoint in the v5 search API, and the
 * /job/<id> page is a client-rendered SPA (plain GET returns the app shell).
 * So `detail` re-queries the search API filtered by the job id and picks the
 * matching entry. This yields the rich fields already in the search response
 * (teaser, bullets, salary, classification, work types, arrangement, location,
 * date) without a headless browser. See url-reference.md.
 */
export async function runDetail(opts: DetailOpts): Promise<number> {
  const id = normalizeId(opts.id)
  if (!id) {
    writeError(`could not parse a JobsDB job id from "${opts.id}"`, "BAD_ID")
    return 1
  }
  // This command can only query the public listing API.  Calling it "detail"
  // is a historical compatibility surface and repeatedly caused models to
  // treat a teaser as a full JD.  Require an explicit acknowledgement and
  // point full-JD callers at the one product gateway route.
  if (!opts.teaserOnly) {
    writeError(
      "JobsDB full-JD retrieval is gateway-only; rerun with --teaser-only for structured listing fields, or use python3 -m tools.workflow scan",
      "DETAIL_REQUIRES_GATEWAY",
    )
    return 2
  }
  try {
    // The search API supports a `jobid` filter that returns exactly the one
    // matching job (totalCount: 1). The plain `/job/<id>` page is a client-
    // rendered SPA, so we use this instead of scraping.
    // Structured detail is still only an API lookup.  Never carry the
    // optional listing cookie bridge into this command: full-JD acquisition is
    // owned by the JobsFlow gateway and must use the live primary-Chrome CDP
    // context, not a copied cookie or a second browser.
    const env = await searchGet(
      { siteKey: SITE_KEY, jobid: id, page: "1", pageSize: "5" },
      { includeSearchCookie: false },
    )
    const match = (env.data || []).map(toResult).find((j: JobResult) => j.id === id)
    if (!match) {
      writeError("job not found", "NOT_FOUND")
      return 1
    }

    if (opts.format === "plain") {
      const lines = [
        match.title,
        `${match.company ?? "—"} · ${match.location ?? "—"}`,
        match.date ? `Posted: ${match.date.slice(0, 10)}` : "",
        match.salary ? `Salary: ${match.salary}` : "",
        match.classification ? `Classification: ${match.classification}` : "",
        match.workTypes.length ? `Work type: ${match.workTypes.join(", ")}` : "",
        match.workArrangements.length ? `Arrangement: ${match.workArrangements.join(", ")}` : "",
        match.teaser ? `\n${match.teaser}` : "",
        "",
        `URL: ${match.url}`,
        `id: ${match.id}`,
      ].filter((l) => l !== "")
      process.stdout.write(lines.join("\n") + "\n")
    } else {
      process.stdout.write(JSON.stringify(match, null, 2) + "\n")
    }
    return 0
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    writeError(msg, "DETAIL_FAILED")
    return 1
  }
}
