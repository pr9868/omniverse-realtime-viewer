#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Start the Omniverse streaming USD viewer server.
#
# Usage:
#   bash scripts/run.sh [--stage path/to/scene.usd] [extra args]
#
# The script:
#   1. Activates the project venv
#   2. Starts Xvfb on :99 if DISPLAY is not set
#   3. Sets OVRTX_SKIP_USD_CHECK (belt-and-suspenders; __main__.py also sets it)
#   4. Launches the server with sensible defaults
#
# Override PUBLIC_IP or any server arg via env / CLI:
#   PUBLIC_IP=1.2.3.4 bash run.sh --stage assets/samples/scene.usd
set -euo pipefail

# ── Resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"    # project root

VENV="$PROJECT_DIR/.venv"

# ── Activate venv ────────────────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "[ERROR] venv not found at $VENV — run scripts/setup.sh first"
    exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ── Headless display (ovrtx requires an X server even in headless environments) ──
if [ -z "${DISPLAY:-}" ]; then
    echo "Starting Xvfb on :99 ..."
    Xvfb :99 -screen 0 1920x1080x24 &
    export DISPLAY=:99
    sleep 1   # give Xvfb a moment to come up
fi

# ── Environment ──────────────────────────────────────────────────────────────
export OVRTX_SKIP_USD_CHECK=1
PUBLIC_IP="${PUBLIC_IP:-}"  # Set to your server's public IP for remote WebRTC

# ── Log file ─────────────────────────────────────────────────────────────────
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/viewer-$(date +%Y%m%d-%H%M%S).log"
echo "Server log: $LOG_FILE"

# ── Launch ───────────────────────────────────────────────────────────────────
cd "$PROJECT_DIR"
exec python3 -m server \
    ${PUBLIC_IP:+--public-ip "$PUBLIC_IP"} \
    --width       1920 \
    --height      1080 \
    --fps         30 \
    --port        49100 \
    --media-port  47998 \
    --health-port 8081 \
    --asset-root  "$PROJECT_DIR/assets/samples" \
    "$@" 2>&1 | tee "$LOG_FILE"
