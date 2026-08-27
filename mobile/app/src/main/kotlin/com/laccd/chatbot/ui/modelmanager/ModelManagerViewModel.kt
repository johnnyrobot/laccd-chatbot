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

import android.app.ActivityManager
import android.content.Context
import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.laccd.chatbot.AppLifecycleProvider
import com.laccd.chatbot.R
import com.laccd.chatbot.common.SystemPromptHelper
import com.laccd.chatbot.customtasks.common.CustomTask
import com.laccd.chatbot.data.Accelerator
import com.laccd.chatbot.data.BuiltInTaskId
import com.laccd.chatbot.data.Category
import com.laccd.chatbot.data.CategoryInfo
import com.laccd.chatbot.data.Config
import com.laccd.chatbot.data.ConfigKeys
import com.laccd.chatbot.data.DataStoreRepository
import com.laccd.chatbot.data.EMPTY_MODEL
import com.laccd.chatbot.data.IMPORTS_DIR
import com.laccd.chatbot.data.Model
import com.laccd.chatbot.data.ModelCapability
import com.laccd.chatbot.data.ModelDownloadStatus
import com.laccd.chatbot.data.ModelDownloadStatusType
import com.laccd.chatbot.data.RuntimeType
import com.laccd.chatbot.data.SystemPromptRepository
import com.laccd.chatbot.data.Task
import com.laccd.chatbot.data.createLlmChatConfigs
import com.laccd.chatbot.inference.GemmaEngine
import com.laccd.chatbot.proto.ImportedModel
import com.laccd.chatbot.proto.Theme
import com.google.ai.edge.litertlm.Contents
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.File
import java.util.Locale
import javax.inject.Inject
import kotlin.collections.sortedWith
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

private const val TAG = "AGModelManagerViewModel"
private const val TEXT_INPUT_HISTORY_MAX_SIZE = 50

data class ModelInitializationStatus(
  val status: ModelInitializationStatusType,
  var error: String = "",
  var initializedBackends: Set<String> = setOf(),
) {
  fun isFirstInitialization(model: Model): Boolean {
    val backend =
      model.getStringConfigValue(key = ConfigKeys.ACCELERATOR, defaultValue = Accelerator.GPU.label)
    return !initializedBackends.contains(backend)
  }
}

enum class ModelInitializationStatusType {
  NOT_INITIALIZED,
  INITIALIZING,
  INITIALIZED,
  ERROR,
}

data class ModelManagerUiState(
  /** A list of tasks available in the application. */
  val tasks: List<Task>,

  /** Tasks grouped by category. */
  val tasksByCategory: Map<String, List<Task>>,

  /** A map that tracks the download status of each model, indexed by model name. */
  val modelDownloadStatus: Map<String, ModelDownloadStatus>,

  /** A map that tracks the initialization status of each model, indexed by model name. */
  val modelInitializationStatus: Map<String, ModelInitializationStatus>,

  /** Whether the app is loading and processing the model list. */
  val loadingModels: Boolean = true,

  /** The error message when loading the model list. */
  val loadingModelsError: String = "",

  /** The currently selected model. */
  val selectedModel: Model = EMPTY_MODEL,

  /** The history of text inputs entered by the user. */
  val textInputHistory: List<String> = listOf(),
  val configValuesUpdateTrigger: Long = 0L,
  // Updated when model is imported of an imported model is deleted.
  val modelImportingUpdateTrigger: Long = 0L,
) {
  fun isModelInitialized(model: Model): Boolean {
    return modelInitializationStatus[model.name]?.status ==
      ModelInitializationStatusType.INITIALIZED
  }

  fun isModelInitializing(model: Model): Boolean {
    return modelInitializationStatus[model.name]?.status ==
      ModelInitializationStatusType.INITIALIZING
  }
}

private val PREDEFINED_LLM_TASK_ORDER =
  listOf(
    BuiltInTaskId.LLM_ASK_IMAGE,
    BuiltInTaskId.LLM_ASK_AUDIO,
    BuiltInTaskId.LLM_CHAT,
    BuiltInTaskId.LLM_PROMPT_LAB,
  )

/**
 * ViewModel responsible for managing models, their download status, and initialization.
 *
 * This ViewModel handles model-related operations such as downloading, deleting, initializing, and
 * cleaning up models. It also manages the UI state for model management, including the list of
 * tasks, models, download statuses, and initialization statuses.
 */
