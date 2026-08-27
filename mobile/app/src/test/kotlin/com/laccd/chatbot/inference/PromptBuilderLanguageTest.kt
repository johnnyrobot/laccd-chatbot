/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.inference

import com.laccd.chatbot.data.campus.Campus
import com.laccd.chatbot.proto.LanguagePreference
import java.time.LocalDate
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PromptBuilderLanguageTest {

  private val anyDate = LocalDate.of(2026, 5, 6)
  private val campus = Campus.LAMC

  @Test
  fun autoLanguage_addsNoHint() {
    val prompt =
      PromptBuilder.buildSystemPrompt(campus, anyDate, LanguagePreference.LANGUAGE_AUTO)
    assertTrue(prompt.isNotBlank())
    // No language clause when AUTO.
    assertTrue(prompt.indexOf("Language:") == -1)
    assertTrue(prompt.indexOf("Idioma:") == -1)
    assertNull(PromptBuilder.languageHint(LanguagePreference.LANGUAGE_AUTO))
    assertNull(PromptBuilder.languageHint(LanguagePreference.LANGUAGE_UNSPECIFIED))
  }

  @Test
  fun englishOverride_addsEnglishHint() {
    val prompt =
      PromptBuilder.buildSystemPrompt(campus, anyDate, LanguagePreference.LANGUAGE_EN)
    assertTrue(prompt.contains("respond in English"))
  }

  @Test
  fun spanishOverride_addsSpanishHint() {
    val prompt =
      PromptBuilder.buildSystemPrompt(campus, anyDate, LanguagePreference.LANGUAGE_ES)
    assertTrue(prompt.contains("Idioma:"))
    assertTrue(prompt.contains("español"))
  }
}
