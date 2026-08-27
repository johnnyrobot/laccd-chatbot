/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.notifications

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.laccd.chatbot.MainActivity
import com.laccd.chatbot.R

/**
 * CoroutineWorker that posts a single study-reminder notification.
 * Scheduled via [StudyReminderScheduler.scheduleOneShot]. WorkManager owns
 * persistence: a OneTimeWorkRequest enqueued before reboot survives the
 * reboot and fires when its initial delay elapses.
 *
 * The worker is intentionally dumb — it reads input data, builds a
 * NotificationCompat with the academic-tone defaults, and posts. Anything
 * dynamic (which campus, what time of day) is fixed at scheduling time.
 */
class StudyReminderWorker(
  appContext: Context,
  params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

  override suspend fun doWork(): Result {
    val title = inputData.getString(KEY_TITLE) ?: DEFAULT_TITLE
    val body = inputData.getString(KEY_BODY) ?: DEFAULT_BODY
    val notificationId =
      inputData.getInt(KEY_NOTIFICATION_ID, DEFAULT_NOTIFICATION_ID)
    postReminder(applicationContext, notificationId, title, body)
    return Result.success()
  }

  companion object {
    const val KEY_TITLE = "title"
    const val KEY_BODY = "body"
    const val KEY_NOTIFICATION_ID = "notification_id"

    const val DEFAULT_TITLE = "Study reminder"
    const val DEFAULT_BODY = "Time to study."
    const val DEFAULT_NOTIFICATION_ID = 0x57001 // arbitrary, distinct from calendar IDs

    internal fun postReminder(
      context: Context,
      notificationId: Int,
      title: String,
      body: String,
    ) {
      // Android 13+ requires the user to grant POST_NOTIFICATIONS at runtime.
      // If the user hasn't granted it (or revoked it), silently drop — the
      // worker still succeeded; the OS just can't show the toast.
      if (
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
          PackageManager.PERMISSION_GRANTED
      ) {
        return
      }
      val tapIntent =
        Intent(context, MainActivity::class.java).apply {
          flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
      val pending =
        PendingIntent.getActivity(
          context,
          notificationId,
          tapIntent,
          PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
      val builder =
        NotificationCompat.Builder(context, NotificationChannels.STUDY_REMINDERS)
          .setSmallIcon(R.drawable.ic_launcher_foreground)
          .setContentTitle(title)
          .setContentText(body)
          .setStyle(NotificationCompat.BigTextStyle().bigText(body))
          .setPriority(NotificationCompat.PRIORITY_LOW)
          .setAutoCancel(true)
          .setContentIntent(pending)
      NotificationManagerCompat.from(context).notify(notificationId, builder.build())
    }
  }
}
