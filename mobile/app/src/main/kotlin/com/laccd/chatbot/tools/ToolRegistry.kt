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

import com.laccd.chatbot.data.knowledge.GraphQueries
import com.laccd.chatbot.data.knowledge.KnowledgePack
import com.laccd.chatbot.data.knowledge.RagQueries
import org.json.JSONObject

/**
 * On-device tool catalog. The registry is the single source of truth for both
 * (a) the tool-call schema we surface to the on-device Gemma model, and
 * (b) the dispatcher that turns a parsed tool call into a [ToolResult].
 *
 * Two flavors of tools:
 *
 *  1. **Cloud REST tools** — `live_schedule`, `assist_articulation`,
 *     `search_programs`, `course_sequence`, `elumen_course`. These call the
 *     `/api/tools/...` endpoints implemented in `backend/app/routes/mobile_tools.py`.
 *  2. **On-device KG tools** — `course_info_kg`, `ge_courses_kg`,
 *     `prereqs_kg`, `transfer_status_kg`, `rag_search`. These run against the
 *     per-campus CozoDB pack via [GraphQueries] / [RagQueries].
 *
 * The model is shown the union of these as JSON-schema'd tools and chooses
 * which to invoke. The router ([ToolRouter]) can also short-circuit and call
 * a tool directly when keyword-routing makes the choice obvious — this is
 * the main on-device latency win for high-volume queries.
 */
