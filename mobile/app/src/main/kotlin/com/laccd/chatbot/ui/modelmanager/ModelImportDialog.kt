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

package com.laccd.chatbot.ui.modelmanager

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Log
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Error
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshots.SnapshotStateMap
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.laccd.chatbot.R
import com.laccd.chatbot.common.isPixel10
import com.laccd.chatbot.data.Accelerator
import com.laccd.chatbot.data.BooleanSwitchConfig
import com.laccd.chatbot.data.Config
import com.laccd.chatbot.data.ConfigKey
import com.laccd.chatbot.data.ConfigKeys
import com.laccd.chatbot.data.DEFAULT_MAX_TOKEN
import com.laccd.chatbot.data.DEFAULT_TEMPERATURE
import com.laccd.chatbot.data.DEFAULT_TOPK
import com.laccd.chatbot.data.DEFAULT_TOPP
import com.laccd.chatbot.data.IMPORTS_DIR
import com.laccd.chatbot.data.LabelConfig
import com.laccd.chatbot.data.NumberSliderConfig
import com.laccd.chatbot.data.SegmentedButtonConfig
import com.laccd.chatbot.data.ValueType
import com.laccd.chatbot.data.convertValueToTargetType
import com.laccd.chatbot.proto.ImportedModel
import com.laccd.chatbot.proto.LlmConfig
import com.laccd.chatbot.security.SafePaths
import com.laccd.chatbot.ui.common.ConfigEditorsPanel
import com.laccd.chatbot.ui.common.ensureValidFileName
import com.laccd.chatbot.ui.common.humanReadableSize
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

private const val TAG = "AGModelImportDialog"
private const val MAX_MODEL_IMPORT_BYTES = 8L * 1024 * 1024 * 1024
private const val MIN_FREE_SPACE_RESERVE_BYTES = 256L * 1024 * 1024

private val SUPPORTED_ACCELERATORS: List<Accelerator> =
  if (isPixel10()) {
    val accelerators = mutableListOf(Accelerator.CPU)
    accelerators.toList()
  } else {
    listOf(Accelerator.CPU, Accelerator.GPU, Accelerator.NPU)
  }

private val IMPORT_CONFIGS_LLM: List<Config> =
  listOf(
    LabelConfig(key = ConfigKeys.NAME),
    LabelConfig(key = ConfigKeys.MODEL_TYPE),
    NumberSliderConfig(
      key = ConfigKeys.DEFAULT_MAX_TOKENS,
      sliderMin = 100f,
      sliderMax = 4096f,
      defaultValue = DEFAULT_MAX_TOKEN.toFloat(),
      valueType = ValueType.INT,
    ),
    NumberSliderConfig(
      key = ConfigKeys.DEFAULT_TOPK,
      sliderMin = 5f,
      sliderMax = 40f,
      defaultValue = DEFAULT_TOPK.toFloat(),
      valueType = ValueType.INT,
    ),
    NumberSliderConfig(
      key = ConfigKeys.DEFAULT_TOPP,
      sliderMin = 0.0f,
      sliderMax = 1.0f,
      defaultValue = DEFAULT_TOPP,
      valueType = ValueType.FLOAT,
    ),
    NumberSliderConfig(
      key = ConfigKeys.DEFAULT_TEMPERATURE,
      sliderMin = 0.0f,
      sliderMax = 2.0f,
      defaultValue = DEFAULT_TEMPERATURE,
      valueType = ValueType.FLOAT,
    ),
    BooleanSwitchConfig(key = ConfigKeys.SUPPORT_IMAGE, defaultValue = false),
    BooleanSwitchConfig(key = ConfigKeys.SUPPORT_AUDIO, defaultValue = false),
    BooleanSwitchConfig(key = ConfigKeys.SUPPORT_THINKING, defaultValue = false),
    BooleanSwitchConfig(key = ConfigKeys.SUPPORT_SPECULATIVE_DECODING, defaultValue = false),
    SegmentedButtonConfig(
      key = ConfigKeys.COMPATIBLE_ACCELERATORS,
      defaultValue = SUPPORTED_ACCELERATORS[0].label,
      options = SUPPORTED_ACCELERATORS.map { it.label },
      allowMultiple = true,
    ),
  )

