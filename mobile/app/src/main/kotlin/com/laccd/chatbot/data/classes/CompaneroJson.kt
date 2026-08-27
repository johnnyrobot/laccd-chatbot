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

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import java.util.UUID

/**
 * bidirectional converter between [ClassEntry] and Companero PWA's exact JSON
 * shape. Verified against the original Companero export format:
 *
 *   - Class entries: `{ id, name, section, daysTimes, location, notes }`
 *   - Profile: `{ studentId, studentEmail }`
 *   - Wrapper accepted by Companero's `importData`: `{ classes: [...] }` or
 *     `{ profile: {...}, classes: [...] }`. We export the second form when a profile is
 *     supplied so a single file round-trips both halves.
 *
 * Pure module: no Android dependencies, so unit tests can exercise the round trip without
 * Robolectric.
 */
object CompaneroJson {
  private const val MAX_CLASSES = 500
  private const val MAX_FIELD_CHARS = 500
  private const val MAX_NOTES_CHARS = 2_000
  private val json = Json {
    prettyPrint = true
    ignoreUnknownKeys = true
    encodeDefaults = true
  }

  private fun JsonObject.textOrNull(key: String): String? {
    val value = this[key] ?: return ""
    val primitive = value as? JsonPrimitive ?: return null
    if (!primitive.isString) return null
    return primitive.contentOrNull?.trim().orEmpty()
  }

  private fun JsonObject.text(key: String): String = textOrNull(key).orEmpty()

  @Serializable
  data class CompaneroProfileDto(
    @SerialName("studentId") val studentId: String = "",
    @SerialName("studentEmail") val studentEmail: String = "",
  )

  @Serializable
  data class CompaneroClassDto(
    @SerialName("id") val id: String = "",
    @SerialName("name") val name: String = "",
    @SerialName("section") val section: String = "",
    @SerialName("daysTimes") val daysTimes: String = "",
    @SerialName("location") val location: String = "",
    @SerialName("notes") val notes: String = "",
  )

  @Serializable
  data class CompaneroExport(
    @SerialName("profile") val profile: CompaneroProfileDto? = null,
    @SerialName("classes") val classes: List<CompaneroClassDto> = emptyList(),
  )

  fun encode(entries: List<ClassEntry>, profile: CompaneroProfileDto? = null): String {
    val sorted = entries.sortedBy { it.orderIndex }
    val payload =
      CompaneroExport(
        profile = profile,
        classes =
          sorted.map {
            CompaneroClassDto(
              id = it.id,
              name = it.name,
              section = it.section,
              daysTimes = it.daysTimes,
              location = it.location,
              notes = it.notes,
            )
          },
      )
    return json.encodeToString(CompaneroExport.serializer(), payload)
  }

  /**
   * Parse a Companero JSON document. Accepts either a `{ classes: [...] }` wrapper, a bare
   * array `[ ... ]`, or a profile-only object `{ profile: {...} }`. Unknown / malformed
   * entries are dropped (mirrors Companero's `cleaned` pass) — but [name] is required;
   * entries missing a non-empty name are skipped.
   *
   * Returned entries carry sequential `orderIndex` values starting at 0.
   */
  fun decode(raw: String): Decoded {
    val parsed: JsonElement =
      try {
        json.parseToJsonElement(raw)
      } catch (_: Exception) {
        return Decoded(emptyList(), null)
      }
    val classesArr =
      when {
        parsed is JsonObject && parsed.containsKey("classes") ->
          parsed["classes"]?.takeIf { it is kotlinx.serialization.json.JsonArray }?.jsonArray
        parsed is kotlinx.serialization.json.JsonArray -> parsed
        else -> null
      }
    if (classesArr != null) {
      if (classesArr.size > MAX_CLASSES) return Decoded(emptyList(), null)
      for (element in classesArr) {
        val obj = element as? JsonObject ?: continue
        for (key in listOf("id", "name", "section", "daysTimes", "location", "notes")) {
          if (obj.textOrNull(key) == null) return Decoded(emptyList(), null)
        }
        for (key in listOf("id", "name", "section", "daysTimes", "location")) {
          if (obj.text(key).length > MAX_FIELD_CHARS) {
            return Decoded(emptyList(), null)
          }
        }
        if (obj.text("notes").length > MAX_NOTES_CHARS) {
          return Decoded(emptyList(), null)
        }
      }
    }
    val classes =
      classesArr
        ?.mapIndexedNotNull { idx, element ->
          val obj = element as? JsonObject ?: return@mapIndexedNotNull null
          val name = obj.text("name")
          if (name.isEmpty()) return@mapIndexedNotNull null
          val rawId = obj.text("id")
          ClassEntry(
            id = rawId.ifEmpty { UUID.randomUUID().toString() },
            name = name,
            section = obj.text("section"),
            daysTimes = obj.text("daysTimes"),
            location = obj.text("location"),
            notes = obj.text("notes"),
            orderIndex = idx,
          )
        }
        ?: emptyList()
    val profile =
      (parsed as? JsonObject)?.get("profile")?.let { p ->
        val o = p as? JsonObject ?: return@let null
        if (o.textOrNull("studentId") == null || o.textOrNull("studentEmail") == null) {
          return Decoded(emptyList(), null)
        }
        val studentId = o.text("studentId")
        val studentEmail = o.text("studentEmail")
        if (studentId.length > MAX_FIELD_CHARS || studentEmail.length > MAX_FIELD_CHARS) null
        else CompaneroProfileDto(studentId = studentId, studentEmail = studentEmail)
      }
    return Decoded(classes, profile)
  }

  data class Decoded(val classes: List<ClassEntry>, val profile: CompaneroProfileDto?)
}
