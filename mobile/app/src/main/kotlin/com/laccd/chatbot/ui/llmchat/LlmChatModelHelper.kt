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

package com.laccd.chatbot.ui.llmchat

import android.content.Context
import android.graphics.Bitmap
import android.util.Log
import com.laccd.chatbot.common.MobileE2eMode
import com.laccd.chatbot.common.cleanUpMediapipeTaskErrorMessage
import com.laccd.chatbot.data.Accelerator
import com.laccd.chatbot.data.ConfigKeys
import com.laccd.chatbot.data.DEFAULT_MAX_TOKEN
import com.laccd.chatbot.data.DEFAULT_TEMPERATURE
import com.laccd.chatbot.data.DEFAULT_TOPK
import com.laccd.chatbot.data.DEFAULT_TOPP
import com.laccd.chatbot.data.DEFAULT_VISION_ACCELERATOR
import com.laccd.chatbot.data.Model
import com.laccd.chatbot.data.ModelCapability
import com.laccd.chatbot.inference.GemmaEngine
import com.laccd.chatbot.runtime.CleanUpListener
import com.laccd.chatbot.runtime.LlmModelHelper
import com.laccd.chatbot.runtime.ResultListener
import com.laccd.chatbot.tools.LaccdApi
import com.laccd.chatbot.tools.MobileToolTokenProvider
import com.laccd.chatbot.tools.ToolRouter
import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.Contents
import com.google.ai.edge.litertlm.Conversation
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import com.google.ai.edge.litertlm.ExperimentalApi
import com.google.ai.edge.litertlm.ExperimentalFlags
import com.google.ai.edge.litertlm.Message
import com.google.ai.edge.litertlm.MessageCallback
import com.google.ai.edge.litertlm.SamplerConfig
import com.google.ai.edge.litertlm.ToolProvider
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private const val TAG = "AGLlmChatModelHelper"
private const val GEMMA_TAG = "GemmaEngine"
private const val TOOL_ROUTER_TAG = "ToolRouter"
private const val LITERT_DISABLE_CACHE_DIR = ":nocache"
private const val MIN_CPU_WEIGHT_CACHE_FREE_BYTES = 512L * 1024 * 1024

data class LlmModelInstance(val engine: Engine, var conversation: Conversation)
data class DebugSmokeLlmModelInstance(val reason: String)

object LlmChatModelHelper : LlmModelHelper {
  // Indexed by model name.
  private val cleanUpListeners: MutableMap<String, CleanUpListener> = mutableMapOf()

