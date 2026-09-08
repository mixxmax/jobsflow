#!/usr/bin/env bash
# Temp/daily scan → internal triage → configured scan depth → retention view.
# Does NOT build CV materials. Materials = separate job_materials / handbook step.
#
# Usage:
#   ./tools/fresh_24h/temp_two_pass.sh              # default: temp
#   ./tools/fresh_24h/temp_two_pass.sh temp
#   ./tools/fresh_24h/temp_two_pass.sh 临时          # same as temp
#   ./tools/fresh_24h/temp_two_pass.sh daily
#   ./tools/fresh_24h/temp_two_pass.sh 3             # last 3 hours
#   ./tools/fresh_24h/temp_two_pass.sh 24            # last 24 hours
#   PASS1_GATE=3.3 ./tools/fresh_24h/temp_two_pass.sh temporary  # advanced only
#
# Pass-2 deep: LinkedIn CLI + JobsDB user-Chrome CDP handoff when required.
# CT stays teaser/solver-policy controlled; this script never opens a manual
# Playwright verification window or copies cookies into a detail browser.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
if [[ -f "$ROOT/JobSearch_2026/00_Profile/queries.json" ]]; then
  export JOBSEARCH_ROOT="$ROOT/JobSearch_2026"
fi
ARG="${1:-temp}"
PASS1_GATE="${PASS1_GATE:-${GATE:-3.3}}"
MODE=""
HOURS=""

# Numeric arg -> custom hours mode
if [[ "$ARG" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  MODE="temp"
  HOURS="--hours $ARG"
else
  case "$ARG" in
    daily) MODE="daily" ;;
    temp) MODE="temp" ;;
    临时|temporary|ad-hoc|adhoc)
      MODE="temp"
      ;;
    *)
      echo "ERROR: mode must be daily|temp|N(hours); got: $ARG" >&2
      exit 2
      ;;
  esac
fi

echo "=== 1) Scan (${MODE}${HOURS:+ hours=$ARG}) ==="
SCAN_ARGS=(python3 -m tools.workflow scan --mode "$MODE" --json)
if [[ -n "$HOURS" ]]; then
  # HOURS is deliberately kept as an argument array.  This avoids the old
  # unquoted shell expansion and keeps the scan window bound to this run.
  SCAN_ARGS+=(--hours "$ARG")
fi
if [[ "$PASS1_GATE" != "3.3" ]]; then
  SCAN_ARGS+=(--gate "$PASS1_GATE")
fi
echo "    Canonical workflow run: scan → score → run.json → cursor commit"
echo "    Cache hits use zero network budget; preferences come from private setup/intent"
"${SCAN_ARGS[@]}"

echo ""
echo "Done. Open JobSearch_2026/02_Tracker/*_twopass_scored.csv"
echo "Columns: 初评分数 / 深评分数 / JD深度 / 评估状态 ; CareerOps* = 深评或明确 provisional"
echo "JD深度: full (浏览器深取) | cache (URL缓存) | teaser (CT/其他) | paste_needed (熔断/预算停止) | teaser_unavailable | teaser_capped"
echo ""
echo "Next: review the preview, then use workflow push preview/confirm to enter selected rows"
echo "  python3 -m tools.workflow push --run-id <scan-run-id>"
echo "  python3 -m tools.workflow push --run-id <scan-run-id> --confirm <proposal-id>"
echo "Materials only on demand (never from this script):"
echo "  python3 -m tools.workflow materials --job-id C0-xxx"
