#!/usr/bin/env bash
# Dev-only: wait until the MCP gateway answers tool calls again, then run, audit and package.
# dist/submission.zip is replaced only if scripts/check_submission.py passes.
# Usage: bash scripts/wait_and_run.sh <label> [max_minutes] [probe_interval_seconds]
set -u
cd "$(dirname "$0")/.."
LABEL="${1:?label, e.g. v4}"
MAX_MIN="${2:-90}"
EVERY="${3:-180}"
PY=.venv/Scripts/python.exe
DAY09=.venv/Scripts/day09.exe
export PYTHONIOENCODING=utf-8
LOG="debug/wait_${LABEL}.log"
mkdir -p debug
: > "$LOG"

probe() {
  "$PY" - <<'EOF'
import asyncio, sys
from pathlib import Path
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
async def main():
    root = Path(".").resolve()
    s = Settings.load(root)
    c = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(s.mcp_endpoint, s.team_api_key, c) as gw:
        r = await gw._session.call_tool(
            "get_policy", arguments={"case_id": "L3B_CASE_001", "policy_version": "EC_POLICY_V2"}
        )
        return 1 if r.is_error else 0
# sys.exit inside the MCP session would be wrapped in an ExceptionGroup: return the code.
try:
    code = asyncio.run(main())
except BaseException:
    code = 2
sys.exit(code)
EOF
}

deadline=$(( $(date +%s) + MAX_MIN * 60 ))
until probe; do
  echo "$(date +%H:%M:%S) gateway still rejecting tool calls" >> "$LOG"
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "GAVE UP after ${MAX_MIN} min; dist/submission.zip untouched" >> "$LOG"
    exit 3
  fi
  sleep "$EVERY"
done
echo "$(date +%H:%M:%S) gateway OK, running ${LABEL}" >> "$LOG"

DUMPS="debug/mcp_${LABEL}"
rm -rf "$DUMPS"
DAY09_DUMP_DIR="$DUMPS" "$DAY09" run >> "$LOG" 2>&1
for _ in 1 2 3 4; do
  [ "$(ls outputs/*.json 2>/dev/null | wc -l)" -ge 100 ] && break
  DAY09_DUMP_DIR="$DUMPS" "$DAY09" run --resume >> "$LOG" 2>&1
done

ZIP="dist/submission-${LABEL}.zip"
if "$DAY09" validate >> "$LOG" 2>&1 \
  && "$DAY09" package --output "$ZIP" >> "$LOG" 2>&1 \
  && "$PY" scripts/check_submission.py "$ZIP" --dumps "$DUMPS" \
       --baseline dist/submission-v1.zip ${CHECK_EXTRA:-} >> "$LOG" 2>&1; then
  cp "$ZIP" dist/submission.zip
  echo "SUCCESS: ${ZIP} passed every check and is now dist/submission.zip" >> "$LOG"
else
  echo "FAILED: ${LABEL} did not pass; dist/submission.zip untouched" >> "$LOG"
  exit 4
fi
