/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data.classes

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CompaneroJsonTest {

  @Test
  fun roundTrip_preservesAllFields() {
    val source =
      listOf(
        ClassEntry(
          id = "id-1",
          name = "Calculus I",
          section = "001",
          daysTimes = "Mon/Wed/Fri 9:00–9:50 AM",
          location = "Sci 204",
          notes = "Prof. García; office hours T 2–4.",
          orderIndex = 0,
        ),
        ClassEntry(
          id = "id-2",
          name = "Composition",
          section = "A2",
          daysTimes = "Tue/Thu 10:00–11:30 AM",
          location = "Hum 18",
          notes = "",
          orderIndex = 1,
        ),
      )
    val profile =
      CompaneroJson.CompaneroProfileDto(studentId = "881234567", studentEmail = "x@laccd.edu")

    val encoded = CompaneroJson.encode(source, profile)
    val decoded = CompaneroJson.decode(encoded)

    assertEquals(source.map { it.copy(orderIndex = source.indexOf(it)) }, decoded.classes)
    assertEquals(profile, decoded.profile)
  }

  @Test
  fun decode_acceptsCompaneroPwaShape() {
    // Verbatim shape produced by the Companero PWA's exportData().
    val raw =
      """
      {
        "exportedAt": "2026-05-06T10:00:00Z",
        "version": 1,
        "profile": { "studentId": "999", "studentEmail": "Y@LACCD.EDU" },
        "classes": [
          { "id": "abc", "name": "MATH 261", "section": "5483", "daysTimes": "MWF 9-10",
            "location": "MS 100", "notes": "" },
          { "id": "def", "name": "ENGL 101", "section": "001", "daysTimes": "TTh 2-3:30",
            "location": "AHS 201", "notes": "Bring laptop" }
        ]
      }
      """
        .trimIndent()

    val decoded = CompaneroJson.decode(raw)
    assertEquals(2, decoded.classes.size)
    assertEquals("MATH 261", decoded.classes[0].name)
    assertEquals("ENGL 101", decoded.classes[1].name)
    assertEquals("999", decoded.profile?.studentId)
    // Email is preserved verbatim from the Companero shape; ProfileStore lowercases on save.
    assertEquals("Y@LACCD.EDU", decoded.profile?.studentEmail)
    assertEquals(0, decoded.classes[0].orderIndex)
    assertEquals(1, decoded.classes[1].orderIndex)
  }

  @Test
  fun decode_skipsEntriesWithoutName() {
    val raw =
      """{
        "classes": [
          { "id": "1", "name": "Real class" },
          { "id": "2", "name": "" },
          { "id": "3" }
        ]
      }"""
        .trimIndent()
    val decoded = CompaneroJson.decode(raw)
    assertEquals(1, decoded.classes.size)
    assertEquals("Real class", decoded.classes[0].name)
  }

  @Test
  fun decode_returnsEmptyOnGarbage() {
    assertTrue(CompaneroJson.decode("not json").classes.isEmpty())
    assertTrue(CompaneroJson.decode("").classes.isEmpty())
    assertNull(CompaneroJson.decode("not json").profile)
  }

  @Test
  fun decode_rejectsWronglyTypedFields() {
    for (value in listOf("{}", "42", "true")) {
      val decoded = CompaneroJson.decode("""{"classes":[{"name":$value}]}""")
      assertTrue(decoded.classes.isEmpty())
    }
    assertTrue(
      CompaneroJson.decode("""{"classes":[{"name":"Math","section":42}]}""")
        .classes.isEmpty()
    )
    assertNull(CompaneroJson.decode("""{"profile":{"studentId":true}}""").profile)
  }

  @Test
  fun decode_acceptsBareArray() {
    val raw = """[{"id":"x","name":"Hist 11"}]"""
    val decoded = CompaneroJson.decode(raw)
    assertEquals(1, decoded.classes.size)
    assertEquals("Hist 11", decoded.classes[0].name)
    assertNull(decoded.profile)
  }

  @Test
  fun decode_assignsRandomIdWhenMissing() {
    val raw = """{"classes":[{"name":"Spanish 1"}]}"""
    val decoded = CompaneroJson.decode(raw)
    assertEquals(1, decoded.classes.size)
    assertNotNull(decoded.classes[0].id)
    assertTrue(decoded.classes[0].id.isNotEmpty())
  }

  @Test
  fun encode_sortsByOrderIndex() {
    val source =
      listOf(
        ClassEntry(id = "b", name = "B", orderIndex = 2),
        ClassEntry(id = "a", name = "A", orderIndex = 0),
        ClassEntry(id = "c", name = "C", orderIndex = 1),
      )
    val encoded = CompaneroJson.encode(source)
    val decoded = CompaneroJson.decode(encoded)
    assertEquals(listOf("A", "C", "B"), decoded.classes.map { it.name })
  }
}
