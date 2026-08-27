/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.inference

import android.app.ActivityManager
import android.content.Context
import android.graphics.Bitmap
import android.os.Build
import com.laccd.chatbot.common.MobileE2eMode
import com.laccd.chatbot.data.Accelerator
import com.laccd.chatbot.data.ConfigKeys
import com.laccd.chatbot.data.DEFAULT_MAX_TOKEN
import com.laccd.chatbot.data.Model
import com.laccd.chatbot.data.RuntimeType
import com.laccd.chatbot.data.createLlmChatConfigs
import com.laccd.chatbot.runtime.runtimeHelper
import com.laccd.chatbot.ui.llmchat.LlmModelInstance
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.Contents
import com.google.ai.edge.litertlm.Conversation
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.ExperimentalApi
import com.google.ai.edge.litertlm.Message
import com.google.ai.edge.litertlm.MessageCallback
import com.google.ai.edge.litertlm.SamplerConfig
import java.io.ByteArrayOutputStream
import java.io.File
import kotlin.coroutines.resume
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.suspendCancellableCoroutine

/**
 * Singleton wrapping the on-device Gemma 4 E4B model. Replaces Edge Gallery's
 * `LlmModelHelper` for the single-model path: there is one
 * canonical [Model] (the [model] property), one LiteRT-LM `Engine`, and one active
 * `Conversation` at a time.
 *
 * Internally delegates init / inference / reset / cleanup to [com.laccd.chatbot.ui.llmchat.LlmChatModelHelper]
 * (resolved via [Model.runtimeHelper]) — that file already implements the LiteRT-LM
 * EngineConfig → Engine → Conversation → sendMessageAsync flow correctly. GemmaEngine's
 * value-add over calling LlmChatModelHelper directly:
 *
 *   1. Owns the canonical Gemma 4 E4B [Model] specification (URL, size, runtimeType,
 *      capabilities, max tokens, accelerator preference) so callers don't reconstruct
 *      it ad-hoc.
 *   2. Picks GPU vs CPU accelerator based on a SOC heuristic + a 6 GB RAM floor before
 *      handing off to LlmChatModelHelper.
 *   3. Exposes a Kotlin [Flow]-based streaming API ([streamResponse]) that future tracks
 *      tool calling, skills, and chat can consume without
 *      re-implementing the MessageCallback bridging.
 *
 * This is the programmatic inference primitive. The existing
 * [com.laccd.chatbot.ui.llmchat.LlmChatViewModel.generateResponse] path keeps working
 * unchanged because it already routes through `model.runtimeHelper.runInference(...)` —
 * the only extra wiring is making sure the Gemma 4 E4B [model] is the one passed in.
 */
object GemmaEngine {

  // ── Canonical Gemma 4 E4B model spec ──────────────────────────────────────

  /**
   * Filename on disk. Resolves to:
   *   /storage/emulated/0/Android/data/com.laccd.chatbot/files/gemma_4_e4b/gemma-4-e4b.litertlm
   * via [localFileRelativeDirPath] so a developer can sideload via:
   *   adb push gemma-4-e4b.litertlm /sdcard/Android/data/com.laccd.chatbot/files/gemma_4_e4b/
   * before the cloud-hosted URL is plumbed in.
   */
  const val MODEL_FILE_NAME = "gemma-4-e4b.litertlm"

  /** Declared model download size: 3.65 GB on disk. */
  const val MODEL_SIZE_BYTES: Long = 3_650_000_000L

  /** Local sideload directory (matches `Model.localFileRelativeDirPathOverride`). */
  const val LOCAL_FILE_RELATIVE_DIR = "gemma_4_e4b/"

  /** Stable name for the one bundled/sideloaded LACCD model. */
  const val MODEL_NAME = "Gemma 4 E4B"

  /** RAM threshold below which we run text-only and prefer the CPU backend. */
  const val LOW_MEMORY_THRESHOLD_BYTES: Long = 6L * 1024 * 1024 * 1024

  /** Max input tokens for Gemma 4 E4B (128K context, but we cap to a reasonable working size). */
  const val MAX_TOKENS = 4096

  // ── Public state ──────────────────────────────────────────────────────────

  /**
   * The single canonical Gemma 4 E4B [Model] object. Declared lazily so it isn't
   * constructed until something asks (avoids a big static-init on app start).
   */
  val model: Model by lazy { buildGemma4E4BModel() }

