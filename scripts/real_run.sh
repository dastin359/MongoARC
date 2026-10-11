#!/bin/bash
# Play one real ARC-AGI-3 game through the unmodified CCARC driver with the real agent,
# as harness generation 0 (no carried lessons). Superseded by `python -m mongoarc.launch`.
# usage: scripts/real_run.sh <game_id> <out_dir> [timeout_s=2400]
set -euo pipefail
cd "$(dirname "$0")/.."
GAME="${1:?game_id}"; OUT="${2:?out_dir}"; TIMEOUT="${3:-2400}"
set -a; . ./.env; set +a
mkdir -p "$OUT" .local/stub-cfg
[ -f .local/stub-cfg/.credentials.json ] || echo '{"claudeAiOauth":{"accessToken":"stub","refreshToken":"stub","expiresAt":4102444800000}}' > .local/stub-cfg/.credentials.json
PYTHONPATH="$PWD/CCARC/src" PYTHONDONTWRITEBYTECODE=1 python3 - "$GAME" "$OUT" <<'PY'
import json, sys, time, pathlib
from ccarc.arc_3.client import list_games
game, out = sys.argv[1], pathlib.Path(sys.argv[2])
info = next(g for g in list_games(None) if g.game_id == game)
marker = {"run_id": f"g00-{game}-{time.strftime('%Y%m%dT%H%M%S')}", "game_id": game, "generation": 0, "lessons_used": [],
          "agent": "claude", "model": "claude-opus-5", "effort": "high", "baseline_actions": list(info.baseline_actions), "label": "real"}
(out / "mongoarc_run.json").write_text(json.dumps(marker, indent=1))
print(marker["run_id"])
PY
exec env -u MONGODB_URI CLAUDE_CONFIG_DIR="$PWD/.local/stub-cfg" PYTHONPATH="$PWD/CCARC/src" PYTHONDONTWRITEBYTECODE=1 \
  CCARC3_NETGUARD=0 CCARC3_CONFINE=0 CCARC3_LIVE=1 CCARC3_CREDENTIAL_PROBE=0 CLAUDE_BINARY="$PWD/tools/claude_login.sh" \
  python3 -m ccarc arc-3 run --game "$GAME" --out-dir "$OUT" --timeout "$TIMEOUT"
