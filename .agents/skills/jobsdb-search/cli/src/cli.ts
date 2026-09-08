#!/usr/bin/env bun
// Self-contained CLI for searching JobsDB Hong Kong's public REST search API.
// Zero runtime dependencies — runs anywhere `bun` is available.
//
// Data source: https://hk.jobsdb.com/api/jobsearch/v5/search (Seek "HK-Main"
// market). No authentication, no browser; returns JSON. See url-reference.md.

import { runSearch, searchData, type SearchOpts } from "./commands/search.js"
import { runDetail, type DetailOpts } from "./commands/detail.js"

interface Flags {
  _: string[]
  [k: string]: string | boolean | string[]
}

const ALIAS: Record<string, string> = { q: "query", n: "limit" }

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (!a.startsWith("-")) {
      ;(flags._ as string[]).push(a)
      continue
    }
    const name = a.replace(/^-+/, "")
    const key = ALIAS[name] ?? name
    const next = argv[i + 1]
    let value: string | boolean = true
    if (next !== undefined && !next.startsWith("-")) {
      value = next
      i++
    }
    flags[key] = value
  }
  return flags
}

function stringFlag(raw: string | boolean | string[] | undefined): string | undefined {
  return typeof raw === "string" ? raw : undefined
}

function parseIntOr(raw: string | boolean | string[] | undefined, fallback: number): number {
  if (raw === undefined) return fallback
  const v = parseInt(raw as string, 10)
  return isNaN(v) ? fallback : v
}

const HELP = `jobsdb-cli — search JobsDB Hong Kong job listings (https://hk.jobsdb.com)

USAGE
  bun run src/cli.ts search [-q "<keywords>"] [--jobage <days>] [--page <n>] [--limit <n>] [--format json|table|plain]
  bun run src/cli.ts batch --delay-ms <milliseconds> < requests.jsonl
  bun run src/cli.ts detail <id|url> --teaser-only [--format json|plain]

SEARCH FLAGS
  --query, -q <text>    Keywords (title, skill, role, company). Optional.
  --jobage <days>       Posted within N days: 7 | 14 | 30. Omit for all postings.
  --page <n>            1-indexed page. Default 1.
  --limit, -n <n>       Cap results emitted (client-side). Default 30.
  --format <fmt>        json (default) | table | plain.

DETAIL
  <id|url>              A numeric JobsDB job id (e.g. 93369834) or a
                        https://hk.jobsdb.com/job/<id> URL.
  --teaser-only         Explicitly acknowledge that this is structured/listing
                        data only; it is never a full JD. Full JD uses the
                        JobsFlow gateway and the user's primary Chrome CDP.

EXAMPLES
  bun run src/cli.ts search -q "paralegal" --jobage 14 --format table
  bun run src/cli.ts search -q "legal counsel" --limit 10 --format json
  bun run src/cli.ts detail 93369834 --teaser-only --format plain

No authentication required. Personal use only — keep volume low; the endpoint is
public but Seek/Cloudflare may rate-limit bulk access.
`

interface BatchRequest {
  request_id?: unknown
  query?: unknown
  jobage?: unknown
  page?: unknown
  limit?: unknown
}

function batchNumber(raw: unknown, fallback: number): number {
  if (typeof raw === "number" && Number.isFinite(raw)) return raw
  if (typeof raw === "string") {
    const parsed = Number(raw)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

function batchSleep(ms: number): Promise<void> {
  return ms > 0 ? new Promise((resolve) => setTimeout(resolve, ms)) : Promise.resolve()
}

/** Keep one JobsDB process alive; requests for this portal are serial. */
async function runBatch(delayMs: number): Promise<number> {
  const input = await Bun.stdin.text()
  const lines = input.split(/\r?\n/).filter((line) => line.trim())
  for (let index = 0; index < lines.length; index++) {
    if (index > 0) await batchSleep(delayMs)
    let requestId = `line-${index + 1}`
    try {
      const request = JSON.parse(lines[index]) as BatchRequest
      if (!request || typeof request !== "object") throw new Error("request must be a JSON object")
      if (request.request_id !== undefined) requestId = String(request.request_id)
      const payload = await searchData({
        query: typeof request.query === "string" ? request.query : undefined,
        jobage: batchNumber(request.jobage, 9999),
        page: Math.max(1, Math.trunc(batchNumber(request.page, 1))),
        limit: Math.max(1, Math.trunc(batchNumber(request.limit, 30))),
        format: "json",
      })
      process.stdout.write(JSON.stringify({ request_id: requestId, ok: true, payload }) + "\n")
    } catch (e) {
      const error = e instanceof Error ? e.message : String(e)
      process.stdout.write(JSON.stringify({ request_id: requestId, ok: false, error }) + "\n")
    }
  }
  return 0
}

async function main(): Promise<number> {
  const argv = process.argv.slice(2)
  const flags = parseFlags(argv)
  const cmd = (flags._ as string[])[0]

  if (!cmd || flags.help || flags.h) {
    process.stdout.write(HELP)
    return cmd ? 0 : 1
  }

  if (cmd === "search") {
    const fmt = (flags.format as string) || "json"
    const opts: SearchOpts = {
      query: stringFlag(flags.query),
      jobage: parseIntOr(flags.jobage, 9999),
      page: Math.max(1, parseIntOr(flags.page, 1)),
      limit: flags.limit !== undefined ? Math.max(1, parseIntOr(flags.limit, 30)) : 30,
      format: (["json", "table", "plain"].includes(fmt) ? fmt : "json") as SearchOpts["format"],
    }
    return runSearch(opts)
  }

  if (cmd === "batch") {
    const delayMs = Math.max(0, Math.trunc(parseIntOr(flags["delay-ms"], 0)))
    return runBatch(delayMs)
  }

  if (cmd === "detail") {
    const id = (flags._ as string[])[1]
    if (!id) {
      process.stderr.write(JSON.stringify({ error: "detail requires a <id|url>", code: "NO_ID" }) + "\n")
      return 1
    }
    const fmt = (flags.format as string) || "json"
    const opts: DetailOpts = {
      id,
      format: fmt === "plain" ? "plain" : "json",
      teaserOnly: flags["teaser-only"] === true,
    }
    return runDetail(opts)
  }

  process.stderr.write(JSON.stringify({ error: `Unknown command "${cmd}"`, code: "BAD_CMD" }) + "\n")
  return 1
}

main().then((code) => process.exit(code))