class ToolRegistry(
  private val laccdApi: LaccdApi,
  private val packProvider: PackProvider,
) {

  /** Map of tool name → [ToolSpec]. Stable ordering for prompt determinism. */
  val tools: Map<String, ToolSpec> = linkedMapOf(
    // ── Cloud REST tools ──
    "live_schedule" to ToolSpec(
      name = "live_schedule",
      description = "Look up the current term's class schedule for a course at a campus. " +
        "Use for questions about days/times, sections, instructors, ZTC offerings.",
      kind = ToolKind.CLOUD,
      schemaJson = """
        {
          "type": "object",
          "properties": {
            "campus":        {"type": "string", "description": "Campus code: LAMC, ELAC, LACC, ..."},
            "term":          {"type": "string", "description": "Term code (e.g., 2264 for Spring 2026)"},
            "subject":       {"type": "string", "description": "Subject prefix (MATH, ENGLISH, ...)"},
            "course_number": {"type": "string"},
            "keyword":       {"type": "string", "description": "Free-text keyword search"}
          },
          "required": ["campus", "term"]
        }
      """.trimIndent(),
    ),
    "assist_articulation" to ToolSpec(
      name = "assist_articulation",
      description = "ASSIST.org articulation: confirm whether a community-college course transfers " +
        "to a UC or CSU institution. Use for questions like 'does CHEM 101 transfer to UCLA?'.",
      kind = ToolKind.CLOUD,
      schemaJson = """
        {
          "type": "object",
          "properties": {
            "course": {"type": "string", "description": "Source course (e.g., 'MATH 261')"},
            "from":   {"type": "string", "description": "Source institution (LACCD campus code)"},
            "to":     {"type": "string", "description": "Destination UC or CSU campus name or code"}
          },
          "required": ["course", "from", "to"]
        }
      """.trimIndent(),
    ),
    "search_programs" to ToolSpec(
      name = "search_programs",
      description = "Search Program Mapper for degrees and certificates. Set campus to limit to one campus, " +
        "or omit to search across all 9 LACCD campuses.",
      kind = ToolKind.CLOUD,
      schemaJson = """
        {
          "type": "object",
          "properties": {
            "q":      {"type": "string", "description": "Search query"},
            "campus": {"type": "string", "description": "Campus code (optional)"}
          },
          "required": ["q"]
        }
      """.trimIndent(),
    ),
    "course_sequence" to ToolSpec(
      name = "course_sequence",
      description = "Generate a multi-semester course plan for a program. Pace controls units per term: " +
        "full=15, momentum=18, part_time=9.",
      kind = ToolKind.CLOUD,
      schemaJson = """
        {
          "type": "object",
          "properties": {
            "program_id": {"type": "string"},
            "campus":     {"type": "string"},
            "pace":       {"type": "string", "enum": ["full", "momentum", "part_time"]}
          },
          "required": ["program_id"]
        }
      """.trimIndent(),
    ),
    "elumen_course" to ToolSpec(
      name = "elumen_course",
      description = "eLumen catalog of record for a single course (canonical title, units, official " +
        "prerequisite/advisory text).",
      kind = ToolKind.CLOUD,
      schemaJson = """
        {
          "type": "object",
          "properties": {
            "subject": {"type": "string"},
            "number":  {"type": "string"},
            "campus":  {"type": "string"}
          },
          "required": ["subject", "number"]
        }
      """.trimIndent(),
    ),

    // ── On-device KG tools ──
    "prereqs_kg" to ToolSpec(
      name = "prereqs_kg",
      description = "On-device prerequisites lookup. Walks the renumbering chain on miss.",
      kind = ToolKind.ON_DEVICE,
      schemaJson = """
        {"type": "object", "properties": {"course": {"type": "string"}}, "required": ["course"]}
      """.trimIndent(),
    ),
    "ge_areas_kg" to ToolSpec(
      name = "ge_areas_kg",
      description = "On-device lookup of which IGETC/CSU GE areas a course fulfills.",
      kind = ToolKind.ON_DEVICE,
      schemaJson = """
        {"type": "object", "properties": {"course": {"type": "string"}}, "required": ["course"]}
      """.trimIndent(),
    ),
    "transfer_status_kg" to ToolSpec(
      name = "transfer_status_kg",
      description = "On-device lookup of whether a course is UC- or CSU-transferable.",
      kind = ToolKind.ON_DEVICE,
      schemaJson = """
        {"type": "object", "properties": {"course": {"type": "string"}}, "required": ["course"]}
      """.trimIndent(),
    ),
    "ge_courses_kg" to ToolSpec(
      name = "ge_courses_kg",
      description = "On-device list of every course at this campus that fulfills a given GE area.",
      kind = ToolKind.ON_DEVICE,
      schemaJson = """
        {
          "type": "object",
          "properties": {
            "ge_code": {"type": "string", "description": "GE area code (e.g., '1A', 'B2')"},
            "system":  {"type": "string", "enum": ["IGETC", "CSU"], "description": "GE system"}
          },
          "required": ["ge_code", "system"]
        }
      """.trimIndent(),
    ),
    "rag_search" to ToolSpec(
      name = "rag_search",
      description = "On-device retrieval against this campus's catalog/website corpus. Use for " +
        "general questions about programs, services, deadlines that aren't in the KG.",
      kind = ToolKind.ON_DEVICE,
      schemaJson = """
        {
          "type": "object",
          "properties": {
            "query": {"type": "string"},
            "k":     {"type": "integer", "default": 6}
          },
          "required": ["query"]
        }
      """.trimIndent(),
    ),
  )

  /**
   * Render the tool catalog as a single JSON-array string for inclusion in
   * the system prompt. Keeps a stable shape so prompt-cache hits don't break
   * when the registry changes.
   */
  fun toolCatalogJson(): String =
    tools.values.joinToString(prefix = "[", postfix = "]", separator = ", ") { spec ->
      """{"name":"${spec.name}","description":${quote(spec.description)},"parameters":${spec.schemaJson}}"""
    }

  /** Dispatch a parsed tool call to the appropriate backend. */
  suspend fun invoke(call: ToolCall, campus: String): ToolResult {
    val spec = tools[call.name]
      ?: return ToolResult.error(call.name, "Unknown tool: ${call.name}")
    return runCatching {
      when (spec.name) {
        "live_schedule" -> invokeSchedule(call, campus)
        "assist_articulation" -> invokeAssist(call, campus)
        "search_programs" -> invokePrograms(call, campus)
        "course_sequence" -> invokeSequence(call, campus)
        "elumen_course" -> invokeElumen(call, campus)
        "prereqs_kg" -> invokePrereqs(call, campus)
        "ge_areas_kg" -> invokeGeAreas(call, campus)
        "transfer_status_kg" -> invokeTransferStatus(call, campus)
        "ge_courses_kg" -> invokeGeCourses(call, campus)
        "rag_search" -> invokeRagSearch(call, campus)
        else -> ToolResult.error(spec.name, "No invoker for ${spec.name}")
      }
    }.getOrElse { exc ->
      ToolResult.error(spec.name, exc.message ?: exc::class.simpleName.orEmpty())
    }
  }

  // ── Cloud invokers ──

  private suspend fun invokeSchedule(call: ToolCall, campus: String): ToolResult {
    val resp = laccdApi.schedule(
      campus = call.arg("campus", campus),
      term = call.arg("term") ?: error("term is required"),
      subject = call.argOrNull("subject"),
      courseNumber = call.argOrNull("course_number"),
      keyword = call.argOrNull("keyword"),
    )
    return ToolResult.ok(call.name, summary = formatSchedule(resp), source = "live")
  }

  private suspend fun invokeAssist(call: ToolCall, campus: String): ToolResult {
    val resp = laccdApi.assist(
      course = call.arg("course") ?: error("course is required"),
      from = call.arg("from", campus),
      to = call.arg("to") ?: error("to is required"),
    )
    return ToolResult.ok(call.name, summary = formatAssist(resp), source = resp.source)
  }

  private suspend fun invokePrograms(call: ToolCall, campus: String): ToolResult {
    val resp = laccdApi.programs(
      query = call.arg("q") ?: error("q is required"),
      campus = call.argOrNull("campus") ?: campus,
    )
    return ToolResult.ok(call.name, summary = formatPrograms(resp), source = "live")
  }

  private suspend fun invokeSequence(call: ToolCall, campus: String): ToolResult {
    val resp = laccdApi.sequence(
      programId = call.arg("program_id") ?: error("program_id is required"),
      campus = call.arg("campus", campus),
      pace = call.arg("pace", "full"),
    )
    return ToolResult.ok(call.name, summary = formatSequence(resp), source = "live")
  }

  private suspend fun invokeElumen(call: ToolCall, campus: String): ToolResult {
    val resp = laccdApi.elumen(
      subject = call.arg("subject") ?: error("subject is required"),
      number = call.arg("number") ?: error("number is required"),
      campus = call.arg("campus", campus),
    )
    return ToolResult.ok(call.name, summary = formatElumen(resp), source = "live")
  }

  // ── On-device invokers ──

  private fun invokePrereqs(call: ToolCall, campus: String): ToolResult {
    val pack = packProvider.openOrNull(campus)
      ?: return ToolResult.notInstalled(call.name, campus)
    val course = call.arg("course") ?: error("course is required")
    val rows = GraphQueries(pack).getPrerequisites(course)
    val summary = if (rows.isEmpty()) "No prerequisites recorded for $course."
    else "Prerequisites for $course:\n" + rows.joinToString("\n") { "- ${it.code}: ${it.title}" }
    return ToolResult.ok(call.name, summary = summary, source = "on-device:${pack.campus}")
  }

  private fun invokeGeAreas(call: ToolCall, campus: String): ToolResult {
    val pack = packProvider.openOrNull(campus)
      ?: return ToolResult.notInstalled(call.name, campus)
    val course = call.arg("course") ?: error("course is required")
    val rows = GraphQueries(pack).getGeAreas(course)
    val summary = if (rows.isEmpty()) "$course doesn't fulfill a GE area in this catalog."
    else "$course fulfills:\n" + rows.joinToString("\n") {
      "- ${it.geSystem} ${it.geCode} (${it.geName})"
    }
    return ToolResult.ok(call.name, summary = summary, source = "on-device:${pack.campus}")
  }

  private fun invokeTransferStatus(call: ToolCall, campus: String): ToolResult {
    val pack = packProvider.openOrNull(campus)
      ?: return ToolResult.notInstalled(call.name, campus)
    val course = call.arg("course") ?: error("course is required")
    val status = GraphQueries(pack).getTransferStatus(course)
      ?: return ToolResult.ok(call.name, summary = "$course not found in catalog.", source = "on-device:${pack.campus}")
    val parts = mutableListOf<String>()
    if (status.csuTransferable) parts.add("CSU-transferable")
    if (status.ucTransferable) parts.add("UC-transferable")
    val transferText = if (parts.isEmpty()) "not UC- or CSU-transferable" else parts.joinToString(", ")
    return ToolResult.ok(
      call.name,
      summary = "${status.code} (${status.title}) is $transferText.",
      source = "on-device:${pack.campus}",
    )
  }

  private fun invokeGeCourses(call: ToolCall, campus: String): ToolResult {
    val pack = packProvider.openOrNull(campus)
      ?: return ToolResult.notInstalled(call.name, campus)
    val rows = GraphQueries(pack).getGeCourses(
      geCode = call.arg("ge_code") ?: error("ge_code is required"),
      system = call.arg("system", "IGETC"),
    )
    val summary = if (rows.isEmpty()) "No courses found." else
      rows.joinToString("\n") { "- ${it.code}: ${it.title} (${it.unitsMax} u)" }
    return ToolResult.ok(call.name, summary = summary, source = "on-device:${pack.campus}")
  }

  private fun invokeRagSearch(call: ToolCall, campus: String): ToolResult {
    val pack = packProvider.openOrNull(campus)
      ?: return ToolResult.notInstalled(call.name, campus)
    val embedder = packProvider.embedderFor(pack)
    val k = call.argInt("k") ?: 6
    val hits = RagQueries(pack, embedder).search(
      query = call.arg("query") ?: error("query is required"),
      k = k,
    )
    val summary = if (hits.isEmpty()) "No relevant passages found." else
      hits.joinToString("\n\n") {
        "${it.text}\n[${it.sourceTitle.ifBlank { it.sourceUrl }}](${it.sourceUrl})"
      }
    return ToolResult.ok(call.name, summary = summary, source = "on-device:${pack.campus}")
  }

  // ── Formatters ──

  private fun formatSchedule(resp: ScheduleResponse): String {
    if (resp.totalSections == 0) return "No sections found for ${resp.campus} ${resp.termName}."
    return "${resp.totalSections} sections at ${resp.campus} (${resp.termName}):\n" +
      resp.sections.take(8).joinToString("\n") { sec ->
        "- ${sec.courseCode} #${sec.section} ${sec.daysTimes ?: "TBA"} ${sec.location ?: ""} • " +
          (sec.instructor ?: "TBA")
      }
  }

  private fun formatAssist(resp: AssistResponse): String {
    if (resp.matches.isEmpty()) {
      return "No ${resp.areaKey} match found from ${resp.from} to ${resp.to} for ${resp.course}."
    }
    return "Articulation matches (${resp.areaKey}, ${resp.from} → ${resp.to}):\n" +
      resp.matches.take(5).joinToString("\n") { match ->
        val code = match.code ?: match.courseCode ?: "?"
        "- $code: ${match.title ?: ""}"
      }
  }

  private fun formatPrograms(resp: ProgramsResponse): String {
    if (resp.programs.isEmpty()) return "No programs match \"${resp.query}\"."
    return "${resp.count} program(s) match \"${resp.query}\":\n" +
      resp.programs.take(8).joinToString("\n") { hit ->
        "- ${hit.title} (${hit.awardType ?: "?"}) at ${hit.campusCode ?: ""}"
      }
  }

  private fun formatSequence(resp: SequenceResponse): String {
    if (resp.sequence.isEmpty()) return "No sequence generated for ${resp.programName}."
    val terms = resp.sequence.joinToString("\n") { term ->
      "Term ${term.termNo} (${term.name}, ${term.units} u):\n" +
        term.courses.joinToString("\n") { "  - ${it.code}: ${it.title} (${it.units} u)" }
    }
    return "${resp.programName} • ${resp.pace} pace • ${resp.termsToComplete} terms\n$terms"
  }

  private fun formatElumen(resp: ElumenResponse): String {
    val c = resp.course
    return buildString {
      append("${c.code.ifBlank { "${resp.subject} ${resp.number}" }}: ${c.title}")
      val units = c.units ?: c.unitsMax ?: c.unitsMin
      if (units != null) append(" (${units} u)")
      c.description?.takeIf { it.isNotBlank() }?.let { append("\n").append(it) }
      val prereqText =
        c.prerequisite?.takeIf { it.isNotBlank() }
          ?: c.prerequisites.flatten().filter { it.isNotBlank() }.joinToString(", ")
      prereqText.takeIf { it.isNotBlank() }?.let { append("\nPrereq: ").append(it) }
      val coreqText =
        c.corequisite?.takeIf { it.isNotBlank() }
          ?: c.corequisites.flatten().filter { it.isNotBlank() }.joinToString(", ")
      coreqText.takeIf { it.isNotBlank() }?.let { append("\nCoreq: ").append(it) }
      val advisoryText =
        c.advisory?.takeIf { it.isNotBlank() }
          ?: c.advisories.filter { it.isNotBlank() }.joinToString(", ")
      advisoryText.takeIf { it.isNotBlank() }?.let { append("\nAdvisory: ").append(it) }
    }
  }

  private fun quote(s: String): String =
    JSONObject().put("v", s).getString("v").let {
      // JSONObject quoting handles escaping; use the put/get round-trip to get a safe string,
      // then re-encode with quotes.
      "\"" + it.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
    }
}

