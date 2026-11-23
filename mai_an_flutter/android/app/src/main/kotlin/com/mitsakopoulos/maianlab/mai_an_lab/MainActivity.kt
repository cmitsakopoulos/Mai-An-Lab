package com.mitsakopoulos.maianlab.mai_an_lab

import com.ryanheise.audioservice.AudioServiceActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

/**
 * AudioServiceActivity, not FlutterActivity: audio_service keeps the Flutter
 * engine cached, so the Dart isolate (player, queue, controllers) outlives
 * this activity. Playback continues with the activity gone, and reopening the
 * app attaches a new activity to the same engine. (The Flet build patched its
 * generated MainActivity to the same base class at build time.)
 */
class MainActivity : AudioServiceActivity() {
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "mai_an/app")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    // Back on the root screen: go to the background like Home,
                    // instead of finishing the activity. Music keeps playing
                    // and the UI comes back exactly as it was.
                    "moveTaskToBack" -> result.success(moveTaskToBack(true))
                    else -> result.notImplemented()
                }
            }
    }
}
