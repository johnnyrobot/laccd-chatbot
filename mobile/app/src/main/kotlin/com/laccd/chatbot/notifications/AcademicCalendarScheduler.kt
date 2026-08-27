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

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import androidx.core.content.getSystemService
import com.laccd.chatbot.data.knowledge.CozoException
import com.laccd.chatbot.data.knowledge.CozoRow
import com.laccd.chatbot.data.knowledge.KnowledgePack
import com.laccd.chatbot.data.knowledge.PackNotDownloaded
import com.laccd.chatbot.data.knowledge.PackVersionMismatch
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneId
import java.time.format.DateTimeParseException

/**
 * schedules local notifications for upcoming academic-calendar
 * events from the on-device knowledge pack. No Firebase, no FCM. Calendar
 * data is the `academic_calendar` Cozo relation populated by
 * `scripts/build_knowledge_pack.py`:
 *
 *   academic_calendar { campus, term, kind => date, notes }
 *
 * `kind` is one of `term_start`, `term_end`, `drop_no_w`, `drop_with_w`,
 * `finals_week`. `date` is `YYYY-MM-DD`.
 *
 * AlarmManager exact alarms are dropped on reboot, so [BootReceiver] re-runs
 * [scheduleAllForCampus] on `BOOT_COMPLETED`. Re-scheduling is idempotent —
 * we derive a deterministic request code from `(campus, term, kind, date)`,
 * so an existing alarm is replaced rather than duplicated when the pack
 * changes.
 *
 * Trigger time: each calendar date fires at [DEFAULT_TRIGGER_HOUR] local
 * time (8 AM by default — early enough that "term starts today" feels timely
 * without waking the user). Override via the `triggerHour` parameter.
 */
object AcademicCalendarScheduler {

  private const val TAG = "AcademicCalendar"
  internal const val DEFAULT_TRIGGER_HOUR = 8

  /**
   * Read every future academic-calendar event for [campus] from the on-device
   * pack and arm an exact alarm per event. Idempotent — runs cleanly after
   * reboot (no prior alarms to clean up because the OS already dropped them).
   *
   * Returns the count of armed alarms. Zero is a normal outcome when the pack
   * is missing or every calendar date is in the past.
   */
  fun scheduleAllForCampus(
    context: Context,
    campus: String,
    nowMillis: Long = System.currentTimeMillis(),
    zone: ZoneId = ZoneId.systemDefault(),
    triggerHour: Int = DEFAULT_TRIGGER_HOUR,
  ): Int {
    val events =
      runCatching { loadEvents(context, campus) }
        .onFailure { Log.w(TAG, "loadEvents failed for $campus", it) }
        .getOrElse { return 0 }
    val alarmManager = context.getSystemService<AlarmManager>() ?: return 0
    var armed = 0
    for (event in events) {
      if (!isFuture(event, nowMillis, triggerHour, zone)) continue
      val triggerAt = triggerEpochMillis(event, triggerHour, zone)
      val pending = createPendingIntent(context, event)
      armExactAlarm(alarmManager, triggerAt, pending)
      armed++
    }
    Log.i(TAG, "armed $armed academic-calendar alarms for $campus")
    return armed
  }

  /**
   * Cancel any academic-calendar alarms that were previously armed for
   * [campus]. Walks the same events the scheduler would have armed, builds
   * the matching PendingIntents, and cancels them. Safe to call when none
   * were armed.
   */
  fun cancelAllForCampus(context: Context, campus: String): Int {
    val events =
      runCatching { loadEvents(context, campus) }
        .onFailure { Log.w(TAG, "loadEvents failed for $campus", it) }
        .getOrElse { return 0 }
    val alarmManager = context.getSystemService<AlarmManager>() ?: return 0
    var cancelled = 0
    for (event in events) {
      // FLAG_NO_CREATE — only return an existing PendingIntent, don't create one.
      val pending = findPendingIntent(context, event) ?: continue
      alarmManager.cancel(pending)
      pending.cancel()
      cancelled++
    }
    return cancelled
  }

  private fun loadEvents(context: Context, campus: String): List<AcademicCalendarEvent> {
    val pack =
      try {
        KnowledgePack.open(context.filesDir, campus)
      } catch (e: PackNotDownloaded) {
        Log.i(TAG, "skip $campus — pack not downloaded")
        return emptyList()
      } catch (e: PackVersionMismatch) {
        Log.w(TAG, "skip $campus — pack schema mismatch ${e.actual} vs ${e.expected}")
        return emptyList()
      } catch (e: CozoException) {
        Log.w(TAG, "skip $campus — pack open failed", e)
        return emptyList()
      }
    val script =
      "?[term, kind, date, notes] := " +
        "*academic_calendar[c, term, kind, date, notes], c = \$campus"
    val result =
      runCatching { pack.client.runWithParams(script, mapOf("campus" to campus.uppercase())) }
        .onFailure { Log.w(TAG, "academic_calendar query failed", it) }
        .getOrElse { return emptyList() }
    return result.rows.mapNotNull { row -> parseEventRow(campus.uppercase(), row) }
  }

