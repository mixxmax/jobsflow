# jobsdb-search CLI

Searches JobsDB Hong Kong (`https://hk.jobsdb.com`, Seek "HK-Main" market) via its
public REST search API. No authentication, no API key, no browser — **zero runtime
dependencies** (runs with just `bun`).

## Install (dev types only)

```bash
cd .agents/skills/jobsdb-search/cli && bun install && cd ../../../..
```

`bun install` only pulls TypeScript dev types; the CLI itself needs nothing but `bun`.

## Commands

### Search

```bash
bun run .agents/skills/jobsdb-search/cli/src/cli.ts search --query "paralegal" [flags]
```

| Flag | Alias | Description |
|------|-------|-------------|
| `--query` | `-q` | Keywords (title/skill/role/company). Optional. |
| `--jobage <days>` | | Recency: `7`, `14`, or `30`. Omit for all. |
| `--page <n>` | | 1-indexed page. Default 1. |
| `--limit <n>` | `-n` | Cap results emitted. Default 30. |
| `--format` | | `json` (default) \| `table` \| `plain`. |

### Detail

```bash
bun run .agents/skills/jobsdb-search/cli/src/cli.ts detail <id|url> --teaser-only [--format json|plain]
```

`<id>` is a numeric JobsDB job id (e.g. `93369834`) or a `/job/<id>` URL.
The required `--teaser-only` flag is an explicit acknowledgement that this
command only returns structured listing fields. It never fetches the full
client-rendered description. Full-JD retrieval is owned by the fixed
`python3 -m tools.workflow scan` gateway, which uses the user's visible primary
Chrome over CDP after one manual verification; do not start a browser, pass a
profile/storage-state, or copy cookies from this CLI.
On Chrome 136+ the user's remote-debugging toggle may intentionally return 404
for `/json/version` and expose only `/devtools/browser` over WebSocket. This is
handled by the gateway's single attach and `Browser.getVersion` identity check;
do not treat the HTTP 404 as permission to start another browser.

## Notes

- Data source: `GET https://hk.jobsdb.com/api/jobsearch/v5/search`. Verified live,
  no auth. See `../url-reference.md`.
- The full job **description body** is not returned by `detail` (the `/job/<id>`
  page is a client-rendered SPA). `detail` surfaces the rich fields already in the
  search response: teaser, bullet points, salary, classification, work types,
  work arrangement, location, and date.
- Keep volume low. The endpoint is public but Seek/Cloudflare may rate-limit bulk
  personal use.
