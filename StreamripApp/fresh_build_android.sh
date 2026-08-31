#!/bin/bash
# ==============================================================================
# Rebuild + reinstall + reinject state (the whole update loop, one command).
# ------------------------------------------------------------------------------
# The default (and only) flow, so updating between code changes is trivial:
#
#   ./fresh_build_android.sh
#
#   1. APPRAISE + capture the current on-device state. The app auto-exports a
#      snapshot (`mai_an_lab_state_latest.zip`) on every boot — to /sdcard/Download
#      AND to the configured music/library folder, both on PUBLIC storage that
#      survives uninstall. We DISCOVER it wherever it landed (the library folder
#      lives in app-private prefs we can't read, so we search shared storage for
#      it), back it up to the host, and stage it as ..._import.zip.
#   2. RECOMPILE the APK from scratch on the Mac (`flet clean` + `flet build`).
#   3. REINSTALL: uninstall (clean slate, no signature-mismatch surprises) then
#      install the fresh APK. Uninstall erases the app's private DB — that's why
#      step 1 staged the snapshot on public storage first.
#   4. REINJECT: relaunch. The app's startup hook imports the staged bundle,
#      which REPLACES library.db — so the DSP features (play_counts.timbre) and
#      graph coordinates ride straight back into the DB — then rebuilds the
#      similarity graph and deletes the import zip.
#   5. VERIFY: re-appraise the fresh post-import snapshot to confirm the features
#      actually landed on the newly-built app.
#
# There is nothing to preserve on a first-ever install (no snapshot yet); the
# script says so and just does a clean install. `--force` skips the safety
# countdowns for unattended runs.
#
# NOTE: the snapshot reflects the app's state at its LAST boot. If you changed
# the library since then, launch the app once before rebuilding so it re-exports.
# ==============================================================================

set -o pipefail

PACKAGE="com.mitsakopoulos.maianlab.mai_an_lab"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DL_DIR="/sdcard/Download"
IMPORT_ZIP="$DL_DIR/mai_an_lab_state_import.zip"
BACKUP_DIR="$SCRIPT_DIR/../tools/state_backups"
APPRAISE="$SCRIPT_DIR/../tools/appraise_state_bundle.py"
APK="$SCRIPT_DIR/build/apk/mai-an-lab.apk"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --keep-state|--preserve|--keep)
            echo "Note: state preservation is now the DEFAULT — '$arg' is a no-op." ;;
        *) echo "Unknown argument: $arg (supported: --force)" ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
# require_device(), die() and $ADB resolution live in _adb_common.sh, shared with
# build_android.sh so both scripts pin the same adb binary and diagnose a missing
# phone identically (cable vs. unauthorised vs. offline vs. wireless).
# shellcheck source=_adb_common.sh
source "$SCRIPT_DIR/_adb_common.sh"

# Newest state snapshot on the device, wherever the app wrote it (Downloads OR
# the user's music/library folder). Searches shared storage shallowly, sorts by
# mtime, prints the winning path (empty if none). `while read` + IFS handles the
# rare spaced library-folder path; toybox find/stat ship the flags used here.
discover_snapshot() {
    "$ADB" shell 'find /storage/emulated/0 -maxdepth 4 -name mai_an_lab_state_latest.zip 2>/dev/null | while IFS= read -r f; do stat -c "%Y %n" "$f" 2>/dev/null; done' \
        | tr -d '\r' | sort -rn | head -1 | cut -d' ' -f2-
}

snapshot_mtime() {
    # Epoch mtime of a device file, or 0 if absent/unreadable.
    "$ADB" shell "stat -c %Y \"$1\" 2>/dev/null" | tr -d '\r' | head -1 | grep -E '^[0-9]+$' || echo 0
}

confirm_or_force() {
    # A brief bail-out window before the destructive uninstall. Skipped by --force.
    [ "$FORCE" -eq 1 ] && return 0
    echo "$1 in 5s (Ctrl-C to abort)..."
    sleep 5
}

# ── 1. Appraise + capture + stage the current state snapshot ─────────────────
require_device
echo "=== Appraising on-device state before the rebuild ==="
SNAP="$(discover_snapshot)"
PRE_MTIME=0
STAGED=0

if [ -z "$SNAP" ]; then
    echo "No state snapshot found on device (first install, or the app has never"
    echo "exported one). Nothing to reinject — this will be a clean install."
    confirm_or_force "Proceeding with a clean install"
else
    echo "Found snapshot on device: $SNAP"
    PRE_MTIME="$(snapshot_mtime "$SNAP")"
    mkdir -p "$BACKUP_DIR"
    TS="$(date +%Y%m%d_%H%M%S)"
    HOST_SNAP="$BACKUP_DIR/mai_an_lab_state_$TS.zip"
    if ! "$ADB" pull "$SNAP" "$HOST_SNAP" >/dev/null 2>&1; then
        echo "WARNING: failed to pull snapshot to host for appraisal."
        HOST_SNAP=""
    fi

    APPRAISE_RC=2
    if [ -n "$HOST_SNAP" ] && [ -f "$APPRAISE" ]; then
        echo "--- pre-rebuild appraisal ----------------------------------------------"
        python3 "$APPRAISE" "$HOST_SNAP"
        APPRAISE_RC=$?
        echo "------------------------------------------------------------------------"
    fi

    if [ "$APPRAISE_RC" -eq 0 ]; then
        # Good bundle → stage as the auto-import in Downloads (the app's import
        # hook looks there, and Downloads survives uninstall wherever SNAP was).
        "$ADB" shell "cp \"$SNAP\" \"$IMPORT_ZIP\"" || die "failed to stage the snapshot; aborting before the destructive uninstall."
        STAGED=1
        echo "Staged snapshot -> $IMPORT_ZIP (DSP features reinjected after reinstall)."
    else
        echo "WARNING: snapshot is empty or unreadable (rc=$APPRAISE_RC) — the wipe"
        echo "         would leave NOTHING to reinject. Launch the app once so it can"
        echo "         re-export a good snapshot, then re-run."
        confirm_or_force "Proceeding WITHOUT reinjection (clean install)"
    fi
