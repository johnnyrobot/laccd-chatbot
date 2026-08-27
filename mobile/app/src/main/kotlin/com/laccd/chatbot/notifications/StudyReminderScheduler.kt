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

import android.content.Context
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/**
 * public API for one-shot study reminders. WorkManager handles
 * scheduling and persistence — a OneTimeWorkRequest enqueued before reboot
 * survives the reboot and fires when its initial delay elapses, so we don't
 * need a BootReceiver hook for these.
 *
 * Daily / time-of-day-precise study reminders are deliberately NOT in scope
 * for local reminders. They appear alongside the Settings UI and the `study-timer`
 * skill in a later track; that wiring will use AlarmManager directly because
 * WorkManager's PeriodicWorkRequest has a 15-minute floor and is not aligned
 * to wall-clock time.
 *
 * The unique work name ([WORK_NAME_PREFIX] + [id]) lets callers cancel an
 * already-scheduled reminder by re-using the same id (or via [cancel]).
 */
object StudyReminderScheduler {

  const val WORK_NAME_PREFIX = "study-reminder-"

  /**
   * Schedule a one-shot study reminder to fire `delayMillis` from now.
   * If a reminder with the same [id] is already pending, it is replaced.
   * [id] doubles as the OS notification id so re-scheduling also re-uses
   * (and replaces) the prior notification slot.
   */
  fun scheduleOneShot(
    context: Context,
    id: Int,
    delayMillis: Long,
    title: String = StudyReminderWorker.DEFAULT_TITLE,
    body: String = StudyReminderWorker.DEFAULT_BODY,
  ) {
    val data =
      Data.Builder()
        .putString(StudyReminderWorker.KEY_TITLE, title)
        .putString(StudyReminderWorker.KEY_BODY, body)
        .putInt(StudyReminderWorker.KEY_NOTIFICATION_ID, id)
        .build()
    val request =
      OneTimeWorkRequestBuilder<StudyReminderWorker>()
        .setInitialDelay(delayMillis.coerceAtLeast(0L), TimeUnit.MILLISECONDS)
        .setInputData(data)
        .addTag(WORK_NAME_PREFIX + id)
        .build()
    WorkManager.getInstance(context)
      .enqueueUniqueWork(WORK_NAME_PREFIX + id, ExistingWorkPolicy.REPLACE, request)
  }

  /** Cancel a one-shot reminder previously scheduled with [id]. No-op if absent. */
  fun cancel(context: Context, id: Int) {
    WorkManager.getInstance(context).cancelUniqueWork(WORK_NAME_PREFIX + id)
  }
}