  /**
   * True once [init] has succeeded and the underlying [Engine] + [Conversation] are wired up.
   * Hold-to-dictate callers probe this at press time to decide whether to attempt
   * the on-device Gemma audio transcription path or stay on the SpeechRecognizer fallback.
   */
  val isReady: Boolean
    get() = model.instance != null

  /**
   * Applies the current device's backend choice and token budget to [model].
   *
   * This is separate from [init] because the bundled-model path preprocesses models
   * before the chat screen initializes them. Without applying these values there, the runtime helper
   * falls back to GPU + DEFAULT_MAX_TOKEN.
   */
  fun configureForDevice(context: Context): DeviceCapabilities {
    val capabilities = probeDeviceCapabilities(context)
    applyCapabilitiesToModelConfig(capabilities)
    return capabilities
  }

  /**
   * Debug-only escape hatch for emulator smoke tests. The real Gemma 4 E4B model cannot be loaded
   * on a default 2 GB test AVD, so smoke-mode debug builds on low-memory
   * emulators use a deterministic runtime after the sideloaded file is present. Real-mode E2E,
   * release builds, and physical devices always go through LiteRT-LM.
   */
  fun shouldUseDebugSmokeRuntime(context: Context, candidate: Model = model): Boolean {
    if (!MobileE2eMode.isSmoke) return false
    if (candidate.name != MODEL_NAME) return false
    val capabilities = probeDeviceCapabilities(context)
    return capabilities.isLowMemory &&
      isLikelyAndroidEmulator() &&
      File(candidate.getPath(context, candidate.downloadFileName)).isFile
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  /**
   * Probes device capabilities and initializes the LiteRT-LM engine + a fresh
   * conversation seeded with [systemPrompt]. Safe to call only after the model file is present.
   *
   * @param onReady invoked on the calling scope's dispatcher when init completes
   *   successfully (errorMessage is empty).
   * @param onError invoked when init fails. The string is the human-readable reason
   *   already cleaned up by `cleanUpMediapipeTaskErrorMessage`.
   */
  @OptIn(ExperimentalApi::class)
  fun init(
    context: Context,
    scope: CoroutineScope,
    systemPrompt: String,
    onReady: () -> Unit,
    onError: (String) -> Unit,
  ) {
    val capabilities = configureForDevice(context)

    val supportImage = !capabilities.isLowMemory && model.llmSupportImage
    val supportAudio = !capabilities.isLowMemory && model.llmSupportAudio

    model.runtimeHelper.initialize(
      context = context,
      model = model,
      taskId = TASK_ID,
      supportImage = supportImage,
      supportAudio = supportAudio,
      onDone = { errorMessage ->
        if (errorMessage.isBlank()) onReady() else onError(errorMessage)
      },
      systemInstruction = Contents.of(systemPrompt),
      tools = listOf(),
      enableConversationConstrainedDecoding = false,
      coroutineScope = scope,
    )
  }

  /** Drop the current conversation and start a new one with [systemPrompt]. */
  fun reset(systemPrompt: String) {
    model.runtimeHelper.resetConversation(
      model = model,
      supportImage = model.llmSupportImage,
      supportAudio = model.llmSupportAudio,
      systemInstruction = Contents.of(systemPrompt),
      tools = listOf(),
      enableConversationConstrainedDecoding = false,
    )
  }

  /** Stop an in-flight token stream. Idempotent. */
  fun stop() {
    if (model.instance != null) model.runtimeHelper.stopResponse(model)
  }

  /** Release the engine + native resources. Call from the application's onLowMemory hook. */
  fun cleanup(onDone: () -> Unit = {}) {
    model.runtimeHelper.cleanUp(model = model, onDone = onDone)
  }

  // ── Streaming ─────────────────────────────────────────────────────────────

  /**
   * Run a turn against the active conversation. Emits one [GemmaToken] per chunk the
   * model produces, then a final token with `isDone = true`. Cancelling the collector
   * aborts the in-flight inference via `Conversation.cancelProcess()`.
   *
   * Bridges LiteRT-LM's [MessageCallback] → Kotlin Flow as the doc specifies.
   *
   * @param userInput the raw user question.
   * @param retrievedContext optional `[CONTEXT]` block from KG/RAG retrieval.
   * @param images optional bitmap inputs (only honored when `model.llmSupportImage`).
   * @param audioClips optional audio inputs (PCM byte arrays — caller is responsible for
   *   correct WAV/PCM framing).
   */
  @OptIn(ExperimentalApi::class)
  fun streamResponse(
    userInput: String,
    retrievedContext: String? = null,
    images: List<Bitmap> = emptyList(),
    audioClips: List<ByteArray> = emptyList(),
  ): Flow<GemmaToken> = callbackFlow {
    val instance = model.instance as? LlmModelInstance
    if (instance == null) {
      close(
        IllegalStateException(
          "GemmaEngine.init() must complete before streamResponse()."
        )
      )
      return@callbackFlow
    }

    val composedText = PromptBuilder.composeUserMessage(userInput, retrievedContext)

    val contents = mutableListOf<Content>()
    for (image in images) contents.add(Content.ImageBytes(image.toPngByteArray()))
    for (clip in audioClips) contents.add(Content.AudioBytes(clip))
    if (composedText.isNotEmpty()) contents.add(Content.Text(composedText))

    val conversation = instance.conversation
    conversation.sendMessageAsync(
      Contents.of(contents),
      object : MessageCallback {
        override fun onMessage(message: Message) {
          trySend(
            GemmaToken(
              text = message.toString(),
              thinking = message.channels["thought"],
              isDone = false,
            )
          )
        }

        override fun onDone() {
          trySend(GemmaToken(text = "", isDone = true, thinking = null))
          close()
        }

        override fun onError(throwable: Throwable) {
          close(throwable)
        }
      },
      emptyMap(),
    )

    awaitClose { runCatching { conversation.cancelProcess() } }
  }

  // ── Audio transcription ──────────────────────────────────────────────────

  /**
   * One-shot audio transcription for the hold-to-dictate path. Sends the WAV-framed audio
   * clip to a *side-channel* [Conversation] (built from the same [Engine] as the active chat)
   * with a tightly-scoped transcription system instruction. The chat conversation is left
   * untouched — transcription doesn't pollute message history.
   *
   * Returns the trimmed transcript on success, or `null` when:
   *   • [isReady] is false (model not loaded yet),
   *   • [audioWavBytes] is empty,
   *   • the model wasn't initialized with audio support (e.g., low-memory device),
   *   • LiteRT-LM raised an error,
   *   • the model produced an empty/whitespace-only output.
   *
   * The caller is responsible for the timeout / fallback orchestration (see
   * [HoldToDictateViewModel]). This function does not enforce a deadline of its own.
   *
   * @param audioWavBytes 16-bit linear PCM mono at 16 kHz, wrapped in a 44-byte RIFF/WAVE
   *   header. The same format the [com.laccd.chatbot.ui.common.chat.AudioRecorderPanel]
   *   produces.
   * @param language IETF tag root: "en" or "es". Picks the output-language hint in the
   *   transcription system prompt and the user-message instruction wording.
   */
  @OptIn(ExperimentalApi::class)
  suspend fun transcribeAudioClip(
    audioWavBytes: ByteArray,
    language: String = "en",
  ): String? {
    val instance = model.instance as? LlmModelInstance ?: return null
    if (audioWavBytes.isEmpty()) return null
    if (!model.llmSupportAudio) return null

    val sideConversation: Conversation =
      try {
        instance.engine.createConversation(
          ConversationConfig(
            samplerConfig =
              SamplerConfig(
                topK = 16,
                topP = 0.95,
                temperature = 0.1, // low — verbatim transcription, not creative writing.
              ),
            systemInstruction = Contents.of(transcriptionSystemPrompt(language)),
            tools = listOf(),
          )
        )
      } catch (_: Throwable) {
        return null
      }

    return try {
      suspendCancellableCoroutine<String?> { cont ->
        val builder = StringBuilder()
        val instruction =
          if (language == "es") "Transcribe el audio." else "Transcribe the audio."
        sideConversation.sendMessageAsync(
          Contents.of(
            listOf(
              Content.AudioBytes(audioWavBytes),
              Content.Text(instruction),
            )
          ),
          object : MessageCallback {
            override fun onMessage(message: Message) {
              builder.append(message.toString())
            }

            override fun onDone() {
              if (cont.isActive) {
                cont.resume(builder.toString().trim().ifEmpty { null })
              }
            }

            override fun onError(throwable: Throwable) {
              if (cont.isActive) cont.resume(null)
            }
          },
          emptyMap(),
        )
        cont.invokeOnCancellation { runCatching { sideConversation.cancelProcess() } }
      }
    } finally {
      runCatching { sideConversation.close() }
    }
  }

  /**
   * System prompt used by [transcribeAudioClip]. Hard-bounded to "verbatim transcription
   * only" with a domain glossary for LACCD course codes, subject prefixes, campus names,
   * and articulation jargon — Gemma's audio path otherwise tends to mis-segment course
   * codes (e.g. "MATH261" vs "MATH 261", "C211" vs "see two eleven").
   */
  internal fun transcriptionSystemPrompt(language: String): String {
    val outputLanguage = if (language == "es") "Spanish" else "English"
    return """
      You are an audio transcription engine. Output ONLY the verbatim transcription
      of the audio clip. No commentary, no greeting, no preamble, no quotation marks.
      Empty output is acceptable when the audio is silent or unintelligible.

      The speaker is a Los Angeles Community College District (LACCD) student. They will
      frequently say:
        • Course codes like "CHEM C211", "MATH 261", "ENGLISH 101", "BIO 6", "PHIL 1" —
          keep the subject prefix and number contiguous, no extra words inserted, no
          spelling-out of digits.
        • Subject prefixes (use ALL CAPS exactly as the speaker said): MATH, BIO,
          BIOLOGY, CHEM, CHEMISTRY, PHYS, PHYSICS, ENGLISH, HIST, HISTORY, PSYCH,
          PSYCHOLOGY, COMM, SOC, SOCIOLOGY, CS, COMP SCI, COMPUTER SCIENCE, ART, MUSIC,
          KIN, ANATOMY, SPANISH, ASL.
        • Campus names: Mission, City, Harbor, Valley, Pierce, Southwest, Trade-Tech,
          West LA, East LA. Full names: Los Angeles Mission College, Los Angeles City
          College, Los Angeles Harbor College, Los Angeles Valley College, Los Angeles
          Pierce College, Los Angeles Southwest College, Los Angeles Trade-Technical
          College, West Los Angeles College, East Los Angeles College.
        • Program / degree words: associate, bachelor, transfer, articulation,
          prerequisite, corequisite, advisory, IGETC, CSU GE, ZTC, ASSIST.
        • The LACCD shared phone number "818-214-5505".

      Output language: $outputLanguage.
    """
      .trimIndent()
  }

  // ── Capabilities ──────────────────────────────────────────────────────────

  /**
   * Probes device RAM + SOC family to pick a backend. Conservative for emulators and
   * low-memory devices: prefer CPU when the GPU delegate is unlikely to create a LiteRT-LM engine.
   */
  fun probeDeviceCapabilities(context: Context): DeviceCapabilities {
    val activityManager = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
    val memInfo = ActivityManager.MemoryInfo()
    activityManager.getMemoryInfo(memInfo)
    val totalRam = memInfo.totalMem
    val isLowMemory = totalRam > 0L && totalRam < LOW_MEMORY_THRESHOLD_BYTES

    val socModel =
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) Build.SOC_MODEL.orEmpty() else ""
    val socManufacturer =
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
        Build.SOC_MANUFACTURER.orEmpty()
      else ""
    val gpuKnown = socManufacturer.matches(KNOWN_GPU_VENDOR_REGEX) ||
      socModel.matches(KNOWN_GPU_SOC_REGEX)
    val isEmulator = isLikelyAndroidEmulator()

    val accelerator = when {
      isEmulator -> Accelerator.CPU
      isLowMemory -> Accelerator.CPU
      gpuKnown -> Accelerator.GPU
      // Unknown physical device SOC: try GPU first, which is the better default for Gemma.
      else -> Accelerator.GPU
    }

    return DeviceCapabilities(
      totalRamBytes = totalRam,
      isLowMemory = isLowMemory,
      socModel = socModel,
      socManufacturer = socManufacturer,
      preferredAccelerator = accelerator,
    )
  }

