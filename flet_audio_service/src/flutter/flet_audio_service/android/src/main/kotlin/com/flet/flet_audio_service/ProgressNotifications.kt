package com.flet.flet_audio_service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat

/**
 * Builds the library-task progress notifications, shared by the per-job path
 * in the plugin and LibraryTaskService's foreground notification. On Android
 * 16+ (API 36) this uses the platform ProgressStyle; older releases get the
 * classic progress bar. Deliberately NOT promoted to a Live Update: a library
 * scan isn't the kind of time-sensitive activity the status-bar chip is for.
 */
object ProgressNotifications {
    const val CHANNEL_ID = "library_tasks"

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID, "Library tasks", NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Progress of downloads, track analysis and metadata sync"
                setShowBadge(false)
            }
        )
        // Superseded by library_tasks; drop it so Settings doesn't list a dead channel.
        nm.deleteNotificationChannel("dsp_scan_channel")
    }

    fun build(
        context: Context, notificationId: Int, title: String, content: String,
        progress: Int, total: Int, done: Boolean,
    ): Notification {
        ensureChannel(context)
        val launch = context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?.apply { flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP }
        val tapIntent = launch?.let {
            PendingIntent.getActivity(
                context, notificationId, it,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        }

        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            val b = Notification.Builder(context, CHANNEL_ID)
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
            val b = NotificationCompat.Builder(context, CHANNEL_ID)
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
    }
}
