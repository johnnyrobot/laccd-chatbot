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

import com.laccd.chatbot.data.knowledge.CozoRow
import java.time.LocalDate
import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the pure-logic helpers in
 * [AcademicCalendarScheduler]. The Android-bound surface (AlarmManager,
 * BroadcastReceiver, Cozo native bridge) is exercised on-device; here we
 * cover row parsing, request-code stability, trigger-time math, and the
 * notification-text fallbacks that determine whether a calendar entry ever
 * makes it onto the user's lockscreen.
 */
class AcademicCalendarSchedulerTest {

  private val headers = listOf("term", "kind", "date", "notes")

  private fun row(term: String, kind: String, date: String, notes: String): CozoRow =
    CozoRow(values = listOf<Any?>(term, kind, date, notes), headers = headers)

  // ── parseEventRow ────────────────────────────────────────────────────────

  @Test
  fun parseEventRow_validIsoDate_parses() {
    val event = parseEventRow("LAMC", row("2026sp", "term_start", "2026-02-09", "Spring 2026"))
    assertEquals("LAMC", event!!.campus)
    assertEquals("2026sp", event.term)
    assertEquals("term_start", event.kind)
    assertEquals(LocalDate.of(2026, 2, 9), event.date)
    assertEquals("Spring 2026", event.notes)
  }

  @Test
  fun parseEventRow_lowercaseCampus_normalizesToUpper() {
    val event = parseEventRow("lamc", row("2026sp", "term_start", "2026-02-09", "n"))
    assertEquals("LAMC", event!!.campus)
  }

  @Test
  fun parseEventRow_blankDate_returnsNull() {
    assertNull(parseEventRow("LAMC", row("2026sp", "term_start", "", "n")))
  }

  @Test
  fun parseEventRow_malformedDate_returnsNull() {
    assertNull(parseEventRow("LAMC", row("2026sp", "term_start", "not-a-date", "n")))
    assertNull(parseEventRow("LAMC", row("2026sp", "term_start", "2026/02/09", "n")))
  }

  @Test
  fun parseEventRow_emptyTermOrKind_returnsNull() {
    assertNull(parseEventRow("LAMC", row("", "term_start", "2026-02-09", "n")))
    assertNull(parseEventRow("LAMC", row("2026sp", "", "2026-02-09", "n")))
  }

  // ── requestCodeFor ───────────────────────────────────────────────────────

  @Test
  fun requestCodeFor_isStableAcrossCalls() {
    val event = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    assertEquals(requestCodeFor(event), requestCodeFor(event))
  }

  @Test
  fun requestCodeFor_isPositiveInt() {
    val event = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    assertTrue("request code must be non-negative", requestCodeFor(event) >= 0)
  }

  @Test
  fun requestCodeFor_distinguishesEventsByDate() {
    val a = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    val b = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 10))
    assertNotEquals(requestCodeFor(a), requestCodeFor(b))
  }

  @Test
  fun requestCodeFor_distinguishesEventsByCampus() {
    val a = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    val b = makeEvent("ELAC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    assertNotEquals(requestCodeFor(a), requestCodeFor(b))
  }

  // ── triggerEpochMillis ───────────────────────────────────────────────────

  @Test
  fun triggerEpochMillis_combinesDateAndHourInUtc() {
    val event = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    val millis = triggerEpochMillis(event, triggerHour = 8, zone = ZoneId.of("UTC"))
    // 2026-02-09T08:00:00Z = 1770624000 seconds since epoch
    assertEquals(1770624000_000L, millis)
  }

  @Test
  fun triggerEpochMillis_respectsZone() {
    val event = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    val utc = triggerEpochMillis(event, 8, ZoneId.of("UTC"))
    val la = triggerEpochMillis(event, 8, ZoneId.of("America/Los_Angeles"))
    // LA is UTC-8 in February → 8 AM LA == 16:00 UTC, eight hours later than UTC's 8 AM.
    assertEquals(8 * 3_600_000L, la - utc)
  }

  // ── isFuture ─────────────────────────────────────────────────────────────

  @Test
  fun isFuture_pastDateReturnsFalse() {
    val event = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    val now = triggerEpochMillis(event, 8, ZoneId.of("UTC")) + 1_000L
    assertFalse(isFuture(event, now, 8, ZoneId.of("UTC")))
  }

  @Test
  fun isFuture_futureDateReturnsTrue() {
    val event = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    val now = triggerEpochMillis(event, 8, ZoneId.of("UTC")) - 1_000L
    assertTrue(isFuture(event, now, 8, ZoneId.of("UTC")))
  }

  @Test
  fun isFuture_sameInstantReturnsFalse() {
    val event = makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))
    val now = triggerEpochMillis(event, 8, ZoneId.of("UTC"))
    assertFalse(
      "an alarm whose trigger has already arrived isn't 'future'",
      isFuture(event, now, 8, ZoneId.of("UTC")),
    )
  }

  // ── titleFor / bodyFor ───────────────────────────────────────────────────

  @Test
  fun titleFor_knownKindsReturnFriendlyTitles() {
    assertEquals(
      "Term starts today",
      titleFor(makeEvent("LAMC", "2026sp", "term_start", LocalDate.of(2026, 2, 9))),
    )
    assertEquals(
      "Last day to drop with a 'W'",
      titleFor(makeEvent("LAMC", "2026sp", "drop_with_w", LocalDate.of(2026, 5, 4))),
    )
    assertEquals(
      "Finals week begins",
      titleFor(makeEvent("LAMC", "2026sp", "finals_week", LocalDate.of(2026, 6, 1))),
    )
  }

  @Test
  fun titleFor_unknownKindFallsBackToGeneric() {
    assertEquals(
      "Academic calendar",
      titleFor(makeEvent("LAMC", "2026sp", "spirit_week", LocalDate.of(2026, 4, 1))),
    )
  }

  @Test
  fun bodyFor_usesNotesWhenPresent() {
    val event =
      AcademicCalendarEvent(
        campus = "LAMC",
        term = "2026sp",
        kind = "term_start",
        date = LocalDate.of(2026, 2, 9),
        notes = "Spring 2026 first day of instruction",
      )
    assertEquals("Spring 2026 first day of instruction", bodyFor(event))
  }

  @Test
  fun bodyFor_blankNotesFallsBackToKindAndDate() {
    val event =
      AcademicCalendarEvent(
        campus = "LAMC",
        term = "2026sp",
        kind = "term_start",
        date = LocalDate.of(2026, 2, 9),
        notes = "",
      )
    assertEquals("term_start — 2026-02-09", bodyFor(event))
  }

  private fun makeEvent(campus: String, term: String, kind: String, date: LocalDate) =
    AcademicCalendarEvent(campus = campus, term = term, kind = kind, date = date, notes = "")
}
