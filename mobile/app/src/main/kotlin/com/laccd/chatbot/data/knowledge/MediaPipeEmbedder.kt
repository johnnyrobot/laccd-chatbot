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

import android.content.Context
import com.google.mediapipe.tasks.components.containers.Embedding
import com.google.mediapipe.tasks.core.BaseOptions
import com.google.mediapipe.tasks.text.textembedder.TextEmbedder

/**
 * MediaPipe Tasks Text Embedder backed by the Universal Sentence Encoder Lite
 * model. The `.tflite` is bundled in app assets so the encoder is offline-only;
 * USE Lite is ~7 MB and shared across all 9 campuses.
 *
 * The build pipeline (`scripts/build_knowledge_pack.py` with `--embedder mediapipe`)
 * embeds with l2_normalize=True; we do the same here so dot-product matches
 * cosine distance in the HNSW index.
 */
class MediaPipeEmbedder(
  context: Context,
  private val modelAssetPath: String = DEFAULT_MODEL_ASSET,
  override val dim: Int = KnowledgePack.EMBEDDING_DIM,
) : Embedder {

  override val name: String = "mediapipe-use-lite::${modelAssetPath.substringAfterLast('/')}"

  private val textEmbedder: TextEmbedder by lazy {
    val baseOptions = BaseOptions.builder().setModelAssetPath(modelAssetPath).build()
    val options = TextEmbedder.TextEmbedderOptions.builder()
      .setBaseOptions(baseOptions)
      .setL2Normalize(true)
      .build()
    TextEmbedder.createFromOptions(context.applicationContext, options)
  }

  override fun embed(text: String): FloatArray {
    val result = textEmbedder.embed(text)
    val embedding: Embedding = result.embeddingResult().embeddings().first()
    val raw: FloatArray = embedding.floatEmbedding()
    if (raw.size == dim) return raw
    // USE Lite is 100-d but a swapped-in model could differ — truncate or pad.
    val out = FloatArray(dim)
    System.arraycopy(raw, 0, out, 0, minOf(raw.size, dim))
    return out
  }

  override fun close() {
    runCatching { textEmbedder.close() }
  }

  companion object {
    /** Filename inside `app/src/main/assets/` shipped by the build. */
    const val DEFAULT_MODEL_ASSET = "universal_sentence_encoder.tflite"
  }
}
