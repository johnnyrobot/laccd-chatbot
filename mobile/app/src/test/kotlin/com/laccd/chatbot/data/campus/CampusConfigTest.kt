/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data.campus

import androidx.compose.ui.graphics.Color
import com.laccd.chatbot.ui.theme.luminance
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM-only sanity checks on the [Campus] enum and the luminance helper that drives
 * onColor selection. Full Material 3 [ColorScheme] construction is exercised via the live
 * UI; here we just guard against the static contract: 9 campuses, distinct primaries,
 * non-blank labels.
 */
class CampusConfigTest {

  @Test
  fun nineCampusesPresent() {
    assertEquals(9, Campus.entries.size)
  }

  @Test
  fun allCampusesHavePopulatedLabels() {
    for (campus in Campus.entries) {
      assertFalse("code blank for $campus", campus.code.isBlank())
      assertFalse("displayName blank for $campus", campus.displayName.isBlank())
      assertFalse("shortName blank for $campus", campus.shortName.isBlank())
      assertFalse("nickname blank for $campus", campus.nickname.isBlank())
      assertFalse("website blank for $campus", campus.website.isBlank())
      assertFalse("subdomain blank for $campus", campus.subdomain.isBlank())
      assertFalse("phoneNumber blank for $campus", campus.phoneNumber.isBlank())
      assertFalse("lkAgentName blank for $campus", campus.lkAgentName.isBlank())
    }
  }

  @Test
  fun primaryColorsAreDistinct() {
    val primaries = Campus.entries.map { it.primaryColor.value }.toSet()
    assertEquals(
      "Each of the 9 campuses must have a distinct brand primary",
      9,
      primaries.size,
    )
  }

  @Test
  fun byCodeMatchesAllEntries_caseInsensitive() {
    for (campus in Campus.entries) {
      assertEquals(campus, Campus.byCode(campus.code))
      assertEquals(campus, Campus.byCode(campus.code.lowercase()))
    }
  }

  @Test
  fun byCodeOrNull_returnsNullForBlankAndUnknown() {
    assertNotNull(Campus.byCode("LAMC"))
    assertEquals(null, Campus.byCodeOrNull(""))
    assertEquals(null, Campus.byCodeOrNull("XYZ_NOPE"))
  }

  @Test
  fun byCodeFallsBackToDefault_forUnknown() {
    assertEquals(Campus.DEFAULT, Campus.byCode("XYZ_NOPE"))
  }

  @Test
  fun luminanceWhite_isOne() {
    val l = Color.White.luminance()
    assertTrue("white luminance ~ 1.0, got $l", l > 0.95f)
  }

  @Test
  fun luminanceBlack_isZero() {
    val l = Color.Black.luminance()
    assertTrue("black luminance ~ 0.0, got $l", l < 0.05f)
  }

  @Test
  fun luminanceOrdersDarkBeforeLight() {
    val dark = Color(0xFF002856).luminance() // LASC navy
    val light = Color(0xFFFFC52E).luminance() // LAHC gold
    assertTrue("navy should be darker than gold", dark < light)
  }
}
