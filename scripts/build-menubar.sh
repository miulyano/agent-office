#!/bin/sh
# Build the menubar app: menubar/AgentOffice.swift → build/AgentOffice.app (macOS, swiftc).
# Usage: scripts/build-menubar.sh [port]
# The app remembers where this repo and python3 are (Info.plist), so keep the repo in place.
set -eu

PORT="${1:-8767}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
APP="$ROOT/build/AgentOffice.app"
BIN="$APP/Contents/MacOS/AgentOffice"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -framework Cocoa -framework WebKit "$ROOT/menubar/AgentOffice.swift" -o "$BIN"

# app icon: the binary draws its own pixel office
ICONSET="$ROOT/build/AppIcon.iconset"
rm -rf "$ICONSET"
mkdir -p "$APP/Contents/Resources"
"$BIN" --render-icon "$ICONSET"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.agent-office</string>
  <key>CFBundleName</key><string>Agent Office</string>
  <key>CFBundleExecutable</key><string>AgentOffice</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$(cat "$ROOT/VERSION")</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>AOPython</key><string>$PYTHON</string>
  <key>AOServer</key><string>$ROOT/server.py</string>
  <key>AOPort</key><integer>$PORT</integer>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP" >/dev/null 2>&1
echo "Built $APP (server: $ROOT/server.py, port $PORT)"
