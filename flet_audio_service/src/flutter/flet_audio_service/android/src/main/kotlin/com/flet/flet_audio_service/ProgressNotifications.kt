package com.flet.flet_audio_service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat

/**
 * Modern, visually appealing Android progress notifications for long-running
 * background tasks (Downloads, Device Sync, DSP Analysis, Metadata Sync).
 *
 * Provides:
 * - Granular notification channels with silent LOW importance.
 * - Dynamic color accents and tailored vector silhouette icons per job.
 * - Structured text hierarchy: setSubText (category/ETA), setContentTitle (milestone), setContentText (details).
 * - Inline [Cancel] action button with PendingIntent to NotificationActionReceiver.
 * - Swipe-away dismissal tracking via deleteIntent.
 * - Deep-link navigation via tap intent.
 * - Robust Android 14/15/16 compatibility with graceful fallback.
 */
object ProgressNotifications {
    private const val TAG = "FletProgressNotifs"

    const val CHANNEL_DOWNLOADS = "channel_downloads"
    const val CHANNEL_SYNC = "channel_sync"
    const val CHANNEL_DSP = "channel_dsp"
    const val CHANNEL_METADATA = "channel_metadata"
    const val CHANNEL_FALLBACK = "library_tasks"

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

        fun create(id: String, name: String, desc: String) {
            val ch = NotificationChannel(id, name, NotificationManager.IMPORTANCE_LOW).apply {
                description = desc
                setShowBadge(false)
                setSound(null, null)
                enableVibration(false)
            }
            nm.createNotificationChannel(ch)
        }

        create(CHANNEL_DOWNLOADS, "Downloads", "Track and album downloads from streaming services and companion devices")
        create(CHANNEL_SYNC, "Device Sync", "Wi-Fi sync, DSP offloading, and backup transfers with Mac")
        create(CHANNEL_DSP, "Audio Analysis", "DSP acoustic feature extraction and similarity graph building")
        create(CHANNEL_METADATA, "Metadata Sync", "Artist details and album metadata enrichment")
        create(CHANNEL_FALLBACK, "Library Tasks", "Background library maintenance tasks")

