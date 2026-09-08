---
name: jobsdb-search
version: 1.1.0
description: >
  Search or inspect JobsDB Hong Kong listings for any profession. Use for JobsDB,
  HK job search, find jobs in Hong Kong, search a role or company, look up a job
  ID or URL, recent vacancies, salary/work-type checks, 搵工, 求職, 香港招聘, or
  when another workflow needs structured JobsDB results. Supports engineering,
  healthcare, finance, operations, marketing, legal and all other keyword queries.
context: fork
allowed-tools: Bash(bun run .agents/skills/jobsdb-search/cli/src/cli.ts *)
---

# JobsDB Hong Kong search

Use JobsDB's structured search results for a user-supplied or private
setup-generated query. Never substitute a built-in profession.

## Step 1: Detect the available path

```bash
bun run .agents/skills/jobsdb-search/cli/src/cli.ts --help
```

Decision tree:

1. If help succeeds, use the CLI.
2. If Bun or the CLI is missing, use JobsFlow's normal `/scan` orchestration if
   available.
3. If neither path is available, give the user a direct JobsDB search URL and
   clear setup instructions; do not fabricate results.

Gate: continue only after identifying one usable path.

## Defaults

| Parameter | Default | Reason |
|-----------|---------|--------|
| Query | private setup query; otherwise user's exact role text | no profession bias |
| Recency | 7 days | useful balance of freshness and coverage |
| Page | 1 | bounded request volume |
| Limit | 20 | enough to compare without bulk collection |
| Format | JSON | deterministic downstream processing |
| Location | Hong Kong market | this integration targets hk.jobsdb.com |

## Step 2: Search

```bash
bun run .agents/skills/jobsdb-search/cli/src/cli.ts search \
  --query "<keywords>" --jobage 7 --limit 20 --format json
```

Use the user's query verbatim unless the private setup supplies scoped terms.
For broader discovery, issue separate core/adjacent/exploration queries rather
than joining unrelated professions.

Gate: JSON must contain the CLI contract envelope. A successful empty result is
different from a failed request.

## Step 3: Inspect promising results (teaser only)

```bash
bun run .agents/skills/jobsdb-search/cli/src/cli.ts detail <id-or-url> \
  --teaser-only --format json
```

`detail --teaser-only` returns structured fields such as teaser, salary,
classification, work type, arrangement, location and date. It does not
guarantee the full client-rendered description. The flag is mandatory: without
it the CLI exits with `DETAIL_REQUIRES_GATEWAY`, so a model cannot accidentally
promote structured data to a full JD. Mark the actual JD depth; for materials,
request a full-JD paste if the gateway cannot retrieve it.

Gate: never label structured fields or a teaser as a full JD.

When this skill runs inside JobsFlow, `detail --teaser-only` is not the full-JD
path and the model must not start a browser itself. Full JobsDB details are
requested through `python3 -m tools.workflow scan` (or the gateway materials
flow). If Cloudflare appears, the gateway performs one bounded handoff to a
**visible user Chrome** CDP session; after the user verifies once, that same
live context is reused serially for the remaining detail pages. A headless
browser must never be used for the manual click, and the optional cookie header
is only a search-API bridge, never a credential for detail-page fetching. If
the gateway returns `requires_user_action`, follow its printed command and
rerun the same gateway action instead of improvising another entry point.
Chrome 136+ toggle mode may expose only the browser WebSocket
`/devtools/browser` and return 404 from `/json/version`; this is expected. The
gateway performs the single attach and validates `Browser.getVersion`. Do not
repeat WebSocket probes or start another browser in response to the HTTP 404.
The Python compatibility helpers (`portal_jd_browser.py` and
`portal_jd_cdp.py`) are gateway-owned implementation details; direct JobsDB
detail invocation is rejected with `jobsdb_gateway_only`. Do not set the
gateway's internal process marker yourself.

## Step 4: Handle failures safely

- The CLI retries 429/5xx with bounded exponential backoff.
- On rate limit, network error or malformed output, report the portal failure and
  preserve other portal results.
- Keep volume low and do not use the endpoint for bulk/commercial collection.
- Never invent salary, employer, date or requirements when a field is absent.

## Step 5: Respond

Return:

1. **Run status** — method, query, recency and any failure.
2. **Results** — title, company, location, date, salary when disclosed and URL.
3. **Depth** — `search_teaser`, `structured_detail` or `full_jd`.
4. **Next action** — shortlist, inspect detail, paste full JD, or broaden one
   configured bucket.

Data source: `https://hk.jobsdb.com/api/jobsearch/v5/search`. Use is
personal/low-volume and subject to the portal's current terms and rate limits.
