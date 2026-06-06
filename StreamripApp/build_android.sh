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
# ==============================================================================

PACKAGE="com.mitsakopoulos.maianlab.mai_an_lab"

for arg in "$@"; do
    case "$arg" in
        --keep-state|--preserve|--keep)
            echo "Note: --keep-state is unnecessary for the incremental build —"
            echo "      'adb install -r' already preserves data and permissions."
            ;;
        *) echo "Unknown argument: $arg" ;;
    esac
done

# 1. Resolve local paths
python3 configure_paths.py

# 2. Run build
echo "Starting incremental Flet build..."

# Kill any hung java/gradle processes (Android build locks)
pkill -9 java || true

# Execute Flet build (incremental, reuse existing gradle & build caches)
flet build apk -v --yes

# Reinstall the APK, preserving user data and application state
adb install -r build/apk/mai-an-lab.apk
