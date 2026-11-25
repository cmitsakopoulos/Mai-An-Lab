#!/usr/bin/env bash
# Build the Flutter port, install it on the connected Android phone and start
# it. Installs as com.mitsakopoulos.maianlab.mai_an_lab.port, next to the Flet
# app, never over it (PORT_MAP §1 row 1).
#
#   tool/install_phone.sh                 debug build: install, launch
#   tool/install_phone.sh --db            ...and copy the newest phone state
#                                         bundle's library.db into the app first
#   tool/install_phone.sh --db FILE       ...or this bundle .zip / library.db
#   tool/install_phone.sh --release       release build (smooth; for daily use)
#   tool/install_phone.sh --profile       profile build: release speed, plus
#                                         DevTools (jank, CPU, memory); then
#                                         `flutter attach` to measure
#   tool/install_phone.sh --no-build      reinstall the last APK built
#   tool/install_phone.sh --grant         grant audio, notification and
#                                         all-files access without the prompts
#   tool/install_phone.sh --logs          follow the app's log after launch
#   tool/install_phone.sh --wifi IP:PORT  adb connect first (Wireless debugging)
#
# Flags combine. Typical first run: --db, then try the phase 1/2 checks in
# docs/PORT_MAP.md §4. Later, for everyday listening: --release (the data in
# the app is kept across reinstalls).
#
# Debug vs release: a debug build is slow to scroll but lets --db write into
# the app's private folder (run-as). A release build is fast but not
# debuggable, so --db needs a debug build installed at that moment: run
# `--db` once, then `--release`; the database stays. All three modes are
# signed with ~/.android/debug.keystore, so they install over each other.
# Judge speed (scrolling, DSP seconds per track) only in --profile/--release.
#
# Several devices attached: ANDROID_SERIAL=<serial> tool/install_phone.sh
# Another adb: ADB=/path/to/adb tool/install_phone.sh
set -euo pipefail

PKG=com.mitsakopoulos.maianlab.mai_an_lab.port
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"

mode=debug build=1 db=0 db_src="" grant=0 logs=0 wifi=""
while (($#)); do
  case $1 in
    --release) mode=release ;;
    --profile) mode=profile ;;
    --debug) mode=debug ;;
    --no-build) build=0 ;;
    --db)
      db=1
      if [[ ${2:-} && $2 != --* ]]; then db_src=$2; shift; fi
      ;;
    --grant) grant=1 ;;
    --logs) logs=1 ;;
    --wifi) wifi=${2:?--wifi needs IP:PORT}; shift ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if ((db)) && [[ $mode != debug ]]; then
  echo "--db needs a debug build ($mode builds are not debuggable)." >&2
  echo "Run '$0 --db' first, then '$0 --release'; the database is kept." >&2
  exit 2
fi

# ── adb ─────────────────────────────────────────────────────────────────────
# The SDK's adb, not Homebrew's: two versions fight over the daemon on
# tcp:5037 (same rule as StreamripApp/_adb_common.sh).
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android}}"
ADB="${ADB:-$SDK/platform-tools/adb}"
[[ -x $ADB ]] || ADB="$(command -v adb)" || { echo "No adb found." >&2; exit 1; }
export ADB

if [[ -n $wifi ]]; then
  "$ADB" connect "$wifi"
fi

n=$("$ADB" devices | tail -n +2 | grep -cw device || true)
if ((n == 0)); then
  cat >&2 <<EOF
No phone in 'device' state:
$("$ADB" devices -l | tail -n +2 | sed 's/^/  /')

  - Unlock the phone before plugging in, and accept "Allow USB debugging?".
  - 'unauthorized': accept that prompt. 'offline': replug, or '$ADB kill-server'.
  - Nothing listed: a charge-only cable, or macOS blocked the accessory
    (System Settings > Privacy & Security > Allow accessories to connect).
    StreamripApp/usb_probe.sh watches USB enumeration live.
  - No cable: Developer options > Wireless debugging > Pair device with code,
      $ADB pair IP:PAIR_PORT CODE
    then re-run with --wifi IP:CONNECT_PORT.
EOF
  exit 1
elif ((n > 1)) && [[ -z ${ANDROID_SERIAL:-} ]]; then
  echo "$n devices attached; pick one with ANDROID_SERIAL=<serial>:" >&2
  "$ADB" devices -l >&2
  exit 1
fi
echo "Phone: $("$ADB" shell getprop ro.product.model | tr -d '\r')," \
     "Android $("$ADB" shell getprop ro.build.version.release | tr -d '\r')"

# ── build ───────────────────────────────────────────────────────────────────
APK="$APP/build/app/outputs/flutter-apk/app-$mode.apk"
if ((build)); then
  command -v flutter >/dev/null || { echo "flutter is not on PATH." >&2; exit 1; }
  echo "Building the $mode APK (arm64)..."
  (cd "$APP" && flutter build apk "--$mode" --target-platform android-arm64)
fi
[[ -f $APK ]] || { echo "No APK at $APK; run without --no-build." >&2; exit 1; }
echo "APK: $APK ($(du -h "$APK" | cut -f1 | tr -d ' '))"

# ── install ─────────────────────────────────────────────────────────────────
"$ADB" shell am force-stop "$PKG" 2>/dev/null || true
if ! out=$("$ADB" install -r "$APK" 2>&1); then
  echo "$out" >&2
  if grep -q INSTALL_FAILED_UPDATE_INCOMPATIBLE <<<"$out"; then
    echo >&2
    echo "The installed $PKG was signed with another key. Uninstalling it" >&2
    echo "deletes the port's data (not the Flet app's):" >&2
    echo "  $ADB uninstall $PKG" >&2
  fi
  exit 1
fi
echo "Installed $PKG ($mode)."

if ((grant)); then
  for p in READ_MEDIA_AUDIO POST_NOTIFICATIONS READ_EXTERNAL_STORAGE; do
    "$ADB" shell pm grant "$PKG" "android.permission.$p" 2>/dev/null || true
  done
  "$ADB" shell appops set "$PKG" MANAGE_EXTERNAL_STORAGE allow
  echo "Granted audio, notifications and all-files access."
fi

if ((db)); then
  "$HERE/push_db.sh" ${db_src:+"$db_src"}
fi

# ── launch ──────────────────────────────────────────────────────────────────
"$ADB" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
echo "Started. In the launcher it is \"Mai An (port)\", next to the Flet app."

if ((logs)); then
  pid=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pid=$("$ADB" shell pidof "$PKG" 2>/dev/null | tr -d '\r' || true)
    [[ -n $pid ]] && break
    sleep 0.5
  done
  [[ -n $pid ]] || { echo "The app is not running (crashed at start?)." >&2; exec "$ADB" logcat -d -t 200 '*:E'; }
  echo "Following the app's log (Ctrl-C to stop)..."
  exec "$ADB" logcat --pid="$pid"
fi