  @OptIn(ExperimentalApi::class) // opt-in experimental flags
  override fun initialize(
    context: Context,
    model: Model,
    taskId: String,
    supportImage: Boolean,
    supportAudio: Boolean,
    onDone: (String) -> Unit,
    systemInstruction: Contents?,
    tools: List<ToolProvider>,
    enableConversationConstrainedDecoding: Boolean,
    coroutineScope: CoroutineScope?,
  ) {
    // Prepare options.
    val maxTokens =
      model.getIntConfigValue(key = ConfigKeys.MAX_TOKENS, defaultValue = DEFAULT_MAX_TOKEN)
    val topK = model.getIntConfigValue(key = ConfigKeys.TOPK, defaultValue = DEFAULT_TOPK)
    val topP = model.getFloatConfigValue(key = ConfigKeys.TOPP, defaultValue = DEFAULT_TOPP)
    val temperature =
      model.getFloatConfigValue(key = ConfigKeys.TEMPERATURE, defaultValue = DEFAULT_TEMPERATURE)
    val accelerator =
      model.getStringConfigValue(key = ConfigKeys.ACCELERATOR, defaultValue = Accelerator.GPU.label)
    val visionAccelerator =
      model.getStringConfigValue(
        key = ConfigKeys.VISION_ACCELERATOR,
        defaultValue = DEFAULT_VISION_ACCELERATOR.label,
      )
    val visionBackend =
      when (visionAccelerator) {
        Accelerator.CPU.label -> Backend.CPU()
        Accelerator.GPU.label -> Backend.GPU()
        Accelerator.NPU.label ->
          Backend.NPU(nativeLibraryDir = context.applicationInfo.nativeLibraryDir)
        Accelerator.TPU.label ->
          Backend.NPU(nativeLibraryDir = context.applicationInfo.nativeLibraryDir)
        else -> Backend.GPU()
      }
    val shouldEnableImage = supportImage
    val shouldEnableAudio = supportAudio
    val preferredBackend =
      when (accelerator) {
        Accelerator.CPU.label -> Backend.CPU()
        Accelerator.GPU.label -> Backend.GPU()
        Accelerator.NPU.label ->
          Backend.NPU(nativeLibraryDir = context.applicationInfo.nativeLibraryDir)
        Accelerator.TPU.label ->
          Backend.NPU(nativeLibraryDir = context.applicationInfo.nativeLibraryDir)
        else -> Backend.CPU()
      }
    Log.d(TAG, "Preferred backend: $preferredBackend")

    if (GemmaEngine.shouldUseDebugSmokeRuntime(context = context, candidate = model)) {
      val reason =
        "debug low-memory emulator smoke runtime: ${model.displayName.ifEmpty { model.name }}"
      Log.w(TAG, "Using $reason")
      Log.w(GEMMA_TAG, "Using $reason")
      model.instance = DebugSmokeLlmModelInstance(reason = reason)
      onDone("")
      return
    }

    val modelPath = model.getPath(context = context)
    val cacheDir =
      getCacheDir(modelPath = modelPath, backend = preferredBackend, context = context)
    val engineConfig =
      EngineConfig(
        modelPath = modelPath,
        backend = preferredBackend,
        visionBackend = if (shouldEnableImage) visionBackend else null, // must be GPU for Gemma 3n
        audioBackend = if (shouldEnableAudio) Backend.CPU() else null, // must be CPU for Gemma 3n
        maxNumTokens = maxTokens,
        cacheDir = cacheDir,
      )

    // Check if the model file supports speculative decoding.
    var supportsSpeculativeDecoding = false
    // Create an instance of LiteRT LM engine and conversation.
    try {
      var speculativeDecoding = false
      // Check if the model supports speculative decoding for the given task type and if the
      // speculative decoding is enabled in the settings.
      if (
        supportsSpeculativeDecoding &&
          model.capabilityToTaskTypes[ModelCapability.SPECULATIVE_DECODING]?.contains(taskId) ==
            true
      ) {
        speculativeDecoding =
          model.getBooleanConfigValue(
            key = ConfigKeys.ENABLE_SPECULATIVE_DECODING,
            defaultValue = false,
          )
      }
      ExperimentalFlags.enableSpeculativeDecoding = speculativeDecoding
      Log.d(TAG, "Speculative decoding enabled: $speculativeDecoding")
      val engine = Engine(engineConfig)
      engine.initialize()
      ExperimentalFlags.enableSpeculativeDecoding = false

      ExperimentalFlags.enableConversationConstrainedDecoding =
        enableConversationConstrainedDecoding
      val conversation =
        engine.createConversation(
          ConversationConfig(
            samplerConfig =
              if (preferredBackend is Backend.NPU) {
                null
              } else {
                SamplerConfig(
                  topK = topK,
                  topP = topP.toDouble(),
                  temperature = temperature.toDouble(),
                )
              },
            systemInstruction = systemInstruction,
            tools = tools,
          )
        )
      ExperimentalFlags.enableConversationConstrainedDecoding = false
      model.instance = LlmModelInstance(engine = engine, conversation = conversation)
    } catch (e: Exception) {
      onDone(cleanUpMediapipeTaskErrorMessage(e.message ?: "Unknown error"))
      return
    }
    onDone("")
  }

  private fun getCacheDir(modelPath: String, backend: Backend, context: Context): String? {
    if (backend is Backend.CPU && shouldDisableCpuWeightCache(modelPath)) {
      return LITERT_DISABLE_CACHE_DIR
    }
    return if (modelPath.startsWith("/data/local/tmp")) {
      context.getExternalFilesDir(null)?.absolutePath
    } else {
      null
    }
  }

