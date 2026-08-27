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

import android.content.Context
import com.google.gson.annotations.SerializedName
import java.io.File

const val IMPORTS_DIR = "__imports"
private val NORMALIZE_NAME_REGEX = Regex("[^a-zA-Z0-9]")

data class PromptTemplate(val title: String, val description: String, val prompt: String)

enum class ModelCapability {
  @SerializedName("llm_thinking") LLM_THINKING,
  @SerializedName("speculative_decoding") SPECULATIVE_DECODING,
}

enum class RuntimeType {
  @SerializedName("unknown") UNKNOWN,
  @SerializedName("litert_lm") LITERT_LM,
}

/**
 * A model for a task (see [Task]).
 *
 * A task can have multiple models. For example, a task might be "LLM Chat", and it might have
 * models such as Gemma2, Gemma3, etc.
 */
data class Model(
  /**
   * The name of the model.
   *
   * This field is used to uniquely identify this model among all the tasks.
   *
   * IMPORTANT: it shouldn't contain "/" character.
   */
  val name: String,

  /**
   * The display name of the model, for display purpose.
   *
   * If this field is not set, the `name` field above will be used as the default display name.
   */
  val displayName: String = "",

  /**
   * (optional)
   *
   * A description or information about the model (Markdown supported).
   *
   * Displayed in the expanded model info card.
   */
  val info: String = "",

  /**
   * (optional)
   *
   * A list of configurable parameters for the model.
   *
   * If set, a gear icon appears on the right side of the model main screen's app bar. When
   * selected, a dialog pops up, allowing users to update the model's configurations.
   *
   * See [Config] for more details
   */
  var configs: List<Config> = listOf(),

  /**
   * (optional)
   *
   * The url to jump to when clicking "learn more" in model's info card.
   */
  val learnMoreUrl: String = "",

  /**
   * (optional)
   *
   * The task type ids that this model is best for.
   *
   * When set, the model's info card is pinned to the top of the model list when the corresponding
   * task is selected, expanded by default, and displays a "best overall" banner.
   *
   * Each task should only have one such model.
   */
  val bestForTaskIds: List<String> = listOf(),

  /**
   * (optional)
   *
   * The minimum device memory in GB to run the model.
   *
   * If set, a warning dialog will be shown when user trying to download the model or enter the
   * model screen.
   */
  val minDeviceMemoryInGb: Int? = null,

  /**
   * The size of the model file in bytes.
   *
   * This will be used to calculate download progress.
   */
  val sizeInBytes: Long = 0L,

  /**
   * The name of the downloaded model file.
   *
   * It will be used to define the file path on local device to store the downloaded model.
   * {context.getExternalFilesDir}/{normalizedName}/{version}/{downloadFileName}
   */
  var downloadFileName: String = "_",

  /** Whether the model is LLM or not. */
  val isLlm: Boolean = false,

  /**
   * The name of the parent model that this model is a variant of.
   *
   * If set, this model will be displayed as a variant (an item in a list) of the parent model's
   * model card,
   */
  val parentModelName: String? = null,

  /** The label of the model variant. */
  val variantLabel: String? = null,

  /** The type of local runtime environment to use for running the model. */
  val runtimeType: RuntimeType = RuntimeType.UNKNOWN,

  /**
   * Set this to a relative path pointing to a dir (e.g., my_model/local_dir/) if you want to
   * manually manage model files instead of downloading them. This dir is relative to the app's
   * "External Files Directory", which is: /storage/emulated/0/Android/data/<app_id>/files/.
   *
   * The <app_id> depends on how the app was built:
   * - `com.google.aiedge.gallery` for builds from the GitHub source.
   * - `com.laccd.chatbot` for other builds (Play store, internal, etc).
   *
   * For example, if this field is set to "my_model/local_dir/", then the location you should push
   * files to is (assuming non-github builds):
   *
   * /storage/emulated/0/Android/data/com.laccd.chatbot/files/my_model/local_dir/
   *
   * You can get the full path to a specific file within your code using `Model.getPath(Context,
   * fileNameToGet)`.
   *
   * Using this field is recommended when:
   * - Your model files are not publicly accessible on the internet (e.g. private models).
   * - Your "model" or experience requires multiple files. Manually pushing these files to the
   *   device and using Model.getPath() for each one is often simpler than downloading them,
   *   especially for demos.
   */
  val localFileRelativeDirPathOverride: String = "",

  // The following fields are only used for built-in tasks. Can ignore if you are creating your own
  // custom tasks.
  //

  /** Whether to show the "run again" button in the UI. */
  val showRunAgainButton: Boolean = true,

  /** Whether to show the "benchmark" button in the UI. */
  val showBenchmarkButton: Boolean = true,

  /** The prompt templates for the model (only for LLM). */
  val llmPromptTemplates: List<PromptTemplate> = listOf(),

  /** Whether the LLM model supports image input. */
  val llmSupportImage: Boolean = false,

  /** Whether the LLM model supports audio input. */
  val llmSupportAudio: Boolean = false,

  /** The capabilities of the model. */
  val capabilities: List<ModelCapability> = listOf(),

  /** The max token for llm model. */
  val llmMaxToken: Int = 0,

  /** Compatible accelerators. */
  val accelerators: List<Accelerator> = listOf(),

  /** Accelerator for running vision encoder. */
  val visionAccelerator: Accelerator = Accelerator.GPU,

  /** Whether the model is imported or not. */
  val imported: Boolean = false,

  /** A map of model capability to the task type ids that the model capability is allowed for. */
  val capabilityToTaskTypes: Map<ModelCapability, List<String>> = mapOf(),

  // The following fields are managed by the app. Don't need to set manually.
  //
  var normalizedName: String = "",
  var instance: Any? = null,
  var initializing: Boolean = false,
  // TODO(jingjin): use a "queue" system to manage model init and cleanup.
  var cleanUpAfterInit: Boolean = false,
  var configValues: Map<String, Any> = mapOf(),
  var prevConfigValues: Map<String, Any> = mapOf(),
) {
  init {
    normalizedName = NORMALIZE_NAME_REGEX.replace(name, "_")
  }

  fun preProcess() {
    val configValues: MutableMap<String, Any> = mutableMapOf()
    for (config in this.configs) {
      configValues[config.key.label] = config.defaultValue
    }
    this.configValues = configValues
  }

  fun getPath(context: Context, fileName: String = downloadFileName): String {
    if (imported) {
      return listOf(context.getExternalFilesDir(null)?.absolutePath ?: "", fileName)
        .joinToString(File.separator)
    }

    if (localFileRelativeDirPathOverride.isNotEmpty()) {
      return listOf(
          context.getExternalFilesDir(null)?.absolutePath ?: "",
          localFileRelativeDirPathOverride,
          fileName,
        )
        .joinToString(File.separator)
    }

    return listOf(context.getExternalFilesDir(null)?.absolutePath ?: "", normalizedName, fileName)
      .joinToString(File.separator)
  }

  fun getIntConfigValue(key: ConfigKey, defaultValue: Int = 0): Int {
    return getTypedConfigValue(key = key, valueType = ValueType.INT, defaultValue = defaultValue)
      as Int
  }

  fun getFloatConfigValue(key: ConfigKey, defaultValue: Float = 0.0f): Float {
    return getTypedConfigValue(key = key, valueType = ValueType.FLOAT, defaultValue = defaultValue)
      as Float
  }

  fun getBooleanConfigValue(key: ConfigKey, defaultValue: Boolean = false): Boolean {
    return getTypedConfigValue(
      key = key,
      valueType = ValueType.BOOLEAN,
      defaultValue = defaultValue,
    )
      as Boolean
  }

  fun getStringConfigValue(key: ConfigKey, defaultValue: String = ""): String {
    return getTypedConfigValue(key = key, valueType = ValueType.STRING, defaultValue = defaultValue)
      as String
  }

  private fun getTypedConfigValue(key: ConfigKey, valueType: ValueType, defaultValue: Any): Any {
    return convertValueToTargetType(
      value = configValues.getOrDefault(key.label, defaultValue),
      valueType = valueType,
    )
  }
}

enum class ModelDownloadStatusType {
  UNAVAILABLE,
  AVAILABLE,
  ERROR,
}

data class ModelDownloadStatus(
  val status: ModelDownloadStatusType,
  val errorMessage: String = "",
)

////////////////////////////////////////////////////////////////////////////////////////////////////
// Configs.

val EMPTY_MODEL: Model = Model(name = "empty", downloadFileName = "empty.tflite", sizeInBytes = 0L)
