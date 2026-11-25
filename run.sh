#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
PORT="${FILLVIEWER_PORT:-8099}"
echo "Starting FillViewer on http://127.0.0.1:${PORT}/"
command -v xdg-open >/dev/null 2>&1 && xdg-open "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 &
exec python3 server.py
