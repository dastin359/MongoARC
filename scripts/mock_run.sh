#!/bin/bash
# Play one mock game through the unmodified CCARC driver with the fake agent (zero cost).
# usage: scripts/mock_run.sh <out_dir> [game_id=mk01-toy] [pause_s=0.05]
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:?out_dir}"; GAME="${2:-mk01-toy}"; PAUSE="${3:-0.05}"
PORT="${MOCK_ARC_PORT:-18765}"
if ! curl -s --max-time 1 "http://127.0.0.1:$PORT/api/games" >/dev/null; then
  nohup python3 tools/mock_arc.py "$PORT" >/dev/null 2>&1 &
  sleep 1
fi
mkdir -p .local/stub-cfg
[ -f .local/stub-cfg/.credentials.json ] || echo '{"claudeAiOauth":{"accessToken":"stub","refreshToken":"stub","expiresAt":4102444800000}}' > .local/stub-cfg/.credentials.json
env -u CLAUDE_CODE_SESSION_ID CLAUDE_CONFIG_DIR="$PWD/.local/stub-cfg" PYTHONPATH="$PWD/CCARC/src" PYTHONDONTWRITEBYTECODE=1 \
  CCARC3_ARC_ROOT="http://127.0.0.1:$PORT" CCARC3_NETGUARD=0 CCARC3_CONFINE=0 CCARC3_LIVE=1 CCARC3_CREDENTIAL_PROBE=0 \
  CLAUDE_BINARY="$PWD/tools/fake_claude.py" FAKE_POLICY="${FAKE_POLICY:-seek}" FAKE_PAUSE_S="$PAUSE" \
  python3 -m ccarc arc-3 run --game "$GAME" --out-dir "$OUT" --timeout "${TIMEOUT:-600}"