@Composable
fun ModelImportDialog(
  uri: Uri,
  onDismiss: () -> Unit,
  onDone: (ImportedModel) -> Unit,
  defaultValues: Map<ConfigKey, Any> = emptyMap(),
) {
  val context = LocalContext.current
  val info = remember { getFileSizeAndDisplayNameFromUri(context = context, uri = uri) }
  val fileSize by remember { mutableLongStateOf(info.first) }
  val fileName by remember { mutableStateOf(ensureValidFileName(info.second)) }

  val initialValues: Map<String, Any> = remember {
    mutableMapOf<String, Any>().apply {
      for (config in IMPORT_CONFIGS_LLM) {
        put(config.key.label, config.defaultValue)
      }
      put(ConfigKeys.NAME.label, fileName)
      // TODO: support other types.
      put(ConfigKeys.MODEL_TYPE.label, "LLM")

      for ((key, value) in defaultValues) {
        put(key.label, value)
      }
    }
  }
  val values: SnapshotStateMap<String, Any> = remember {
    mutableStateMapOf<String, Any>().apply { putAll(initialValues) }
  }
  val interactionSource = remember { MutableInteractionSource() }

  Dialog(onDismissRequest = onDismiss) {
    val focusManager = LocalFocusManager.current
    Card(
      modifier =
        Modifier.fillMaxWidth().clickable(
          interactionSource = interactionSource,
          indication = null, // Disable the ripple effect
        ) {
          focusManager.clearFocus()
        },
      shape = RoundedCornerShape(16.dp),
    ) {
      Column(
        modifier = Modifier.padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
      ) {
        // Title.
        Text(
          "Import Model",
          style = MaterialTheme.typography.titleLarge,
          modifier = Modifier.padding(bottom = 8.dp),
        )

        Column(
          modifier = Modifier.verticalScroll(rememberScrollState()).weight(1f, fill = false),
          verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
          // Default configs for users to set.
          ConfigEditorsPanel(configs = IMPORT_CONFIGS_LLM, values = values)
        }

        // Button row.
        Row(
          modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
          horizontalArrangement = Arrangement.End,
        ) {
          // Cancel button.
          TextButton(onClick = { onDismiss() }) { Text("Cancel") }

          // Import button
          Button(
            onClick = {
              val supportedAccelerators =
                (convertValueToTargetType(
                    value = values.get(ConfigKeys.COMPATIBLE_ACCELERATORS.label)!!,
                    valueType = ValueType.STRING,
                  )
                    as String)
                  .split(",")
              val defaultMaxTokens =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.DEFAULT_MAX_TOKENS.label)!!,
                  valueType = ValueType.INT,
                )
                  as Int
              val defaultTopk =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.DEFAULT_TOPK.label)!!,
                  valueType = ValueType.INT,
                )
                  as Int
              val defaultTopp =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.DEFAULT_TOPP.label)!!,
                  valueType = ValueType.FLOAT,
                )
                  as Float
              val defaultTemperature =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.DEFAULT_TEMPERATURE.label)!!,
                  valueType = ValueType.FLOAT,
                )
                  as Float
              val supportImage =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.SUPPORT_IMAGE.label)!!,
                  valueType = ValueType.BOOLEAN,
                )
                  as Boolean
              val supportAudio =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.SUPPORT_AUDIO.label)!!,
                  valueType = ValueType.BOOLEAN,
                )
                  as Boolean
              val supportThinking =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.SUPPORT_THINKING.label)!!,
                  valueType = ValueType.BOOLEAN,
                )
                  as Boolean
              val supportSpeculativeDecoding =
                convertValueToTargetType(
                  value = values.get(ConfigKeys.SUPPORT_SPECULATIVE_DECODING.label)!!,
                  valueType = ValueType.BOOLEAN,
                )
                  as Boolean
              val importedModel: ImportedModel =
                ImportedModel.newBuilder()
                  .setFileName(fileName)
                  .setFileSize(fileSize)
                  .setLlmConfig(
                    LlmConfig.newBuilder()
                      .addAllCompatibleAccelerators(supportedAccelerators)
                      .setDefaultMaxTokens(defaultMaxTokens)
                      .setDefaultTopk(defaultTopk)
                      .setDefaultTopp(defaultTopp)
                      .setDefaultTemperature(defaultTemperature)
                      .setSupportImage(supportImage)
                      .setSupportAudio(supportAudio)
                      .setSupportThinking(supportThinking)
                      .setSupportSpeculativeDecoding(supportSpeculativeDecoding)
                      .build()
                  )
                  .build()
              onDone(importedModel)
            }
          ) {
            Text("Import")
          }
        }
      }
    }
  }
}

@Composable
fun ModelImportingDialog(
  uri: Uri,
  info: ImportedModel,
  onDismiss: () -> Unit,
  onDone: (ImportedModel) -> Unit,
) {
  var error by remember { mutableStateOf("") }
  val context = LocalContext.current
  val coroutineScope = rememberCoroutineScope()
  var progress by remember { mutableFloatStateOf(0f) }

  LaunchedEffect(Unit) {
    // Import.
    importModel(
      context = context,
      coroutineScope = coroutineScope,
      fileName = info.fileName,
      fileSize = info.fileSize,
      uri = uri,
      onDone = { onDone(info) },
      onProgress = { progress = it },
      onError = { error = it },
    )
  }

  Dialog(
    properties = DialogProperties(dismissOnBackPress = false, dismissOnClickOutside = false),
    onDismissRequest = onDismiss,
  ) {
    Card(modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(16.dp)) {
      Column(
        modifier = Modifier.padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
      ) {
        // Title.
        Text(
          "Import Model",
          style = MaterialTheme.typography.titleLarge,
          modifier = Modifier.padding(bottom = 8.dp),
        )

        // No error.
        if (error.isEmpty()) {
          // Progress bar.
          Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
              "${info.fileName} (${info.fileSize.humanReadableSize()})",
              style = MaterialTheme.typography.labelSmall,
            )
            val animatedProgress = remember { Animatable(0f) }
            LinearProgressIndicator(
              progress = { animatedProgress.value },
              modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp),
            )
            LaunchedEffect(progress) {
              animatedProgress.animateTo(progress, animationSpec = tween(150))
            }
          }
        }
        // Has error.
        else {
          Row(
            verticalAlignment = Alignment.Top,
            horizontalArrangement = Arrangement.spacedBy(6.dp),
          ) {
            Icon(
              Icons.Rounded.Error,
              contentDescription = stringResource(R.string.cd_error),
              tint = MaterialTheme.colorScheme.error,
            )
            Text(
              error,
              style = MaterialTheme.typography.labelSmall,
              color = MaterialTheme.colorScheme.error,
              modifier = Modifier.padding(top = 4.dp),
            )
          }
          Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            Button(onClick = { onDismiss() }) { Text("Close") }
          }
        }
      }
    }
  }
}

