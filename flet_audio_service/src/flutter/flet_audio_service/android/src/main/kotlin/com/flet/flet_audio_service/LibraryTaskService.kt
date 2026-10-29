package com.flet.flet_audio_service

import android.app.Notification
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/**
 * Keeps the process alive while long tasks (downloads, device sync,
 * track analysis, metadata sync) run, so Android doesn't freeze or kill
 * them when the screen goes off.
 *
 * Driven by the progress notification bridge: the first running job starts
 * the service, and the last finished one stops it.
 */
class LibraryTaskService : Service() {

    data class Job(
        val jobKey: String,
        val title: String,
        val content: String,
        val progress: Int,
        val total: Int
    )

    companion object {
        private const val TAG = "FletLibraryTasks"
        const val ANCHOR_NOTIFICATION_ID = 9990
        private const val STALE_MS = 20 * 60 * 1000L
        private const val RETRY_START_MS = 15_000L

        /** Running jobs, keyed by jobKey ("download", "sync", "dsp", "metadata"). */
        val jobs = LinkedHashMap<String, Job>()

        private var instance: LibraryTaskService? = null
        private var startPending = false
        private var lastRefusedAt = 0L

        fun notificationIdFor(job: String): Int = when (job) {
            "download" -> 9100
            "sync" -> 9200
            "dsp" -> 9300
            "metadata" -> 9400
            else -> 9000 + (job.hashCode() and 0x3FF)
        }

        fun update(context: Context, job: String, state: Job): Boolean {
            jobs[job] = state
            instance?.let {
                it.refresh()
                return true
            }
            if (startPending) return true
            val now = SystemClock.elapsedRealtime()
            if (lastRefusedAt != 0L && now - lastRefusedAt < RETRY_START_MS) return false
            return try {
                ContextCompat.startForegroundService(
                    context, Intent(context, LibraryTaskService::class.java))
                startPending = true
                true
            } catch (t: Throwable) {
                lastRefusedAt = now
                Log.w(TAG, "foreground start refused for '$job': ${t.message}")
                false
            }
        }

        fun finish(job: String) {
            jobs.remove(job)
            val svc = instance ?: return
            if (jobs.isEmpty()) {
                svc.stopNow()
            } else {
                svc.refresh()
            }
        }
    }

    private val handler = Handler(Looper.getMainLooper())
    private var wakeLock: PowerManager.WakeLock? = null
    private val staleCheck = Runnable {
        Log.w(TAG, "no progress for ${STALE_MS / 60000} min; releasing ${jobs.keys}")
        jobs.clear()
        stopNow()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startPending = false
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0

        try {
            ServiceCompat.startForeground(this, ANCHOR_NOTIFICATION_ID, buildAnchorNotification(), type)
        } catch (t: Throwable) {
            Log.w(TAG, "startForeground failed: ${t.message}")
            lastRefusedAt = SystemClock.elapsedRealtime()
            stopSelf()
            return START_NOT_STICKY
        }
        instance = this
        if (jobs.isEmpty()) {
            stopNow()
            return START_NOT_STICKY
        }
        refresh()
        return START_NOT_STICKY
    }

    fun refresh() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val running = jobs.values.toList()

        if (running.isEmpty()) {
            stopNow()
            return
        }

        // Update foreground anchor notification
        try {
            nm.notify(ANCHOR_NOTIFICATION_ID, buildAnchorNotification())
        } catch (e: SecurityException) {
            Log.w(TAG, "notify denied: ${e.message}")
        }

        // If multiple jobs are running, update individual cards for non-dismissed jobs
        if (running.size > 1) {
            for (j in running) {
                if (FletAudioServicePlugin.dismissedJobs.contains(j.jobKey)) continue
                val id = notificationIdFor(j.jobKey)
                try {
                    nm.notify(
                        id,
                        ProgressNotifications.build(
                            this, id, j.jobKey, j.title, j.content, j.progress, j.total, done = false
                        )
                    )
                } catch (e: SecurityException) {
                    Log.w(TAG, "notify denied for ${j.jobKey}: ${e.message}")
                }
            }
        } else {
            // When single job is active, clear individual notification ID to avoid duplicate with anchor
            val single = running.first()
            val singleId = notificationIdFor(single.jobKey)
            if (singleId != ANCHOR_NOTIFICATION_ID) {
                nm.cancel(singleId)
            }
        }

        // Maintain wakelock
        val wl = wakeLock ?: (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MaiAnLab:library_tasks")
            .apply { setReferenceCounted(false) }
            .also { wakeLock = it }
        wl.acquire(STALE_MS)

        handler.removeCallbacks(staleCheck)
        handler.postDelayed(staleCheck, STALE_MS)
    }

    fun stopNow() {
        handler.removeCallbacks(staleCheck)
        wakeLock?.let { if (it.isHeld) it.release() }
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        if (instance === this) instance = null
        stopSelf()
    }

    /** Android 15+ caps dataSync at 6 hours a day; past it we must stop cleanly. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        Log.w(TAG, "dataSync time limit reached; notifying Python and stopping gracefully")
        FletAudioServicePlugin.handleNotificationAction("all", "fgs_timeout")
        stopNow()
    }

    override fun onDestroy() {
        handler.removeCallbacks(staleCheck)
        wakeLock?.let { if (it.isHeld) it.release() }
        if (instance === this) instance = null
        super.onDestroy()
    }

    private fun buildAnchorNotification(): Notification {
        val running = jobs.values.toList()
        if (running.size == 1) {
            val single = running.first()
            return ProgressNotifications.build(
                this,
                ANCHOR_NOTIFICATION_ID,
                single.jobKey,
                single.title,
                single.content,
                single.progress,
                single.total,
                done = false
            )
        }

        val count = running.size
        val title = "$count Background Tasks Active"
        val content = running.joinToString(" • ") { "${it.title}: ${it.content}" }
        return ProgressNotifications.build(
            this,
            ANCHOR_NOTIFICATION_ID,
            "library",
            title,
            content,
            0,
            0,
            done = false
        )
    }
}