        // Clean up legacy channel
        try {
            nm.deleteNotificationChannel("dsp_scan_channel")
        } catch (_: Throwable) {}
    }

    fun channelFor(job: String): String = when (job) {
        "download" -> CHANNEL_DOWNLOADS
        "sync" -> CHANNEL_SYNC
        "dsp" -> CHANNEL_DSP
        "metadata" -> CHANNEL_METADATA
        else -> CHANNEL_FALLBACK
    }

    fun iconFor(job: String): Int = when (job) {
        "download" -> R.drawable.ic_stat_download
        "sync" -> R.drawable.ic_stat_sync
        "dsp" -> R.drawable.ic_stat_dsp
        "metadata" -> R.drawable.ic_stat_metadata
        else -> R.drawable.ic_stat_library
    }

    fun colorFor(job: String): Int = when (job) {
        "download" -> 0xFF1DB954.toInt() // Spotify Green
        "sync" -> 0xFF00E5FF.toInt()     // Electric Cyan
        "dsp" -> 0xFFFFB300.toInt()      // Amber Gold
        "metadata" -> 0xFF7C4DFF.toInt() // Violet / Indigo
        else -> 0xFF00E5FF.toInt()
    }

    fun displayNameFor(job: String): String = when (job) {
        "download" -> "Downloads"
        "sync" -> "Device Sync"
        "dsp" -> "Audio Analysis"
        "metadata" -> "Metadata Sync"
        else -> "Library Task"
    }

    fun build(
        context: Context,
        notificationId: Int,
        job: String,
        title: String,
        content: String,
        progress: Int,
        total: Int,
        done: Boolean,
    ): Notification {
        ensureChannels(context)
        val channelId = channelFor(job)
        val iconRes = iconFor(job)
        val accentColor = colorFor(job)
        val jobName = displayNameFor(job)

        // ── Deep-Link Tap Intent ───────────────────────────────────────────
        val launch = context.packageManager.getLaunchIntentForPackage(context.packageName)?.apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("navigate_to", when (job) {
                "download" -> "downloads"
                "sync" -> "sync"
                "dsp" -> "library"
                "metadata" -> "workbench"
                else -> "home"
            })
        }
        val tapIntent = launch?.let {
            PendingIntent.getActivity(
                context, notificationId, it,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        }

        // ── Cancel Action Intent ───────────────────────────────────────────
        val cancelPending = if (!done) {
            val cancelIntent = Intent(context, NotificationActionReceiver::class.java).apply {
                action = NotificationActionReceiver.ACTION_CANCEL_TASK
                putExtra(NotificationActionReceiver.EXTRA_JOB, job)
            }
            PendingIntent.getBroadcast(
                context, notificationId, cancelIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        } else null

        // ── Swipe-Away Dismissal (Delete) Intent ───────────────────────────
        val deletePending = if (!done) {
            val deleteIntent = Intent(context, NotificationActionReceiver::class.java).apply {
                action = NotificationActionReceiver.ACTION_NOTIFICATION_DISMISSED
                putExtra(NotificationActionReceiver.EXTRA_JOB, job)
            }
            PendingIntent.getBroadcast(
                context, notificationId + 50000, deleteIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        } else null

        // ── Subtext & Visual Typography ────────────────────────────────────
        val subText = if (done) {
            "$jobName • Finished"
        } else if (total > 0) {
            val pct = (progress.toLong() * 100 / total).toInt().coerceIn(0, 100)
            "$jobName • $pct%"
        } else {
            jobName
        }

        val displayTitle = if (title.isNotEmpty()) title else jobName
        val displayContent = content

        // ── Android 16 (Baklava) ProgressStyle with Safe Fallback ───────────
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            try {
                val b = Notification.Builder(context, channelId)
                    .setSmallIcon(iconRes)
                    .setColor(accentColor)
                    .setContentTitle(displayTitle)
                    .setContentText(displayContent)
                    .setSubText(subText)
                    .setOnlyAlertOnce(true)
                    .setCategory(Notification.CATEGORY_PROGRESS)
                if (tapIntent != null) b.setContentIntent(tapIntent)
                if (deletePending != null) b.setDeleteIntent(deletePending)

                if (done) {
                    b.setOngoing(false).setAutoCancel(true).setTimeoutAfter(12_000)
                } else {
                    val style = Notification.ProgressStyle()
                    if (total > 0) {
                        style.setProgressSegments(listOf(Notification.ProgressStyle.Segment(total)))
                        style.setProgress(progress.coerceIn(0, total))
                    } else {
                        style.setProgressIndeterminate(true)
                    }
                    b.setStyle(style).setOngoing(true)
                    if (cancelPending != null) {
                        b.addAction(
                            Notification.Action.Builder(
                                R.drawable.ic_action_cancel, "Cancel", cancelPending
                            ).build()
                        )
                    }
                }
                return b.build()
            } catch (t: Throwable) {
                Log.w(TAG, "ProgressStyle build failed on Baklava, falling back to NotificationCompat", t)
            }
        }

        // ── Standard NotificationCompat (Android 8.0 - 15) ─────────────────
        val b = NotificationCompat.Builder(context, channelId)
            .setSmallIcon(iconRes)
            .setColor(accentColor)
            .setContentTitle(displayTitle)
            .setContentText(displayContent)
            .setSubText(subText)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)

        if (tapIntent != null) b.setContentIntent(tapIntent)
        if (deletePending != null) b.setDeleteIntent(deletePending)

        if (done) {
            b.setOngoing(false).setAutoCancel(true).setTimeoutAfter(12_000)
        } else {
            b.setOngoing(true)
            if (total > 0) {
                b.setProgress(total, progress.coerceIn(0, total), false)
            } else {
                b.setProgress(0, 0, true)
            }
            if (cancelPending != null) {
                b.addAction(R.drawable.ic_action_cancel, "Cancel", cancelPending)
            }
        }

        return b.build()
    }
}
