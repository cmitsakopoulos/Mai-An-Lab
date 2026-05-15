#!/bin/bash
# 1. Resolve local paths
python3 configure_paths.py

# 2. Run build
echo "Starting Flet build..."

# Kill any hung java/gradle processes (Android build locks)
pkill -9 java || true

# Wipe previous build artifacts and gradle cache to ensure a fresh state
echo "Wiping previous build directory: $(pwd)/build"
rm -rf build .gradle

# Uninstall existing app from connected device to avoid signature mismatches
adb uninstall com.mitsakopoulos.maianlab.mai_an_lab || true

# Execute Flet build
flet build apk --clear-cache -v --yes

# Reinstall the fresh APK
adb install build/apk/mai-an-lab.apk
