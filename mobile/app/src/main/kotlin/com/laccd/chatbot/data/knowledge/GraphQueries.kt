/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data.knowledge

/**
 * Five Datalog rules ported from `backend/app/services/neo4j_service.py`. They run
 * against the per-campus CozoDB pack opened by [KnowledgePack]:
 *
 *   1. [getPrerequisites]   ↔ neo4j_service.get_prerequisites
 *   2. [getCorequisites]    ↔ neo4j_service.get_course_corequisites
 *   3. [getAdvisories]      ↔ neo4j_service.get_course_advisories
 *   4. [getGeAreas]         ↔ neo4j_service.get_course_ge_areas
 *   5. [getTransferStatus]  ↔ neo4j_service.get_course_transfer_status
 *   6. [getGeCourses]       ↔ neo4j_service.get_ge_courses
 *
 * Plus the renumbering walker [resolveCourseCode] (≤3 hops, breaks on cycles)
 * that mirrors `backend/app/services/neo4j_service.py::resolve_course_code`.
 *
 * Each method runs against [KnowledgePack.client]. They normalize the user's
 * course code first ("math 261" → "MATH 261") and walk the renumbering chain
 * on miss, so callers can ask in either old or new code form.
 *
 * Note on Cozo parameter syntax: parameters are referenced as `$name` in the
 * script. We use `${'$'}` inside Kotlin triple-quoted strings to emit a literal
 * dollar sign without Kotlin attempting string interpolation.
 */
class GraphQueries(private val pack: KnowledgePack) {

  // ── Prereqs / coreqs / advisories ────────────────────────────────────────

  /** Direct prereqs of [courseCode]. Walks renumbering chain if no direct match. */
  fun getPrerequisites(courseCode: String): List<Prerequisite> {
    val resolved = resolveCourseCode(courseCode) ?: return emptyList()
    val rs = pack.client.runWithParams(
      """
      ?[parent_code, parent_title, kind, group_id] :=
        *has_prerequisite[child_code, parent_code, kind, group_id, _campus],
        child_code = ${'$'}code,
        *course{code: parent_code => parent_title};
      """.trimIndent(),
      mapOf("code" to resolved),
    )
    return rs.rows.map {
      Prerequisite(
        code = it.string("parent_code"),
        title = it.string("parent_title"),
        kind = it.string("kind", "prereq"),
        groupId = it.int("group_id"),
      )
    }
  }

  /** Direct corequisites of [courseCode] (undirected — query returns both directions). */
  fun getCorequisites(courseCode: String): List<RelatedCourse> {
    val resolved = resolveCourseCode(courseCode) ?: return emptyList()
    val rs = pack.client.runWithParams(
      """
      ?[other_code, other_title] :=
        *has_corequisite[a_code, b_code, _campus],
        a_code = ${'$'}code,
        other_code = b_code,
        *course{code: other_code => other_title};
      ?[other_code, other_title] :=
        *has_corequisite[a_code, b_code, _campus],
        b_code = ${'$'}code,
        other_code = a_code,
        *course{code: other_code => other_title};
      """.trimIndent(),
      mapOf("code" to resolved),
    )
    return rs.rows.map {
      RelatedCourse(code = it.string("other_code"), title = it.string("other_title"))
    }
  }

  /** Advisories on [courseCode]. */
  fun getAdvisories(courseCode: String): List<RelatedCourse> {
    val resolved = resolveCourseCode(courseCode) ?: return emptyList()
    val rs = pack.client.runWithParams(
      """
      ?[parent_code, parent_title] :=
        *has_advisory[child_code, parent_code, _campus],
        child_code = ${'$'}code,
        *course{code: parent_code => parent_title};
      """.trimIndent(),
      mapOf("code" to resolved),
    )
    return rs.rows.map {
      RelatedCourse(code = it.string("parent_code"), title = it.string("parent_title"))
    }
  }

  // ── GE / transfer ────────────────────────────────────────────────────────

  /** GE areas [courseCode] fulfills. Walks renumbering chain on miss. */
  fun getGeAreas(courseCode: String): List<GeArea> {
    val resolved = resolveCourseCode(courseCode) ?: return emptyList()
    val rs = pack.client.runWithParams(
      """
      ?[ge_code, ge_system, ge_name] :=
        *fulfills_ge[course_code, ge_code, ge_system, _campus],
        course_code = ${'$'}code,
        *goal_area{code: ge_code, system: ge_system => ge_name};
      """.trimIndent(),
      mapOf("code" to resolved),
    )
    return rs.rows.map {
      GeArea(
        geCode = it.string("ge_code"),
        geName = it.string("ge_name"),
        geSystem = it.string("ge_system"),
      )
    }
  }

