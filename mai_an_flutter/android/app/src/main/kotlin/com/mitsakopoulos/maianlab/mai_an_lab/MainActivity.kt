package com.mitsakopoulos.maianlab.mai_an_lab

import android.content.Context
import android.media.MediaScannerConnection
import android.net.ConnectivityManager
import android.net.wifi.WifiManager
import com.ryanheise.audioservice.AudioServiceActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.net.Inet4Address

/**
 * AudioServiceActivity, not FlutterActivity: audio_service keeps the Flutter
 * engine cached, so the Dart isolate (player, queue, controllers) outlives
 * this activity. Playback continues with the activity gone, and reopening the
 * app attaches a new activity to the same engine. (The Flet build patched its
 * generated MainActivity to the same base class at build time.)
 */
class MainActivity : AudioServiceActivity() {
    companion object {
        // Process-wide, like the engine: a new activity must release the
        // lock an earlier one took.
        private var multicastLock: WifiManager.MulticastLock? = null
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "mai_an/app")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    // Back on the root screen: go to the background like Home,
                    // instead of finishing the activity. Music keeps playing
                    // and the UI comes back exactly as it was.
                    "moveTaskToBack" -> result.success(moveTaskToBack(true))
                    // Companion discovery (PORT_MAP §1 row 17).
                    "defaultRouteIpv4" -> result.success(defaultRouteIpv4())
                    "acquireMulticastLock" -> {
                        acquireMulticastLock()
                        result.success(null)
                    }
                    "releaseMulticastLock" -> {
                        releaseMulticastLock()
                        result.success(null)
                    }
                    // A file the app saved (a Mac transfer): index it in
                    // MediaStore so other apps see it.
                    "scanMediaFile" -> {
                        val path = call.arguments as? String
                        if (path != null) {
                            MediaScannerConnection.scanFile(
                                applicationContext, arrayOf(path), null, null)
                        }
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            }
    }

    /**
     * The IPv4 address of the active (default-route) network: what Python's
     * resolve_local_ip learns by "connecting" a UDP socket to 8.8.8.8, which
     * dart:io can't do.
     */
    private fun defaultRouteIpv4(): String? = try {
        val cm = applicationContext.getSystemService(ConnectivityManager::class.java)
        cm?.getLinkProperties(cm.activeNetwork)?.linkAddresses
            ?.map { it.address }
            ?.firstOrNull { it is Inet4Address && !it.isLoopbackAddress }
            ?.hostAddress
    } catch (e: Exception) {
        null
    }

    /** Without it Android drops inbound broadcast datagrams (the Mac's beacons). */
    private fun acquireMulticastLock() {
        if (multicastLock?.isHeld == true) return
        try {
            val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            multicastLock = wifi.createMulticastLock("MaiAnLab:Discovery").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (e: Exception) {
            multicastLock = null
        }
    }

    private fun releaseMulticastLock() {
        try {
            multicastLock?.takeIf { it.isHeld }?.release()
        } catch (_: Exception) {
        }
        multicastLock = null
    }
}
