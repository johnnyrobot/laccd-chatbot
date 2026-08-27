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

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import com.laccd.chatbot.R

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProfileScreen(onBack: () -> Unit, viewModel: ProfileViewModel = hiltViewModel()) {
  val profile by viewModel.profile.collectAsState()
  var studentId by remember { mutableStateOf(profile.studentId) }
  var studentEmail by remember { mutableStateOf(profile.studentEmail) }

  // Keep local fields in sync when DataStore emits.
  LaunchedEffect(profile.studentId, profile.studentEmail) {
    studentId = profile.studentId
    studentEmail = profile.studentEmail
  }

  Scaffold(
    topBar = {
      TopAppBar(
        title = { Text(stringResource(R.string.laccd_profile_title)) },
        navigationIcon = {
          IconButton(onClick = onBack) {
            Icon(
              Icons.AutoMirrored.Filled.ArrowBack,
              contentDescription = stringResource(R.string.cd_menu),
            )
          }
        },
      )
    }
  ) { padding ->
    Column(
      modifier =
        Modifier.fillMaxSize().padding(padding).padding(20.dp).verticalScroll(rememberScrollState()),
      verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
      Text(
        text = stringResource(R.string.laccd_profile_intro),
        style = MaterialTheme.typography.bodyMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
      )

      Text(
        text = stringResource(R.string.laccd_profile_privacy),
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
      )

      OutlinedTextField(
        value = studentId,
        onValueChange = { studentId = it.filter { ch -> ch.isDigit() }.take(20) },
        label = { Text(stringResource(R.string.laccd_profile_student_id_label)) },
        placeholder = { Text(stringResource(R.string.laccd_profile_student_id_placeholder)) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        modifier = Modifier.fillMaxWidth(),
      )

      OutlinedTextField(
        value = studentEmail,
        onValueChange = { studentEmail = it.trim().take(254) },
        label = { Text(stringResource(R.string.laccd_profile_email_label)) },
        placeholder = { Text(stringResource(R.string.laccd_profile_email_placeholder)) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
        modifier = Modifier.fillMaxWidth(),
      )

      Spacer(Modifier.height(8.dp))

      Button(
        onClick = { viewModel.save(studentId, studentEmail) },
        modifier = Modifier.fillMaxWidth(),
      ) {
        Text(stringResource(R.string.laccd_profile_save))
      }

      OutlinedButton(onClick = { viewModel.clear() }, modifier = Modifier.fillMaxWidth()) {
        Text(stringResource(R.string.laccd_profile_clear))
      }
    }
  }
}