  /**
   * UC/CSU transferability + canonical title for [courseCode]. Returns null
   * when the code can't be resolved even after walking renumbering.
   */
  fun getTransferStatus(courseCode: String): TransferStatus? {
    val resolved = resolveCourseCode(courseCode) ?: return null
    val rs = pack.client.runWithParams(
      """
      ?[code, title, csu_transferable, uc_transferable] :=
        *course{code: ${'$'}resolved => title, csu_transferable, uc_transferable},
        code = ${'$'}resolved;
      """.trimIndent(),
      mapOf("resolved" to resolved),
    )
    val row = rs.rows.firstOrNull() ?: return null
    return TransferStatus(
      code = row.string("code"),
      title = row.string("title"),
      csuTransferable = row.bool("csu_transferable"),
      ucTransferable = row.bool("uc_transferable"),
    )
  }

  /** Every course in this campus that fulfills [geCode] under [system]. */
  fun getGeCourses(geCode: String, system: String): List<GeCourse> {
    val rs = pack.client.runWithParams(
      """
      ?[course_code, title, units_min, units_max] :=
        *fulfills_ge[course_code, ge_code, ge_system, _campus],
        ge_code = ${'$'}ge_code,
        ge_system = ${'$'}system,
        *course{code: course_code => title, units_min, units_max};
      :order course_code;
      """.trimIndent(),
      mapOf("ge_code" to geCode.uppercase(), "system" to system.uppercase()),
    )
    return rs.rows.map {
      GeCourse(
        code = it.string("course_code"),
        title = it.string("title"),
        unitsMin = it.double("units_min"),
        unitsMax = it.double("units_max"),
      )
    }
  }

  // ── Renumbering ──────────────────────────────────────────────────────────

  /**
   * Walk the `renumbered_to` chain from [courseCode] up to [maxHops] times.
   * Returns the canonical (latest) code if the original isn't found in
   * `course` directly, or [courseCode] itself if it already exists. Returns
   * null when no node along the chain is in this campus's pack.
   *
   * Mirrors `backend/app/services/neo4j_service.py::resolve_course_code` — the
   * walk is bounded so a cycle (which shouldn't exist but is cheap to defend
   * against) can't hang the query layer.
   */
  fun resolveCourseCode(courseCode: String, maxHops: Int = 3): String? {
    val normalized = normalizeCourseCode(courseCode)
    if (courseExists(normalized)) return normalized

    var current = normalized
    val seen = mutableSetOf(current)
    repeat(maxHops) {
      val next = nextRenumbering(current) ?: return@repeat
      if (next in seen) return null
      if (courseExists(next)) return next
      seen.add(next)
      current = next
    }
    return null
  }

  private fun courseExists(code: String): Boolean {
    val rs = pack.client.runWithParams(
      "?[c] := *course{code: ${'$'}code => _t}, c = ${'$'}code;",
      mapOf("code" to code),
    )
    return !rs.isEmpty
  }

  private fun nextRenumbering(fromCode: String): String? {
    val rs = pack.client.runWithParams(
      """
      ?[to_code] := *renumbered_to[from_code, to_code, _campus, _year],
                    from_code = ${'$'}code;
      """.trimIndent(),
      mapOf("code" to fromCode),
    )
    return rs.rows.firstOrNull()?.string("to_code")
  }

  // ── Utility ──────────────────────────────────────────────────────────────

  /**
   * Normalize a free-typed course code to the pack's canonical form
   * `"SUBJECT NUMBER"`. Uppercases the subject, leaves the number untouched
   * (CCC numbers can be alphanumeric like `"C211"` or `"261L"`). Tolerates
   * absent space ("MATH261" → "MATH 261") and double spaces.
   */
  fun normalizeCourseCode(input: String): String {
    val trimmed = input.trim().uppercase()
    if (trimmed.isEmpty()) return trimmed
    if (' ' in trimmed) {
      // Collapse multiple spaces to single space.
      return trimmed.split(Regex("\\s+")).joinToString(" ")
    }
    // Find the first digit; everything before is the subject. If none, return as-is.
    val firstDigit = trimmed.indexOfFirst { it.isDigit() }
    if (firstDigit <= 0) return trimmed
    return trimmed.substring(0, firstDigit) + " " + trimmed.substring(firstDigit)
  }
}

// ── Data classes ───────────────────────────────────────────────────────────

data class Prerequisite(
  val code: String,
  val title: String,
  /** "prereq", "or-prereq" (sibling within an alternation group), etc. */
  val kind: String,
  /** Sibling-group id used to render "X or Y" lists. */
  val groupId: Int,
)

data class RelatedCourse(val code: String, val title: String)

data class GeArea(val geCode: String, val geName: String, val geSystem: String)

data class TransferStatus(
  val code: String,
  val title: String,
  val csuTransferable: Boolean,
  val ucTransferable: Boolean,
)

data class GeCourse(
  val code: String,
  val title: String,
  val unitsMin: Double,
  val unitsMax: Double,
)
