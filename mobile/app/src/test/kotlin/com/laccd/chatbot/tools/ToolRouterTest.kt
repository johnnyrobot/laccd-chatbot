/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.tools

import com.laccd.chatbot.tools.ToolRouter.Companion.detectPace
import com.laccd.chatbot.tools.ToolRouter.Companion.extractAssistTarget
import com.laccd.chatbot.tools.ToolRouter.Companion.extractCourseCode
import com.laccd.chatbot.tools.ToolRouter.Companion.extractGeCode
import com.laccd.chatbot.tools.ToolRouter.Companion.matchesAssist
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-function tests for the keyword pre-filter. Routing tests that exercise
 * the full registry+router need a [ToolRegistry], so they live in an
 * instrumented tests cover device-only behavior; these JVM tests cover the
 * deterministic helpers the router depends on.
 */
class ToolRouterTest {

  @Test
  fun detect_pace_picks_part_time() {
    assertEquals("part_time", detectPace("i can only take 9 units while working full time"))
    assertEquals("part_time", detectPace("evenings only please"))
  }

  @Test
  fun detect_pace_picks_momentum() {
    assertEquals("momentum", detectPace("get me through as fast as possible"))
    assertEquals("momentum", detectPace("18-unit fast track"))
  }

  @Test
  fun detect_pace_returns_null_when_neutral() {
    assertNull(detectPace("how should I plan my classes"))
  }

  @Test
  fun extract_course_code_handles_simple_cases() {
    assertEquals("MATH 261", extractCourseCode("what are the prereqs for MATH 261?"))
    assertEquals("CHEM C211", extractCourseCode("does CHEM C211 transfer to UCLA"))
    assertEquals("MATH 261", extractCourseCode("MATH261 prereqs"))
  }

  @Test
  fun extract_course_code_handles_multi_word_subject() {
    assertEquals("CO SCI 952", extractCourseCode("tell me about CO SCI 952"))
  }

  @Test
  fun extract_course_code_returns_null_when_absent() {
    assertNull(extractCourseCode("hello there"))
    assertNull(extractCourseCode("what programs do you have"))
  }

  @Test
  fun extract_ge_code_requires_ge_keyword() {
    assertEquals("1A", extractGeCode("what fulfills igetc 1a"))
    assertEquals("B2", extractGeCode("ge area b2 courses"))
    assertNull(extractGeCode("section 1a is full"))
  }

  @Test
  fun matches_assist_via_explicit_trigger() {
    assertTrue(matchesAssist("show me the articulation for math 261"))
    assertTrue(matchesAssist("does this transfer to ucla"))
    assertTrue(matchesAssist("transferable to csun"))
  }

  @Test
  fun matches_assist_via_target_plus_verb() {
    assertTrue(matchesAssist("does math 261 satisfy at sdsu"))
  }

  @Test
  fun matches_assist_does_not_match_random_transfer() {
    assertFalse(matchesAssist("can i transfer my class registration"))
    assertFalse(matchesAssist("what is the schedule"))
  }

  @Test
  fun extract_assist_target_finds_uc_and_csu() {
    assertEquals("ucla", extractAssistTarget("does math 261 transfer to ucla"))
    assertEquals("csun", extractAssistTarget("transferable to csun"))
    assertEquals("cal poly slo", extractAssistTarget("does this satisfy at cal poly slo"))
  }

  @Test
  fun route_picks_prereqs_kg_for_course_plus_prereq_terms() {
    val router = makeRouter()
    val pref = router.route("what are the prerequisites for MATH 261")
    assertNotNull(pref)
    assertEquals("prereqs_kg", pref!!.tool)
    assertEquals("MATH 261", pref.args["course"])
  }

  @Test
  fun route_picks_prereqs_for_short_prerequisite_phrase() {
    val router = makeRouter()
    val pref = router.route("What the prerequisite for MATH 261 at LAMC")
    assertNotNull(pref)
    assertEquals("prereqs_kg", pref!!.tool)
    assertEquals("MATH 261", pref.args["course"])
  }

  @Test
  fun route_picks_assist_articulation_when_target_plus_course() {
    val router = makeRouter()
    val pref = router.route("does CHEM C211 transfer to UCLA")
    assertNotNull(pref)
    assertEquals("assist_articulation", pref!!.tool)
    assertEquals("CHEM C211", pref.args["course"])
    assertEquals("ucla", pref.args["to"])
  }

  @Test
  fun route_picks_ge_courses_kg_for_ge_listing() {
    val router = makeRouter()
    val pref = router.route("what fulfills IGETC 1A?")
    assertNotNull(pref)
    assertEquals("ge_courses_kg", pref!!.tool)
    assertEquals("1A", pref.args["ge_code"])
    assertEquals("IGETC", pref.args["system"])
  }

  @Test
  fun route_picks_search_programs_for_program_terms() {
    val router = makeRouter()
    val pref = router.route("what nursing programs are available")
    assertNotNull(pref)
    assertEquals("search_programs", pref!!.tool)
  }

  @Test
  fun route_returns_null_for_general_chat() {
    val router = makeRouter()
    assertNull(router.route("hi, how are you?"))
  }

  @Test
  fun route_picks_assist_before_kg_transfer_for_collision_phrase() {
    // "transfer to UCLA" matches both KG_TRANSFER_TERMS (substring 'transfer to')
    // and ASSIST. Must route to ASSIST first per backend ordering.
    val router = makeRouter()
    val pref = router.route("does MATH 261 transfer to UCLA")
    assertNotNull(pref)
    assertEquals("assist_articulation", pref!!.tool)
  }

  // ── Helpers ──

  private fun makeRouter(): ToolRouter {
    // ToolRouter only reads the registry's tools map for `isCloudTool` checks.
    // The pure routing path doesn't dispatch, so we can pass a registry built
    // with stub providers.
    val stubApi = object : LaccdApi {
      override suspend fun schedule(
        campus: String,
        term: String,
        subject: String?,
        courseNumber: String?,
        keyword: String?,
      ): ScheduleResponse = ScheduleResponse()

      override suspend fun assist(course: String, from: String, to: String): AssistResponse = AssistResponse()
      override suspend fun programs(query: String, campus: String?): ProgramsResponse = ProgramsResponse()
      override suspend fun sequence(programId: String, campus: String, pace: String): SequenceResponse =
        SequenceResponse()

      override suspend fun elumen(subject: String, number: String, campus: String): ElumenResponse = ElumenResponse()
    }
    val stubProvider = object : PackProvider {
      override fun openOrNull(campus: String) = null
      override fun embedderFor(pack: com.laccd.chatbot.data.knowledge.KnowledgePack) =
        com.laccd.chatbot.data.knowledge.HashEmbedder()
    }
    return ToolRouter(ToolRegistry(stubApi, stubProvider))
  }
}
