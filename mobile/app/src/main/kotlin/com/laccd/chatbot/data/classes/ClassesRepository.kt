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

import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

/**
 * thin wrapper around [ClassDao] handling search, reorder, and import/export
 * concerns so the ViewModel doesn't need to know about Room.
 */
@Singleton
class ClassesRepository @Inject constructor(private val dao: ClassDao) {

  fun observeAll(): Flow<List<ClassEntry>> = dao.observeAll()

  fun observeFiltered(query: String): Flow<List<ClassEntry>> =
    dao.observeAll().map { all -> filter(all, query) }

  suspend fun listAll(): List<ClassEntry> = dao.listAll()

  suspend fun add(
    name: String,
    section: String,
    daysTimes: String,
    location: String,
    notes: String,
  ): ClassEntry {
    val nextOrder = dao.maxOrderIndex() + 1
    val entry =
      ClassEntry(
        name = name.trim(),
        section = section.trim(),
        daysTimes = daysTimes.trim(),
        location = location.trim(),
        notes = notes.trim(),
        orderIndex = nextOrder,
      )
    dao.upsert(entry)
    return entry
  }

  suspend fun update(entry: ClassEntry) {
    dao.update(
      entry.copy(
        name = entry.name.trim(),
        section = entry.section.trim(),
        daysTimes = entry.daysTimes.trim(),
        location = entry.location.trim(),
        notes = entry.notes.trim(),
      )
    )
  }

  suspend fun delete(entry: ClassEntry) = dao.delete(entry)

  /**
   * Move an entry up (delta = -1) or down (delta = +1) by swapping `orderIndex` with its
   * neighbor. Mirrors Companero's up/down arrows. No-ops at the edges.
   */
  suspend fun move(id: String, delta: Int) {
    if (delta != -1 && delta != 1) return
    val ordered = dao.listAll()
    val idx = ordered.indexOfFirst { it.id == id }
    if (idx == -1) return
    val newIdx = idx + delta
    if (newIdx < 0 || newIdx >= ordered.size) return
    val a = ordered[idx]
    val b = ordered[newIdx]
    val updated =
      ordered.toMutableList().apply {
        set(idx, a.copy(orderIndex = b.orderIndex))
        set(newIdx, b.copy(orderIndex = a.orderIndex))
      }
    dao.upsertAll(updated)
  }

  suspend fun replaceAll(entries: List<ClassEntry>) {
    val ordered = entries.mapIndexed { i, e -> e.copy(orderIndex = i) }
    // The two ops are not in a single SQL transaction; replaceAll runs only on user-initiated
    // import where partial failure is recoverable (the user can re-import).
    dao.clearAll()
    dao.upsertAll(ordered)
  }

  companion object {
    fun filter(entries: List<ClassEntry>, rawQuery: String): List<ClassEntry> {
      val q = rawQuery.trim().lowercase()
      if (q.isEmpty()) return entries
      return entries.filter { e ->
        e.name.lowercase().contains(q) ||
          e.section.lowercase().contains(q) ||
          e.daysTimes.lowercase().contains(q) ||
          e.location.lowercase().contains(q) ||
          e.notes.lowercase().contains(q)
      }
    }
  }
}
