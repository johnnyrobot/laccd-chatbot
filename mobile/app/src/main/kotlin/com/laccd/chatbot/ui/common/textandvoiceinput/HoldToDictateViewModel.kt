/*
 * Copyright 2025 Google LLC
 * Copyright 2026 LACCD Chatbot contributors
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
package com.laccd.chatbot.ui.common.textandvoiceinput

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.laccd.chatbot.data.MAX_AUDIO_CLIP_DURATION_SEC
import com.laccd.chatbot.data.SAMPLE_RATE
import com.laccd.chatbot.inference.GemmaEngine
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import javax.inject.Inject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

private const val TAG = "AGHTD"

private const val AUDIO_METER_MIN_DB = -2.0f
private const val AUDIO_METER_MAX_DB = 100.0f

/**
 * Wall-clock budget for the on-device Gemma transcription path after the user releases
 * the dictate button. If Gemma hasn't returned a non-blank transcript by this deadline,
 * the orchestrator falls back to whatever Android `SpeechRecognizer` produced in tandem.
 *
 * 8 seconds is generous enough for a ~5 s clip on CPU, tight enough that a user on a
 * slow device doesn't sit and stare at "Listening" for a quarter-minute.
 */
private const val GEMMA_TRANSCRIPTION_DEADLINE_MS = 8_000L

/** Which transcription path actually produced the text in [HoldToDictateUiState]. */
enum class TranscriptionSource {
  /** Pre-delivery, or the dictation was cancelled. */
  UNKNOWN,
  /** on-device Gemma 4 E4B audio path. */
  GEMMA,
  /** Android `SpeechRecognizer` fallback path. */
  SYSTEM,
}

/** The UI state of the HoldToDictateViewModel. */
data class HoldToDictateUiState(
  val recognizing: Boolean = false,
  val recognizedText: String = "",
  /**
   * which transcription path produced [recognizedText]. UNKNOWN while still
   * recognizing. The overlay can use this to badge the on-device path; the chat composer
   * just consumes the text.
   */
  val transcriptionSource: TranscriptionSource = TranscriptionSource.UNKNOWN,
)

