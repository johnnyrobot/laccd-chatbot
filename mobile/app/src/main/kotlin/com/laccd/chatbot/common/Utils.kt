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

package com.laccd.chatbot.common

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.ime
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.focus.onFocusEvent
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.exifinterface.media.ExifInterface
import com.laccd.chatbot.GalleryEvent
import com.laccd.chatbot.data.SAMPLE_RATE
import com.laccd.chatbot.firebaseAnalytics
import java.io.File
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.channels.FileChannel
import kotlin.math.abs
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.roundToInt

private const val TAG = "AGUtils"
private const val MAX_WAV_IMPORT_BYTES = 16 * 1024 * 1024

private data class PcmWavInfo(
  val channels: Int,
  val sampleRate: Int,
  val bitDepth: Int,
  val dataOffset: Int,
  val dataSize: Int,
)

private fun parsePcmWav(bytes: ByteArray): PcmWavInfo? {
  if (bytes.size < 44) return null
  fun text(offset: Int) = bytes.copyOfRange(offset, offset + 4).toString(Charsets.US_ASCII)
  fun intLe(offset: Int) = ByteBuffer.wrap(bytes, offset, 4).order(ByteOrder.LITTLE_ENDIAN).int
  if (text(0) != "RIFF" || text(8) != "WAVE") return null
  val riffEnd = intLe(4).toLong() + 8
  if (riffEnd !in 44..bytes.size.toLong()) return null

  var channels = 0
  var sampleRate = 0
  var bitDepth = 0
  var blockAlign = 0
  var dataOffset = -1
  var dataSize = -1
  var offset = 12
  while (offset + 8 <= riffEnd) {
    val chunkId = text(offset)
    val chunkSize = intLe(offset + 4)
    if (chunkSize < 0) return null
    val start = offset + 8
    val end = start.toLong() + chunkSize
    if (end > riffEnd || end > bytes.size) return null
    if (chunkId == "fmt " && chunkSize >= 16) {
      val fmt = ByteBuffer.wrap(bytes, start, chunkSize).order(ByteOrder.LITTLE_ENDIAN)
      if (fmt.short.toInt() != 1) return null
      channels = fmt.short.toInt()
      sampleRate = fmt.int
      fmt.int // byte rate
      blockAlign = fmt.short.toInt()
      bitDepth = fmt.short.toInt()
    } else if (chunkId == "data") {
      dataOffset = start
      dataSize = chunkSize
    }
    offset = (end + (chunkSize and 1)).toInt()
  }
  val expectedAlign = channels * (bitDepth / 8)
  if (
    channels !in 1..2 || sampleRate !in 8_000..192_000 || bitDepth !in setOf(8, 16) ||
      blockAlign != expectedAlign || dataOffset < 0 || dataSize <= 0 || dataSize % blockAlign != 0
  ) return null
  return PcmWavInfo(channels, sampleRate, bitDepth, dataOffset, dataSize)
}

const val LOCAL_URL_BASE = "https://appassets.androidplatform.net"

fun cleanUpMediapipeTaskErrorMessage(message: String): String {
  val index = message.indexOf("=== Source Location Trace")
  if (index >= 0) {
    return message.substring(0, index)
  }
  return message
}

fun processLlmResponse(response: String): String {
  return response.replace("\\n", "\n")
}

