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

package com.laccd.chatbot.data

import android.os.Build
import androidx.compose.ui.unit.dp

// Default values for LLM models.
const val DEFAULT_MAX_TOKEN = 1024
const val DEFAULT_TOPK = 64
const val DEFAULT_TOPP = 0.95f
const val DEFAULT_TEMPERATURE = 1.0f
val DEFAULT_ACCELERATORS = listOf(Accelerator.GPU)
val DEFAULT_VISION_ACCELERATOR = Accelerator.GPU

// Max number of images allowed in a "ask image" session.
const val MAX_IMAGE_COUNT = 10

// Max number of audio clip in an "ask audio" session.
const val MAX_AUDIO_CLIP_COUNT = 1

// Max audio clip duration in seconds.
const val MAX_AUDIO_CLIP_DURATION_SEC = 30

// Audio-recording related consts.
const val SAMPLE_RATE = 16000

// The size the icon shown under each of the model names in the model list screen.
val MODEL_INFO_ICON_SIZE = 18.dp

// The extension of the tmp download files.
// Current device's SOC in lowercase.
val SOC =
  (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      Build.SOC_MODEL ?: ""
    } else {
      ""
    })
    .lowercase()