/** Bridges [ToolRegistry] back to a [KnowledgePack] without a Hilt dependency. */
interface PackProvider {
  fun openOrNull(campus: String): KnowledgePack?
  fun embedderFor(pack: KnowledgePack): com.laccd.chatbot.data.knowledge.Embedder
}

enum class ToolKind { CLOUD, ON_DEVICE }

/** Static metadata shown to the model in the prompt and used by [ToolRouter]. */
data class ToolSpec(
  val name: String,
  val description: String,
  val kind: ToolKind,
  /** JSON-schema document describing the tool's parameters. */
  val schemaJson: String,
)

/** A parsed `{"tool": "...", "arguments": {...}}` from Gemma's stream. */
data class ToolCall(val name: String, val args: Map<String, Any?>) {
  fun arg(key: String): String? = (args[key] as? String)?.takeIf { it.isNotBlank() }
  fun arg(key: String, default: String): String = arg(key) ?: default
  fun argOrNull(key: String): String? = arg(key)
  fun argInt(key: String): Int? = (args[key] as? Number)?.toInt()
}

/** What a tool invocation returns. [summary] is fed back to the model as context. */
data class ToolResult(
  val tool: String,
  val ok: Boolean,
  val summary: String,
  /** Free-form provenance ("live", "on-device:LAMC", "cache", "unavailable"). */
  val source: String,
  val error: String? = null,
) {
  companion object {
    fun ok(tool: String, summary: String, source: String): ToolResult =
      ToolResult(tool, ok = true, summary = summary, source = source)

    fun error(tool: String, message: String): ToolResult =
      ToolResult(tool, ok = false, summary = "Tool $tool failed: $message", source = "error", error = message)

    fun notInstalled(tool: String, campus: String): ToolResult =
      ToolResult(
        tool,
        ok = false,
        summary = "The on-device knowledge pack for $campus isn't installed yet. " +
          "Open Settings → Knowledge packs to download it.",
        source = "unavailable",
      )
  }
}
