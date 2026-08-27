/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data.knowledge

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest

/**
 * Embeds free text into a fixed-dimension float vector for the on-device RAG
 * pipeline. Two implementations:
 *
 *  - [MediaPipeEmbedder] — MediaPipe Tasks Text Embedder loading
 *    `universal_sentence_encoder.tflite` from app assets. This is what
 *    production builds ship — it must use the SAME `.tflite` file (and the
 *    same L2-normalize setting) the build script used.
 *  - [HashEmbedder] — deterministic SHA-256 fallback for dev builds where
 *    the pack was produced with `scripts/build_knowledge_pack.py --embedder
 *    hash`. Bit-for-bit compatible with the Python `HashEmbedder`.
 *
 * The mobile-side [Embedder] is selected based on [PackMeta.embedder]: packs
 * built with `--embedder hash` set `embedder = hash-fallback-v1`, so the
 * on-device side picks [HashEmbedder] for them.
 */
interface Embedder {
  /** Output dimension. Must equal [KnowledgePack.EMBEDDING_DIM] (currently 100). */
  val dim: Int

  /** Identifier persisted in the pack's `pack_meta.embedder` row. */
  val name: String

  /**
   * Embed a single piece of text. The returned vector is L2-normalized so
   * cosine distance ≡ dot-product distance for HNSW.
   */
  fun embed(text: String): FloatArray

  /** Free native resources. Idempotent. */
  fun close() {}
}

/**
 * Deterministic hash-based embedding. Bit-for-bit equivalent to the Python
 * `HashEmbedder` in `scripts/build_knowledge_pack.py`:
 *
 *   1. seed = SHA-256(text)
 *   2. unfold by re-hashing seed||counter (counter big-endian u32) until at
 *      least dim*4 bytes are produced.
 *   3. interpret each 4-byte big-endian unsigned int as a uniform-[0,1) value
 *      and stretch to [-1, 1).
 *   4. L2-normalize.
 *
 * Same input → same output across both Kotlin and Python.
 */
class HashEmbedder(override val dim: Int = KnowledgePack.EMBEDDING_DIM) : Embedder {
  override val name: String = "hash-fallback-v1"

  override fun embed(text: String): FloatArray {
    val seed = sha256(text.toByteArray(Charsets.UTF_8))
    val raw = ByteArray(dim * 4)
    var counter = 0
    var written = 0
    while (written < raw.size) {
      counter += 1
      val block = sha256(seed + counterBytes(counter))
      val take = minOf(block.size, raw.size - written)
      System.arraycopy(block, 0, raw, written, take)
      written += take
    }

    val vec = FloatArray(dim)
    for (i in 0 until dim) {
      val u32 = ((raw[i * 4].toLong() and 0xFF) shl 24) or
        ((raw[i * 4 + 1].toLong() and 0xFF) shl 16) or
        ((raw[i * 4 + 2].toLong() and 0xFF) shl 8) or
        (raw[i * 4 + 3].toLong() and 0xFF)
      // Match Python's (raw_int / (2**32 - 1)) * 2.0 - 1.0 exactly.
      vec[i] = ((u32.toDouble() / U32_MAX) * 2.0 - 1.0).toFloat()
    }
    var norm = 0.0
    for (v in vec) norm += v * v
    norm = if (norm > 0.0) Math.sqrt(norm) else 1.0
    val invNorm = (1.0 / norm).toFloat()
    for (i in vec.indices) vec[i] = vec[i] * invNorm
    return vec
  }

  private fun sha256(input: ByteArray): ByteArray =
    MessageDigest.getInstance("SHA-256").digest(input)

  private fun counterBytes(value: Int): ByteArray {
    val buf = ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN)
    buf.putInt(value)
    return buf.array()
  }

  companion object {
    private const val U32_MAX: Double = 4_294_967_295.0
  }
}

/**
 * Resolve which [Embedder] to use given a pack's `pack_meta.embedder`. Production
 * builds prefer the MediaPipe USE Lite encoder; dev builds with hash-built packs
 * fall back to [HashEmbedder]. The MediaPipe path is constructed lazily by the
 * caller — we don't take an Android Context here so this stays unit-testable.
 */
fun selectEmbedderForPack(meta: PackMeta, mediaPipeFactory: () -> Embedder): Embedder {
  return when {
    meta.embedder.startsWith("mediapipe-use-lite") -> mediaPipeFactory()
    meta.embedder == "hash-fallback-v1" -> HashEmbedder(dim = meta.embeddingDim.takeIf { it > 0 } ?: KnowledgePack.EMBEDDING_DIM)
    else -> HashEmbedder(dim = meta.embeddingDim.takeIf { it > 0 } ?: KnowledgePack.EMBEDDING_DIM)
  }
}