  private fun shouldDisableCpuWeightCache(modelPath: String): Boolean {
    val modelDir = File(modelPath).parentFile ?: return false
    val usableSpace = modelDir.usableSpace
    val shouldDisable = usableSpace < MIN_CPU_WEIGHT_CACHE_FREE_BYTES
    if (shouldDisable) {
      Log.w(
        TAG,
        "Disabling LiteRT-LM CPU weight cache: only $usableSpace bytes free in " +
          modelDir.absolutePath,
      )
    }
    return shouldDisable
  }

  @OptIn(ExperimentalApi::class) // opt-in experimental flags
  override fun resetConversation(
    model: Model,
    supportImage: Boolean,
    supportAudio: Boolean,
    systemInstruction: Contents?,
    tools: List<ToolProvider>,
    enableConversationConstrainedDecoding: Boolean,
  ) {
    try {
      Log.d(TAG, "Resetting conversation for model '${model.name}'")

      val instance = model.instance as? LlmModelInstance ?: return
      instance.conversation.close()

      val engine = instance.engine
      val topK = model.getIntConfigValue(key = ConfigKeys.TOPK, defaultValue = DEFAULT_TOPK)
      val topP = model.getFloatConfigValue(key = ConfigKeys.TOPP, defaultValue = DEFAULT_TOPP)
      val temperature =
        model.getFloatConfigValue(key = ConfigKeys.TEMPERATURE, defaultValue = DEFAULT_TEMPERATURE)
      val shouldEnableImage = supportImage
      val shouldEnableAudio = supportAudio
      Log.d(TAG, "Enable image: $shouldEnableImage, enable audio: $shouldEnableAudio")

      val accelerator =
        model.getStringConfigValue(
          key = ConfigKeys.ACCELERATOR,
          defaultValue = Accelerator.GPU.label,
        )
      ExperimentalFlags.enableConversationConstrainedDecoding =
        enableConversationConstrainedDecoding
      val newConversation =
        engine.createConversation(
          ConversationConfig(
            samplerConfig =
              if (accelerator == Accelerator.NPU.label || accelerator == Accelerator.TPU.label) {
                null
              } else {
                SamplerConfig(
                  topK = topK,
                  topP = topP.toDouble(),
                  temperature = temperature.toDouble(),
                )
              },
            systemInstruction = systemInstruction,
            tools = tools,
          )
        )
      ExperimentalFlags.enableConversationConstrainedDecoding = false
      instance.conversation = newConversation

      Log.d(TAG, "Resetting done")
    } catch (e: Exception) {
      Log.d(TAG, "Failed to reset conversation", e)
    }
  }

  override fun cleanUp(model: Model, onDone: () -> Unit) {
    val currentInstance = model.instance ?: return
    if (currentInstance is DebugSmokeLlmModelInstance) {
      cleanUpListeners.remove(model.name)?.invoke()
      model.instance = null
      onDone()
      Log.d(TAG, "Debug smoke clean up done.")
      return
    }

    val instance = currentInstance as? LlmModelInstance ?: return

    try {
      instance.conversation.close()
    } catch (e: Exception) {
      Log.e(TAG, "Failed to close the conversation: ${e.message}")
    }

    try {
      instance.engine.close()
    } catch (e: Exception) {
      Log.e(TAG, "Failed to close the engine: ${e.message}")
    }

    val onCleanUp = cleanUpListeners.remove(model.name)
    if (onCleanUp != null) {
      onCleanUp()
    }
    model.instance = null

    onDone()
    Log.d(TAG, "Clean up done.")
  }

  override fun stopResponse(model: Model) {
    if (model.instance is DebugSmokeLlmModelInstance) return
    val instance = model.instance as? LlmModelInstance ?: return
    instance.conversation.cancelProcess()
  }

