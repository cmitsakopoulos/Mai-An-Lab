#!/usr/bin/env bash
# Copy a library database into the Flutter port (debug build) on a connected
# Android device or emulator.
#
#   tool/push_db.sh                     newest phone state bundle in
#                                       tools/state_backups/ (Android paths,
#                                       so tracks resolve on the phone)
#   tool/push_db.sh path/to/bundle.zip  a state bundle (state_export.py format)
#   tool/push_db.sh path/to/library.db  any library.db, e.g. the Mac's at
#                                       StreamripApp/.flet/storage/data/library.db
#                                       (Mac paths: browsable, not playable)
#
# Lands at files/data/library.db: the same place the Flet app keeps it
# (FLET_APP_STORAGE_DATA), so the port reads it exactly as it will at cutover.
# It replaces the port's copy, discarding what the port wrote into it (the
# ledger drain's play counts). A bundle's config.toml comes too, to
# files/data/.config/streamrip/config.toml (where utils/config.py keeps it on
# Android), replacing what the port's Settings wrote. The app is stopped
# first: it holds the database open read-write. Start it again afterwards.
set -euo pipefail

PKG=com.mitsakopoulos.maianlab.mai_an_lab.port
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# Same adb as StreamripApp/_adb_common.sh: the SDK copy, not Homebrew's. Two
# adb versions fight over the daemon on tcp:5037.
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android}}"
ADB="${ADB:-$SDK/platform-tools/adb}"
[[ -x $ADB ]] || ADB="$(command -v adb)" || { echo "No adb found" >&2; exit 1; }

SRC="${1:-$(ls -t "$REPO"/tools/state_backups/mai_an_lab_state_*.zip 2>/dev/null | head -1 || true)}"
[[ -n $SRC && -f $SRC ]] || { echo "No such file: ${SRC:-<no bundle in tools/state_backups>}" >&2; exit 1; }

"$ADB" get-state >/dev/null 2>&1 || {
  echo "No device. Plug the phone in (unlocked, USB debugging on) and check '$ADB devices'." >&2
  exit 1
}
"$ADB" shell "run-as $PKG true" 2>/dev/null || {
  echo "$PKG is not installed as a debug build. Run 'flutter run' once first." >&2
  exit 1
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

CONFIG=""
if [[ $SRC == *.zip ]]; then
  unzip -q -o "$SRC" library.db -d "$TMP/bundle"
  SRC_DB="$TMP/bundle/library.db"
  if unzip -q -o "$SRC" config.toml -d "$TMP/bundle" 2>/dev/null; then
    CONFIG="$TMP/bundle/config.toml"
  fi
else
  SRC_DB="$SRC"
fi

# .backup gives a consistent snapshot even while the Python app has it open
# (WAL included). DELETE journal mode so the copy is one self-contained file.
sqlite3 "$SRC_DB" ".backup '$TMP/library.db'"
sqlite3 "$TMP/library.db" "PRAGMA journal_mode=DELETE;" >/dev/null
echo "From:     $SRC"
echo "Snapshot: $(sqlite3 "$TMP/library.db" 'SELECT count(*) FROM tracks') tracks," \
     "$(sqlite3 "$TMP/library.db" 'SELECT count(*) FROM play_counts WHERE pca_coords IS NOT NULL') with coordinates," \
     "e.g. $(sqlite3 "$TMP/library.db" 'SELECT path FROM tracks LIMIT 1')"

"$ADB" push "$TMP/library.db" /data/local/tmp/maian_library.db >/dev/null
# Never swap the file under a running app's open connection (and its WAL).
"$ADB" shell am force-stop "$PKG" 2>/dev/null || true
"$ADB" shell "run-as $PKG sh -c 'mkdir -p files/data && cp /data/local/tmp/maian_library.db files/data/library.db && rm -f files/data/library.db-wal files/data/library.db-shm'"
"$ADB" shell rm -f /data/local/tmp/maian_library.db
echo "Pushed to $PKG:files/data/library.db"
if [[ -n $CONFIG ]]; then
  "$ADB" push "$CONFIG" /data/local/tmp/maian_config.toml >/dev/null
  "$ADB" shell "run-as $PKG sh -c 'mkdir -p files/data/.config/streamrip && cp /data/local/tmp/maian_config.toml files/data/.config/streamrip/config.toml'"
  "$ADB" shell rm -f /data/local/tmp/maian_config.toml
  echo "Pushed to $PKG:files/data/.config/streamrip/config.toml"
fi
