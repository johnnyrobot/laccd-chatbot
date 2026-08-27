/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data.profile

import androidx.datastore.core.DataStore
import com.laccd.chatbot.proto.Profile
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first

/**
 * local-only student profile store. Mirrors Companero's profile shape
 * (`{ studentId, studentEmail }`, key `course_companion.profile.v1`) so a JSON export from
 * one side imports cleanly into the other.
 *
 * Persisted in `profile.pb` via Proto DataStore. Never read by network code — the only
 * consumer is the chat composer's autofill chip path, which inserts the value into the
 * on-device prompt.
 */
@Singleton
class ProfileStore @Inject constructor(private val dataStore: DataStore<Profile>) {

  val profile: Flow<Profile> = dataStore.data

  suspend fun read(): Profile = dataStore.data.first()

  suspend fun setStudentId(studentId: String) {
    dataStore.updateData { current -> current.toBuilder().setStudentId(studentId.trim()).build() }
  }

  suspend fun setStudentEmail(studentEmail: String) {
    dataStore.updateData { current ->
      current.toBuilder().setStudentEmail(studentEmail.trim().lowercase()).build()
    }
  }

  suspend fun replace(studentId: String, studentEmail: String) {
    dataStore.updateData { _ ->
      Profile.newBuilder()
        .setStudentId(studentId.trim())
        .setStudentEmail(studentEmail.trim().lowercase())
        .build()
    }
  }

  suspend fun clear() {
    dataStore.updateData { Profile.getDefaultInstance() }
  }
}
