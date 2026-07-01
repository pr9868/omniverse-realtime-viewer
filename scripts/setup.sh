#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Omniverse Realtime Viewer — production server setup.
#
# Creates a project-local venv at streaming-usd-viewer/.venv and installs
# all server dependencies. Safe to re-run (idempotent).
#
# Usage (from any directory):
#   bash <repo-root>/streaming-usd-viewer/scripts/setup.sh
set -euo pipefail

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # streaming-usd-viewer/
VENV="$PROJECT_DIR/.venv"

# --------------------------------------------------------------------------- #
say "System packages"
# --------------------------------------------------------------------------- #
NEED_PKGS="python3-venv python3-dev libegl1 libopengl0 libglib2.0-0 xvfb"
MISSING=""
for p in $NEED_PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
    echo "Installing missing apt packages:$MISSING"
    sudo apt-get update -qq
    sudo apt-get install -y $MISSING
fi
ok "System packages present"

# --------------------------------------------------------------------------- #
say "Python venv: $VENV"
# --------------------------------------------------------------------------- #
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python3 -m pip install --upgrade pip setuptools wheel -q
ok "venv ready: $(which python3)"

# --------------------------------------------------------------------------- #
say "Install ovrtx (NVIDIA PyPI)"
# --------------------------------------------------------------------------- #
python3 -m pip install --upgrade ovrtx \
    --index-url https://pypi.nvidia.com \
    --extra-index-url https://pypi.org/simple

# --------------------------------------------------------------------------- #
say "Install ovstream / warp-lang / numpy / aiohttp"
# --------------------------------------------------------------------------- #
python3 -m pip install --upgrade ovstream warp-lang numpy aiohttp

# --------------------------------------------------------------------------- #
say "Import verification"
# --------------------------------------------------------------------------- #
python3 - <<'PY'
import importlib, sys
ok = True
for mod in ["numpy", "warp", "ovrtx", "ovstream", "aiohttp"]:
    try:
        m = importlib.import_module(mod)
        print(f"[OK] {mod} {getattr(m, '__version__', '')}")
    except Exception as e:
        print(f"[FAIL] {mod}: {e}")
        ok = False
sys.exit(0 if ok else 1)
PY

ok "Setup complete — activate with: source streaming-usd-viewer/.venv/bin/activate"
