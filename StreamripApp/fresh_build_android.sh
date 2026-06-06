#!/bin/bash
# ==============================================================================
# Fresh Android build (clean uninstall + install).
# ------------------------------------------------------------------------------
# A fresh install runs `adb uninstall`, which ERASES on-device app data:
# the library DB, computed DSP features, the search index, queue state, etc.
#
# Pass --keep-state (alias: --preserve) to carry that computed state across the
# wipe WITHOUT manual export/import:
#
#   ./fresh_build_android.sh --keep-state
#
# How it works (reuses the app's existing on-boot state-bundle machinery):
#   1. The app auto-exports a snapshot to /sdcard/Download/..._latest.zip on
#      every boot. /sdcard/Download is PUBLIC storage and survives uninstall.
#   2. We back that snapshot up to the host, then stage it as ..._import.zip.
#   3. After the clean reinstall we grant storage/runtime permissions via adb
#      (a fresh install resets them, and without all-files access the first boot
#      can't read the bundle) and relaunch.
#   4. The app's startup hook imports the bundle, rebuilds the similarity graph,
#      and deletes it.
#
# NOTE: the snapshot reflects the app's state at its LAST boot. If you changed
# the library since then, launch the app once before rebuilding so it re-exports.
# ==============================================================================

PACKAGE="com.mitsakopoulos.maianlab.mai_an_lab"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DL_DIR="/sdcard/Download"
LATEST_ZIP="$DL_DIR/mai_an_lab_state_latest.zip"
IMPORT_ZIP="$DL_DIR/mai_an_lab_state_import.zip"
BACKUP_DIR="$SCRIPT_DIR/../tools/state_backups"

KEEP_STATE=0
for arg in "$@"; do
    case "$arg" in
        --keep-state|--preserve|--keep) KEEP_STATE=1 ;;
        *) echo "Unknown argument: $arg (supported: --keep-state)" ;;
    esac
done

# ── Pre-wipe: capture + stage the current state snapshot ─────────────────────
if [ "$KEEP_STATE" -eq 1 ]; then
    echo "=== --keep-state: preserving current on-device state across the wipe ==="
    if adb shell test -f "$LATEST_ZIP" 2>/dev/null; then
        # Host-side safety backup (so a failed device import is still recoverable).
        mkdir -p "$BACKUP_DIR"
        TS="$(date +%Y%m%d_%H%M%S)"
        if adb pull "$LATEST_ZIP" "$BACKUP_DIR/mai_an_lab_state_$TS.zip" >/dev/null 2>&1; then
            echo "Backed up snapshot to host: tools/state_backups/mai_an_lab_state_$TS.zip"
        else
            echo "WARNING: host backup pull failed (continuing; device copy still used)."
        fi
        # Stage as the auto-import bundle. /sdcard/Download survives uninstall.
        adb shell cp "$LATEST_ZIP" "$IMPORT_ZIP"
        echo "Staged $LATEST_ZIP -> $IMPORT_ZIP (will be auto-imported after reinstall)."
    else
        echo "WARNING: No state snapshot found at $LATEST_ZIP."
        echo "         Launch the app once (it auto-exports on boot), then re-run."
        echo "         Continuing WITHOUT state preservation in 5s (Ctrl-C to abort)..."
        sleep 5
        KEEP_STATE=0
    fi
fi

# ── 1. Resolve local paths ───────────────────────────────────────────────────
python3 configure_paths.py

# ── 2. Run build ─────────────────────────────────────────────────────────────
echo "Starting fresh Flet build..."

# Kill any hung java/gradle processes (Android build locks)
pkill -9 java || true

# Wipe previous build artifacts and gradle cache to ensure a fresh state
echo "Wiping previous build directory: $(pwd)/build"
rm -rf build .gradle

# Execute Flet build
flet build apk --clear-cache -v --yes

# Uninstall existing app from connected device to avoid signature mismatches
# (this is what ERASES app data; /sdcard/Download is left untouched).
adb uninstall "$PACKAGE" || true

# Reinstall the fresh APK
adb install build/apk/mai-an-lab.apk

# ── 3. Post-install: grant permissions + relaunch to auto-import ─────────────
if [ "$KEEP_STATE" -eq 1 ]; then
    echo "=== --keep-state: granting permissions + relaunching to auto-import ==="
    # A fresh install resets runtime permissions. All-files access is required
    # for the first boot to read the staged bundle from /sdcard/Download; the
    # rest are dev quality-of-life so you skip the on-device permission prompts.
    adb shell appops set "$PACKAGE" MANAGE_EXTERNAL_STORAGE allow || true
    adb shell pm grant "$PACKAGE" android.permission.READ_EXTERNAL_STORAGE 2>/dev/null || true
    adb shell pm grant "$PACKAGE" android.permission.POST_NOTIFICATIONS 2>/dev/null || true
    adb shell pm grant "$PACKAGE" android.permission.RECORD_AUDIO 2>/dev/null || true

    # Launch; the startup hook imports mai_an_lab_state_import.zip, rebuilds the
    # similarity graph + PCA geometry, then deletes the bundle.
    adb shell monkey -p "$PACKAGE" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
    echo "Relaunched. The app is importing your state + rebuilding the graph on boot."
    echo "Host-side safety copy kept in tools/state_backups/ if anything goes wrong."
fi
