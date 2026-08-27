/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.common

import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.laccd.chatbot.data.DataStoreRepository
import com.laccd.chatbot.data.campus.Campus
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn

/**
 * exposes the user's selected [Campus] to the entire UI tree. Reads from the
 * Settings DataStore via Hilt, defaults to [Campus.DEFAULT] until the picker writes a value.
 *
 * Downstream Composables read it via `LocalCampus.current` (e.g. `GalleryTheme` for color
 * scheme seeding, system prompt builders for the on-device prompt, the home screen for the
 * "you're chatting with the assistant for {nickname}" subtitle).
 */
val LocalCampus = staticCompositionLocalOf { Campus.DEFAULT }

@Composable
fun CampusProvider(content: @Composable () -> Unit) {
  val viewModel: CampusProviderViewModel = hiltViewModel()
  val campus by viewModel.campus.collectAsState()
  CompositionLocalProvider(LocalCampus provides campus, content = content)
}

@HiltViewModel
class CampusProviderViewModel @Inject constructor(repository: DataStoreRepository) : ViewModel() {
  val campus: StateFlow<Campus> =
    repository
      .observeSelectedCampus()
      .map { code -> Campus.byCodeOrNull(code) ?: Campus.DEFAULT }
      .stateIn(
        scope = viewModelScope,
        started = SharingStarted.WhileSubscribed(5_000L),
        initialValue = Campus.DEFAULT,
      )
}
