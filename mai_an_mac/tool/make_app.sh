#!/bin/bash
# Builds "Mai An Companion.app": a double-clickable wrapper around maian.
#
#   tool/make_app.sh            build into build/
#   tool/make_app.sh --install  also copy to ~/Applications and put an alias
#                               on the Desktop (Spotlight / Launchpad find it)
#
# Opening the app starts `maian serve` in a Terminal window (that window is
# the log; close it or press Ctrl-C to stop) and the control page opens in
# the browser. If the companion is already running, the app just reopens the
# control page.
set -euo pipefail

cd "$(dirname "$0")/.."
NAME="Mai An Companion"
APP="build/$NAME.app"
VERSION=$(sed -n 's/^version: *//p' pubspec.yaml)

dart pub get >/dev/null
mkdir -p build
dart compile exe cli/maian.dart -o build/maian

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp build/maian "$APP/Contents/Resources/maian"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>lab.maian.companion</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>${VERSION%%+*}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# The window Terminal runs.
cat > "$APP/Contents/Resources/start.command" <<'CMD'
#!/bin/bash
printf '\033]0;Mai An Companion\007'
exec "$(dirname "$0")/maian" serve
CMD

# The app's entry point: reopen the page if already running, else start.
cat > "$APP/Contents/MacOS/launch" <<'LAUNCH'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
HOME_DIR="${MAIAN_HOME:-$HOME/Library/Application Support/MaiAn}"
if PORT=$(cat "$HOME_DIR/ui.port" 2>/dev/null) &&
   curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/"; then
  open "http://127.0.0.1:$PORT/"
else
  open -a Terminal "$RES/start.command"
fi
LAUNCH

chmod +x "$APP/Contents/MacOS/launch" "$APP/Contents/Resources/start.command" \
  "$APP/Contents/Resources/maian"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "Built $APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/$NAME.app"
  cp -R "$APP" "$HOME/Applications/"
  ln -sfn "$HOME/Applications/$NAME.app" "$HOME/Desktop/$NAME"
  echo "Installed ~/Applications/$NAME.app (alias on the Desktop)"
fi
