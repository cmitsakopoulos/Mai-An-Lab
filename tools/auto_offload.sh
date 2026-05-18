#!/bin/bash
# ==============================================================================
# Mai-An Lab — Automatic DSP Offloader CLI
# ==============================================================================
# Automates the entire round-trip of pulling the database from internal storage
# (/data/user/0/com.mitsakopoulos.maianlab.mai_an_lab/app_flutter), running
# host-side feature extraction, and importing it back to the phone.
#
# Assumes:
#   1. USB Debugging is turned on.
#   2. A debuggable/development APK is installed on the connected device.
# ==============================================================================

set -euo pipefail

PACKAGE="com.mitsakopoulos.maianlab.mai_an_lab"
APP_DIR="/data/user/0/$PACKAGE/app_flutter"

# Dynamically resolve directory of this script to support running from any Cwd
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR="$SCRIPT_DIR/offload_temp"
CACHE_DIR="$SCRIPT_DIR/offload_cache"

echo "=== Mai-An Lab: Automated DSP Offloader ==="

# 1. Environmental Checks
if ! command -v adb >/dev/null 2>&1; then
    echo "ERROR: 'adb' tool not found on PATH. Please install Android Platform Tools."
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ERROR: 'ffmpeg' tool not found on PATH. Required for audio decoding."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: 'python3' not found on PATH."
    exit 1
fi

# Check for connected ADB devices
DEVICE_COUNT=$(adb devices | grep -v "List of devices" | grep -v "^$" | wc -l | xargs)
if [ "$DEVICE_COUNT" -eq 0 ]; then
    echo "ERROR: No Android devices detected. Make sure your phone is plugged in and USB Debugging is enabled."
    exit 1
elif [ "$DEVICE_COUNT" -gt 1 ]; then
    echo "WARNING: Multiple ADB devices detected. Using default device..."
fi

# Ensure workspace is clean
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# 2. Find and Pull the Newest State Bundle from /sdcard/Download
echo "Searching for recently exported State Bundles in your phone's Download folder..."

# Get the newest state bundle ZIP from /sdcard/Download, ignoring 'import.zip'
NEWEST_ZIP=$(adb shell "ls -t /sdcard/Download/mai_an_lab_state_*.zip 2>/dev/null | grep -v 'import.zip' | head -n 1" | tr -d '\r\n' || true)

if [ -z "$NEWEST_ZIP" ]; then
    echo "===================================================================="
    echo "ERROR: No exported State Bundles (.zip files) found in /sdcard/Download/"
    echo "--------------------------------------------------------------------"
    echo "Because the app is in Release Mode, we must do a one-time setup export."
    echo "Please execute these quick steps first:"
    echo "  1. Open Mai-An Lab on your phone."
    echo "  2. Go to: Settings -> Advanced -> Export State."
    echo "  3. Save the state ZIP file inside your phone's 'Download' folder."
    echo "  4. Re-run this script!"
    echo "===================================================================="
    exit 1
fi

ZIP_FILENAME=$(basename "$NEWEST_ZIP")
echo "Found exported state: $ZIP_FILENAME"

# Pull the ZIP bundle from the phone keeping its original timestamped name
echo "Pulling $ZIP_FILENAME to laptop..."
adb pull "$NEWEST_ZIP" "$TEMP_DIR/$ZIP_FILENAME"

# 3. Execute Feature Extraction Pipeline
echo "Starting Host-Side DSP & Feature Extraction..."
python3 "$SCRIPT_DIR/dsp_offload.py" "$TEMP_DIR/$ZIP_FILENAME" --workdir "$CACHE_DIR" --keep-workdir

ANALYSED_FILENAME="${ZIP_FILENAME%.zip}.analysed.zip"
ANALYSED_ZIP="$TEMP_DIR/$ANALYSED_FILENAME"

if [ ! -f "$ANALYSED_ZIP" ]; then
    echo "ERROR: Feature extraction failed to produce analyzed state bundle."
    exit 1
fi

# Save a permanent copy on the laptop inside tools/analyzed_states/
mkdir -p "$SCRIPT_DIR/analyzed_states"
cp "$ANALYSED_ZIP" "$SCRIPT_DIR/analyzed_states/$ANALYSED_FILENAME"
echo "Saved a permanent copy on your laptop at: tools/analyzed_states/$ANALYSED_FILENAME"

# 4. Push the analyzed ZIP bundle back as 'mai_an_lab_state_import.zip'
# The app's startup hook will detect this on boot, import it, and delete it!
echo "Pushing analysed bundle to phone as auto-import..."
adb push "$ANALYSED_ZIP" "/sdcard/Download/mai_an_lab_state_import.zip"

# Clean up the original exported ZIP file on the device to avoid cluttering the phone's storage!
echo "Cleaning up original exported state ZIP from your phone's Downloads folder..."
adb shell "rm -f \"$NEWEST_ZIP\""

# 5. Relaunch Application to Trigger Auto-Import Startup Hook
echo "Halting application to load new state..."
adb shell am force-stop "$PACKAGE"

echo "Relaunching Mai-An Lab. The app will automatically sync features on boot!"
adb shell monkey -p "$PACKAGE" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1

# Clean up temporary work folder
rm -rf "$TEMP_DIR"

echo "===================================================================="
echo "SUCCESS: Host-Side DSP Feature Ingestion Completed!"
echo "--------------------------------------------------------------------"
echo "The app has been restarted and has automatically loaded your new features."
echo "You can find your permanent laptop copy at:"
echo "  tools/analyzed_states/$ANALYSED_FILENAME"
echo "Enjoy the vibe!"
echo "===================================================================="