  // ── Internal ──────────────────────────────────────────────────────────────

  private const val TASK_ID = "llm_chat"

  /** Lowercase regex against `Build.SOC_MANUFACTURER`. */
  private val KNOWN_GPU_VENDOR_REGEX =
    Regex(".*(qualcomm|qcom|samsung|google|mediatek|mtk).*", RegexOption.IGNORE_CASE)

  /** Lowercase regex against `Build.SOC_MODEL`. */
  private val KNOWN_GPU_SOC_REGEX =
    Regex(".*(snapdragon|exynos|tensor|dimensity|sm8|sm7|sdm).*", RegexOption.IGNORE_CASE)

  private fun buildGemma4E4BModel(): Model =
    Model(
      name = MODEL_NAME,
      displayName = MODEL_NAME,
      info =
        "On-device LLM. 4.5B effective parameters, 128K context, multimodal " +
          "(text + image + audio). Subject to the Gemma Terms of Use. Runs on this device — " +
          "no chat data leaves the phone.",
      sizeInBytes = MODEL_SIZE_BYTES,
      downloadFileName = MODEL_FILE_NAME,
      localFileRelativeDirPathOverride = LOCAL_FILE_RELATIVE_DIR,
      isLlm = true,
      runtimeType = RuntimeType.LITERT_LM,
      configs =
        createLlmChatConfigs(
          defaultMaxToken = MAX_TOKENS,
          accelerators = listOf(Accelerator.GPU, Accelerator.CPU),
        ),
      llmSupportImage = true,
      llmSupportAudio = true,
      llmMaxToken = MAX_TOKENS,
      accelerators = listOf(Accelerator.GPU, Accelerator.CPU),
      minDeviceMemoryInGb = 6,
    ).also { it.preProcess() }

