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
import org.junit.Test

class ClassesRepositoryFilterTest {

  private val seed =
    listOf(
      ClassEntry(id = "1", name = "Calculus I", section = "001", daysTimes = "MWF 9:00"),
      ClassEntry(id = "2", name = "English 101", location = "Hum 18"),
      ClassEntry(id = "3", name = "Spanish 1", notes = "Bring tu libro"),
    )

  @Test
  fun emptyQuery_returnsAll() {
    assertEquals(3, ClassesRepository.filter(seed, "").size)
    assertEquals(3, ClassesRepository.filter(seed, "   ").size)
  }

  @Test
  fun matchesAcrossFields() {
    assertEquals(listOf("1"), ClassesRepository.filter(seed, "MWF").map { it.id })
    assertEquals(listOf("2"), ClassesRepository.filter(seed, "hum").map { it.id })
    assertEquals(listOf("3"), ClassesRepository.filter(seed, "libro").map { it.id })
  }

  @Test
  fun caseInsensitive() {
    assertEquals(listOf("1"), ClassesRepository.filter(seed, "calculus").map { it.id })
    assertEquals(listOf("1"), ClassesRepository.filter(seed, "CALCULUS").map { it.id })
  }
}
