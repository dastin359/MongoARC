#!/bin/bash
# Launch the real `claude` CLI under this machine's own Claude Code login.
#
# CCARC points every solver at a per-run CLAUDE_CONFIG_DIR. On macOS the login
# lives in the Keychain under the default config dir, so a per-run dir has no
# usable credential. This wrapper drops that override (and any variables
# inherited from a parent Claude Code session) and execs the real binary, so
# the solver authenticates exactly as `claude` does in a terminal here.
# Nothing is copied or exported. Use: CLAUDE_BINARY=tools/claude_login.sh
REAL="${MONGOARC_REAL_CLAUDE:-$(command -v claude)}"
for v in $(env | grep -oE '^(CLAUDE|ANTHROPIC)[A-Za-z0-9_]*' ); do unset "$v"; done
exec "$REAL" "$@"