  private fun isLikelyAndroidEmulator(): Boolean {
    val fingerprint = Build.FINGERPRINT.orEmpty()
    val model = Build.MODEL.orEmpty()
    val manufacturer = Build.MANUFACTURER.orEmpty()
    val brand = Build.BRAND.orEmpty()
    val device = Build.DEVICE.orEmpty()
    val product = Build.PRODUCT.orEmpty().lowercase()
    val hardware = Build.HARDWARE.orEmpty().lowercase()

    return fingerprint.startsWith("generic") ||
      fingerprint.startsWith("unknown") ||
      model.contains("google_sdk", ignoreCase = true) ||
      model.contains("Emulator", ignoreCase = true) ||
      model.contains("Android SDK built for", ignoreCase = true) ||
      model.contains("sdk_gphone", ignoreCase = true) ||
      manufacturer.contains("Genymotion", ignoreCase = true) ||
      (brand.startsWith("generic") && device.startsWith("generic")) ||
      product == "google_sdk" ||
      product == "sdk" ||
      product == "emulator" ||
      product.contains("sdk_gphone") ||
      hardware.contains("goldfish") ||
      hardware.contains("ranchu") ||
      hardware.contains("vbox")
  }

  /**
   * Writes the chosen accelerator into the model's [Model.configValues] so
   * [com.laccd.chatbot.ui.llmchat.LlmChatModelHelper.initialize] picks it up via
   * `Model.getStringConfigValue(ConfigKeys.ACCELERATOR, ...)`. Also bumps MAX_TOKENS
   * to our [MAX_TOKENS] constant rather than [DEFAULT_MAX_TOKEN].
   */
  private fun applyCapabilitiesToModelConfig(caps: DeviceCapabilities) {
    val mutable = model.configValues.toMutableMap()
    mutable[ConfigKeys.ACCELERATOR.label] = caps.preferredAccelerator.label
    mutable[ConfigKeys.VISION_ACCELERATOR.label] = Accelerator.GPU.label
    mutable[ConfigKeys.MAX_TOKENS.label] = MAX_TOKENS.toString()
    model.configValues = mutable
  }

  private fun Bitmap.toPngByteArray(): ByteArray {
    val stream = ByteArrayOutputStream()
    compress(Bitmap.CompressFormat.PNG, 100, stream)
    return stream.toByteArray()
  }
}

/** A single chunk emitted by [GemmaEngine.streamResponse]. */
data class GemmaToken(
  /** Partial visible text. Empty when [isDone] is true (the terminator). */
  val text: String = "",
  /** Optional reasoning channel (Gemma's "thought" stream when thinking is enabled). */
  val thinking: String? = null,
  /** True for the final emission of a stream — no more tokens follow. */
  val isDone: Boolean = false,
)

/** Snapshot of what we know about the device when we picked a backend. */
data class DeviceCapabilities(
  val totalRamBytes: Long,
  val isLowMemory: Boolean,
  val socModel: String,
  val socManufacturer: String,
  val preferredAccelerator: Accelerator,
)
