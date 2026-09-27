package com.flet.flet_audio_service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadataRetriever
import android.media.AudioFormat
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.Executors
import kotlin.math.max
import kotlin.math.min

class FletAudioServicePlugin : FlutterPlugin, MethodChannel.MethodCallHandler {

    companion object {
        private const val TAG = "FletAudioServiceDsp"
        private const val CHANNEL = "com.flet.flet_audio_service/decode"
        // Target rate for the analyser. Keeps tempo/timbre features intact while
        // cutting PCM size: 2x vs 44.1k. 22050 Hz is also the librosa default,
        // so any reference checks line up.
        private const val TARGET_SAMPLE_RATE = 22050
        // Decode window. v3 set this to 120 s to match the offload script's
        // canonical window (utils/dsp.py:MAX_SECONDS). On-device decode is
        // no longer the primary feature-extraction path — the laptop
        // offload script owns that — but we keep this constant in sync so
        // any fallback / interactive analyse on-device produces features
        // comparable to offload output.
        private const val MAX_SECONDS = 120
        private const val DECODE_TIMEOUT_US = 10_000L
    }

    private var channel: MethodChannel? = null
    private var appContext: Context? = null
    // Single-thread executor: MediaCodec instances are not thread-safe and we
    // don't want concurrent decodes thrashing the device's hardware decoder.
    private val executor = Executors.newSingleThreadExecutor()
    // Artwork gets its own worker so a track change never waits behind a
    // multi-second PCM decode during library analysis.
    private val artExecutor = Executors.newSingleThreadExecutor()
    // MethodChannel.Result callbacks MUST be invoked on the main thread.
    // Replying from the decode worker silently drops the reply on some
    // Flutter/Android combos: the Dart side then sees `null`, emits a
    // decode_complete event without `ok`, and Python surfaces it as
    // "decode failed" even though the Kotlin decode ran to EOS cleanly.
    private val mainHandler = Handler(Looper.getMainLooper())

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        appContext = binding.applicationContext
        channel = MethodChannel(binding.binaryMessenger, CHANNEL)
        channel?.setMethodCallHandler(this)
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel?.setMethodCallHandler(null)
        channel = null
        appContext = null
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        if (call.method == "decodePcm") {
            val path = call.argument<String>("path")
            if (path.isNullOrEmpty()) {
                result.error("BAD_ARGS", "missing 'path'", null)
                return
            }
            executor.submit {
                try {
                    val out = decodePcm(path)
                    mainHandler.post { result.success(out) }
                } catch (t: Throwable) {
                    Log.e(TAG, "decodePcm failed for $path", t)
                    mainHandler.post {
                        result.error("DECODE_FAILED", t.message ?: "unknown", null)
                    }
                }
            }
        } else if (call.method == "showProgressNotification") {
            val job = call.argument<String>("job") ?: "library"
            val title = call.argument<String>("title") ?: ""
            val content = call.argument<String>("content") ?: ""
            val progress = call.argument<Int>("progress") ?: 0
            val total = call.argument<Int>("total") ?: 0
            val done = call.argument<Boolean>("done") ?: false

            showProgressNotification(job, title, content, progress, total, done)
            result.success(null)
        } else if (call.method == "resolveArtwork") {
            val audioPath = call.argument<String>("path") ?: ""
            val sidecar = call.argument<String>("sidecar")
            val key = call.argument<String>("key") ?: audioPath
            artExecutor.submit {
                val out = try {
                    resolveArtwork(audioPath, sidecar, key)
                } catch (t: Throwable) {
                    Log.w(TAG, "resolveArtwork failed for $audioPath", t)
                    null
                }
                mainHandler.post { result.success(out) }
            }
        } else {
            result.notImplemented()
        }
    }

    /**
     * Decodes the audio at [srcPath] to mono 16-bit little-endian PCM, written
     * raw (header-less) to a file in the app cache. Returns a map with the
     * output path, sample rate, and sample count.
     *
     * Resampling is a plain linear interpolation. That's good enough for the
     * analysis features we extract (RMS, spectral centroid, onset autocorr,
     * MFCC mean); none of them are sensitive to the small aliasing artefacts
     * a non-anti-aliased decimator introduces. If we ever add pitch detection
     * we should switch to a proper polyphase filter.
     */
    private fun decodePcm(srcPath: String): Map<String, Any> {
        val cacheDir = appContext?.cacheDir
            ?: throw IllegalStateException("no cache dir")
        val pcmDir = File(cacheDir, "dsp_pcm").apply { mkdirs() }
        // Use a stable filename derived from the input so repeated calls for
        // the same track overwrite cleanly and the Python side can locate it
        // without round-tripping the path.
        val outFile = File(pcmDir, "${srcPath.hashCode().toUInt()}.pcm")

        val extractor = MediaExtractor()
        extractor.setDataSource(srcPath)

        val (trackIndex, format) = selectAudioTrack(extractor)
            ?: throw IllegalStateException("no audio track in $srcPath")
        extractor.selectTrack(trackIndex)

        val mime = format.getString(MediaFormat.KEY_MIME)
            ?: throw IllegalStateException("track has no mime")
        val srcSampleRate = format.getInteger(MediaFormat.KEY_SAMPLE_RATE)
        val srcChannels = format.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
        val durationUs = if (format.containsKey(MediaFormat.KEY_DURATION))
            format.getLong(MediaFormat.KEY_DURATION) else 0L

        Log.d(TAG, "decodePcm: $srcPath ($mime, ${srcSampleRate}Hz, $srcChannels ch, ${durationUs/1_000_000}s)")

        // Force 16-bit PCM output.
        format.setInteger(MediaFormat.KEY_PCM_ENCODING, AudioFormat.ENCODING_PCM_16BIT)

        // Seek to the middle minus MAX_SECONDS/2, clamped to 0. Picking the
        // middle skips intros/outros that are often silent or atypical.
        val maxUs = MAX_SECONDS * 1_000_000L
        val startUs = if (durationUs > maxUs)
            (durationUs - maxUs) / 2 else 0L
        val endUs = if (durationUs > maxUs) startUs + maxUs else Long.MAX_VALUE
        if (startUs > 0) {
            Log.d(TAG, "decodePcm: seeking to ${startUs/1_000_000}s")
            extractor.seekTo(startUs, MediaExtractor.SEEK_TO_CLOSEST_SYNC)
        }

        // MediaCodec creation can transiently fail when the foreground
        // player still holds a hardware codec instance; Android logs
        // `Failed to query component interface for required system
        // resources: 6`. Retry with a small back-off so a brief overlap
        // doesn't kill the whole DSP run. Python pauses playback before
        // calling us, but the framework needs a beat to actually free
        // the slot, and on some devices the second/third decode in a
        // batch hits the same window even after the first succeeded.
        val codec = run {
            var lastErr: Throwable? = null
            for (attempt in 0 until 4) {
                try {
                    val c = MediaCodec.createDecoderByType(mime)
                    try {
                        c.configure(format, null, null, 0)
                        c.start()
                        Log.d(TAG, "decodePcm: codec started on attempt $attempt")
                        return@run c
                    } catch (t: Throwable) {
                        try { c.release() } catch (_: Throwable) {}
                        throw t
                    }
                } catch (t: Throwable) {
                    lastErr = t
                    Log.w(TAG, "decodePcm: codec init attempt $attempt failed: ${t.message}")
                    if (attempt < 3) {
                        // 250 ms, 500 ms, 1000 ms back-off.
                        Thread.sleep(250L * (1L shl attempt))
                    }
                }
            }
            throw IllegalStateException(
                "MediaCodec unavailable after retries: ${lastErr?.message}",
                lastErr,
            )
        }

        val raf = RandomAccessFile(outFile, "rw")
        raf.setLength(0)
        val outChannel = raf.channel

        // Resample state. We work in the source-rate timeline:
        //   srcIndex   = number of mono source-rate samples consumed (1-indexed
        //                after each iteration: lastSample is at srcIndex,
        //                prevSample is at srcIndex - 1).
        //   nextOutAt  = position (in source-rate units) where the next output
        //                sample should land. Each emitted output advances this
        //                by `ratio`. Doubles give us sub-sample precision and
        //                no drift over the ~1.3M-sample window we cap at.
        val ratio = srcSampleRate.toDouble() / TARGET_SAMPLE_RATE.toDouble()
        var srcIndex = 0L
        var nextOutAt = 0.0
        var lastSample: Short = 0  // sample at srcIndex
        var prevSample: Short = 0  // sample at srcIndex - 1
        // Output buffer batched to disk in 256KB chunks for better I/O performance.
        // Use Direct buffer for zero-copy file I/O writes.
        val outBuf = ByteBuffer.allocateDirect(256 * 1024).order(ByteOrder.LITTLE_ENDIAN)
        var totalOutSamples = 0L

        // Reusable array to hold decoded shorts, avoiding allocations in the hot loop
        var tempShortArray = ShortArray(0)

        val info = MediaCodec.BufferInfo()
        var inputDone = false
        var outputDone = false
        var lastProgressLogUs = startUs
        var lastDataActivityMs = System.currentTimeMillis()

        try {
            while (!outputDone) {
                var activity = false
                if (!inputDone) {
                    val inIdx = codec.dequeueInputBuffer(DECODE_TIMEOUT_US)
                    if (inIdx >= 0) {
                        activity = true
                        val inBuf = codec.getInputBuffer(inIdx)!!
                        val sampleSize = extractor.readSampleData(inBuf, 0)
                        val sampleTime = extractor.sampleTime
                        if (sampleSize < 0 || sampleTime > endUs) {
                            codec.queueInputBuffer(
                                inIdx, 0, 0, 0,
                                MediaCodec.BUFFER_FLAG_END_OF_STREAM
                            )
                            inputDone = true
                        } else {
                            codec.queueInputBuffer(inIdx, 0, sampleSize, sampleTime, 0)
                            extractor.advance()
                        }
                    }
                }

                val outIdx = codec.dequeueOutputBuffer(info, DECODE_TIMEOUT_US)
                when {
                    outIdx >= 0 -> {
                        activity = true
                        lastDataActivityMs = System.currentTimeMillis()
                        if (info.size > 0) {
                            val pcmBuf = codec.getOutputBuffer(outIdx)!!
                            pcmBuf.position(info.offset)
                            pcmBuf.limit(info.offset + info.size)

                            // Periodic progress logging (every 10s of audio)
                            if (info.presentationTimeUs - lastProgressLogUs > 10_000_000L) {
                                Log.d(TAG, "decodePcm: progress ${info.presentationTimeUs / 1_000_000}s")
                                lastProgressLogUs = info.presentationTimeUs
                            }
                            // PCM 16-bit interleaved per Android docs for
                            // OUTPUT_FORMAT_AUDIO_PCM_16BIT (the default).
                            val shorts = pcmBuf.order(ByteOrder.LITTLE_ENDIAN)
                                .asShortBuffer()
                            val remaining = shorts.remaining()
                            val frameCount = remaining / srcChannels

                            // Ensure our reusable array is large enough
                            if (tempShortArray.size < remaining) {
                                tempShortArray = ShortArray(remaining)
                            }
                            // Bulk JNI transfer to local JVM heap memory
                            shorts.get(tempShortArray, 0, remaining)

                            var arrayIdx = 0
                            for (f in 0 until frameCount) {
                                // Downmix: average channels into a single mono
                                // sample, clamped to int16 range.
                                var acc = 0
                                for (c in 0 until srcChannels) {
                                    acc += tempShortArray[arrayIdx++].toInt()
                                }
                                val mono = (acc / srcChannels)
                                    .coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
                                    .toShort()
                                prevSample = lastSample
                                lastSample = mono
                                srcIndex++
                                // Emit every output sample whose position lies
                                // in [srcIndex - 1, srcIndex]. Both endpoint
                                // sample values are now known (prev, last).
                                while (nextOutAt <= srcIndex) {
                                    val frac = nextOutAt - (srcIndex - 1)
                                    // Optimized linear interpolation math: 1 multiplication instead of 2
                                    val interp = (prevSample + (lastSample - prevSample) * frac)
                                        .toInt()
                                        .coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
                                    if (outBuf.remaining() < 2) {
                                        outBuf.flip()
                                        outChannel.write(outBuf)
                                        outBuf.clear()
                                    }
                                    outBuf.putShort(interp.toShort())
                                    totalOutSamples++
                                    nextOutAt += ratio
                                }
                            }
                        }
                        codec.releaseOutputBuffer(outIdx, false)
                        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
                            outputDone = true
                        }
                    }
                    outIdx == MediaCodec.INFO_TRY_AGAIN_LATER -> {
                        // If no activity on either input or output, sleep briefly
                        // to avoid pegged CPU consumption which can actually
                        // slow down the hardware codec.
                        if (!activity) {
                            Thread.sleep(1)
                            // Hang detection: if no data for 5s while expecting it, bail.
                            if (System.currentTimeMillis() - lastDataActivityMs > 5000L) {
                                throw IllegalStateException("MediaCodec hang detected (no output for 5s)")
                            }
                        }
                    }
                    outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        // Some codecs emit this once after start(). The new
                        // format has authoritative sample-rate/channels but in
                        // practice it matches what we read above. Ignore.
                    }
                }
            }
            if (outBuf.position() > 0) {
                outBuf.flip()
                outChannel.write(outBuf)
            }
        } finally {
            try { codec.stop() } catch (_: Throwable) {}
            try { codec.release() } catch (_: Throwable) {}
            try { extractor.release() } catch (_: Throwable) {}
            try { outChannel.close() } catch (_: Throwable) {}
            try { raf.close() } catch (_: Throwable) {}
        }

        val finalSize = try { outFile.length() } catch (_: Throwable) { -1L }
        Log.d(TAG, "decodePcm: DONE path=${outFile.absolutePath} bytes=$finalSize samples=$totalOutSamples")
        return mapOf(
            "ok" to true,
            "output_path" to outFile.absolutePath,
            "sample_rate" to TARGET_SAMPLE_RATE,
            "num_samples" to totalOutSamples,
            "channels" to 1,
            "src_sample_rate" to srcSampleRate,
            "src_channels" to srcChannels,
        )
    }

    private fun selectAudioTrack(extractor: MediaExtractor): Pair<Int, MediaFormat>? {
        for (i in 0 until extractor.trackCount) {
            val fmt = extractor.getTrackFormat(i)
            val mime = fmt.getString(MediaFormat.KEY_MIME) ?: continue
            if (mime.startsWith("audio/")) return Pair(i, fmt)
        }
        return null
    }

    // ── Now-playing artwork ────────────────────────────────────────────────
    // audio_service decodes MediaItem.artUri itself, but on our side that URI
    // was only ever a sidecar cover.jpg in shared storage: tracks with embedded
    // art got none, and on Android 13+ a sidecar image is unreadable without
    // READ_MEDIA_IMAGES / All-files access (we only hold READ_MEDIA_AUDIO), so
    // BitmapFactory silently returned null. Resolve here instead: embedded
    // picture first (readable under READ_MEDIA_AUDIO), sidecar as fallback,
    // downscaled into the app's private cache where audio_service can always
    // read it. Cached per album key, so each album is decoded once.

    private val artMaxPx = 512

    private fun resolveArtwork(audioPath: String, sidecar: String?, key: String): String? {
        val context = appContext ?: return null
        val dir = File(context.cacheDir, "np_art").apply { mkdirs() }
        val out = File(dir, sha1(key) + ".jpg")
        // The key is album-level and the file persists across launches, so an
        // art or tag edit (metadata workbench) must invalidate it: reuse only
        // while neither the audio file nor the sidecar is newer than the cache.
        // lastModified() is one stat and returns 0 when unreadable (→ reuse).
        if (out.exists() && out.length() > 0) {
            val audioMod = if (audioPath.isNotEmpty() && !audioPath.startsWith("http"))
                File(audioPath).lastModified() else 0L
            val sideMod = if (!sidecar.isNullOrEmpty()) File(sidecar).lastModified() else 0L
            if (maxOf(audioMod, sideMod) <= out.lastModified()) return out.absolutePath
        }

        var bitmap: Bitmap? = null
        if (audioPath.isNotEmpty() && !audioPath.startsWith("http")) {
            val mmr = MediaMetadataRetriever()
            try {
                mmr.setDataSource(audioPath)
                mmr.embeddedPicture?.let { bytes -> bitmap = decodeScaled(bytes, null) }
            } catch (t: Throwable) {
                Log.d(TAG, "no embedded art for $audioPath: ${t.message}")
            } finally {
                try { mmr.release() } catch (_: Throwable) {}
            }
        }
        if (bitmap == null && !sidecar.isNullOrEmpty()) {
            try {
                bitmap = decodeScaled(null, sidecar)
            } catch (t: Throwable) {
                Log.d(TAG, "sidecar art unreadable $sidecar: ${t.message}")
            }
        }
        val bmp = bitmap ?: return null
        val tmp = File(dir, out.name + ".tmp")
        FileOutputStream(tmp).use { bmp.compress(Bitmap.CompressFormat.JPEG, 88, it) }
        bmp.recycle()
        return if (tmp.renameTo(out)) out.absolutePath else null
    }

    private fun decodeScaled(bytes: ByteArray?, path: String?): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        if (bytes != null) BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        else BitmapFactory.decodeFile(path, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        var sample = 1
        while (bounds.outWidth / (sample * 2) >= artMaxPx && bounds.outHeight / (sample * 2) >= artMaxPx) {
            sample *= 2
        }
        val opts = BitmapFactory.Options().apply { inSampleSize = sample }
        val decoded = (if (bytes != null) BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
                       else BitmapFactory.decodeFile(path, opts)) ?: return null
        val longest = max(decoded.width, decoded.height)
        if (longest <= artMaxPx) return decoded
        val scale = artMaxPx.toFloat() / longest
        val scaled = Bitmap.createScaledBitmap(
            decoded, (decoded.width * scale).toInt(), (decoded.height * scale).toInt(), true)
        if (scaled !== decoded) decoded.recycle()
        return scaled
    }

    private fun sha1(s: String): String =
        MessageDigest.getInstance("SHA-1").digest(s.toByteArray())
            .joinToString("") { "%02x".format(it) }

    // ── Library-task progress notifications ────────────────────────────────
    // One notification per job ("dsp", "metadata", ...) so concurrent tasks
    // don't overwrite each other. On Android 16+ (API 36) this uses the
    // platform ProgressStyle; older releases get the classic progress bar.
    // Deliberately NOT promoted to a Live Update: a library scan isn't the kind
    // of time-sensitive activity the status-bar chip is meant for.

    private val progressChannelId = "library_tasks"

    private fun notificationIdFor(job: String): Int = when (job) {
        "dsp" -> 9999          // kept: the id the old single-job version used
        "metadata" -> 9998
        else -> 9000 + (job.hashCode() and 0x3FF)
    }

    private fun showProgressNotification(
        job: String, title: String, content: String, progress: Int, total: Int, done: Boolean,
    ) {
        val context = appContext ?: return
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val notificationId = notificationIdFor(job)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(
                NotificationChannel(
                    progressChannelId, "Library tasks", NotificationManager.IMPORTANCE_LOW
                ).apply {
                    description = "Progress of track analysis and metadata sync"
                    setShowBadge(false)
                }
            )
            // Superseded by library_tasks; drop it so Settings doesn't list a dead channel.
            nm.deleteNotificationChannel("dsp_scan_channel")
        }
        if (!nm.areNotificationsEnabled()) {
            Log.w(TAG, "notifications disabled for app; progress for '$job' not shown")
        }

        if (done && content.isEmpty()) {
            nm.cancel(notificationId)
            return
        }

        val launch = context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?.apply { flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP }
        val tapIntent = launch?.let {
            PendingIntent.getActivity(
                context, notificationId, it,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        }

        val notification: Notification =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
                val b = Notification.Builder(context, progressChannelId)
                    .setSmallIcon(R.drawable.ic_stat_library)
                    .setContentTitle(title)
                    .setContentText(content)
                    .setOnlyAlertOnce(true)
                    .setCategory(Notification.CATEGORY_PROGRESS)
                if (tapIntent != null) b.setContentIntent(tapIntent)
                if (done) {
                    // Final state: leave a short-lived, dismissible summary.
                    b.setOngoing(false).setAutoCancel(true).setTimeoutAfter(15_000)
                } else {
                    val style = Notification.ProgressStyle()
                    if (total > 0) {
                        style.setProgressSegments(listOf(Notification.ProgressStyle.Segment(total)))
                        style.setProgress(progress.coerceIn(0, total))
                    } else {
                        style.setProgressIndeterminate(true)
                    }
                    b.setStyle(style).setOngoing(true)
                }
                b.build()
            } else {
                val b = NotificationCompat.Builder(context, progressChannelId)
                    .setSmallIcon(R.drawable.ic_stat_library)
                    .setContentTitle(title)
                    .setContentText(content)
                    .setOnlyAlertOnce(true)
                    .setCategory(NotificationCompat.CATEGORY_PROGRESS)
                if (tapIntent != null) b.setContentIntent(tapIntent)
                if (done) {
                    b.setOngoing(false).setAutoCancel(true).setTimeoutAfter(15_000)
                } else {
                    b.setOngoing(true)
                    if (total > 0) b.setProgress(total, progress.coerceIn(0, total), false)
                    else b.setProgress(0, 0, true)
                }
                b.build()
            }

        try {
            nm.notify(notificationId, notification)
        } catch (e: SecurityException) {
            // POST_NOTIFICATIONS revoked (Android 13+).
            Log.w(TAG, "notify denied for '$job': ${e.message}")
        }
    }
}
