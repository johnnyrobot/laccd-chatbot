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

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ToolCallParserTest {

  @Test
  fun extracts_call_from_fenced_json_block() {
    val text = """
      Sure, let me look that up.
      ```json
      {"tool": "live_schedule", "arguments": {"campus": "LAMC", "term": "2265"}}
      ```
    """.trimIndent()
    val call = ToolCallParser.firstCall(text)
    assertNotNull(call)
    assertEquals("live_schedule", call!!.name)
    assertEquals("LAMC", call.args["campus"])
    assertEquals("2265", call.args["term"])
  }

  @Test
  fun extracts_call_from_bare_json_object() {
    val text = """{"tool": "prereqs_kg", "arguments": {"course": "MATH 261"}}"""
    val call = ToolCallParser.firstCall(text)
    assertNotNull(call)
    assertEquals("prereqs_kg", call!!.name)
    assertEquals("MATH 261", call.args["course"])
  }

  @Test
  fun extracts_call_with_alternate_name_key() {
    val text = """{"name": "ge_courses_kg", "arguments": {"ge_code": "1A", "system": "IGETC"}}"""
    val call = ToolCallParser.firstCall(text)
    assertNotNull(call)
    assertEquals("ge_courses_kg", call!!.name)
    assertEquals("1A", call.args["ge_code"])
  }

  @Test
  fun extracts_call_with_args_key() {
    val text = """{"tool": "elumen_course", "args": {"subject": "MATH", "number": "261"}}"""
    val call = ToolCallParser.firstCall(text)
    assertNotNull(call)
    assertEquals("MATH", call!!.args["subject"])
    assertEquals("261", call.args["number"])
  }

  @Test
  fun extracts_call_from_tag_wrapper() {
    val text = """
      Calling a tool now.
      <tool_call>{"tool": "transfer_status_kg", "arguments": {"course": "CHEM C211"}}</tool_call>
      Done.
    """.trimIndent()
    val call = ToolCallParser.firstCall(text)
    assertNotNull(call)
    assertEquals("transfer_status_kg", call!!.name)
  }

  @Test
  fun ignores_prose_only_text() {
    val text = "I think you should look at MATH 261 first."
    assertTrue(ToolCallParser.findCalls(text).isEmpty())
    assertNull(ToolCallParser.firstCall(text))
  }

  @Test
  fun ignores_invalid_json_braces() {
    val text = "There's a confusing { in here but no JSON."
    val calls = ToolCallParser.findCalls(text)
    // Bracket scanner may yield candidates; parser must reject all of them.
    assertTrue(calls.isEmpty())
  }

  @Test
  fun parses_nested_arguments_object() {
    val text = """
      ```json
      {"tool": "course_sequence", "arguments": {"program_id": "p-1", "campus": "LAMC", "pace": "momentum"}}
      ```
    """.trimIndent()
    val call = ToolCallParser.firstCall(text)
    assertNotNull(call)
    assertEquals("p-1", call!!.args["program_id"])
    assertEquals("momentum", call.args["pace"])
  }

  @Test
  fun finds_multiple_calls_in_one_response() {
    val text = """
      First:
      ```json
      {"tool": "prereqs_kg", "arguments": {"course": "MATH 261"}}
      ```
      Then:
      ```json
      {"tool": "transfer_status_kg", "arguments": {"course": "MATH 261"}}
      ```
    """.trimIndent()
    val calls = ToolCallParser.findCalls(text)
    assertEquals(2, calls.size)
    assertEquals("prereqs_kg", calls[0].name)
    assertEquals("transfer_status_kg", calls[1].name)
  }

  @Test
  fun handles_brace_inside_string_value() {
    val text = """{"tool": "rag_search", "arguments": {"query": "what is { } really?"}}"""
    val call = ToolCallParser.firstCall(text)
    assertNotNull(call)
    assertEquals("what is { } really?", call!!.args["query"])
  }

  @Test
  fun returns_null_when_object_has_no_tool_or_name_key() {
    val text = """{"unrelated": "thing", "arguments": {}}"""
    assertNull(ToolCallParser.firstCall(text))
  }
}
