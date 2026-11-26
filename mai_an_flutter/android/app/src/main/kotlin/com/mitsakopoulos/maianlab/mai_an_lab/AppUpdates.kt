package com.mitsakopoulos.maianlab.mai_an_lab

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import java.io.File
import java.io.FileInputStream

/**
 * Installing a build of this app that the Mac Companion offered.
 *
 * PackageInstaller sessions, not an ACTION_VIEW of a file: no FileProvider,
 * and the result comes back to [InstallResultReceiver]. Android shows its
 * own confirmation (STATUS_PENDING_USER_ACTION) because the app was not
 * installed by us, and refuses an APK signed with a different key, so the
 * phone never installs anything the Mac's signing key didn't produce.
 */
object AppUpdates {
    /** Set by MainActivity while its engine is attached: (status, message). */
    @Volatile var listener: ((String, String?) -> Unit)? = null

    fun info(context: Context): Map<String, Any?> {
        val pm = context.packageManager
        val pi = if (Build.VERSION.SDK_INT >= 33) {
            pm.getPackageInfo(context.packageName, PackageManager.PackageInfoFlags.of(0))
        } else {
            @Suppress("DEPRECATION") pm.getPackageInfo(context.packageName, 0)
        }
        val code = if (Build.VERSION.SDK_INT >= 28) pi.longVersionCode
            else @Suppress("DEPRECATION") pi.versionCode.toLong()
        return mapOf(
            "package" to context.packageName,
            "versionCode" to code,
            "versionName" to (pi.versionName ?: ""),
            "apkPath" to context.applicationInfo.sourceDir,
            "lastUpdateTime" to pi.lastUpdateTime,
            "sdk" to Build.VERSION.SDK_INT,
        )
    }

    fun canInstall(context: Context): Boolean =
        Build.VERSION.SDK_INT < 26 || context.packageManager.canRequestPackageInstalls()

    /** Settings → Install unknown apps, for this app. */
    fun openInstallSettings(context: Context) {
        val i = if (Build.VERSION.SDK_INT >= 26) {
            Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:${context.packageName}"))
        } else {
            Intent(Settings.ACTION_SECURITY_SETTINGS)
        }
        context.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    /** Stream [path] into an install session and commit it. Blocking: call off the main thread. */
    fun install(context: Context, path: String) {
        val file = File(path)
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(
            PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            setSize(file.length())
            if (Build.VERSION.SDK_INT >= 31) {
                // Silent when Android allows it (we installed the current
                // build); otherwise it asks, as it must.
                setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
            }
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            FileInputStream(file).use { input ->
                session.openWrite("base.apk", 0, file.length()).use { out ->
                    input.copyTo(out, 1 shl 16)
                    session.fsync(out)
                }
            }
            val intent = Intent(context, InstallResultReceiver::class.java)
                .setAction("lab.maian.INSTALL_RESULT")
            var flags = PendingIntent.FLAG_UPDATE_CURRENT
            // The installer fills in the result extras: it must be mutable
            // (and explicit, which it is).
            if (Build.VERSION.SDK_INT >= 31) flags = flags or PendingIntent.FLAG_MUTABLE
            val pi = PendingIntent.getBroadcast(context, id, intent, flags)
            session.commit(pi.intentSender)
        }
    }
}

/** Session results: Android's confirmation screen, then success or why not. */
class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999)
        val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm: Intent? = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
                }
                if (confirm != null) {
                    context.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                }
                AppUpdates.listener?.invoke("confirm", null)
            }
            PackageInstaller.STATUS_SUCCESS -> AppUpdates.listener?.invoke("installed", null)
            PackageInstaller.STATUS_FAILURE_ABORTED ->
                AppUpdates.listener?.invoke("declined", message)
            else -> AppUpdates.listener?.invoke("failed", describe(status, message))
        }
    }

    private fun describe(status: Int, message: String?): String {
        val why = when (status) {
            PackageInstaller.STATUS_FAILURE_CONFLICT ->
                "it is signed with a different key than the installed app"
            PackageInstaller.STATUS_FAILURE_INCOMPATIBLE -> "it doesn't support this phone"
            PackageInstaller.STATUS_FAILURE_INVALID -> "the APK is invalid"
            PackageInstaller.STATUS_FAILURE_STORAGE -> "there isn't enough storage"
            PackageInstaller.STATUS_FAILURE_BLOCKED -> "the install was blocked"
            else -> "error $status"
        }
        return if (message.isNullOrBlank()) why else "$why ($message)"
    }
}

/**
 * After an update the old process is gone and nothing reopens the app: a
 * notification does, one tap away.
 */
class PackageReplacedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        val channel = "channel_app_updates"
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(
                channel, "App updates", NotificationManager.IMPORTANCE_DEFAULT))
        }
        val launch = context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?: return
        val pi = PendingIntent.getActivity(context, 0, launch,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val version = try {
            AppUpdates.info(context)["versionName"] as? String ?: ""
        } catch (_: Exception) { "" }
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(context, channel)
            else @Suppress("DEPRECATION") Notification.Builder(context)
        val n = b.setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("Mai An updated${if (version.isEmpty()) "" else " to $version"}")
            .setContentText("Tap to open")
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        try {
            nm.notify(0x4d41, n)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS not granted: nothing to show.
        }
    }
}
