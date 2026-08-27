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

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import androidx.core.content.getSystemService

/**
 * notification channels for the two on-device push surfaces. No
 * Firebase, no FCM. The channels are registered once at [GalleryApplication]
 * start; channel registration is idempotent so repeating it is harmless.
 *
 * Two channels by intent and importance:
 *   - "Study reminders" (LOW): user-set or skill-driven study breaks. Quiet
 *     by design — no sound, no vibration. Easy to silence per-channel from
 *     system Settings.
 *   - "Academic calendar" (DEFAULT): term-start, finals, drop dates baked
 *     into the per-campus knowledge pack. Should ping audibly because they
 *     are not user-initiated and the user expects them to feel like a
 *     deadline.
 */
object NotificationChannels {

  const val STUDY_REMINDERS = "study_reminders"
  const val ACADEMIC_CALENDAR = "academic_calendar"

  fun ensureCreated(context: Context) {
    val manager = context.getSystemService<NotificationManager>() ?: return
    manager.createNotificationChannels(
      listOf(
        NotificationChannel(
          STUDY_REMINDERS,
          "Study reminders",
          NotificationManager.IMPORTANCE_LOW,
        ).apply {
          description = "Quiet nudges for study sessions you scheduled."
          enableVibration(false)
          setShowBadge(false)
        },
        NotificationChannel(
          ACADEMIC_CALENDAR,
          "Academic calendar",
          NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
          description =
            "Term-start, finals, drop deadlines, and other dates from your campus's calendar."
          enableVibration(true)
          setShowBadge(true)
        },
      )
    )
  }
}
