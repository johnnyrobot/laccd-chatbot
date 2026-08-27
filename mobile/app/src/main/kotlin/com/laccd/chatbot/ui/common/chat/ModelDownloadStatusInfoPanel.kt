/*
 * Copyright 2025 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.laccd.chatbot.ui.common.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.laccd.chatbot.data.Model
import com.laccd.chatbot.data.Task
import com.laccd.chatbot.ui.modelmanager.ModelManagerViewModel

@Composable
fun ModelDownloadStatusInfoPanel(
  model: Model,
  task: Task,
  modelManagerViewModel: ModelManagerViewModel,
  onImportRequested: () -> Unit,
) {
  val modelManagerUiState by modelManagerViewModel.uiState.collectAsState()

  val curStatus = modelManagerUiState.modelDownloadStatus[model.name]

  Column(
    modifier = Modifier.fillMaxSize(),
    horizontalAlignment = Alignment.CenterHorizontally,
    verticalArrangement = Arrangement.Center,
  ) {
    Text(
      "This model is not installed. Import a compatible local model in Model Manager, " +
        "or sideload the documented Gemma file.",
      style = MaterialTheme.typography.bodyLarge,
      modifier = Modifier.padding(horizontal = 32.dp, vertical = 16.dp),
    )

    Button(onClick = onImportRequested) { Text("Choose or import a model") }
  }
}
