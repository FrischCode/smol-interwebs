#!/bin/sh
# ========================================================================
#  siw.sh - the SIW front door (POSIX)
#
#  Starts the console server (kernel API + web console, one port, 7662 by
#  default). The console opens in a browser unless the config sets open_browser
#  to false. The server runs in THIS terminal until you press Ctrl-C.
#
#    siw.sh                     start with the defaults (loopback, 7662,
#                               the machine config dir; writes
#                               ~/.config/siw/config.json on first run)
#    siw.sh --config my.json    start from a named config file
#    siw.sh --port 8000         run-only overrides (the file is not written)
# ========================================================================

cd "$(dirname "$0")" || exit 1

# -- find a Python 3.11+ (a repo venv first, then python3 on PATH) --
PY=""
if [ -x .venv/bin/python ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
fi
if [ -z "$PY" ]; then
  echo "siw: no python3 found on PATH."
  echo "      Install Python 3.11+ (https://www.python.org/downloads/) and retry."
  exit 2
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || {
  echo "siw: $PY is older than 3.11; the server needs Python 3.11+."
  exit 2
}

exec "$PY" -m siw serve "$@"
