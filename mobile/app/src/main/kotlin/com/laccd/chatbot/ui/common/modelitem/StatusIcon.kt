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

package com.laccd.chatbot.ui.common.modelitem

// import androidx.compose.ui.tooling.preview.Preview
// import com.laccd.chatbot.ui.theme.GalleryTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.HelpOutline
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.rounded.Error
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import com.laccd.chatbot.R
import com.laccd.chatbot.data.MODEL_INFO_ICON_SIZE
import com.laccd.chatbot.data.Model
import com.laccd.chatbot.data.ModelDownloadStatus
import com.laccd.chatbot.data.ModelDownloadStatusType
import com.laccd.chatbot.data.Task
import com.laccd.chatbot.ui.common.getTaskBgGradientColors
import com.laccd.chatbot.ui.theme.customColors

/** Composable function to display an icon representing the download status of a model. */
@Composable
fun StatusIcon(
  task: Task?,
  model: Model,
  downloadStatus: ModelDownloadStatus?,
  modifier: Modifier = Modifier,
) {
  Row(
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.Center,
    modifier = modifier,
  ) {
    val color =
      if (task != null) {
        getTaskBgGradientColors(task = task)[1]
      } else {
        MaterialTheme.colorScheme.primary
      }
    when (downloadStatus?.status) {
      ModelDownloadStatusType.UNAVAILABLE ->
        Icon(
          Icons.AutoMirrored.Outlined.HelpOutline,
          tint = MaterialTheme.customColors.modelInfoIconColor,
          contentDescription = stringResource(R.string.cd_model_unavailable_icon),
          modifier = Modifier.size(MODEL_INFO_ICON_SIZE),
        )
      ModelDownloadStatusType.AVAILABLE ->
        Icon(
          Icons.Filled.CheckCircle,
          tint = color,
          contentDescription = stringResource(R.string.cd_model_available_icon),
          modifier = Modifier.size(MODEL_INFO_ICON_SIZE),
        )
      ModelDownloadStatusType.ERROR ->
        Icon(
          Icons.Rounded.Error,
          tint = Color(0xFFAA0000),
          contentDescription = stringResource(R.string.cd_model_error_icon),
          modifier = Modifier.size(MODEL_INFO_ICON_SIZE),
        )
      else -> {}
    }
  }
}
