/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.common.textandvoiceinput

import com.laccd.chatbot.data.SAMPLE_RATE
import com.laccd.chatbot.inference.GemmaEngine
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM-only sanity checks on the bits of the hold-to-dictate path that don't need
 * an Android runtime. Full hold-to-dictate flow (AudioRecord ↔ SpeechRecognizer ↔ Gemma)
 * is exercised on-device; here we just guard against header drift and prompt regressions.
 */
class HoldToDictateViewModelTest {

  @Test
  fun wrapPcmInWav_emitsRiffWaveHeader() {
    val pcm = ByteArray(64) { it.toByte() }
    val wav = wrapPcmInWav(pcm)
    // 44-byte header + PCM payload.
    assertEquals(44 + pcm.size, wav.size)
    assertEquals('R'.code.toByte(), wav[0])
    assertEquals('I'.code.toByte(), wav[1])
    assertEquals('F'.code.toByte(), wav[2])
    assertEquals('F'.code.toByte(), wav[3])
    assertEquals('W'.code.toByte(), wav[8])
    assertEquals('A'.code.toByte(), wav[9])
    assertEquals('V'.code.toByte(), wav[10])
    assertEquals('E'.code.toByte(), wav[11])
    assertEquals('d'.code.toByte(), wav[36])
    assertEquals('a'.code.toByte(), wav[37])
    assertEquals('t'.code.toByte(), wav[38])
    assertEquals('a'.code.toByte(), wav[39])
  }

  @Test
  fun wrapPcmInWav_encodesSampleRateLittleEndian() {
    val wav = wrapPcmInWav(ByteArray(0))
    val sr =
      (wav[24].toInt() and 0xff) or
        ((wav[25].toInt() and 0xff) shl 8) or
        ((wav[26].toInt() and 0xff) shl 16) or
        ((wav[27].toInt() and 0xff) shl 24)
    assertEquals(SAMPLE_RATE, sr)
  }

  @Test
  fun wrapPcmInWav_encodesPcmFormatMonoSixteenBit() {
    val wav = wrapPcmInWav(ByteArray(0))
    // audio format = 1 (PCM)
    assertEquals(1.toByte(), wav[20])
    assertEquals(0.toByte(), wav[21])
    // channels = 1 (mono)
    assertEquals(1.toByte(), wav[22])
    assertEquals(0.toByte(), wav[23])
    // bits per sample = 16
    assertEquals(16.toByte(), wav[34])
    assertEquals(0.toByte(), wav[35])
  }

  @Test
  fun wrapPcmInWav_payloadCopiedAfterHeader() {
    val pcm = byteArrayOf(0x11, 0x22, 0x33, 0x44, 0x55)
    val wav = wrapPcmInWav(pcm)
    for (i in pcm.indices) {
      assertEquals("byte $i mismatch", pcm[i], wav[44 + i])
    }
    // data chunk size header.
    assertEquals(pcm.size.toByte(), wav[40])
  }

  @Test
  fun transcriptionPromptIsTranscriptionOnly() {
    val prompt = GemmaEngine.transcriptionSystemPrompt(language = "en")
    assertTrue("prompt must instruct verbatim only", prompt.contains("verbatim"))
    assertTrue("prompt must forbid commentary", prompt.contains("No commentary"))
  }

  @Test
  fun transcriptionPromptCarriesLaccdGlossary() {
    val prompt = GemmaEngine.transcriptionSystemPrompt(language = "en")
    // High-collision course-code patterns exercise the transcription boundary.
    assertTrue("CHEM C211 must appear in glossary", prompt.contains("CHEM C211"))
    assertTrue("MATH 261 must appear in glossary", prompt.contains("MATH 261"))
    assertTrue("Computer Science must appear in glossary", prompt.contains("COMPUTER SCIENCE"))
    // A couple of campus nicknames.
    assertTrue("Mission must appear in glossary", prompt.contains("Mission"))
    assertTrue("Trade-Tech must appear in glossary", prompt.contains("Trade-Tech"))
  }

  @Test
  fun transcriptionPromptSwitchesLanguage() {
    val en = GemmaEngine.transcriptionSystemPrompt(language = "en")
    val es = GemmaEngine.transcriptionSystemPrompt(language = "es")
    assertTrue("English prompt must declare English output", en.contains("Output language: English"))
    assertTrue("Spanish prompt must declare Spanish output", es.contains("Output language: Spanish"))
  }
}