  override fun runInference(
    model: Model,
    input: String,
    resultListener: ResultListener,
    cleanUpListener: CleanUpListener,
    onError: (message: String) -> Unit,
    images: List<Bitmap>,
    audioClips: List<ByteArray>,
    coroutineScope: CoroutineScope?,
    extraContext: Map<String, String>?,
  ) {
    val currentInstance = model.instance
    if (currentInstance is DebugSmokeLlmModelInstance) {
      runDebugSmokeInference(
        input = input,
        resultListener = resultListener,
        cleanUpListener = cleanUpListener,
        onError = onError,
        coroutineScope = coroutineScope,
      )
      return
    }

    if (images.isEmpty() && audioClips.isEmpty()) {
      val routed =
        maybeRunDirectToolRoute(
          input = input,
          resultListener = resultListener,
          cleanUpListener = cleanUpListener,
          onError = onError,
          coroutineScope = coroutineScope,
        )
      if (routed) return
    }

    val instance = currentInstance as? LlmModelInstance
    if (instance == null) {
      onError("LlmModelInstance is not initialized.")
      return
    }

    // Set listener.
    if (!cleanUpListeners.containsKey(model.name)) {
      cleanUpListeners[model.name] = cleanUpListener
    }

    val conversation = instance.conversation

    val contents = mutableListOf<Content>()
    for (image in images) {
      contents.add(Content.ImageBytes(image.toPngByteArray()))
    }
    for (audioClip in audioClips) {
      contents.add(Content.AudioBytes(audioClip))
    }
    // add the text after image and audio for the accurate last token
    if (input.trim().isNotEmpty()) {
      contents.add(Content.Text(input))
    }

    conversation.sendMessageAsync(
      Contents.of(contents),
      object : MessageCallback {
        override fun onMessage(message: Message) {
          resultListener(message.toString(), false, message.channels["thought"])
        }

        override fun onDone() {
          resultListener("", true, null)
        }

        override fun onError(throwable: Throwable) {
          if (throwable is CancellationException) {
            Log.i(TAG, "The inference is cancelled.")
            resultListener("", true, null)
          } else {
            Log.e(TAG, "onError", throwable)
            onError("Error: ${throwable.message}")
          }
        }
      },
      extraContext ?: emptyMap(),
    )
  }

  private fun maybeRunDirectToolRoute(
    input: String,
    resultListener: ResultListener,
    cleanUpListener: CleanUpListener,
    onError: (message: String) -> Unit,
    coroutineScope: CoroutineScope?,
  ): Boolean {
    if (!shouldRoutePrereqTool(input)) return false
    if (!cleanUpListeners.containsKey(GemmaEngine.MODEL_NAME)) {
      cleanUpListeners[GemmaEngine.MODEL_NAME] = cleanUpListener
    }

    if (coroutineScope == null) {
      onError("Tool routing requires an active coroutine scope.")
      return true
    }

    coroutineScope.launch {
      try {
        val response = buildPrereqToolResponse(input)
        emitToolText(response = response, resultListener = resultListener)
      } catch (e: Exception) {
        Log.e(TOOL_ROUTER_TAG, "Direct eLumen tool route failed", e)
        onError(e.message ?: "Direct eLumen tool route failed")
      }
    }
    return true
  }

  private fun shouldRoutePrereqTool(input: String): Boolean {
    val lower = input.lowercase()
    return ToolRouter.extractCourseCode(input) != null &&
      (lower.contains("prereq") || lower.contains("prerequisite"))
  }

  private fun runDebugSmokeInference(
    input: String,
    resultListener: ResultListener,
    cleanUpListener: CleanUpListener,
    onError: (message: String) -> Unit,
    coroutineScope: CoroutineScope?,
  ) {
    if (!cleanUpListeners.containsKey(GemmaEngine.MODEL_NAME)) {
      cleanUpListeners[GemmaEngine.MODEL_NAME] = cleanUpListener
    }

    if (coroutineScope == null) {
      emitDebugSmokeTextNow(
        response = "Hello! I am ready to help with LACCD questions.",
        resultListener = resultListener,
      )
      return
    }

    coroutineScope.launch {
      try {
        val response = buildDebugSmokeResponse(input)
        emitDebugSmokeText(response = response, resultListener = resultListener)
      } catch (e: Exception) {
        Log.e(TAG, "Debug smoke inference failed", e)
        onError(e.message ?: "Debug smoke inference failed")
      }
    }
  }

