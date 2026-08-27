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

import android.util.Log

/**
 * On-device analog of `backend/app/services/chat.py::ChatOrchestrator`. Decides:
 *
 *  1. Does this message obviously match a single tool? If so, skip a Gemma
 *     round-trip and call it directly. The model is then asked to format the
 *     tool result. This is the on-device latency win for high-volume queries
 *     (course info, schedule, prereqs).
 *  2. Otherwise, return null — the caller hands the question to Gemma with the
 *     full tool catalog and lets the model pick.
 *
 * Tier ordering:
 *   - tools whose data lives on-device run first (KG / RAG)
 *   - tools whose data lives in the cloud (`/api/tools/...`) run when online
 *   - if a cloud tool is needed but the device is offline, we surface an
 *     "open the cloud chatbot" disclaimer rather than hallucinate.
 *
 * Every keyword set here mirrors the cloud orchestrator. When we add an
 * intent there, mirror it here so the mobile app's routing stays parity'd.
 */
class ToolRouter(private val registry: ToolRegistry) {

  /**
   * Returns the [PreferredTool] when keyword routing makes the choice
   * obvious; null when Gemma should pick. The order of checks mirrors the
   * cloud orchestrator and is enforced by tests there
   * (`tests/test_chat_*_routing.py`) — keep ASSIST before knowledge-graph
   * because of substring collisions ("transfer to UCLA" matches both).
   */
  fun route(rawMessage: String): PreferredTool? {
    val text = rawMessage.lowercase()
    if (text.isBlank()) return null
    val courseCode = extractCourseCode(rawMessage)

    // 1. ASSIST articulation — must run before KG transfer routing.
    if (matchesAssist(text)) {
      val from = "" // resolved by caller from active campus
      val to = extractAssistTarget(text) ?: ""
      val course = courseCode ?: ""
      if (course.isNotBlank() && to.isNotBlank()) {
        return PreferredTool(
          tool = "assist_articulation",
          args = mapOf("course" to course, "from" to from, "to" to to),
          reason = "matched ASSIST trigger + target institution",
        )
      }
    }

    // 2. Course-level KG routing — prereqs, GE, transfer, course info.
    if (courseCode != null) {
      if (text.containsAny(KG_PREREQ_TERMS)) {
        return PreferredTool("prereqs_kg", mapOf("course" to courseCode), "matched KG_PREREQ_TERMS")
      }
      if (text.containsAny(KG_GE_TERMS)) {
        return PreferredTool("ge_areas_kg", mapOf("course" to courseCode), "matched KG_GE_TERMS")
      }
      if (text.containsAny(KG_TRANSFER_TERMS)) {
        return PreferredTool("transfer_status_kg", mapOf("course" to courseCode), "matched KG_TRANSFER_TERMS")
      }
      if (text.containsAny(KG_COURSE_INFO_TERMS)) {
        return PreferredTool("elumen_course", courseSubjectNumber(courseCode), "matched KG_COURSE_INFO_TERMS")
      }
    }

    // 3. GE-area listing (no course code present) — "what fulfills IGETC 1A?"
    val geCode = extractGeCode(text)
    if (geCode != null) {
      val system = if (text.contains("igetc")) "IGETC" else "CSU"
      return PreferredTool("ge_courses_kg", mapOf("ge_code" to geCode, "system" to system), "matched GE area + system")
    }

    // 4. Sequence — "what order should I take..." (very specific multi-word
    //    phrases, run before the broad PROGRAM/SCHEDULE keyword sets).
    if (text.containsAny(SEQUENCE_TERMS)) {
      return PreferredTool(
        "course_sequence",
        mapOf("pace" to (detectPace(text) ?: "full")),
        "matched SEQUENCE_TERMS",
      )
    }

    // 5. Programs — "what programs / degrees in computer science". Runs
    //    before schedule because PROGRAM_TERMS like "program" and "degree"
    //    are unambiguous, while SCHEDULE_TERMS includes broad words like
    //    "available" and "take" that frequently appear in program questions.
    if (text.containsAny(PROGRAM_TERMS)) {
      return PreferredTool("search_programs", mapOf("q" to rawMessage.trim()), "matched PROGRAM_TERMS")
    }

    // 6. Schedule — "when is X offered", "is there a section of Y this term".
    if (text.containsAny(SCHEDULE_TERMS)) {
      val args = mutableMapOf<String, Any?>("term" to currentTermCode())
      if (courseCode != null) {
        val (subj, num) = parseCourseCode(courseCode)
        args["subject"] = subj
        args["course_number"] = num
      }
      return PreferredTool("live_schedule", args, "matched SCHEDULE_TERMS")
    }

    return null
  }

  /** Whether [tool] is a [ToolKind.CLOUD] tool that needs a network connection. */
  fun isCloudTool(tool: String): Boolean = registry.tools[tool]?.kind == ToolKind.CLOUD