@HiltViewModel
class HoldToDictateViewModel @Inject constructor(@ApplicationContext private val context: Context) :
  ViewModel(), RecognitionListener {
  protected val _uiState = MutableStateFlow(HoldToDictateUiState())
  val uiState = _uiState.asStateFlow()

  private val speechRecognizer: SpeechRecognizer
  private val recognizerIntent: Intent
  private var onRecognitionDone: ((String) -> Unit)? = null
  private var onAmplitudeChanged: ((Int) -> Unit)? = null

  // parallel raw-PCM capture for the on-device Gemma transcription path. The
  // system SpeechRecognizer keeps running in tandem and acts as the fallback whenever
  // Gemma isn't loaded, times out, or returns nothing usable.
  private var gemmaWillRun: Boolean = false
  private var audioRecord: AudioRecord? = null
  private val audioStream = ByteArrayOutputStream()
  private var captureJob: Job? = null
  private var orchestratorJob: Job? = null
  private val systemRecognizerResult = AtomicReference<String?>(null)
  private val delivered = AtomicBoolean(false)

  init {
    // Initialize SpeechRecognizer
    speechRecognizer =
      SpeechRecognizer.createSpeechRecognizer(context).apply {
        setRecognitionListener(this@HoldToDictateViewModel)
      }

    // Initialize Intent (used for language/model settings)
    recognizerIntent =
      Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
      }
  }

  fun startSpeechRecognition(onDone: (String) -> Unit, onAmplitudeChanged: (Int) -> Unit) {
    onRecognitionDone = onDone
    this.onAmplitudeChanged = onAmplitudeChanged

    setRecognizedText(text = "")
    setRecognizing(recognizing = true)
    _uiState.update { it.copy(transcriptionSource = TranscriptionSource.UNKNOWN) }
    systemRecognizerResult.set(null)
    delivered.set(false)

    // Decide once at press time whether the Gemma path participates. If the model isn't
    // loaded yet, skip the parallel PCM capture entirely — pure-SpeechRecognizer behaviour
    // identical to pre-Track-7. This keeps the cold-start UX snappy.
    gemmaWillRun = GemmaEngine.isReady
    if (gemmaWillRun) startPcmCapture()

    speechRecognizer.startListening(recognizerIntent)
  }

  fun stopSpeechRecognition() {
    viewModelScope.launch {
      delay(500) // existing — let SpeechRecognizer post a final partial.
      speechRecognizer.stopListening()
      if (gemmaWillRun) {
        val capturedPcm = stopPcmCapture()
        orchestrateGemmaThenFallback(capturedPcm)
      }
      // If Gemma isn't running, onResults / onError drive delivery directly.
    }
  }

  fun cancelSpeechRecognition() {
    setRecognizing(recognizing = false)
    delivered.set(true) // suppress any in-flight delivery from the orchestrator.
    captureJob?.cancel()
    orchestratorJob?.cancel()
    runCatching { speechRecognizer.cancel() }
    runCatching {
      audioRecord?.stop()
      audioRecord?.release()
    }
    audioRecord = null
    audioStream.reset()
  }

  override fun onCleared() {
    cancelSpeechRecognition()
    runCatching { speechRecognizer.destroy() }
    super.onCleared()
  }

  fun setRecognizing(recognizing: Boolean) {
    _uiState.update { uiState.value.copy(recognizing = recognizing) }
  }

  fun setRecognizedText(text: String) {
    _uiState.update { uiState.value.copy(recognizedText = text) }
  }

  // ── RecognitionListener ────────────────────────────────────────────────────

  override fun onReadyForSpeech(params: Bundle?) {}

  override fun onBeginningOfSpeech() {}

  override fun onRmsChanged(rmsdB: Float) {
    onAmplitudeChanged?.invoke(convertRmsDbToAmplitude(rmsdB = rmsdB))
  }

  override fun onBufferReceived(buffer: ByteArray?) {}

  override fun onEndOfSpeech() {}

  override fun onError(error: Int) {
    if (!gemmaWillRun) {
      deliver(systemRecognizerResult.get().orEmpty(), TranscriptionSource.SYSTEM)
    }
    // If Gemma is running, the orchestrator deadline handles fallback.
  }

  override fun onResults(results: Bundle?) {
    val text =
      results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
    systemRecognizerResult.set(text)
    setRecognizedText(text)

    if (!gemmaWillRun) {
      // Pre-Track-7 behaviour preserved: deliver immediately when Gemma isn't in the loop.
      deliver(text, TranscriptionSource.SYSTEM)
    }
    setRecognizing(recognizing = false)
  }

  override fun onPartialResults(partialResults: Bundle?) {
    val text =
      partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
        .orEmpty()
    setRecognizedText(text)
    // Continuously stash the latest partial so the orchestrator has *something* to fall back
    // to even if `onResults` never fires (some OEM ASR engines drop straight to onError).
    systemRecognizerResult.set(text)
  }

  override fun onEvent(eventType: Int, params: Bundle?) {}

  // ── On-device transcription plumbing ─────────────────────────────────────

  /** Single-fire delivery — protects against double-calling `onDone` from racing paths. */
  private fun deliver(text: String, source: TranscriptionSource) {
    if (!delivered.compareAndSet(false, true)) return
    _uiState.update {
      it.copy(recognizedText = text, recognizing = false, transcriptionSource = source)
    }
    onRecognitionDone?.invoke(text)
  }

  @SuppressLint("MissingPermission") // Permission gate is in HoldToDictate.kt before this fires.
  private fun startPcmCapture() {
    audioStream.reset()
    val minBufferSize =
      AudioRecord.getMinBufferSize(
        SAMPLE_RATE,
        AudioFormat.CHANNEL_IN_MONO,
        AudioFormat.ENCODING_PCM_16BIT,
      )
    if (minBufferSize <= 0) {
      gemmaWillRun = false
      return
    }
    val recorder =
      try {
        AudioRecord(
          MediaRecorder.AudioSource.MIC,
          SAMPLE_RATE,
          AudioFormat.CHANNEL_IN_MONO,
          AudioFormat.ENCODING_PCM_16BIT,
          minBufferSize,
        )
      } catch (t: Throwable) {
        Log.w(TAG, "AudioRecord init failed; Gemma path disabled.", t)
        gemmaWillRun = false
        return
      }
    audioRecord = recorder
    captureJob =
      viewModelScope.launch(Dispatchers.IO) {
        try {
          recorder.startRecording()
          val buffer = ByteArray(minBufferSize)
          // 16-bit mono at SAMPLE_RATE → 2 bytes per sample. Hard cap at MAX_AUDIO_CLIP_DURATION_SEC.
          val maxBytes = SAMPLE_RATE.toLong() * 2 * MAX_AUDIO_CLIP_DURATION_SEC
          while (isActive && recorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
            val read = recorder.read(buffer, 0, buffer.size)
            if (read > 0) {
              synchronized(audioStream) {
                audioStream.write(buffer, 0, read)
                if (audioStream.size().toLong() > maxBytes) return@launch
              }
            }
          }
        } catch (t: Throwable) {
          Log.w(TAG, "PCM capture failed; will fall back to SpeechRecognizer.", t)
        }
      }
  }

  private suspend fun stopPcmCapture(): ByteArray {
    runCatching { audioRecord?.stop() }
    runCatching { audioRecord?.release() }
    audioRecord = null
    captureJob?.cancelAndJoin()
    captureJob = null
    return synchronized(audioStream) { audioStream.toByteArray().also { audioStream.reset() } }
  }

  /**
   * Wait up to [GEMMA_TRANSCRIPTION_DEADLINE_MS] for the on-device transcription. On
   * success, deliver Gemma's text. On timeout / empty / failure, deliver whatever the
   * system SpeechRecognizer produced in parallel.
   */
  private fun orchestrateGemmaThenFallback(capturedPcm: ByteArray) {
    orchestratorJob =
      viewModelScope.launch {
        val gemmaText: String? =
          if (capturedPcm.isNotEmpty()) {
            withTimeoutOrNull(GEMMA_TRANSCRIPTION_DEADLINE_MS) {
              GemmaEngine.transcribeAudioClip(wrapPcmInWav(capturedPcm))
            }
          } else null

        val gemmaSucceeded = !gemmaText.isNullOrBlank()
        val finalText =
          if (gemmaSucceeded) gemmaText.trim() else systemRecognizerResult.get().orEmpty()
        val source = if (gemmaSucceeded) TranscriptionSource.GEMMA else TranscriptionSource.SYSTEM
        deliver(finalText, source)
      }
  }
}