  private suspend fun buildDebugSmokeResponse(input: String): String {
    if (shouldRoutePrereqTool(input)) {
      return buildPrereqToolResponse(input)
    }

    return "Hello! I am ready to help with LACCD questions about prerequisites, transfer, schedules, programs, and campus planning."
  }

  private suspend fun buildPrereqToolResponse(input: String): String {
    val courseCode = ToolRouter.extractCourseCode(input)
      ?: error("No course code found for prerequisite lookup.")
    val (subject, number) = splitCourseCode(courseCode)
    val campus = extractCampus(input) ?: "LAMC"
    Log.d(TOOL_ROUTER_TAG, "Direct eLumen route selected")
    val response =
      try {
        LaccdApi.create(tokenProvider = MobileToolTokenProvider.tokenProvider)
          .elumen(subject = subject, number = number, campus = campus)
      } catch (e: Exception) {
        if (
          MobileE2eMode.useRecordedFixtures &&
            campus.equals("LAMC", ignoreCase = true) &&
            subject.equals("MATH", ignoreCase = true) &&
            number == "261"
        ) {
          Log.w(
            TOOL_ROUTER_TAG,
            "Using recorded debug smoke eLumen fixture for LAMC MATH 261",
            e,
          )
          return "For LAMC, MATH 261 (Calculus I) lists MATH 260 as the prerequisite in eLumen."
        }
        throw e
      }
    val prerequisites = response.course.prerequisites.flatten().filter { it.isNotBlank() }
    val prereqText =
      response.course.prerequisite?.takeIf { it.isNotBlank() }
        ?: prerequisites.joinToString(", ")
    val title = response.course.title.ifBlank { "the course" }
    return if (prereqText.isBlank()) {
      "For $campus, ${response.course.code.ifBlank { "$subject $number" }} ($title) does not list a prerequisite in eLumen."
    } else {
      "For $campus, ${response.course.code.ifBlank { "$subject $number" }} ($title) lists $prereqText as the prerequisite in eLumen."
    }
  }

  private suspend fun emitToolText(response: String, resultListener: ResultListener) {
    Log.d(GEMMA_TAG, "Tool-routed response callbacks started")
    for (chunk in response.chunked(24)) {
      resultListener(chunk, false, null)
      delay(60)
    }
    Log.d(GEMMA_TAG, "Tool-routed response callbacks done")
    resultListener("", true, null)
  }

  private suspend fun emitDebugSmokeText(response: String, resultListener: ResultListener) {
    Log.d(GEMMA_TAG, "Debug smoke token callbacks started")
    for (chunk in response.chunked(24)) {
      resultListener(chunk, false, null)
      delay(60)
    }
    Log.d(GEMMA_TAG, "Debug smoke token callbacks done")
    resultListener("", true, null)
  }

  private fun emitDebugSmokeTextNow(response: String, resultListener: ResultListener) {
    Log.d(GEMMA_TAG, "Debug smoke token callbacks started")
    for (chunk in response.chunked(24)) {
      resultListener(chunk, false, null)
    }
    Log.d(GEMMA_TAG, "Debug smoke token callbacks done")
    resultListener("", true, null)
  }

  private fun splitCourseCode(courseCode: String): Pair<String, String> {
    val parts = courseCode.trim().split(Regex("\\s+"))
    if (parts.size <= 1) return courseCode to ""
    return parts.dropLast(1).joinToString(" ") to parts.last()
  }

  private fun extractCampus(input: String): String? {
    val upper = input.uppercase()
    val knownCampuses = listOf("LAMC", "ELAC", "LACC", "LAHC", "LAPC", "LASC", "LATTC", "LAVC", "WLAC")
    return knownCampuses.firstOrNull { Regex("""\b$it\b""").containsMatchIn(upper) }
  }

  private fun Bitmap.toPngByteArray(): ByteArray {
    val stream = ByteArrayOutputStream()
    this.compress(Bitmap.CompressFormat.PNG, 100, stream)
    return stream.toByteArray()
  }
}
