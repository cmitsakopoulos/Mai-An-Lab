#!/bin/bash
# ==============================================================================
# USB link probe — reads the Type-C port controller, not just the USB bus.
# ------------------------------------------------------------------------------
# `adb devices` and `system_profiler SPUSBDataType` only answer "did a USB device
# enumerate?", which collapses four very different failures into one blank line.
# The AppleTCControllerType10 nub (ioreg: Port-USB-C@N) exposes the layer BELOW
# that, so the failure can be pinned to a side:
#
#   ConnectionActive = No       -> nothing electrically attached (cable/port).
#   TransportsUnauthorized != ()-> macOS blocked it ("Allow accessories").
#   TransportsActive = (CC)     -> attached + authorised, but the PHONE never
#     with USB2/USB3 provisioned    brought its data lines up. Mac side is fine;
#                                   this is a phone-side USB-data-signalling block.
#   IOUSBHostDevice > 0         -> enumerated; any failure is at the adb layer.
#
# "CC" is the Type-C configuration channel: power delivery and data-role
# negotiation. CC alone is why a phone can be certain it is plugged into a host
# (and show its USB Preferences notification) while the host sees no device.
#
#   ./usb_probe.sh
# ==============================================================================

port_key() { ioreg -r -n "Port-USB-C@$1" -w0 -d 1 2>/dev/null | sed -n "s/.*\"$2\" = //p" | head -1; }

enumerated="$(ioreg -p IOService -c IOUSBHostDevice -w0 2>/dev/null | grep -c IOUSBHostDevice)"
echo "USB devices enumerated by macOS: $enumerated"
echo

found_attached=0
for P in 1 2; do
    active="$(port_key "$P" ConnectionActive)"
    [ -z "$active" ] && continue
    conn="$(port_key "$P" IOAccessoryUSBConnectString)"
    tactive="$(port_key "$P" TransportsActive)"
    tprov="$(port_key "$P" TransportsProvisioned)"
    tunauth="$(port_key "$P" TransportsUnauthorized)"
    auth="$(port_key "$P" UserAuthorizationStatusDescription)"

    echo "── Port-USB-C@$P ──"
    echo "   attached partner : $conn   (ConnectionActive=$active)"
    echo "   transports active: $tactive"
    echo "   provisioned      : $tprov"
    echo "   unauthorised     : $tunauth      auth: $auth"

    if [ "$conn" = "\"Device\"" ]; then
        found_attached=1
        if [ "$enumerated" -gt 0 ]; then
            echo "   VERDICT: enumerated — hardware is fine, debug at the adb layer."
        elif [ -n "$tunauth" ] && [ "$tunauth" != "()" ]; then
            echo "   VERDICT: macOS BLOCKED the data transports."
            echo "            System Settings > Privacy & Security >"
            echo "            'Allow accessories to connect' -> Always, then replug."
        elif echo "$tprov" | grep -q USB2 && ! echo "$tactive" | grep -q USB2; then
            echo "   VERDICT: PHONE-SIDE BLOCK. The Mac attached the phone, authorised it"
            echo "            and provisioned USB2/USB3 — but the phone never brought its"
            echo "            data lines up. Nothing on the Mac can fix this. On the phone:"
            echo "              1. Settings > Security & privacy > Advanced Protection."
            echo "                 Android 16+/17 disables USB DATA under this; charging"
            echo "                 and CC keep working, which is exactly this state."
            echo "              2. The block is re-evaluated AT ATTACH. Unlocking after"
            echo "                 plugging in does NOT revive it — UNPLUG AND REPLUG"
            echo "                 while the phone is unlocked."
            echo "              3. Developer options > Default USB configuration ->"
            echo "                 File transfer, so the gadget comes up without a tap."
            echo "              4. Reboot the phone. The USB gadget driver commonly wedges"
            echo "                 after a major OS upgrade."
            echo "            If Advanced Protection stays on, use wireless debugging —"
            echo "            it is unaffected by the USB data rule."
        else
            echo "   VERDICT: attached, data transports not up — cause unclear."
        fi
    fi
    echo
done

if [ "$found_attached" -eq 0 ]; then
    echo "No USB-C port reports an attached device."
    echo "  Nothing is electrically connected: charge-only cable, dead port, or a"
    echo "  hub passing power only. Plug the phone DIRECTLY into the Mac and re-run."
    exit 1
fi

ADB="${ANDROID_HOME:-$HOME/Android}/platform-tools/adb"
[ -x "$ADB" ] || ADB="$(command -v adb)"
echo "adb:"; "$ADB" devices -l 2>&1 | sed 's/^/   /'
