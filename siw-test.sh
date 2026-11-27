#!/bin/sh
# ========================================================================
#  siw-test.sh - run the SIW test battery (POSIX)
#
#  The battery: the lifted kernel, the console wire shape, the download
#  queue. No network, no kiwix-serve, nothing outside the tree.
#
#    siw-test.sh                 run everything
#    siw-test.sh -k console      pass-through to pytest (any pytest flag)
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
  echo "siw-test: no python3 found on PATH."
  echo "          Install Python 3.11+ (https://www.python.org/downloads/) and retry."
  exit 2
fi

# -- pytest is the only outside need; when it is missing, build a .venv in the
#    tree (gitignored, portable) rather than touching the system Python, which
#    PEP 668 systems (Ubuntu 24.04+) refuse --
if ! "$PY" -m pytest --version >/dev/null 2>&1; then
  echo "siw-test: pytest not found - creating a .venv in the tree and installing it there..."
  "$PY" -m venv .venv || {
    echo "siw-test: the venv build failed (on Debian/Ubuntu: apt install python3-venv)."
    echo "          Or install pytest by hand and retry:"
    echo "          $PY -m pip install pytest"
    exit 2
  }
  .venv/bin/python -m pip install --quiet pytest || {
    echo "siw-test: the pytest install failed. Run it by hand:"
    echo "          .venv/bin/python -m pip install pytest"
    exit 2
  }
  PY=".venv/bin/python"
fi

exec "$PY" -m pytest tests/ "$@"