fun convertWavToMonoWithMaxSeconds(
  context: Context,
  stereoUri: Uri,
  maxSeconds: Int = 30,
): AudioClip? {
  Log.d(TAG, "Start to convert wav file to mono channel")

  try {
    val inputStream =
      (if (stereoUri.scheme == null || stereoUri.scheme == "file") {
        FileInputStream(stereoUri.path ?: "")
      } else {
        context.contentResolver.openInputStream(stereoUri)
      }) ?: return null
    val originalBytes = inputStream.use { it.readNBytes(MAX_WAV_IMPORT_BYTES + 1) }
    if (originalBytes.size > MAX_WAV_IMPORT_BYTES) {
      Log.e(TAG, "WAV file exceeds the import limit")
      return null
    }

    if (maxSeconds !in 1..60 || originalBytes.size < 44) {
      // Not a valid WAV file
      Log.e(TAG, "Not a valid wav file")
      return null
    }

    val wav = parsePcmWav(originalBytes) ?: return null
    val channels = wav.channels
    var sampleRate = wav.sampleRate
    val bitDepth = wav.bitDepth
    Log.d(TAG, "File metadata: channels: $channels, sampleRate: $sampleRate, bitDepth: $bitDepth")

    // Normalize audio to 16-bit.
    val audioDataBytes =
      originalBytes.copyOfRange(wav.dataOffset, wav.dataOffset + wav.dataSize)
    var sixteenBitBytes: ByteArray =
      if (bitDepth == 8) {
        Log.d(TAG, "Converting 8-bit audio to 16-bit.")
        convert8BitTo16Bit(audioDataBytes)
      } else {
        // Assume 16-bit or other format that can be handled directly
        audioDataBytes
      }

    // Convert byte array to short array for processing
    val shortBuffer =
      ByteBuffer.wrap(sixteenBitBytes).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
    var pcmSamples = ShortArray(shortBuffer.remaining())
    shortBuffer.get(pcmSamples)
    val maxInputSamples = (maxSeconds.toLong() * sampleRate * channels).coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
    if (pcmSamples.size > maxInputSamples) pcmSamples = pcmSamples.copyOf(maxInputSamples)

    // Convert stereo to mono before resampling so interleaved channels cannot become silence.
    var monoSamples =
      if (channels == 2) {
        Log.d(TAG, "Converting stereo to mono.")
        val mono = ShortArray(pcmSamples.size / 2)
        for (i in mono.indices) {
          val left = pcmSamples[i * 2]
          val right = pcmSamples[i * 2 + 1]
          mono[i] = ((left + right) / 2).toShort()
        }
        mono
      } else {
        Log.d(TAG, "Audio is already mono. No channel conversion needed.")
        pcmSamples
      }

    if (sampleRate < SAMPLE_RATE) {
      Log.d(TAG, "Resampling from $sampleRate Hz to $SAMPLE_RATE Hz.")
      monoSamples = resample(monoSamples, sampleRate, SAMPLE_RATE, 1)
      sampleRate = SAMPLE_RATE
    }

    // Trim the audio to maxSeconds ---
    val maxSamples = maxSeconds * sampleRate
    if (monoSamples.size > maxSamples) {
      Log.d(TAG, "Trimming clip from ${monoSamples.size} samples to $maxSamples samples.")
      monoSamples = monoSamples.copyOfRange(0, maxSamples)
    }

    val monoByteBuffer = ByteBuffer.allocate(monoSamples.size * 2).order(ByteOrder.LITTLE_ENDIAN)
    monoByteBuffer.asShortBuffer().put(monoSamples)
    return AudioClip(audioData = monoByteBuffer.array(), sampleRate = sampleRate)
  } catch (e: Exception) {
    Log.e(TAG, "Failed to convert wav to mono", e)
    return null
  }
}

/** Converts 8-bit unsigned PCM audio data to 16-bit signed PCM. */
private fun convert8BitTo16Bit(eightBitData: ByteArray): ByteArray {
  // The new 16-bit data will be twice the size
  val sixteenBitData = ByteArray(eightBitData.size * 2)
  val buffer = ByteBuffer.wrap(sixteenBitData).order(ByteOrder.LITTLE_ENDIAN)

  for (byte in eightBitData) {
    // Convert the unsigned 8-bit byte (0-255) to a signed 16-bit short (-32768 to 32767)
    // 1. Get the unsigned value by masking with 0xFF
    // 2. Subtract 128 to center the waveform around 0 (range becomes -128 to 127)
    // 3. Scale by 256 to expand to the 16-bit range
    val unsignedByte = byte.toInt() and 0xFF
    val sixteenBitSample = ((unsignedByte - 128) * 256).toShort()
    buffer.putShort(sixteenBitSample)
  }
  return sixteenBitData
}

/** Resamples PCM audio data from an original sample rate to a target sample rate. */
private fun resample(
  inputSamples: ShortArray,
  originalSampleRate: Int,
  targetSampleRate: Int,
  channels: Int,
): ShortArray {
  if (originalSampleRate == targetSampleRate) {
    return inputSamples
  }

  val ratio = targetSampleRate.toDouble() / originalSampleRate
  val outputLengthLong = (inputSamples.size * ratio).toLong()
  if (outputLengthLong < 0 || outputLengthLong > SAMPLE_RATE.toLong() * 60 * channels) {
    throw IllegalArgumentException("resampled audio exceeds output limit")
  }
  val outputLength = outputLengthLong.toInt()
  val resampledData = ShortArray(outputLength)

  if (channels == 1) { // Mono
    for (i in resampledData.indices) {
      val position = i / ratio
      val index1 = floor(position).toInt()
      val index2 = index1 + 1
      val fraction = position - index1

      val sample1 = if (index1 < inputSamples.size) inputSamples[index1].toDouble() else 0.0
      val sample2 = if (index2 < inputSamples.size) inputSamples[index2].toDouble() else 0.0

      resampledData[i] = (sample1 * (1 - fraction) + sample2 * fraction).toInt().toShort()
    }
  }

  return resampledData
}

fun calculatePeakAmplitude(buffer: ByteArray, bytesRead: Int): Int {
  // Wrap the byte array in a ByteBuffer and set the order to little-endian
  val shortBuffer =
    ByteBuffer.wrap(buffer, 0, bytesRead).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()

  var maxAmplitude = 0
  // Iterate through the short buffer to find the maximum absolute value
  while (shortBuffer.hasRemaining()) {
    val currentSample = abs(shortBuffer.get().toInt())
    if (currentSample > maxAmplitude) {
      maxAmplitude = currentSample
    }
  }
  return maxAmplitude
}

