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

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.laccd.chatbot.data.DataStoreRepository
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.android.EntryPointAccessors
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * re-arms academic-calendar alarms after device reboot.
 * AlarmManager exact alarms do NOT survive reboot; the OS drops them. The
 * standard fix is a `BOOT_COMPLETED` broadcast receiver that walks the same
 * data source the original scheduler used and re-schedules.
 *
 * WorkManager-driven study reminders ([StudyReminderWorker]) survive reboot
 * natively — WorkManager re-enqueues persisted requests on its own — so we
 * don't touch them here.
 *
 * Pulls the singleton [DataStoreRepository] via [EntryPointAccessors] rather
 * than `@AndroidEntryPoint`. Hilt's generated parent for receivers calls
 * `super.onReceive`, which Kotlin disallows because
 * [BroadcastReceiver.onReceive] is abstract; the entry-point accessor is
 * the supported way to grab a Hilt singleton from a non-entry-pointed
 * receiver.
 */
class BootReceiver : BroadcastReceiver() {

  @EntryPoint
  @InstallIn(SingletonComponent::class)
  interface BootEntryPoint {
    fun dataStoreRepository(): DataStoreRepository
  }

  override fun onReceive(context: Context, intent: Intent) {
    if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
    val app = context.applicationContext
    val entryPoint = EntryPointAccessors.fromApplication(app, BootEntryPoint::class.java)
    val dataStoreRepository = entryPoint.dataStoreRepository()
    val pending = goAsync()
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    scope.launch {
      try {
        val campusCode = dataStoreRepository.readSelectedCampus()
        if (campusCode.isBlank()) {
          Log.i(TAG, "no selected campus; nothing to re-arm")
          return@launch
        }
        val armed = AcademicCalendarScheduler.scheduleAllForCampus(app, campusCode)
        Log.i(TAG, "boot re-arm: $armed alarms for $campusCode")
      } catch (t: Throwable) {
        Log.w(TAG, "boot re-arm failed", t)
      } finally {
        pending.finish()
      }
    }
  }

  companion object {
    private const val TAG = "BootReceiver"
  }
}
