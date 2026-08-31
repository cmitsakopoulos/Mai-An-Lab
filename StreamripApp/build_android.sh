#!/bin/bash
# ==============================================================================
# Incremental Android build (reinstall over the existing app).
# ------------------------------------------------------------------------------
# Uses `adb install -r`, which PRESERVES app data (library DB, DSP features,
# index) AND previously granted runtime permissions. So unlike the fresh build,
# nothing is lost here and no state round-trip is needed.
#
# --keep-state (alias: --preserve) is accepted for symmetry with
# fresh_build_android.sh but is a no-op: `-r` already keeps your state.
#
# NOTE: incremental builds do NOT clear build/, so a stale APK from a previous
# run survives a failed build. The script therefore refuses to install an APK
# that the build it just ran did not actually produce — see step 3.
# ==============================================================================

set -o pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_adb_common.sh
source "$SCRIPT_DIR/_adb_common.sh"

APK="$SCRIPT_DIR/build/apk/mai-an-lab.apk"

for arg in "$@"; do
    case "$arg" in
        --keep-state|--preserve|--keep)
            echo "Note: --keep-state is unnecessary for the incremental build —"
            echo "      'adb install -r' already preserves data and permissions."
            ;;
        *) echo "Unknown argument: $arg" ;;
    esac
done

# ── 0. Fail before the 10-minute build, not after ────────────────────────────
require_device

# ── 1. Resolve local paths ───────────────────────────────────────────────────
python3 configure_paths.py || die "configure_paths.py failed — pyproject.toml may still point at a stale extension path."

# ── 2. Run build ─────────────────────────────────────────────────────────────
echo "Starting incremental Flet build..."

# Kill hung Gradle daemons holding the Android build locks. Scoped to Gradle:
# a blanket `pkill -9 java` also takes out unrelated IDEs and language servers.
pkill -9 -f "GradleDaemon" 2>/dev/null || true

# Stamp the moment the build starts, so step 3 can tell a fresh APK from a stale
# one by mtime. Touch a marker rather than reading the clock: mtime comparison
# then uses the same filesystem timestamp source as the APK itself.
STAMP="$(mktemp -t maianlab_build_stamp)"

export SERIOUS_PYTHON_VERSION=3.12
flet build apk -v --yes
BUILD_RC=$?

# ── 3. Verify the build actually produced a NEW apk ──────────────────────────
# Three distinct failure modes, all of which previously ended with `adb install`
# silently pushing 17-day-old code and the change under test appearing to have
# had no effect:
#   - build failed outright                  (BUILD_RC != 0)
#   - build "succeeded" but wrote no apk     (missing file)
#   - build bailed early, leaving the old apk (apk older than the stamp)
if [ "$BUILD_RC" -ne 0 ]; then
    rm -f "$STAMP"
    die "flet build failed (exit $BUILD_RC). NOT installing — the apk in build/apk is stale."
fi
if [ ! -f "$APK" ]; then
    rm -f "$STAMP"
    die "build reported success but $APK does not exist."
fi
if [ ! "$APK" -nt "$STAMP" ]; then
    rm -f "$STAMP"
    die "$APK was not rewritten by this build (it predates it) — refusing to install stale code."
fi
rm -f "$STAMP"

# ── 4. Reinstall, preserving user data and application state ─────────────────
# Re-check the device: the build takes minutes, and a cable that drops in that
# window used to turn into a silent no-op install.
require_device
"$ADB" install -r "$APK" || die "adb install -r failed — the app on the phone is unchanged."
echo "Installed $(basename "$APK") ($(date -r "$APK" '+%Y-%m-%d %H:%M'))."