fun decodeSampledBitmapFromUri(context: Context, uri: Uri, reqWidth: Int, reqHeight: Int): Bitmap? {
  // First, decode with inJustDecodeBounds=true to check dimensions
  val options =
    BitmapFactory.Options().apply {
      inJustDecodeBounds = true
      (if (uri.scheme == null || uri.scheme == "file") {
          FileInputStream(uri.path ?: "")
        } else {
          context.contentResolver.openInputStream(uri)
        })
        ?.use { BitmapFactory.decodeStream(it, null, this) }

      // Calculate inSampleSize
      inSampleSize = calculateInSampleSize(this, reqWidth, reqHeight)

      // Decode bitmap with inSampleSize set
      inJustDecodeBounds = false
    }

  return (if (uri.scheme == null || uri.scheme == "file") {
      FileInputStream(uri.path ?: "")
    } else {
      context.contentResolver.openInputStream(uri)
    })
    ?.use { BitmapFactory.decodeStream(it, null, options) }
}

fun rotateBitmap(bitmap: Bitmap, orientation: Int): Bitmap {
  val matrix = Matrix()
  when (orientation) {
    ExifInterface.ORIENTATION_ROTATE_90 -> matrix.postRotate(90f)
    ExifInterface.ORIENTATION_ROTATE_180 -> matrix.postRotate(180f)
    ExifInterface.ORIENTATION_ROTATE_270 -> matrix.postRotate(270f)
    ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.preScale(-1.0f, 1.0f)
    ExifInterface.ORIENTATION_FLIP_VERTICAL -> matrix.preScale(1.0f, -1.0f)
    ExifInterface.ORIENTATION_TRANSPOSE -> {
      matrix.postRotate(90f)
      matrix.preScale(-1.0f, 1.0f)
    }
    ExifInterface.ORIENTATION_TRANSVERSE -> {
      matrix.postRotate(270f)
      matrix.preScale(-1.0f, 1.0f)
    }
    ExifInterface.ORIENTATION_NORMAL -> return bitmap
    else -> return bitmap
  }
  return Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
}

private fun calculateInSampleSize(
  options: BitmapFactory.Options,
  reqWidth: Int,
  reqHeight: Int,
): Int {
  // Raw height and width of image
  val height: Int = options.outHeight
  val width: Int = options.outWidth
  var inSampleSize = 1

  if (height > reqHeight || width > reqWidth) {
    // Calculate the ratio of height and width to the requested height and width
    val heightRatio = (height.toFloat() / reqHeight.toFloat()).roundToInt()
    val widthRatio = (width.toFloat() / reqWidth.toFloat()).roundToInt()

    // Choose the largest ratio as inSampleSize value to ensure
    // that both dimensions are smaller than or equal to the requested dimensions.
    inSampleSize = max(heightRatio, widthRatio)
  }

  return inSampleSize
}

fun readFileToByteBuffer(file: File): ByteBuffer? {
  return try {
    val fileInputStream = FileInputStream(file)
    val fileChannel: FileChannel = fileInputStream.channel
    val byteBuffer = ByteBuffer.allocateDirect(fileChannel.size().toInt())
    fileChannel.read(byteBuffer)
    byteBuffer.rewind()
    fileInputStream.close()
    byteBuffer
  } catch (e: Exception) {
    e.printStackTrace()
    null
  }
}

fun isPixel10(): Boolean {
  return Build.MODEL != null && Build.MODEL.lowercase().contains("pixel 10")
}

fun Modifier.clearFocusOnKeyboardDismiss(): Modifier = composed {
  var isFocused by remember { mutableStateOf(false) }
  var keyboardAppearedSinceLastFocused by remember { mutableStateOf(false) }

  if (isFocused) {
    val imeIsVisible = WindowInsets.ime.getBottom(LocalDensity.current) > 0
    val focusManager = LocalFocusManager.current

    LaunchedEffect(imeIsVisible) {
      if (imeIsVisible) {
        keyboardAppearedSinceLastFocused = true
      } else if (keyboardAppearedSinceLastFocused) {
        focusManager.clearFocus()
      }
    }
  }

  onFocusEvent {
    if (isFocused != it.isFocused) {
      isFocused = it.isFocused
      if (isFocused) keyboardAppearedSinceLastFocused = false
    }
  }
}

fun logErrorToFirebase(event: GalleryEvent, errorType: String, errorMessage: String?) {
  firebaseAnalytics?.logEvent(
    event.id,
    Bundle().apply {
      putBoolean("success", false)
      putString("error_type", errorType)
      putString("error_message", errorMessage ?: "Unknown error")
    },
  )
}