private fun convertRmsDbToAmplitude(rmsdB: Float): Int {
  // Clamp the input value to the defined range
  var clampedRmsdB = Math.max(rmsdB, AUDIO_METER_MIN_DB)
  clampedRmsdB = Math.min(clampedRmsdB, AUDIO_METER_MAX_DB)

  // Linear scaling to a 0-65535 range
  return ((clampedRmsdB - AUDIO_METER_MIN_DB) * 65535f / (AUDIO_METER_MAX_DB - AUDIO_METER_MIN_DB))
    .toInt()
}

/**
 * Wrap raw 16-bit linear PCM mono samples (recorded by [AudioRecord] at [SAMPLE_RATE])
 * in a 44-byte RIFF/WAVE header so LiteRT-LM can decode the clip via [Content.AudioBytes].
 *
 * Mirrors the canonical header in `ChatMessageAudioClip.genByteArrayForWav()` — kept inline
 * here so the textandvoiceinput package doesn't reach into the chat package just for byte
 * conversion. `internal` so the unit test in the same module can assert RIFF framing.
 */
internal fun wrapPcmInWav(pcm: ByteArray): ByteArray {
  val sampleRate = SAMPLE_RATE
  val channels = 1
  val bitsPerSample = 16
  val byteRate = sampleRate * channels * bitsPerSample / 8
  val pcmDataSize = pcm.size
  val wavFileSize = pcmDataSize + 44

  val header = ByteArray(44)
  // RIFF / WAVE
  header[0] = 'R'.code.toByte()
  header[1] = 'I'.code.toByte()
  header[2] = 'F'.code.toByte()
  header[3] = 'F'.code.toByte()
  header[4] = (wavFileSize and 0xff).toByte()
  header[5] = (wavFileSize shr 8 and 0xff).toByte()
  header[6] = (wavFileSize shr 16 and 0xff).toByte()
  header[7] = (wavFileSize shr 24 and 0xff).toByte()
  header[8] = 'W'.code.toByte()
  header[9] = 'A'.code.toByte()
  header[10] = 'V'.code.toByte()
  header[11] = 'E'.code.toByte()
  // "fmt " chunk
  header[12] = 'f'.code.toByte()
  header[13] = 'm'.code.toByte()
  header[14] = 't'.code.toByte()
  header[15] = ' '.code.toByte()
  header[16] = 16
  header[17] = 0
  header[18] = 0
  header[19] = 0 // sub-chunk size = 16 (PCM)
  header[20] = 1
  header[21] = 0 // audio format = 1 (PCM)
  header[22] = channels.toByte()
  header[23] = 0
  header[24] = (sampleRate and 0xff).toByte()
  header[25] = (sampleRate shr 8 and 0xff).toByte()
  header[26] = (sampleRate shr 16 and 0xff).toByte()
  header[27] = (sampleRate shr 24 and 0xff).toByte()
  header[28] = (byteRate and 0xff).toByte()
  header[29] = (byteRate shr 8 and 0xff).toByte()
  header[30] = (byteRate shr 16 and 0xff).toByte()
  header[31] = (byteRate shr 24 and 0xff).toByte()
  header[32] = (channels * bitsPerSample / 8).toByte()
  header[33] = 0
  header[34] = bitsPerSample.toByte()
  header[35] = 0
  // "data" chunk
  header[36] = 'd'.code.toByte()
  header[37] = 'a'.code.toByte()
  header[38] = 't'.code.toByte()
  header[39] = 'a'.code.toByte()
  header[40] = (pcmDataSize and 0xff).toByte()
  header[41] = (pcmDataSize shr 8 and 0xff).toByte()
  header[42] = (pcmDataSize shr 16 and 0xff).toByte()
  header[43] = (pcmDataSize shr 24 and 0xff).toByte()

  return header + pcm
}
