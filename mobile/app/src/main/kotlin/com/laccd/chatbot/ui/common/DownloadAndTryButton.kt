/*
 * Copyright 2025 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.common

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.height
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowForward
import androidx.compose.material.icons.outlined.FolderOpen
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.laccd.chatbot.data.Model
import com.laccd.chatbot.data.ModelDownloadStatusType
import com.laccd.chatbot.data.Task
import com.laccd.chatbot.ui.modelmanager.ModelManagerViewModel

/**
 * Opens a model that is already present on the device.
 *
 * Public builds deliberately do not download model weights or collect provider credentials. Users
 * import a compatible local model in Model Manager, or sideload the canonical Gemma file into the
 * app-specific directory documented in README.md.
 */
@Composable
fun DownloadAndTryButton(
  task: Task?,
  model: Model,
  enabled: Boolean,
  downloadStatus: ModelDownloadStatusType?,
  downloadProgress: Float,
  modelManagerViewModel: ModelManagerViewModel,
  onClicked: () -> Unit,
  modifier: Modifier = Modifier,
  modifierWhenExpanded: Modifier = Modifier,
  compact: Boolean = false,
  canShowTryIt: Boolean = true,
  downloadButtonBackgroundColor: Color = MaterialTheme.colorScheme.surfaceContainer,
) {
  val available = downloadStatus == ModelDownloadStatusType.AVAILABLE
  var buttonModifier: Modifier = modifier.height(42.dp)
  if (!compact) buttonModifier = buttonModifier.then(modifierWhenExpanded)

  Button(
    modifier = buttonModifier,
    enabled = enabled && available && canShowTryIt,
    colors =
      ButtonDefaults.buttonColors(
        containerColor =
          if (task == null) MaterialTheme.colorScheme.primary
          else getTaskBgGradientColors(task)[1],
        disabledContainerColor = downloadButtonBackgroundColor,
      ),
    contentPadding = PaddingValues(horizontal = 12.dp),
    onClick = onClicked,
  ) {
    Row(
      verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
      Icon(
        imageVector =
          if (available) Icons.AutoMirrored.Rounded.ArrowForward else Icons.Outlined.FolderOpen,
        contentDescription = null,
      )
      if (!compact) Text(if (available) "Try it" else "Model unavailable")
    }
  }
}
