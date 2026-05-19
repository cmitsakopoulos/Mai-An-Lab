#!/bin/bash
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
