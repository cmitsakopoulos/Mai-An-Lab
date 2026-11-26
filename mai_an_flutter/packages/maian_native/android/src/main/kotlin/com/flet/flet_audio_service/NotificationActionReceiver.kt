package com.flet.flet_audio_service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Handles notification actions (Cancel) and user swipe dismissals.
 * Fired via explicit PendingIntents from notification action buttons
 * and deleteIntents.
 */
class NotificationActionReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "FletNotifReceiver"
        const val ACTION_CANCEL_TASK = "com.flet.flet_audio_service.ACTION_CANCEL_TASK"
        const val ACTION_NOTIFICATION_DISMISSED = "com.flet.flet_audio_service.ACTION_NOTIFICATION_DISMISSED"
        const val EXTRA_JOB = "extra_job"
    }

    override fun onReceive(context: Context, intent: Intent?) {
        if (intent == null) return
        val action = intent.action ?: return
        val job = intent.getStringExtra(EXTRA_JOB) ?: "unknown"
        Log.d(TAG, "onReceive: action=$action job=$job")

        when (action) {
            ACTION_CANCEL_TASK -> {
                Log.i(TAG, "User clicked Cancel on notification for job: $job")
                FletAudioServicePlugin.handleNotificationAction(job, "cancel")
            }
            ACTION_NOTIFICATION_DISMISSED -> {
                Log.i(TAG, "User swiped away notification for job: $job")
                FletAudioServicePlugin.handleNotificationDismissed(job)
            }
        }
    }
}

