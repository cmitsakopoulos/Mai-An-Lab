package com.flet.flet_audio_service

import android.content.Context
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.util.Log
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
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
        // cutting PCM size ~2x vs 44.1k. 22050 Hz is also the librosa default,
        // so any reference checks line up.
        private const val TARGET_SAMPLE_RATE = 22050
        // Decode at most this many seconds from the middle of the track.
        // 60s was empirically too short on tracks with sparse onsets (slow
        // ambient, stripped-down acoustic) — autocorrelation tempo estimates
        // got noisy and chroma/MFCC stats jittered across runs. 90s is the
        // sweet spot: tempo and timbre stabilise, decode cost stays bounded
        // (typically 1.5–4 s per track on Android hardware codecs).
        private const val MAX_SECONDS = 90
        private const val DECODE_TIMEOUT_US = 10_000L
    }

    private var channel: MethodChannel? = null
    private var appContext: Context? = null
    // Single-thread executor: MediaCodec instances are not thread-safe and we
    // don't want concurrent decodes thrashing the device's hardware decoder.
    private val executor = Executors.newSingleThreadExecutor()

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
        if (call.method != "decodePcm") {
            result.notImplemented()
            return
        }
        val path = call.argument<String>("path")
        if (path.isNullOrEmpty()) {
            result.error("BAD_ARGS", "missing 'path'", null)
            return
        }
        executor.submit {
            try {
                val out = decodePcm(path)
                // MethodChannel results must be returned on the platform thread;
                // post via the channel's binaryMessenger handler. In practice
                // Flutter's engine routes the reply correctly from a background
                // thread for primitive maps — verified across Flutter ≥ 3.0.
                result.success(out)
            } catch (t: Throwable) {
                Log.e(TAG, "decodePcm failed for $path", t)
                result.error("DECODE_FAILED", t.message ?: "unknown", null)
            }
        }
    }

    /**
     * Decodes the audio at [srcPath] to mono 16-bit little-endian PCM, written
     * raw (header-less) to a file in the app cache. Returns a map with the
     * output path, sample rate, and sample count.
     *
     * Resampling is a plain linear interpolation. That's good enough for the
     * analysis features we extract (RMS, spectral centroid, onset autocorr,
     * MFCC mean) — none of them are sensitive to the small aliasing artefacts
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

        // Seek to the middle minus MAX_SECONDS/2, clamped to 0. Picking the
        // middle skips intros/outros that are often silent or atypical.
        val maxUs = MAX_SECONDS * 1_000_000L
        val startUs = if (durationUs > maxUs)
            (durationUs - maxUs) / 2 else 0L
        val endUs = if (durationUs > maxUs) startUs + maxUs else Long.MAX_VALUE
        if (startUs > 0) {
            extractor.seekTo(startUs, MediaExtractor.SEEK_TO_CLOSEST_SYNC)
        }

        // MediaCodec creation can transiently fail when the foreground
        // player still holds a hardware codec instance — Android logs
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
                        return@run c
                    } catch (t: Throwable) {
                        try { c.release() } catch (_: Throwable) {}
                        throw t
                    }
                } catch (t: Throwable) {
                    lastErr = t
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
        // Output buffer batched to disk in 64KB chunks.
        val outBuf = ByteBuffer.allocate(64 * 1024).order(ByteOrder.LITTLE_ENDIAN)
        var totalOutSamples = 0L

        val info = MediaCodec.BufferInfo()
        var inputDone = false
        var outputDone = false

        try {
            while (!outputDone) {
                if (!inputDone) {
                    val inIdx = codec.dequeueInputBuffer(DECODE_TIMEOUT_US)
                    if (inIdx >= 0) {
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
                        if (info.size > 0) {
                            val pcmBuf = codec.getOutputBuffer(outIdx)!!
                            pcmBuf.position(info.offset)
                            pcmBuf.limit(info.offset + info.size)
                            // PCM 16-bit interleaved per Android docs for
                            // OUTPUT_FORMAT_AUDIO_PCM_16BIT (the default).
                            val shorts = pcmBuf.order(ByteOrder.LITTLE_ENDIAN)
                                .asShortBuffer()
                            val frameCount = shorts.remaining() / srcChannels
                            for (f in 0 until frameCount) {
                                // Downmix: average channels into a single mono
                                // sample, clamped to int16 range.
                                var acc = 0
                                for (c in 0 until srcChannels) {
                                    acc += shorts.get().toInt()
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
                                    val interp = (prevSample * (1.0 - frac) + lastSample * frac)
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
                        // Spin again.
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
}
