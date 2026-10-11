#!/bin/bash
# Launch the real `claude` CLI under this machine's own Claude Code login.
#
# CCARC points every solver at a per-run CLAUDE_CONFIG_DIR. On macOS the login
# lives in the Keychain under the default config dir, so a per-run dir has no
# usable credential. This wrapper drops that override (and any variables
# inherited from a parent Claude Code session) and execs the real binary, so
# the solver authenticates exactly as `claude` does in a terminal here.
# Nothing is copied or exported. Use: CLAUDE_BINARY=tools/claude_login.sh
#
# The solver must not inherit the operator's personal setup either: user-level
# hooks, MCP servers and auto-memory would leak state between games outside the
# harness's own (MongoDB) memory. So user settings and MCP config are skipped.
REAL="${MONGOARC_REAL_CLAUDE:-$(command -v claude)}"
for v in $(env | grep -oE '^(CLAUDE|ANTHROPIC)[A-Za-z0-9_]*' ); do unset "$v"; done
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 DISABLE_AUTOUPDATER=1
# Optional: run solvers on a different subscription than this machine's login.
# MONGOARC_CLAUDE_OAUTH_TOKEN is a `claude setup-token` token, read from the
# environment or the repo's .env, and handed to the solver process only.
if [ -z "${MONGOARC_CLAUDE_OAUTH_TOKEN:-}" ] && [ -f "$(dirname "$0")/../.env" ]; then
  MONGOARC_CLAUDE_OAUTH_TOKEN="$(grep -E '^MONGOARC_CLAUDE_OAUTH_TOKEN=' "$(dirname "$0")/../.env" | tail -1 | cut -d= -f2-)"
fi
[ -n "${MONGOARC_CLAUDE_OAUTH_TOKEN:-}" ] && export CLAUDE_CODE_OAUTH_TOKEN="$MONGOARC_CLAUDE_OAUTH_TOKEN"
exec "$REAL" "$@" --setting-sources project,local --strict-mcp-config
