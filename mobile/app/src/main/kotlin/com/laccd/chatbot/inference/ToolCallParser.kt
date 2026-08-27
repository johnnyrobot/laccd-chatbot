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

import com.laccd.chatbot.tools.ToolCall
import org.json.JSONObject

/**
 * Pulls `{"tool": "...", "arguments": {...}}` objects out of Gemma's streamed
 * text output. The model may surround the call with prose, fences, or
 * trailing whitespace; the parser tolerates all of that.
 *
 * Recognized shapes (in order of precedence):
 *
 *   1. ```json
 *      {"tool": "live_schedule", "arguments": {...}}
 *      ```
 *
 *   2. {"tool": "live_schedule", "arguments": {...}}   (bare JSON object)
 *
 *   3. {"name": "live_schedule", "arguments": {...}}   (alternate key — some
 *      function-calling fine-tunes prefer "name")
 *
 *   4. <tool_call>{"tool": "...", ...}</tool_call>      (tag-wrapped)
 *
 * The parser is bracket-matching, not regex-driven, so nested objects
 * inside `arguments` parse correctly even when keys contain colons or
 * commas in their values.
 *
 * Usage:
 *   - call [findCalls] on the model's full final response after streaming
 *     completes. Streaming-time partial parsing is intentionally NOT
 *     supported — Gemma sometimes "thinks" before emitting a tool call, and
 *     we'd otherwise fire half-formed calls.
 *   - the caller enforces a two-iteration cap (model → tool → model →
 *     tool → answer) — this parser doesn't track turn count.
 */
object ToolCallParser {

  /**
   * Extract every well-formed tool call from [text]. Empty when none. The
   * fenced/tag extractors and the bracket scanner often see the same call
   * twice (a fenced ```json``` block whose inner JSON the bracket scanner
   * also finds). We dedupe on `(name, args)` so the caller doesn't fire the
   * same tool twice.
   */
  fun findCalls(text: String): List<ToolCall> {
    if (text.isBlank()) return emptyList()
    val candidates = collectJsonCandidates(text)
    val calls = mutableListOf<ToolCall>()
    val seen = mutableSetOf<Pair<String, String>>()
    for (raw in candidates) {
      val call = runCatching { parseOne(raw) }.getOrNull() ?: continue
      val key = call.name to call.args.entries.sortedBy { it.key }
        .joinToString(",") { "${it.key}=${it.value}" }
      if (seen.add(key)) calls.add(call)
    }
    return calls
  }

  /** Convenience: returns the first [ToolCall] in [text], or null. */
  fun firstCall(text: String): ToolCall? = findCalls(text).firstOrNull()

  // ── Internals ──

  private fun collectJsonCandidates(text: String): List<String> {
    val out = mutableListOf<String>()

    // 1. ```json ... ``` fenced blocks.
    val fenceRegex = Regex("```(?:json|tool_call)?\\s*([\\s\\S]*?)```", RegexOption.IGNORE_CASE)
    for (m in fenceRegex.findAll(text)) out.add(m.groupValues[1])

    // 2. <tool_call>...</tool_call> tags.
    val tagRegex = Regex("<tool_call>([\\s\\S]*?)</tool_call>", RegexOption.IGNORE_CASE)
    for (m in tagRegex.findAll(text)) out.add(m.groupValues[1])

    // 3. Bare top-level JSON objects (bracket-matched).
    out.addAll(scanBalancedObjects(text))

    return out
  }

  /**
   * Walk [text] and emit substrings that are balanced JSON objects. Skips
   * over braces inside string literals so `"foo: { bar }"` doesn't trip the
   * bracket counter.
   */
  private fun scanBalancedObjects(text: String): List<String> {
    val out = mutableListOf<String>()
    var i = 0
    while (i < text.length) {
      val ch = text[i]
      if (ch != '{') { i++; continue }
      val end = findMatchingBrace(text, i)
      if (end == -1) break
      out.add(text.substring(i, end + 1))
      i = end + 1
    }
    return out
  }

  /** Returns the index of the `}` matching `text[start] == '{'`, or -1 if unbalanced. */
  private fun findMatchingBrace(text: String, start: Int): Int {
    var depth = 0
    var inString = false
    var escaped = false
    var i = start
    while (i < text.length) {
      val c = text[i]
      if (inString) {
        when {
          escaped -> escaped = false
          c == '\\' -> escaped = true
          c == '"' -> inString = false
        }
      } else {
        when (c) {
          '"' -> inString = true
          '{' -> depth++
          '}' -> {
            depth--
            if (depth == 0) return i
          }
        }
      }
      i++
    }
    return -1
  }

  private fun parseOne(raw: String): ToolCall? {
    val trimmed = raw.trim()
    if (trimmed.isEmpty() || trimmed.first() != '{') return null
    val obj = runCatching { JSONObject(trimmed) }.getOrNull() ?: return null

    val name = obj.optString("tool").ifBlank { obj.optString("name") }.ifBlank { return null }
    val argsObj = obj.optJSONObject("arguments")
      ?: obj.optJSONObject("args")
      ?: obj.optJSONObject("parameters")
    val args = mutableMapOf<String, Any?>()
    if (argsObj != null) {
      val keys = argsObj.keys()
      while (keys.hasNext()) {
        val k = keys.next()
        args[k] = argsObj.opt(k)
      }
    }
    return ToolCall(name = name, args = args)
  }
}
