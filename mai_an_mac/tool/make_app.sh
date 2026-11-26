#!/bin/bash
# Builds "Mai An Companion.app": a double-clickable wrapper around maian.
#
#   tool/make_app.sh            build into build/
#   tool/make_app.sh --install  also install to ~/Applications (Spotlight and
#                               Launchpad find it) and restart a running copy
#
# Opening the app starts `maian serve` in the background (no Terminal
# window; the log is ~/Library/Logs/MaiAn/companion.log) and opens the
# control page. Opening it again while it runs just reopens the page; the
# page's Quit button stops it.
#
# New phone builds: the app watches this checkout's
# mai_an_flutter/build/app/outputs/flutter-apk/app-release.apk, so
# `flutter build apk` there is enough to offer the build to the phone.
set -euo pipefail

cd "$(dirname "$0")/.."
NAME="Mai An Companion"
APP="build/$NAME.app"
VERSION=$(sed -n 's/^version: *//p' pubspec.yaml)
PROJECT="$(cd .. && pwd)/mai_an_flutter"
APK_DIR="$PROJECT/build/app/outputs/flutter-apk"
HOME_DIR="${MAIAN_HOME:-$HOME/Library/Application Support/MaiAn}"

dart pub get >/dev/null
mkdir -p build
dart compile exe cli/maian.dart -o build/maian

# The icon: drawn by tool/make_icon.dart, packed by macOS's own tools.
ICONSET=build/AppIcon.iconset
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
dart run tool/make_icon.dart build/icon_1024.png 1024
for s in 16 32 128 256 512; do
  sips -z $s $s build/icon_1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d build/icon_1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o build/AppIcon.icns

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp build/maian "$APP/Contents/Resources/maian"
cp build/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

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
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Mai An Companion finds your phone on the local network to sync music, run audio analysis and send app updates.</string>
</dict>
</plist>
PLIST

# The app's entry point: reopen the page if this version already runs;
# replace an older one; else start maian in the background. Extra arguments
# (--no-open from the login item) are passed on.
cat > "$APP/Contents/MacOS/launch" <<'LAUNCH'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
HOME_DIR="${MAIAN_HOME:-$HOME/Library/Application Support/MaiAn}"
LOGS="$HOME/Library/Logs/MaiAn"
VERSION="@VERSION@"
PORT=$(cat "$HOME_DIR/ui.port" 2>/dev/null || echo 8946)

up() { curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:$1/" 2>/dev/null; }
running_version() {
  curl -fsS --max-time 2 -H 'x-maian: 1' "http://127.0.0.1:$1/api/state" 2>/dev/null |
    grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4
}
# Ask it to quit; a companion too old to have Quit is stopped by its PID
# (only a process named maian, only the one on our port).
stop_companion() {
  curl -fsS -o /dev/null --max-time 3 -X POST -H 'x-maian: 1' \
    "http://127.0.0.1:$1/api/quit" 2>/dev/null || true
  for _ in $(seq 1 25); do up "$1" || return 0; sleep 0.2; done
  for p in $(lsof -nP -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null); do
    [[ "$(ps -o comm= -p "$p")" == *maian ]] && kill "$p"
  done
  for _ in $(seq 1 25); do up "$1" || return 0; sleep 0.2; done
  return 1
}

if up "$PORT"; then
  if [[ "$(running_version "$PORT")" == "$VERSION" ]]; then
    [[ " $* " == *" --no-open "* ]] || open "http://localhost:$PORT/"
    exit 0
  fi
  stop_companion "$PORT" || {
    osascript -e 'display alert "Mai An Companion" message "An older companion is still running and would not stop. Quit it (or restart the Mac) and open the app again."' >/dev/null 2>&1
    exit 1
  }
fi
mkdir -p "$LOGS"
LOG="$LOGS/companion.log"
# Keep one old log, so a long-running Mac doesn't grow it forever.
if [[ -f "$LOG" && $(stat -f %z "$LOG") -gt 5000000 ]]; then
  mv -f "$LOG" "$LOG.1"
fi
nohup "$RES/maian" serve \
  --default-watch-apk "@APK_DIR@" \
  --default-flutter-project "@PROJECT@" \
  "$@" >>"$LOG" 2>&1 </dev/null &
disown
LAUNCH
sed -i '' -e "s#@VERSION@#${VERSION%%+*}#" -e "s#@APK_DIR@#$APK_DIR#" \
  -e "s#@PROJECT@#$PROJECT#" "$APP/Contents/MacOS/launch"

chmod +x "$APP/Contents/MacOS/launch" "$APP/Contents/Resources/maian"
codesign --force --sign - "$APP/Contents/Resources/maian" >/dev/null 2>&1 || true
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "Built $APP ($VERSION)"

if [[ "${1:-}" == "--install" ]]; then
  DEST="$HOME/Applications/$NAME.app"
  # A running copy keeps serving the OLD binary until it stops: stop it
  # (by PID if it is too old to have Quit), install, start the new one.
  PORT=$(cat "$HOME_DIR/ui.port" 2>/dev/null || echo 8946)
  was_running=0
  if curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" 2>/dev/null; then
    was_running=1
    curl -fsS -o /dev/null --max-time 3 -X POST -H 'x-maian: 1' \
      "http://127.0.0.1:$PORT/api/quit" 2>/dev/null || true
    stopped=0
    for _ in $(seq 1 25); do
      curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" 2>/dev/null || { stopped=1; break; }
      sleep 0.2
    done
    if [[ $stopped == 0 ]]; then
      for p in $(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null); do
        if [[ "$(ps -o comm= -p "$p")" == *maian ]]; then
          echo "Stopping the old companion (PID $p)"
          kill "$p"
        fi
      done
      for _ in $(seq 1 25); do
        curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/" 2>/dev/null || { stopped=1; break; }
        sleep 0.2
      done
    fi
    [[ $stopped == 1 ]] || echo "Warning: the running companion did not stop; quit it before opening the app" >&2
  fi
  mkdir -p "$HOME/Applications"
  rm -rf "$DEST"
  cp -R "$APP" "$HOME/Applications/"
  # Finder and Spotlight pick up the new icon.
  touch "$DEST"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" >/dev/null 2>&1 || true
  [[ -e "$HOME/Desktop/$NAME" ]] || ln -s "$DEST" "$HOME/Desktop/$NAME"
  echo "Installed $DEST"
  if [[ $was_running == 1 ]]; then
    open -a "$DEST"
    echo "Started the new companion; its page opens in your browser"
  else
    echo "Open it from Spotlight, Launchpad or ~/Applications"
  fi
fi
