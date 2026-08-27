/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.profile

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.laccd.chatbot.data.profile.ProfileStore
import com.laccd.chatbot.proto.Profile
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

@HiltViewModel
class ProfileViewModel @Inject constructor(private val store: ProfileStore) : ViewModel() {

  val profile: StateFlow<Profile> =
    store.profile.stateIn(
      scope = viewModelScope,
      started = SharingStarted.WhileSubscribed(5_000L),
      initialValue = Profile.getDefaultInstance(),
    )

  fun save(studentId: String, studentEmail: String) {
    viewModelScope.launch { store.replace(studentId = studentId, studentEmail = studentEmail) }
  }

  fun clear() {
    viewModelScope.launch { store.clear() }
  }
}
