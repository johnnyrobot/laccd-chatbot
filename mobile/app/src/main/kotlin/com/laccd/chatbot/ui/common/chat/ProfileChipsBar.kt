/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.common.chat

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import com.laccd.chatbot.R
import com.laccd.chatbot.ui.profile.ProfileViewModel

/**
 * chips that appear above the chat composer when the student has saved a Student
 * ID or email in their Profile screen. Tapping a chip *appends* the formatted token to the
 * current composer text via [onInsert] — the value never leaves the device, it just goes
 * into the on-device prompt next time the student hits send.
 *
 * Hidden entirely when both fields are blank, so users who haven't filled out a profile
 * don't see empty chrome.
 */
@Composable
fun ProfileChipsBar(
  onInsert: (String) -> Unit,
  modifier: Modifier = Modifier,
  viewModel: ProfileViewModel = hiltViewModel(),
) {
  val profile by viewModel.profile.collectAsState()
  val hasId = profile.studentId.isNotBlank()
  val hasEmail = profile.studentEmail.isNotBlank()
  if (!hasId && !hasEmail) return

  val idTemplate = stringResource(R.string.laccd_chat_chip_insert_id, profile.studentId)
  val emailTemplate = stringResource(R.string.laccd_chat_chip_insert_email, profile.studentEmail)
  val idLabel = stringResource(R.string.laccd_chat_chip_label_id)
  val emailLabel = stringResource(R.string.laccd_chat_chip_label_email)

  Row(
    modifier =
      modifier
        .fillMaxWidth()
        .horizontalScroll(rememberScrollState())
        .padding(horizontal = 8.dp, vertical = 4.dp),
    horizontalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    if (hasId) {
      AssistChip(
        onClick = { onInsert(idTemplate) },
        label = { Text(idLabel) },
        elevation = AssistChipDefaults.assistChipElevation(),
      )
    }
    if (hasEmail) {
      AssistChip(
        onClick = { onInsert(emailTemplate) },
        label = { Text(emailLabel) },
        elevation = AssistChipDefaults.assistChipElevation(),
      )
    }
  }
}

/**
 * Append a chip's token to the current composer text. Inserts a leading space when the
 * existing text doesn't end with whitespace and is non-empty.
 */
fun appendChipToken(current: String, token: String): String =
  when {
    current.isEmpty() -> token
    current.last().isWhitespace() -> current + token
    else -> "$current $token"
  }
