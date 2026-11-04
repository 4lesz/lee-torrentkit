#!/usr/bin/env sh
# torrentkit one-click launcher (macOS / Linux) - author: lee
# Usage: ./start.sh [port]
set -e
cd "$(dirname "$0")"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[torrentkit] Python 3.9+ not found." >&2
    exit 1
fi

PORT="${1:-8080}"
mkdir -p downloads
echo "[torrentkit] Web UI: http://127.0.0.1:$PORT/   (Ctrl+C to stop)"
exec "$PY" -m torrentkit web -d downloads --port "$PORT" --open
