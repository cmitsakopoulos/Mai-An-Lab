package com.flet.flet_audio_service

import android.app.Notification
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
 * Keeps the process alive while long library jobs (downloads, metadata sync,
 * track analysis) run, so Android doesn't freeze or kill them when the screen
 * goes off. A plain notification keeps nothing alive. The media service only
 * pins the process while it holds the foreground, i.e. never without music,
 * and not after the player's idle release.
 *
 * Driven entirely by the existing progress-notification bridge: the first
 * running job starts the service, and the last finished one stops it. Its
 * foreground notification IS the progress notification (one job looks exactly
 * like before; several share it), so the user never sees a duplicate. Done
 * summaries are still posted per job by the plugin.
 *
 * All state is touched on the main thread only (method-channel calls and
 * service callbacks both run there).
 */
class LibraryTaskService : Service() {

    data class Job(val title: String, val content: String, val progress: Int, val total: Int)

    companion object {
        private const val TAG = "FletLibraryTasks"
        const val NOTIFICATION_ID = 9990
        // No update for this long ⇒ the job's owner died without finishing it
        // (a crashed Python task). Drop it rather than pin the process forever.
        // Jobs post at least on every visible percent step, far more often.
        private const val STALE_MS = 20 * 60 * 1000L
        // A refused start (app in the background on Android 12+) is retried
        // at most this often, so a job ticking every second doesn't throw on
        // every tick; the next update after the app is reopened succeeds.
        private const val RETRY_START_MS = 15_000L

        /** Running jobs, oldest first: the oldest one's bar is shown. */
        val jobs = LinkedHashMap<String, Job>()

        private var instance: LibraryTaskService? = null
        private var startPending = false
        private var lastRefusedAt = 0L

        /**
         * Record [job]'s progress and make sure the service holds the
         * foreground. Returns true when the foreground notification now shows
         * it, false when the service couldn't start, in which case the caller
         * posts the per-job notification itself (the old behaviour).
         */
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
                // ForegroundServiceStartNotAllowedException (API 31+) when the
                // job starts while the app is in the background.
                lastRefusedAt = now
                Log.w(TAG, "foreground start refused for '$job': ${t.message}")
                false
            }
        }

        /** Forget [job]; stops the service when it was the last one. */
        fun finish(job: String) {
            if (jobs.remove(job) == null) return
            val svc = instance ?: return
            if (jobs.isEmpty()) svc.stopNow() else svc.refresh()
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
        // startForeground is mandatory after startForegroundService, even if
        // every job finished in the meantime; stop right after in that case.
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, buildNotification(), type)
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
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.notify(NOTIFICATION_ID, buildNotification())
        // Partial wakelock so the CPU (and the job's network I/O) keeps going
        // with the screen off. Re-acquired with a timeout on every update, so
        // it lapses by itself if updates ever stop.
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

    /** Android 15+ caps dataSync at 6 h a day; past it we must stop at once. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        Log.w(TAG, "dataSync time limit reached; jobs continue unprotected")
        stopNow()
    }

    override fun onDestroy() {
        handler.removeCallbacks(staleCheck)
        wakeLock?.let { if (it.isHeld) it.release() }
        if (instance === this) instance = null
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val running = jobs.values.toList()
        val first = running.firstOrNull() ?: Job("Library tasks", "Finishing…", 0, 0)
        val title = if (running.size > 1) "${running.size} library tasks" else first.title
        val content = if (running.size > 1)
            running.joinToString(" · ") { "${it.title}: ${it.content}" }
        else first.content
        return ProgressNotifications.build(
            this, NOTIFICATION_ID, title, content, first.progress, first.total, done = false)
    }
}