@HiltViewModel
open class ModelManagerViewModel
@Inject
constructor(
  val dataStoreRepository: DataStoreRepository,
  private val lifecycleProvider: AppLifecycleProvider,
  private val customTasks: Set<@JvmSuppressWildcards CustomTask>,
  private val systemPromptRepository: SystemPromptRepository,
  @ApplicationContext private val context: Context,
) : ViewModel() {
  private val externalFilesDir = context.getExternalFilesDir(null)
  protected val _uiState = MutableStateFlow(createEmptyUiState())
  open val uiState = _uiState.asStateFlow()

  private var _bundledModels: MutableList<Model> = mutableListOf()
  val bundledModels: List<Model>
    get() = _bundledModels

  fun getTaskById(id: String): Task? {
    return uiState.value.tasks.find { it.id == id }
  }

  fun getTasksByIds(ids: Set<String>): List<Task> {
    return uiState.value.tasks.filter { ids.contains(it.id) }
  }

  fun getCustomTaskByTaskId(id: String): CustomTask? {
    return getActiveCustomTasks().find { it.task.id == id }
  }

  fun getActiveCustomTasks(): List<CustomTask> {
    return customTasks.toList()
  }

  fun getSelectedModel(): Model? {
    return uiState.value.selectedModel
  }

  fun getModelByName(name: String): Model? {
    for (task in uiState.value.tasks) {
      for (model in task.models) {
        if (model.name == name) {
          return model
        }
      }
    }
    return null
  }

  fun getAllModels(): List<Model> {
    val allModels = mutableSetOf<Model>()
    for (task in uiState.value.tasks) {
      for (model in task.models) {
        allModels.add(model)
      }
    }
    return allModels.toList().sortedBy { it.displayName.ifEmpty { it.name } }
  }

  fun getAllDownloadedModels(): List<Model> {
    return getAllModels().filter {
      uiState.value.modelDownloadStatus[it.name]?.status == ModelDownloadStatusType.AVAILABLE &&
        it.isLlm
    }
  }

  fun processTasks() {
    val curTasks = getActiveCustomTasks().map { it.task }
    for (task in curTasks) {
      for (model in task.models) {
        model.preProcess()
        if (model.name == GemmaEngine.MODEL_NAME) {
          GemmaEngine.configureForDevice(context)
        }
      }
      // Move the model that is best for this task to the front.
      val bestModel = task.models.find { it.bestForTaskIds.contains(task.id) }
      if (bestModel != null) {
        task.models.remove(bestModel)
        task.models.add(0, bestModel)
      }
    }
  }

  fun updateConfigValuesUpdateTrigger() {
    _uiState.update { _uiState.value.copy(configValuesUpdateTrigger = System.currentTimeMillis()) }
  }

  fun selectModel(model: Model) {
    if (_uiState.value.selectedModel.name != model.name) {
      _uiState.update { _uiState.value.copy(selectedModel = model) }
    }
  }

  fun deleteModel(model: Model) {
    if (model.imported) {
      if (!deleteImportedModelFile(model)) {
        Log.e(TAG, "Unable to delete imported model file; metadata retained")
        return
      }
    } else {
      deleteDirFromExternalFilesDir(model.normalizedName)
    }

    // Update model download status to NotDownloaded.
    val curModelDownloadStatus = uiState.value.modelDownloadStatus.toMutableMap()
    curModelDownloadStatus[model.name] =
      ModelDownloadStatus(status = ModelDownloadStatusType.UNAVAILABLE)

    // Delete model from the list if model is imported as a local model.
    if (model.imported) {
      for (curTask in uiState.value.tasks) {
        val index = curTask.models.indexOf(model)
        if (index >= 0) {
          curTask.models.removeAt(index)
        }
        curTask.updateTrigger.value = System.currentTimeMillis()
      }
      curModelDownloadStatus.remove(model.name)

      // Update data store.
      val importedModels = dataStoreRepository.readImportedModels().toMutableList()
      val importedModelIndex = importedModels.indexOfFirst { it.fileName == model.name }
      if (importedModelIndex >= 0) {
        importedModels.removeAt(importedModelIndex)
      }
      dataStoreRepository.saveImportedModels(importedModels = importedModels)
    }
    val newUiState =
      uiState.value.copy(
        modelDownloadStatus = curModelDownloadStatus,
        tasks = uiState.value.tasks.toList(),
        modelImportingUpdateTrigger = System.currentTimeMillis(),
      )
    _uiState.update { newUiState }
  }

  fun initializeModel(
    context: Context,
    task: Task,
    model: Model,
    force: Boolean = false,
    onDone: () -> Unit = {},
  ) {
    viewModelScope.launch(Dispatchers.Default) {
      // Skip if initialized already.
      if (
        !force &&
          uiState.value.modelInitializationStatus[model.name]?.status ==
            ModelInitializationStatusType.INITIALIZED
      ) {
        Log.d(TAG, "Selected model is already initialized; skipping")
        return@launch
      }

      // Skip if initialization is in progress.
      if (model.initializing) {
        model.cleanUpAfterInit = false
        Log.d(TAG, "Selected model is already initializing; skipping")
        return@launch
      }

      getMinimumMemoryError(context = context, model = model)?.let { error ->
        Log.w(TAG, "Selected model initialization blocked")
        updateModelInitializationStatus(
          model = model,
          status = ModelInitializationStatusType.ERROR,
          error = error,
        )
        onDone()
        return@launch
      }

      // Clean up.
      cleanupModel(context = context, task = task, model = model)

      // Start initialization.
      Log.d(TAG, "Initializing selected model")
      model.initializing = true
      updateModelInitializationStatus(
        model = model,
        status = ModelInitializationStatusType.INITIALIZING,
      )

      val onDoneFn: (error: String) -> Unit = { error ->
        model.initializing = false
        if (model.instance != null) {
          Log.d(TAG, "Selected model initialized successfully")
          updateModelInitializationStatus(
            model = model,
            status = ModelInitializationStatusType.INITIALIZED,
          )
          if (model.cleanUpAfterInit) {
            Log.d(TAG, "Selected model needs cleanup after initialization")
            cleanupModel(context = context, task = task, model = model)
          }
          onDone()
        } else if (error.isNotEmpty()) {
          Log.d(TAG, "Selected model failed to initialize")
          updateModelInitializationStatus(
            model = model,
            status = ModelInitializationStatusType.ERROR,
            error = error,
          )
        }
      }

      // Call the model initialization function.
      val systemPrompt = SystemPromptHelper.getEffectiveSystemPrompt(systemPromptRepository, task)
      getCustomTaskByTaskId(id = task.id)
        ?.initializeModelFn(
          context = context,
          coroutineScope = viewModelScope,
          model = model,
          systemInstruction = Contents.of(systemPrompt),
          onDone = onDoneFn,
        )
    }
  }

  fun cleanupModel(
    context: Context,
    task: Task,
    model: Model,
    instanceToCleanUp: Any? = model.instance,
    onDone: () -> Unit = {},
  ) {
    if (instanceToCleanUp != null && instanceToCleanUp !== model.instance) {
      Log.d(TAG, "Stale model cleanup request; aborting")
      onDone()
      return
    }

    if (model.instance != null) {
      model.cleanUpAfterInit = false
      Log.d(TAG, "Cleaning up selected model")
      val onDoneFn: () -> Unit = {
        model.instance = null
        model.initializing = false
        updateModelInitializationStatus(
          model = model,
          status = ModelInitializationStatusType.NOT_INITIALIZED,
        )
        Log.d(TAG, "Selected model cleanup complete")
        onDone()
      }
      getCustomTaskByTaskId(id = task.id)
        ?.cleanUpModelFn(
          context = context,
          coroutineScope = viewModelScope,
          model = model,
          onDone = onDoneFn,
        )
    } else {
      // When model is being initialized and we are trying to clean it up at same time, we mark it
      // to clean up and it will be cleaned up after initialization is done.
      if (model.initializing) {
        Log.d(TAG, "Selected model is initializing; cleanup deferred")
        model.cleanUpAfterInit = true
      }
    }
  }

  fun setDownloadStatus(curModel: Model, status: ModelDownloadStatus) {
    // Update model download progress.
    val curModelDownloadStatus = uiState.value.modelDownloadStatus.toMutableMap()
    curModelDownloadStatus[curModel.name] = status
    val newUiState = uiState.value.copy(modelDownloadStatus = curModelDownloadStatus)

    // Delete downloaded file if status is failed or not_downloaded.
    if (
      status.status == ModelDownloadStatusType.ERROR ||
        status.status == ModelDownloadStatusType.UNAVAILABLE
    ) {
      deleteFileFromExternalFilesDir(curModel.downloadFileName)
    }

    _uiState.update { newUiState }
  }

  fun setInitializationStatus(model: Model, status: ModelInitializationStatus) {
    val curStatus = uiState.value.modelInitializationStatus.toMutableMap()
    if (curStatus.containsKey(model.name)) {
      val initializedBackends = curStatus[model.name]?.initializedBackends ?: setOf()
      val backend =
        model.getStringConfigValue(
          key = ConfigKeys.ACCELERATOR,
          defaultValue = Accelerator.GPU.label,
        )
      val newInitializedBackends =
        if (status.status == ModelInitializationStatusType.INITIALIZED) {
          initializedBackends + backend
        } else {
          initializedBackends
        }
      curStatus[model.name] = status.copy(initializedBackends = newInitializedBackends)
      _uiState.update { _uiState.value.copy(modelInitializationStatus = curStatus) }
    }
  }

  fun addTextInputHistory(text: String) {
    if (uiState.value.textInputHistory.indexOf(text) < 0) {
      val newHistory = uiState.value.textInputHistory.toMutableList()
      newHistory.add(0, text)
      if (newHistory.size > TEXT_INPUT_HISTORY_MAX_SIZE) {
        newHistory.removeAt(newHistory.size - 1)
      }
      _uiState.update { _uiState.value.copy(textInputHistory = newHistory) }
      dataStoreRepository.saveTextInputHistory(_uiState.value.textInputHistory)
    } else {
      promoteTextInputHistoryItem(text)
    }
  }

  fun promoteTextInputHistoryItem(text: String) {
    val index = uiState.value.textInputHistory.indexOf(text)
    if (index >= 0) {
      val newHistory = uiState.value.textInputHistory.toMutableList()
      newHistory.removeAt(index)
      newHistory.add(0, text)
      _uiState.update { _uiState.value.copy(textInputHistory = newHistory) }
      dataStoreRepository.saveTextInputHistory(_uiState.value.textInputHistory)
    }
  }

  fun deleteTextInputHistory(text: String) {
    val index = uiState.value.textInputHistory.indexOf(text)
    if (index >= 0) {
      val newHistory = uiState.value.textInputHistory.toMutableList()
      newHistory.removeAt(index)
      _uiState.update { _uiState.value.copy(textInputHistory = newHistory) }
      dataStoreRepository.saveTextInputHistory(_uiState.value.textInputHistory)
    }
  }

  fun clearTextInputHistory() {
    _uiState.update { _uiState.value.copy(textInputHistory = mutableListOf()) }
    dataStoreRepository.saveTextInputHistory(_uiState.value.textInputHistory)
  }

  fun readThemeOverride(): Theme {
    return dataStoreRepository.readTheme()
  }

  fun saveThemeOverride(theme: Theme) {
    dataStoreRepository.saveTheme(theme = theme)
  }

  // language toggle persistence. Reads/writes Settings.language; the active
  // tree picks up the change via AppLanguageProvider observing observeLanguage().
  fun readLanguageOverride(): com.laccd.chatbot.proto.LanguagePreference =
    dataStoreRepository.readLanguage()

  fun saveLanguageOverride(language: com.laccd.chatbot.proto.LanguagePreference) {
    dataStoreRepository.saveLanguage(language = language)
  }

  // selected-campus persistence. Reads/writes Settings.selected_campus_code;
  // CampusProvider's StateFlow picks up the change reactively so the entire UI re-themes.
  fun readSelectedCampusCode(): String = dataStoreRepository.readSelectedCampus()

  fun saveSelectedCampusCode(campusCode: String) {
    dataStoreRepository.saveSelectedCampus(campusCode = campusCode)
  }

  // runtime mobile-tool bearer token (cloud /api/tools/*). Persisted in
  // the encrypted secret store and read back by MobileToolTokenProvider on each
  // request. Trims incidental whitespace; empty input is treated as "clear".
  fun readMobileToolToken(): String? = dataStoreRepository.readMobileToolToken()

  fun saveMobileToolToken(token: String) {
    val trimmed = token.trim()
    if (trimmed.isEmpty()) {
      dataStoreRepository.deleteMobileToolToken()
    } else {
      dataStoreRepository.saveMobileToolToken(trimmed)
    }
  }

  fun clearMobileToolToken() {
    dataStoreRepository.deleteMobileToolToken()
  }

  fun addImportedLlmModel(info: ImportedModel) {
    Log.d(TAG, "Adding imported local model")

    // Create model.
    val model = createModelFromImportedModelInfo(info = info)

    val setOfTasks =
      mutableSetOf(
        BuiltInTaskId.LLM_CHAT,
        BuiltInTaskId.LLM_ASK_IMAGE,
        BuiltInTaskId.LLM_ASK_AUDIO,
        BuiltInTaskId.LLM_PROMPT_LAB,
      )
    for (task in getTasksByIds(ids = setOfTasks)) {
      // Remove duplicated imported model if existed.
      val modelIndex = task.models.indexOfFirst { info.fileName == it.name && it.imported }
      if (modelIndex >= 0) {
        Log.d(TAG, "duplicated imported model found in task. Removing it first")
        task.models.removeAt(modelIndex)
      }
      if (
        (task.id == BuiltInTaskId.LLM_ASK_IMAGE && model.llmSupportImage) ||
          (task.id == BuiltInTaskId.LLM_ASK_AUDIO && model.llmSupportAudio) ||
          (task.id != BuiltInTaskId.LLM_ASK_IMAGE &&
            task.id != BuiltInTaskId.LLM_ASK_AUDIO)
      ) {
        task.models.add(model)
      }
      task.updateTrigger.value = System.currentTimeMillis()
    }

    // Add initial status and states.
    val modelDownloadStatus = uiState.value.modelDownloadStatus.toMutableMap()
    val modelInstances = uiState.value.modelInitializationStatus.toMutableMap()
    modelDownloadStatus[model.name] =
      ModelDownloadStatus(status = ModelDownloadStatusType.AVAILABLE)
    modelInstances[model.name] =
      ModelInitializationStatus(status = ModelInitializationStatusType.NOT_INITIALIZED)

    // Update ui state.
    _uiState.update {
      uiState.value.copy(
        tasks = uiState.value.tasks.toList(),
        modelDownloadStatus = modelDownloadStatus,
        modelInitializationStatus = modelInstances,
        modelImportingUpdateTrigger = System.currentTimeMillis(),
      )
    }

    // Add to data store.
    val importedModels = dataStoreRepository.readImportedModels().toMutableList()
    val importedModelIndex = importedModels.indexOfFirst { info.fileName == it.fileName }
    if (importedModelIndex >= 0) {
      Log.d(TAG, "duplicated imported model found in data store. Removing it first")
      importedModels.removeAt(importedModelIndex)
    }
    importedModels.add(info)
    dataStoreRepository.saveImportedModels(importedModels = importedModels)
  }

  fun loadLocalModels() {
    _uiState.update {
      uiState.value.copy(loadingModels = true, loadingModelsError = "")
    }

    viewModelScope.launch(Dispatchers.IO) {
      try {
        _bundledModels.clear()
        val importsDir = File(context.getExternalFilesDir(null), IMPORTS_DIR)
        val retainedNames = dataStoreRepository.readImportedModels().map { it.fileName }.toSet()
        importsDir.listFiles()?.forEach { file ->
          if ((file.name.startsWith(".import-") || file.name !in retainedNames) && !file.delete()) {
            throw IllegalStateException("Unable to clean an incomplete model import")
          }
        }
        getActiveCustomTasks().forEach { it.task.models.clear() }
        registerSyntheticGemma4Model()
        processTasks()
        _uiState.update {
          createUiState()
            .copy(
              loadingModels = false,
              loadingModelsError = "",
              tasks = getActiveCustomTasks().map { it.task },
              tasksByCategory = groupTasksByCategory(),
            )
        }
      } catch (e: Exception) {
        Log.e(TAG, "Unable to load local models", e)
        _uiState.update {
          uiState.value.copy(
            loadingModels = false,
            loadingModelsError = "Unable to load local models",
          )
        }
      }
    }
  }

  /**
   * LACCD: register [GemmaEngine.model] (the canonical Gemma 4 E4B spec) against the four
   * built-in LLM task ids so the home screen shows chat / ask-image / ask-audio
   * tiles. Idempotent — safe to call multiple times if `loadLocalModels` is retried.
   */
  private fun registerSyntheticGemma4Model() {
    GemmaEngine.configureForDevice(context)
    val model = GemmaEngine.model
    if (_bundledModels.none { it.name == model.name }) {
      _bundledModels.add(model)
    }
    val taskTypes =
      listOf(
        BuiltInTaskId.LLM_CHAT,
        BuiltInTaskId.LLM_ASK_IMAGE,
        BuiltInTaskId.LLM_ASK_AUDIO,
      )
    val curTasks = getActiveCustomTasks().map { it.task }
    for (taskType in taskTypes) {
      val task = curTasks.find { it.id == taskType } ?: continue
      if (task.models.none { it.name == model.name }) {
        task.models.add(model)
      }
    }
  }

  private fun getMinimumMemoryError(context: Context, model: Model): String? {
    val requiredGb = model.minDeviceMemoryInGb ?: return null
    if (GemmaEngine.shouldUseDebugSmokeRuntime(context = context, candidate = model)) {
      Log.w(TAG, "Allowing selected model on low-memory emulator via debug smoke runtime")
      return null
    }
    val activityManager = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
    val memInfo = ActivityManager.MemoryInfo()
    activityManager.getMemoryInfo(memInfo)
    val totalRamBytes = memInfo.totalMem
    if (totalRamBytes <= 0L) return null

    val requiredBytes = requiredGb.toLong() * 1024 * 1024 * 1024
    if (totalRamBytes >= requiredBytes) return null

    val totalRamGb = totalRamBytes.toDouble() / (1024.0 * 1024.0 * 1024.0)
    val displayName = model.displayName.ifEmpty { model.name }
    return "$displayName requires at least $requiredGb GB RAM. This device reports " +
      "${String.format(Locale.US, "%.1f", totalRamGb)} GB, so initialization was skipped " +
      "to avoid Android killing the app."
  }

  fun clearLoadModelsError() {
    val curTasks = getActiveCustomTasks().map { it.task }
    processTasks()
    _uiState.update {
      createUiState()
        .copy(
          loadingModels = false,
          tasks = curTasks,
          loadingModelsError = "",
          tasksByCategory = groupTasksByCategory(),
        )
    }
  }

  fun setAppInForeground(foreground: Boolean) {
    lifecycleProvider.isAppInForeground = foreground
  }

  private fun createEmptyUiState(): ModelManagerUiState {
    return ModelManagerUiState(
      tasks = listOf(),
      tasksByCategory = mapOf(),
      modelDownloadStatus = mapOf(),
      modelInitializationStatus = mapOf(),
    )
  }

  private fun createUiState(): ModelManagerUiState {
    val modelDownloadStatus: MutableMap<String, ModelDownloadStatus> = mutableMapOf()
    val modelInstances: MutableMap<String, ModelInitializationStatus> = mutableMapOf()
    val tasks: MutableMap<String, Task> = mutableMapOf()
    val checkedModelNames = mutableSetOf<String>()
    for (customTask in getActiveCustomTasks()) {
      val task = customTask.task
      tasks.put(key = task.id, value = task)
      for (model in task.models) {
        if (checkedModelNames.contains(model.name)) {
          continue
        }
        modelDownloadStatus[model.name] = getModelDownloadStatus(model = model)
        modelInstances[model.name] =
          ModelInitializationStatus(status = ModelInitializationStatusType.NOT_INITIALIZED)
        checkedModelNames.add(model.name)
      }
    }

    // Load imported models.
    for (importedModel in dataStoreRepository.readImportedModels()) {
      Log.d(TAG, "Loading stored imported model")

      // Create model.
      val model = createModelFromImportedModelInfo(info = importedModel)
      if (!File(model.getPath(context)).isFile) {
        Log.w(TAG, "Skipping missing imported model metadata")
        continue
      }

      // Add to task.
      tasks.get(key = BuiltInTaskId.LLM_CHAT)?.models?.add(model)
      tasks.get(key = BuiltInTaskId.LLM_PROMPT_LAB)?.models?.add(model)
      if (model.llmSupportImage) {
        tasks.get(key = BuiltInTaskId.LLM_ASK_IMAGE)?.models?.add(model)
      }
      if (model.llmSupportAudio) {
        tasks.get(key = BuiltInTaskId.LLM_ASK_AUDIO)?.models?.add(model)
      }
      // Update status.
      modelDownloadStatus[model.name] =
        ModelDownloadStatus(status = ModelDownloadStatusType.AVAILABLE)
    }

    val textInputHistory = dataStoreRepository.readTextInputHistory()
    Log.d(TAG, "Loaded ${textInputHistory.size} text history entries")
    return ModelManagerUiState(
      tasks = getActiveCustomTasks().map { it.task }.toList(),
      tasksByCategory = mapOf(),
      modelDownloadStatus = modelDownloadStatus,
      modelInitializationStatus = modelInstances,
      textInputHistory = textInputHistory,
    )
  }

  private fun createModelFromImportedModelInfo(info: ImportedModel): Model {
    val accelerators: MutableList<Accelerator> =
      info.llmConfig.compatibleAcceleratorsList
        .mapNotNull { acceleratorLabel ->
          when (acceleratorLabel.trim()) {
            Accelerator.GPU.label -> Accelerator.GPU
            Accelerator.CPU.label -> Accelerator.CPU
            Accelerator.NPU.label -> Accelerator.NPU
            else -> null // Ignore unknown accelerator labels
          }
        }
        .toMutableList()
    val llmMaxToken = info.llmConfig.defaultMaxTokens
    val llmSupportImage = info.llmConfig.supportImage
    val llmSupportAudio = info.llmConfig.supportAudio
    val llmSupportThinking = info.llmConfig.supportThinking
    val llmSupportSpeculativeDecoding = info.llmConfig.supportSpeculativeDecoding
    val configs: MutableList<Config> =
      createLlmChatConfigs(
          defaultMaxToken = llmMaxToken,
          defaultTopK = info.llmConfig.defaultTopk,
          defaultTopP = info.llmConfig.defaultTopp,
          defaultTemperature = info.llmConfig.defaultTemperature,
          accelerators = accelerators,
          supportThinking = llmSupportThinking,
          supportSpeculativeDecoding = llmSupportSpeculativeDecoding,
        )
        .toMutableList()
    val capabilities: MutableList<ModelCapability> = mutableListOf()
    val capabilityToTaskTypes: MutableMap<ModelCapability, List<String>> = mutableMapOf()
    if (llmSupportThinking) {
      capabilities.add(ModelCapability.LLM_THINKING)
      capabilityToTaskTypes[ModelCapability.LLM_THINKING] =
        listOf(
          BuiltInTaskId.LLM_CHAT,
          BuiltInTaskId.LLM_ASK_IMAGE,
          BuiltInTaskId.LLM_ASK_AUDIO,
        )
    }
    if (llmSupportSpeculativeDecoding) {
      capabilities.add(ModelCapability.SPECULATIVE_DECODING)
      capabilityToTaskTypes[ModelCapability.SPECULATIVE_DECODING] =
        listOf(
          BuiltInTaskId.LLM_CHAT,
          BuiltInTaskId.LLM_ASK_IMAGE,
          BuiltInTaskId.LLM_ASK_AUDIO,
          BuiltInTaskId.LLM_PROMPT_LAB,
        )
    }
    val model =
      Model(
        name = info.fileName,
        configs = configs,
        sizeInBytes = info.fileSize,
        downloadFileName = "$IMPORTS_DIR/${info.fileName}",
        showBenchmarkButton = false,
        showRunAgainButton = false,
        imported = true,
        llmSupportImage = llmSupportImage,
        llmSupportAudio = llmSupportAudio,
        capabilities = capabilities.toList(),
        capabilityToTaskTypes = capabilityToTaskTypes.toMap(),
        llmMaxToken = llmMaxToken,
        accelerators = accelerators,
        // We assume all imported models are LLM for now.
        isLlm = true,
        runtimeType = RuntimeType.LITERT_LM,
      )
    model.preProcess()

    return model
  }

  private fun groupTasksByCategory(): Map<String, List<Task>> {
    val tasks = getActiveCustomTasks().map { it.task }

    val categoryMap: Map<String, CategoryInfo> =
      tasks.associateBy { it.category.id }.mapValues { it.value.category }

    val groupedTasks = tasks.groupBy { it.category.id }
    val groupedSortedTasks: MutableMap<String, List<Task>> = mutableMapOf()
    // Sort the tasks in categories by pre-defined order. Sort other tasks by label.
    for (categoryId in groupedTasks.keys) {
      val sortedTasks =
        groupedTasks[categoryId]!!.sortedWith { a, b ->
          if (categoryId == Category.LLM.id) {
            val order: List<String> =
              when (categoryId) {
                Category.LLM.id -> PREDEFINED_LLM_TASK_ORDER
                else -> listOf()
              }
            val indexA = order.indexOf(a.id)
            val indexB = order.indexOf(b.id)
            if (indexA != -1 && indexB != -1) {
              indexA.compareTo(indexB)
            } else if (indexA != -1) {
              -1
            } else if (indexB != -1) {
              1
            } else {
              val ca = categoryMap[a.id]!!
              val cb = categoryMap[b.id]!!
              val caLabel = getCategoryLabel(context = context, category = ca)
              val cbLabel = getCategoryLabel(context = context, category = cb)
              caLabel.compareTo(cbLabel)
            }
          } else {
            a.label.compareTo(b.label)
          }
        }
      for ((index, task) in sortedTasks.withIndex()) {
        task.index = index
      }
      groupedSortedTasks[categoryId] = sortedTasks
    }

    return groupedSortedTasks
  }

  private fun getCategoryLabel(context: Context, category: CategoryInfo): String {
    val stringRes = category.labelStringRes
    val label = category.label
    if (stringRes != null) {
      return context.getString(stringRes)
    } else if (label != null) {
      return label
    }
    return context.getString(R.string.category_unlabeled)
  }

  /**
   * Retrieves the download status of a model.
   *
   * This function determines the download status of a given model by checking if it's fully
   * downloaded, partially downloaded, or not downloaded at all. It also retrieves the received and
   * total bytes for partially downloaded models.
   */
  private fun getModelDownloadStatus(model: Model): ModelDownloadStatus {
    Log.d(TAG, "Checking selected model availability")

    val downloaded = isModelDownloaded(model = model)
    val status =
      if (downloaded) ModelDownloadStatusType.AVAILABLE else ModelDownloadStatusType.UNAVAILABLE

    return ModelDownloadStatus(
      status = status,
    )
  }

  private fun isFileInExternalFilesDir(fileName: String): Boolean {
    if (externalFilesDir != null) {
      val file = File(externalFilesDir, fileName)
      return file.exists()
    } else {
      return false
    }
  }

  private fun deleteFileFromExternalFilesDir(fileName: String) {
    if (isFileInExternalFilesDir(fileName)) {
      val file = File(externalFilesDir, fileName)
      file.delete()
    }
  }

  private fun deleteImportedModelFile(model: Model): Boolean {
    val externalDir = context.getExternalFilesDir(null) ?: return false
    val importsDir = File(externalDir, IMPORTS_DIR).canonicalFile
    val file = File(model.getPath(context)).canonicalFile
    if (file.parentFile != importsDir) return false
    return !file.exists() || file.delete()
  }

  private fun deleteDirFromExternalFilesDir(dir: String) {
    if (isFileInExternalFilesDir(dir)) {
      val file = File(externalFilesDir, dir)
      file.deleteRecursively()
    }
  }

  private fun updateModelInitializationStatus(
    model: Model,
    status: ModelInitializationStatusType,
    error: String = "",
  ) {
    val curModelInstance = uiState.value.modelInitializationStatus.toMutableMap()
    val initializedBackends = curModelInstance[model.name]?.initializedBackends ?: setOf()
    val backend =
      model.getStringConfigValue(key = ConfigKeys.ACCELERATOR, defaultValue = Accelerator.GPU.label)
    val newInitializedBackends =
      if (status == ModelInitializationStatusType.INITIALIZED) {
        initializedBackends + backend
      } else {
        initializedBackends
      }
    curModelInstance[model.name] =
      ModelInitializationStatus(
        status = status,
        error = error,
        initializedBackends = newInitializedBackends,
      )
    val newUiState = uiState.value.copy(modelInitializationStatus = curModelInstance)
    _uiState.update { newUiState }
  }

  @androidx.annotation.VisibleForTesting
  fun isModelDownloaded(model: Model): Boolean {
    return checkIfModelDownloaded(model, model.downloadFileName)
  }

  private fun checkIfModelDownloaded(
    model: Model,
    fileName: String,
  ): Boolean {
    if (model.localFileRelativeDirPathOverride.isNotEmpty()) {
      return File(model.getPath(context, fileName)).isFile
    }
    return fileName.isNotEmpty() && File(model.getPath(context, fileName)).isFile
  }
}