  private fun fireIntent(context: Context, event: AcademicCalendarEvent): Intent =
    Intent(context, AcademicCalendarAlarmReceiver::class.java).apply {
      action = AcademicCalendarAlarmReceiver.ACTION
      putExtra(AcademicCalendarAlarmReceiver.EXTRA_CAMPUS, event.campus)
      putExtra(AcademicCalendarAlarmReceiver.EXTRA_TERM, event.term)
      putExtra(AcademicCalendarAlarmReceiver.EXTRA_KIND, event.kind)
      putExtra(AcademicCalendarAlarmReceiver.EXTRA_DATE, event.date.toString())
      putExtra(AcademicCalendarAlarmReceiver.EXTRA_TITLE, titleFor(event))
      putExtra(AcademicCalendarAlarmReceiver.EXTRA_BODY, bodyFor(event))
    }

  /** FLAG_UPDATE_CURRENT — non-null; replaces an existing matching alarm. */
  private fun createPendingIntent(context: Context, event: AcademicCalendarEvent): PendingIntent =
    PendingIntent.getBroadcast(
      context,
      requestCodeFor(event),
      fireIntent(context, event),
      PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

  /** FLAG_NO_CREATE — null when no matching alarm exists; used by cancel paths. */
  private fun findPendingIntent(context: Context, event: AcademicCalendarEvent): PendingIntent? =
    PendingIntent.getBroadcast(
      context,
      requestCodeFor(event),
      fireIntent(context, event),
      PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE,
    )

  private fun armExactAlarm(am: AlarmManager, triggerAt: Long, pending: PendingIntent) {
    // Android 12+ (API 31) requires SCHEDULE_EXACT_ALARM and a runtime check;
    // the user can revoke. If we can't schedule exactly, fall back to inexact
    // — better an off-by-15-minutes alarm than no alarm.
    val canExact = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()
    if (canExact) {
      am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending)
    } else {
      am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending)
    }
  }

}

/** One row from the `academic_calendar` Cozo relation, parsed and validated. */
data class AcademicCalendarEvent(
  val campus: String,
  val term: String,
  val kind: String,
  val date: LocalDate,
  val notes: String,
)

/**
 * Parse a raw Cozo row into an event. Returns null if the date column is
 * missing, blank, or not ISO-8601 — we'd rather drop a malformed row than
 * crash scheduling for the rest of the campus.
 */
internal fun parseEventRow(campus: String, row: CozoRow): AcademicCalendarEvent? {
  val term = row.string("term").trim()
  val kind = row.string("kind").trim()
  val dateStr = row.string("date").trim()
  val notes = row.string("notes").trim()
  if (term.isEmpty() || kind.isEmpty() || dateStr.isEmpty()) return null
  val date =
    try {
      LocalDate.parse(dateStr)
    } catch (e: DateTimeParseException) {
      return null
    }
  return AcademicCalendarEvent(
    campus = campus.uppercase(),
    term = term,
    kind = kind,
    date = date,
    notes = notes,
  )
}

/**
 * Stable, deterministic request code for an event so re-scheduling overwrites
 * the prior alarm rather than stacking. The 31-bit positive mask keeps the
 * value within the AlarmManager request-code domain (Android docs say "a
 * positive int").
 */
internal fun requestCodeFor(event: AcademicCalendarEvent): Int {
  val key = "${event.campus}|${event.term}|${event.kind}|${event.date}"
  return key.hashCode() and 0x7fffffff
}

/** UTC-millis timestamp at which [event] should fire (date + [triggerHour] local). */
internal fun triggerEpochMillis(
  event: AcademicCalendarEvent,
  triggerHour: Int,
  zone: ZoneId,
): Long {
  val triggerAt = LocalDateTime.of(event.date, LocalTime.of(triggerHour, 0))
  return triggerAt.atZone(zone).toInstant().toEpochMilli()
}

/** True when the event's trigger time is strictly after [nowMillis]. */
internal fun isFuture(
  event: AcademicCalendarEvent,
  nowMillis: Long,
  triggerHour: Int,
  zone: ZoneId,
): Boolean = triggerEpochMillis(event, triggerHour, zone) > nowMillis

/** Friendly notification title derived from the event kind. */
internal fun titleFor(event: AcademicCalendarEvent): String =
  when (event.kind) {
    "term_start" -> "Term starts today"
    "term_end" -> "Last day of instruction"
    "drop_no_w" -> "Last day to drop without a 'W'"
    "drop_with_w" -> "Last day to drop with a 'W'"
    "finals_week" -> "Finals week begins"
    else -> "Academic calendar"
  }

/**
 * Notification body. Uses the curated `notes` column when present, falling
 * back to the date so the user always sees something specific.
 */
internal fun bodyFor(event: AcademicCalendarEvent): String =
  if (event.notes.isNotBlank()) event.notes else "${event.kind} — ${event.date}"
