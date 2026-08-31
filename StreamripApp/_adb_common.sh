#!/bin/bash
# ==============================================================================
# Shared device/ADB plumbing for build_android.sh and fresh_build_android.sh.
# ------------------------------------------------------------------------------
# Sourced, not executed. Exists so both build scripts resolve the SAME adb
# binary and fail the SAME (loud, early) way when the phone isn't there.
# ==============================================================================

die() { echo "ERROR: $*" >&2; exit 1; }

# ── Which adb? ───────────────────────────────────────────────────────────────
# There can be two on this machine: Homebrew's (/opt/homebrew/bin/adb) and the
# SDK's ($ANDROID_HOME/platform-tools/adb). They are DIFFERENT VERSIONS, and adb
# is client/server: whichever runs first owns the daemon on tcp:5037, and a
# mismatched client kills and restarts it mid-build. Pin to the SDK copy — it's
# the one the Flet/Gradle toolchain itself uses — so client and server always
# agree. Override with ADB=/path/to/adb.
resolve_adb() {
    if [ -n "$ADB" ] && [ -x "$ADB" ]; then return 0; fi
    local sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android}}"
    if [ -x "$sdk/platform-tools/adb" ]; then
        ADB="$sdk/platform-tools/adb"
    else
        ADB="$(command -v adb)" || die "no adb on PATH and none in $sdk/platform-tools"
    fi
    export ADB

    # Warn about a shadowing second copy: it's the classic "worked yesterday,
    # hangs today" cause, and it is invisible unless you go looking.
    local onpath; onpath="$(command -v adb 2>/dev/null)"
    if [ -n "$onpath" ] && [ "$onpath" != "$ADB" ]; then
        local vp vs
        vp="$("$onpath" version 2>/dev/null | sed -n '2s/.*Version //p')"
        vs="$("$ADB"    version 2>/dev/null | sed -n '2s/.*Version //p')"
        if [ -n "$vp" ] && [ -n "$vs" ] && [ "$vp" != "$vs" ]; then
            echo "NOTE: two adb versions present — using $ADB ($vs);"
            echo "      $onpath ($vp) is first on PATH and would fight it for the daemon."
        fi
    fi
}

# ── Is anything actually plugged in? ─────────────────────────────────────────
# Distinguishes the three failure modes, because the fix differs wildly:
#   (a) macOS enumerates NO usb device  -> cable/port. Not an adb problem at all.
#   (b) enumerated but 'unauthorized'   -> accept the RSA prompt on the phone.
#   (c) enumerated but 'offline'        -> replug / restart the daemon.
diagnose_no_device() {
    echo
    echo "=== No Android device in 'device' state ==============================="
    "$ADB" devices -l 2>&1 | sed 's/^/  /'

    local usb_count=0
    if command -v ioreg >/dev/null 2>&1; then
        usb_count="$(ioreg -p IOService -c IOUSBHostDevice -w0 2>/dev/null | grep -c IOUSBHostDevice)"
    fi

    if [ "${usb_count:-0}" -eq 0 ]; then
        echo
        echo "  macOS has enumerated ZERO USB devices, so no data link reached the"
        echo "  Mac. Note that the phone's \"USB Preferences / File transfer\""
        echo "  notification fires on VBUS (power) alone — it appears on a dumb"
        echo "  charger too, so it is NOT evidence the Mac is seeing the phone."
        echo
        echo "  Causes, in order of likelihood on this machine:"
        echo "    1. macOS BLOCKED IT. Apple silicon laptops gate new USB accessories:"
        echo "       System Settings > Privacy & Security > 'Allow accessories to"
        echo "       connect', default 'Ask for new accessories'. Miss or dismiss that"
        echo "       prompt and macOS refuses to enumerate, silently. Set to 'Always'."
        echo "    2. ANDROID BLOCKED IT. Android 16+/17 Advanced Protection disables USB"
        echo "       DATA while the phone is LOCKED — charging continues, which is"
        echo "       exactly this symptom. A link established while UNLOCKED survives"
        echo "       later locking, so: unlock the phone BEFORE plugging in."
        echo "       Toggle at Settings > Security & privacy > Advanced Protection."
        echo "    3. Charge-only cable, or a hub passing power but not data."
        echo
        echo "  Settle it decisively:  ./usb_probe.sh"
        echo "  (watches kernel USB enumeration live while you plug in)"
    else
        echo
        echo "  macOS sees $usb_count USB device(s), so the cable carries data."
        echo "  If the phone shows 'unauthorized': unlock it and accept the"
        echo "  'Allow USB debugging?' RSA prompt (tick 'always allow')."
        echo "  If it shows 'offline' or nothing: revoke USB-debugging"
        echo "  authorisations in Developer options, replug, then '$ADB kill-server'."
    fi

    echo
    echo "  ── Or skip the cable entirely (Android 11+, same Wi-Fi) ──"
    echo "  On phone: Developer options > Wireless debugging > Pair device with code"
    echo "    $ADB pair <ip>:<pair-port> <6-digit-code>   # ports differ, read both"
    echo "    $ADB connect <ip>:<connect-port>"
    echo "  Then re-run this script — it drives the wireless device identically."
    echo "======================================================================="
}

require_device() {
    resolve_adb
    # get-state also fails when MULTIPLE devices are attached; count explicitly
    # so that case reports itself instead of masquerading as "not connected".
    local n
    n="$("$ADB" devices | tail -n +2 | grep -cw "device")"
    if [ "$n" -eq 0 ]; then
        diagnose_no_device
        exit 1
    elif [ "$n" -gt 1 ] && [ -z "$ANDROID_SERIAL" ]; then
        echo "ERROR: $n devices attached — adb cannot pick one." >&2
        "$ADB" devices -l >&2
        die "set ANDROID_SERIAL=<serial> and re-run."
    fi
}