private fun importModel(
  context: Context,
  coroutineScope: CoroutineScope,
  fileName: String,
  fileSize: Long,
  uri: Uri,
  onDone: () -> Unit,
  onProgress: (Float) -> Unit,
  onError: (String) -> Unit,
) {
  // TODO: handle error.
  coroutineScope.launch(Dispatchers.IO) {
    if (fileSize < 0L || fileSize > MAX_MODEL_IMPORT_BYTES) {
      onError("Model file exceeds the 8 GB import limit.")
      return@launch
    }

    val externalFilesDir = context.getExternalFilesDir(null)
    if (externalFilesDir == null) {
      onError("External files directory is unavailable.")
      return@launch
    }

    val outputFile =
      try {
        // Create <app_external_dir>/imports if not exist.
        val importsDir = SafePaths.resolveContained(externalFilesDir, IMPORTS_DIR)
        if (!importsDir.exists()) {
          importsDir.mkdirs()
        }

        // Import by copying the file over.
        SafePaths.resolveContained(importsDir, SafePaths.safeFileName(fileName))
      } catch (e: IllegalArgumentException) {
        onError(e.message ?: "Unsafe file name.")
        return@launch
      }
    if (externalFilesDir.usableSpace - fileSize < MIN_FREE_SPACE_RESERVE_BYTES) {
      onError("Not enough free storage to import this model safely.")
      return@launch
    }
    val tempFile =
      SafePaths.resolveContained(outputFile.parentFile ?: externalFilesDir, ".import-${UUID.randomUUID()}")
    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
    var bytesRead: Int
    var lastSetProgressTs: Long = 0
    var importedBytes = 0L
    try {
      val inputStream = context.contentResolver.openInputStream(uri)
        ?: throw IllegalArgumentException("Unable to open selected model file.")
      inputStream.use { source ->
        FileOutputStream(tempFile).use { outputStream ->
          while (source.read(buffer).also { bytesRead = it } != -1) {
            importedBytes += bytesRead
            if (
              importedBytes > MAX_MODEL_IMPORT_BYTES ||
                externalFilesDir.usableSpace < MIN_FREE_SPACE_RESERVE_BYTES
            ) {
              throw IllegalArgumentException("Model import exceeds the safe storage limit.")
            }
            outputStream.write(buffer, 0, bytesRead)

            val curTs = System.currentTimeMillis()
            if (curTs - lastSetProgressTs > 200) {
              lastSetProgressTs = curTs
              if (fileSize > 0L) {
                onProgress((importedBytes.toFloat() / fileSize.toFloat()).coerceAtMost(1f))
              }
            }
          }
          outputStream.fd.sync()
        }
      }
      Files.move(
        tempFile.toPath(),
        outputFile.toPath(),
        StandardCopyOption.ATOMIC_MOVE,
        StandardCopyOption.REPLACE_EXISTING,
      )
    } catch (e: Exception) {
      tempFile.delete()
      onError(e.message ?: "Failed to import")
      return@launch
    }
    onProgress(1f)
    onDone()
  }
}

private fun getFileSizeAndDisplayNameFromUri(context: Context, uri: Uri): Pair<Long, String> {
  val contentResolver = context.contentResolver
  var fileSize = 0L
  var displayName = ""

  try {
    contentResolver
      .query(uri, arrayOf(OpenableColumns.SIZE, OpenableColumns.DISPLAY_NAME), null, null, null)
      ?.use { cursor ->
        if (cursor.moveToFirst()) {
          val sizeIndex = cursor.getColumnIndexOrThrow(OpenableColumns.SIZE)
          fileSize = cursor.getLong(sizeIndex)

          val nameIndex = cursor.getColumnIndexOrThrow(OpenableColumns.DISPLAY_NAME)
          displayName = cursor.getString(nameIndex)
        }
      }
  } catch (e: Exception) {
    e.printStackTrace()
    return Pair(0L, "")
  }

  return Pair(fileSize, displayName)
}
