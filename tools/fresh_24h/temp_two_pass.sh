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
# Pass-2 deep: LinkedIn CLI + JobsDB Playwright. CT = teaser only (no browser).
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
python3 tools/fresh_24h/fresh_24h_scan.py --mode "$MODE" $HOURS --no-record

echo ""
echo "=== 2) Two-pass score (internal gate=$PASS1_GATE + configured depth/retention) ==="
echo "    Cache hits use zero network budget; preferences come from private setup/intent"
python3 tools/fresh_24h/two_pass_score.py --gate "$PASS1_GATE"

echo ""
echo "=== 3) Commit refresh cursor after scored artifact ==="
python3 -c "from pathlib import Path; from tools.workflow.refresh_commit import commit_refresh_after_score; raise SystemExit(0 if commit_refresh_after_score(workspace=Path('JobSearch_2026'), mode='$MODE') is not None else 2)"

echo ""
echo "Done. Open JobSearch_2026/02_Tracker/*_twopass_scored.csv"
echo "Columns: 初评分数 / 深评分数 / JD深度 / 评估状态 ; CareerOps* = 深评或明确 provisional"
echo "JD深度: full (浏览器深取) | cache (URL缓存) | teaser (CT/其他) | paste_needed (熔断/预算停止) | teaser_unavailable | teaser_capped"
echo ""
echo "Next (optional sheet): push_to_gsheet.py --also-local --mode $MODE"
echo "Materials only on demand (never from this script):"
echo "  python3 -m tools.job_materials pipeline --package '…/C0-xxx_未投_…' --lane C"
