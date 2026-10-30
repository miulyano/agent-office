#!/bin/sh
# Переснять картинки README на вымышленных данных (macOS, Google Chrome, Node 22+, ffmpeg).
# Usage: scripts/demo/shoot.sh [hero] [styles] [codex] [menubar] [gif]
# Журналы лежат во временной папке, настоящие сессии в кадр не попадают.
set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO="$(mktemp -d)"
PORT=8799
trap 'kill $SERVER $PULSE 2>/dev/null; rm -rf "$DEMO"' EXIT

python3 "$ROOT/scripts/demo/gen.py" "$DEMO"
CLAUDE_CONFIG_DIR="$DEMO/claude" CODEX_HOME="$DEMO/codex" \
  python3 "$ROOT/server.py" --port "$PORT" --data "$DEMO/data" --shots "$DEMO/shots" &
SERVER=$!
python3 "$ROOT/scripts/demo/gen.py" "$DEMO" --pulse &
PULSE=$!
sleep 4

node "$ROOT/scripts/demo/shoot.mjs" "$DEMO" "$ROOT/docs/screenshots" "$@"