fi

# ── 2. Resolve local paths ───────────────────────────────────────────────────
python3 configure_paths.py || die "configure_paths.py failed — pyproject.toml may still point at a stale extension path."

# ── 3. Recompile the APK from scratch ────────────────────────────────────────
echo "Starting fresh Flet build..."

# Kill hung Gradle daemons holding the Android build locks. Scoped to Gradle:
# a blanket `pkill -9 java` also takes out unrelated IDEs and language servers.
pkill -9 -f "GradleDaemon" 2>/dev/null || true

# Clean the previous build output. `flet clean` is the 0.86 replacement for the
# deprecated `flet build --clear-cache`; it removes the whole build/ dir
# (Flutter bootstrap + cached artifacts + output). We also drop the stray
# top-level gradle cache, which flet clean does not own.
echo "Cleaning previous build output (flet clean)..."
flet clean || rm -rf build
rm -rf .gradle

# Stamp the build start so the apk can be proven fresh by mtime below.
STAMP="$(mktemp -t maianlab_build_stamp)"

# Execute Flet build (release APK by default — see `libapp.so` AOT in the apk).
export SERIOUS_PYTHON_VERSION=3.12
flet build apk -v --yes
BUILD_RC=$?

# ── 4. Gate the DESTRUCTIVE step on a build that actually produced an apk ────
# Step 5 uninstalls the app, which erases its private DB. Previously nothing
# checked the build first, so a failed build still ran the uninstall and then
# failed to install anything — leaving the phone with NO app at all and the
# state surviving only as the staged zip. Refuse to cross that line without a
# verified-fresh apk in hand.
if [ "$BUILD_RC" -ne 0 ]; then
    rm -f "$STAMP"
    die "flet build failed (exit $BUILD_RC). The app on your phone is untouched."
fi
if [ ! -f "$APK" ]; then
    rm -f "$STAMP"
    die "build reported success but $APK does not exist. App untouched."
fi
if [ ! "$APK" -nt "$STAMP" ]; then
    rm -f "$STAMP"
    die "$APK predates this build — refusing to wipe the app for stale code."
fi
rm -f "$STAMP"

# ── 5. Reinstall (uninstall = clean slate; public snapshot copies untouched) ──
# Re-check the device: the build takes minutes and a link that drops in that
# window turned the whole reinstall into a silent no-op.
require_device
"$ADB" uninstall "$PACKAGE" || true
"$ADB" install "$APK" || die "install FAILED after uninstall — the phone now has no app. Re-run once the device is stable; your state is safe in $IMPORT_ZIP and $BACKUP_DIR."

# ── 6. Reinject: grant permissions + relaunch to auto-import ─────────────────
# A fresh install resets runtime permissions. All-files access is required for
# the first boot to read the staged bundle from /sdcard/Download; the rest are
# dev quality-of-life so you skip the on-device permission prompts.
"$ADB" shell appops set "$PACKAGE" MANAGE_EXTERNAL_STORAGE allow || true
"$ADB" shell pm grant "$PACKAGE" android.permission.READ_EXTERNAL_STORAGE 2>/dev/null || true
"$ADB" shell pm grant "$PACKAGE" android.permission.POST_NOTIFICATIONS 2>/dev/null || true
"$ADB" shell pm grant "$PACKAGE" android.permission.RECORD_AUDIO 2>/dev/null || true

# Launch; the startup hook imports mai_an_lab_state_import.zip (replacing
# library.db, so DSP features + coords ride back in), rebuilds the similarity
# graph, then deletes the bundle.
"$ADB" shell monkey -p "$PACKAGE" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
echo "Relaunched."

if [ "$STAGED" -ne 1 ]; then
    echo "Clean install complete (nothing was reinjected)."
    exit 0
fi

echo "The app is reinjecting your state + rebuilding the graph on boot."

# ── 7. Verify: confirm the features/graph actually landed ────────────────────
# Wait for a FRESH snapshot (newer than the pre-rebuild one) — it only appears
# once the reinstalled app has booted, imported, and re-exported.
echo "Waiting for the app to re-export a post-import snapshot (up to ~150s)..."
NEW_SNAP=""
for _ in $(seq 1 30); do
    sleep 5
    CAND="$(discover_snapshot)"
    if [ -n "$CAND" ]; then
        CAND_MTIME="$(snapshot_mtime "$CAND")"
        if [ "$CAND_MTIME" -gt "$PRE_MTIME" ] 2>/dev/null; then
            NEW_SNAP="$CAND"
            break
        fi
    fi
done

if [ -n "$NEW_SNAP" ] && [ -f "$APPRAISE" ]; then
    VERIFY="$BACKUP_DIR/mai_an_lab_state_postimport.zip"
    if "$ADB" pull "$NEW_SNAP" "$VERIFY" >/dev/null 2>&1; then
        echo "--- post-import appraisal (on the freshly-built app) -------------------"
        python3 "$APPRAISE" "$VERIFY"
        echo "------------------------------------------------------------------------"
        echo "If DSP features are present but graph coordinates are 0, the graph is"
        echo "still rebuilding in the background — re-appraise in a minute."
    fi
else
    echo "No fresh snapshot seen yet — the app may still be importing/rebuilding."
    echo "Give it a minute, then: python3 $APPRAISE <pulled snapshot>"
fi

echo "Host-side safety copy kept in tools/state_backups/ if anything goes wrong."
