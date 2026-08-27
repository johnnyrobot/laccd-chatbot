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
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.laccd.chatbot.MainActivity
import com.laccd.chatbot.R

/**
 * receives the AlarmManager wakeup that
 * [AcademicCalendarScheduler] armed and posts the corresponding
 * notification. Posts on the [NotificationChannels.ACADEMIC_CALENDAR]
 * channel (default importance, audible).
 *
 * Tap action opens [MainActivity]. Once the chat surface lands on a route
 * that accepts a deep-link campus + topic, this should be upgraded to an
 * intent that drops the user into a chat about the relevant term.
 */
class AcademicCalendarAlarmReceiver : BroadcastReceiver() {

  override fun onReceive(context: Context, intent: Intent) {
    if (intent.action != ACTION) return
    val campus = intent.getStringExtra(EXTRA_CAMPUS).orEmpty()
    val term = intent.getStringExtra(EXTRA_TERM).orEmpty()
    val kind = intent.getStringExtra(EXTRA_KIND).orEmpty()
    val date = intent.getStringExtra(EXTRA_DATE).orEmpty()
    val title = intent.getStringExtra(EXTRA_TITLE) ?: "Academic calendar"
    val body =
      intent.getStringExtra(EXTRA_BODY)
        ?: "$kind — $date"
    val notificationId = ("$campus|$term|$kind|$date").hashCode() and 0x7fffffff
    postCalendarNotification(context, notificationId, title, body)
  }

  companion object {
    const val ACTION = "com.laccd.chatbot.notifications.ACADEMIC_CALENDAR_FIRE"
    const val EXTRA_CAMPUS = "campus"
    const val EXTRA_TERM = "term"
    const val EXTRA_KIND = "kind"
    const val EXTRA_DATE = "date"
    const val EXTRA_TITLE = "title"
    const val EXTRA_BODY = "body"

    internal fun postCalendarNotification(
      context: Context,
      notificationId: Int,
      title: String,
      body: String,
    ) {
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
        NotificationCompat.Builder(context, NotificationChannels.ACADEMIC_CALENDAR)
          .setSmallIcon(R.drawable.ic_launcher_foreground)
          .setContentTitle(title)
          .setContentText(body)
          .setStyle(NotificationCompat.BigTextStyle().bigText(body))
          .setPriority(NotificationCompat.PRIORITY_DEFAULT)
          .setAutoCancel(true)
          .setContentIntent(pending)
      NotificationManagerCompat.from(context).notify(notificationId, builder.build())
    }
  }
}