  /** Cloud-fallback notice for an offline device. */
  fun offlineDisclaimer(campus: String): String =
    "I can't reach the live $campus data right now (no internet). " +
      "For class schedule, articulation, or eLumen lookups, open the cloud chatbot at " +
      "https://${campus.lowercase()}-chat.johnnyrobot.ai."

  // ── Keyword sets — mirror backend/app/services/chat.py ────────────────────

  companion object {
    val PROGRAM_TERMS = setOf(
      "program", "degree", "certificate", "certifications", "major", "pathway",
      "requirements", "career", "study",
    )
    val SCHEDULE_TERMS = setOf(
      "schedule", "class", "section", "sections", "instructor", "time",
      "offered", "available", "take",
    )
    val KG_TRANSFER_TERMS = setOf(
      "transferable", "uc transferable", "csu transferable",
      "transfer to uc", "transfer to csu", "transfer to a uc", "transfer to a csu",
    )
    val KG_GE_TERMS = setOf(
      "igetc", "csu-ge", "csuge", "cal-getc", "calgetc", "ge area", "general education",
    )
    val KG_PREREQ_TERMS = setOf(
      "prerequisite", "prereq", "pre-req", "need to take before", "required before",
    )
    val KG_COURSE_INFO_TERMS = setOf(
      "tell me about", "course description", "what is", "describe", "course info", "course details",
    )
    val SEQUENCE_TERMS = setOf(
      "course sequence", "course plan", "course order", "semester plan",
      "what order", "take first", "prerequisite chain", "course roadmap",
      "course map", "course pathway",
      "in what order", "what classes should i take", "what courses should i take",
    )
    val ASSIST_EXPLICIT_TRIGGERS = listOf(
      "articulation", "articulat", "assist.org",
      "transfer to ", "transfers to ", "transfer agreement",
      "transferable to", "satisfies at",
    )
    val ASSIST_TARGET_INSTITUTIONS = listOf(
      "ucla", "uc berkeley", "ucb", "berkeley", "ucsd", "uc san diego",
      "uci", "uc irvine", "ucd", "uc davis", "ucsb", "uc santa barbara",
      "ucsc", "uc santa cruz", "ucr", "uc riverside", "ucm", "uc merced",
      "csun", "cal state northridge", "csula", "cal state la",
      "csulb", "cal state long beach", "long beach state",
      "sdsu", "san diego state", "sfsu", "san francisco state",
      "sjsu", "san jose state", "cpp", "cal poly pomona",
      "cal poly slo", "cal poly san luis obispo", "cal poly",
      "csu fullerton", "csuf", "fullerton",
      "csu dominguez hills", "csudh",
    )
    val ASSIST_TRANSFER_VERBS = listOf("transfer", "satisfies", "satisfy", "count for", "counts for")

    /**
     * Map free-typed pace intent ("part time", "fast track") to one of
     * `full | momentum | part_time`. Mirrors `chat.py::PACE_KEYWORDS`.
     */
    fun detectPace(lowered: String): String? {
      if (PART_TIME_KEYWORDS.any { it in lowered }) return "part_time"
      if (MOMENTUM_KEYWORDS.any { it in lowered }) return "momentum"
      return null
    }

    private val PART_TIME_KEYWORDS = listOf(
      "part time", "part-time", "9 units", "9-unit", "working full time",
      "while working", "evening only", "evenings only",
    )
    private val MOMENTUM_KEYWORDS = listOf(
      "fast track", "fast-track", "accelerated", "ahead of schedule",
      "as fast as possible", "as quickly as possible",
      "18 units", "18-unit", "momentum",
    )

    /** Course-code regex anchors. Apply against the upper-cased message. */
    private val COURSE_ONE_WORD = Regex("""\b([A-Z]{2,5})\s+([A-Z]?\d{1,4}[A-Z]{0,3})\b""")
    private val COURSE_TWO_WORD = Regex("""\b([A-Z]{2,5})\s+([A-Z]{2,5})\s+([A-Z]?\d{1,4}[A-Z]{0,3})\b""")
    private val COURSE_NO_SPACE = Regex("""\b([A-Z]{2,8})(\d{2,4}[A-Z]{0,3})\b""")
    private val GE_CODE_REGEX = Regex("""\b([1-9][A-Z]|[A-Z][1-9])\b""", RegexOption.IGNORE_CASE)

    /**
     * English filler words that the regex would otherwise treat as a 2-5 letter
     * "subject" because they happen to sit before a real course code. Without
     * this list, "for MATH 261" yields "FOR MATH 261" — wrong. Update when the
     * tool router starts mistreating real LACCD subjects.
     */
    private val SUBJECT_BLACKLIST = setOf(
      "FOR", "AND", "WITH", "THE", "TAKE", "TAKES", "THAT", "WHAT", "YOUR", "OUR",
      "ABOUT", "WHEN", "HOW", "DOES", "TELL", "WHERE", "ARE", "WAS", "WERE", "HAVE",
      "BEEN", "BEING", "PREREQS", "PREREQ", "PREREQUISITE", "PREREQUISITES",
      "TRANSFER", "TRANSFERS", "ARTICULATE", "ARTICULATES", "SATISFIES",
      "FROM", "INTO", "UNTO", "UPON", "DOWN", "OVER", "UNDER", "INTO",
      // GE-system tokens that look like subjects but aren't:
      "IGETC", "CSU", "AREA", "GE", "OF", "IN", "AT", "BY",
    )

    /**
     * Extract a course code like "MATH 261", "CHEM C211", or "CO SCI 952"
     * from a free-typed sentence. Returns the canonical "SUBJ NUMBER" form,
     * or null when no code is present.
     *
     * Tries the 2-word subject form first (so "CO SCI 952" wins over "SCI 952"),
     * then the 1-word form, then the no-space form ("MATH261"). Filters out
     * matches whose subject is a common English filler word like "FOR" or "AND".
     */
    fun extractCourseCode(message: String): String? {
      val upper = message.uppercase()
      // 1. Two-word subject ("CO SCI 952", "CHILD DEV 7"). Take the first match
      //    whose first word isn't in the blacklist.
      for (m in COURSE_TWO_WORD.findAll(upper)) {
        val first = m.groupValues[1]
        val second = m.groupValues[2]
        if (first in SUBJECT_BLACKLIST || second in SUBJECT_BLACKLIST) continue
        return "$first $second ${m.groupValues[3]}"
      }
      // 2. Single-word subject ("MATH 261", "CHEM C211").
      for (m in COURSE_ONE_WORD.findAll(upper)) {
        val subj = m.groupValues[1]
        if (subj in SUBJECT_BLACKLIST) continue
        return "$subj ${m.groupValues[2]}"
      }
      // 3. No-space form ("MATH261").
      for (m in COURSE_NO_SPACE.findAll(upper)) {
        val subj = m.groupValues[1]
        if (subj in SUBJECT_BLACKLIST) continue
        return "$subj ${m.groupValues[2]}"
      }
      return null
    }

    /** Pulls a GE area code ("1A", "B2", "5C") out of a message. */
    fun extractGeCode(loweredMessage: String): String? {
      if (!loweredMessage.containsAny(KG_GE_TERMS)) return null
      val match = GE_CODE_REGEX.find(loweredMessage) ?: return null
      return match.groupValues[1].uppercase()
    }

    /**
     * Pull the destination institution name from a message that already
     * matched [matchesAssist]. Returns null when there's no recognizable
     * target — the caller should bail to "ask for clarification".
     */
    fun extractAssistTarget(loweredMessage: String): String? =
      ASSIST_TARGET_INSTITUTIONS.firstOrNull { it in loweredMessage }

    /**
     * Returns true when the message has an explicit ASSIST trigger or pairs
     * a transfer verb with a recognized destination institution.
     */
    fun matchesAssist(loweredMessage: String): Boolean {
      if (ASSIST_EXPLICIT_TRIGGERS.any { it in loweredMessage }) return true
      val hasTarget = ASSIST_TARGET_INSTITUTIONS.any { it in loweredMessage }
      val hasVerb = ASSIST_TRANSFER_VERBS.any { it in loweredMessage }
      return hasTarget && hasVerb
    }

    /**
     * Today's term code, used as the schedule tool's default term parameter.
     * The cloud chat orchestrator computes this dynamically; on-device we
     * approximate from the calendar — Spring=2264, Summer=2265, Fall=2266
     * (2026's term codes). Caller can always override.
     */
    fun currentTermCode(): String {
      val now = java.time.LocalDate.now()
      val year = now.year - 2000  // 26 for 2026
      // LACCD term code = "2{2-digit-year}{4|5|6|7}" where 4=Winter, 5=Spring, 6=Summer, 7=Fall.
      val code = when (now.monthValue) {
        in 1..2 -> "4"
        in 3..5 -> "5"
        in 6..7 -> "6"
        else -> "7"
      }
      return "2$year$code"
    }

    /** Split a normalized "SUBJ NUMBER" code back into its parts for the schedule API. */
    private fun parseCourseCode(code: String): Pair<String, String> {
      val parts = code.split(' ', limit = 2)
      return if (parts.size == 2) parts[0] to parts[1] else code to ""
    }

    /** Convenience for [invokeElumen] which expects subject+number, not the joined code. */
    private fun courseSubjectNumber(code: String): Map<String, Any?> {
      val (subj, num) = parseCourseCode(code)
      return mapOf("subject" to subj, "number" to num)
    }

    private fun String.containsAny(terms: Iterable<String>): Boolean = terms.any { it in this }
  }
}

/**
 * What [ToolRouter.route] returns when keyword pre-filtering picks a tool.
 * The caller (chat orchestrator on the device) constructs a [ToolCall] from
 * this and passes it through [ToolRegistry.invoke].
 */
data class PreferredTool(
  val tool: String,
  val args: Map<String, Any?>,
  /** Why the router picked this — surfaced in the chat-debug overlay only. */
  val reason: String,
) {
  fun toCall(): ToolCall = ToolCall(name = tool, args = args)
}
