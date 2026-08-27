/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.classes

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.laccd.chatbot.data.classes.ClassEntry
import com.laccd.chatbot.data.classes.ClassesRepository
import com.laccd.chatbot.data.classes.CompaneroJson
import com.laccd.chatbot.data.profile.ProfileStore
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

@HiltViewModel
class ClassesViewModel
@Inject
constructor(
  private val repository: ClassesRepository,
  private val profileStore: ProfileStore,
) : ViewModel() {

  private val _query = MutableStateFlow("")
  val query: StateFlow<String> = _query.asStateFlow()

  val items: StateFlow<List<ClassEntry>> =
    combine(repository.observeAll(), _query) { all, q -> ClassesRepository.filter(all, q) }
      .stateIn(
        scope = viewModelScope,
        started = SharingStarted.WhileSubscribed(5_000L),
        initialValue = emptyList(),
      )

  fun setQuery(q: String) {
    _query.value = q
  }

  fun add(
    name: String,
    section: String,
    daysTimes: String,
    location: String,
    notes: String,
    onResult: (ClassEntry?) -> Unit = {},
  ) {
    val cleaned = name.trim()
    if (cleaned.isEmpty()) {
      onResult(null)
      return
    }
    viewModelScope.launch {
      val entry = repository.add(cleaned, section, daysTimes, location, notes)
      onResult(entry)
    }
  }

  fun update(entry: ClassEntry) {
    if (entry.name.isBlank()) return
    viewModelScope.launch { repository.update(entry) }
  }

  fun delete(entry: ClassEntry) {
    viewModelScope.launch { repository.delete(entry) }
  }

  fun moveUp(id: String) {
    viewModelScope.launch { repository.move(id, delta = -1) }
  }

  fun moveDown(id: String) {
    viewModelScope.launch { repository.move(id, delta = 1) }
  }

  /** Encode current classes + the locally-stored profile in Companero's JSON shape. */
  suspend fun exportJson(): String {
    val classes = repository.listAll()
    val p = profileStore.read()
    val dto =
      if (p.studentId.isBlank() && p.studentEmail.isBlank()) {
        null
      } else {
        CompaneroJson.CompaneroProfileDto(
          studentId = p.studentId,
          studentEmail = p.studentEmail,
        )
      }
    return CompaneroJson.encode(classes, dto)
  }

  /**
   * Import a Companero-shaped JSON document. Replaces the existing class list (mirrors
   * Companero's importData). If the document includes a profile section and the local
   * profile is empty, the import seeds it; otherwise existing profile is preserved.
   */
  suspend fun importJson(raw: String): ImportResult {
    val decoded = CompaneroJson.decode(raw)
    if (decoded.classes.isEmpty()) {
      return ImportResult.Failure
    }
    repository.replaceAll(decoded.classes)
    decoded.profile?.let { incoming ->
      val existing = profileStore.read()
      if (existing.studentId.isBlank() && existing.studentEmail.isBlank()) {
        profileStore.replace(
          studentId = incoming.studentId,
          studentEmail = incoming.studentEmail,
        )
      }
    }
    return ImportResult.Success(decoded.classes.size)
  }

  sealed interface ImportResult {
    data class Success(val count: Int) : ImportResult

    data object Failure : ImportResult
  }
}
