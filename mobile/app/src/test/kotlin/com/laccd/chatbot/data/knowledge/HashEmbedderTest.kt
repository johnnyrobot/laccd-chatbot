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

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * The hash embedder must be bit-for-bit deterministic and L2-normalized so a
 * pack produced by `scripts/build_knowledge_pack.py --embedder hash` returns
 * the same vectors when queried from the device.
 */
class HashEmbedderTest {

  @Test
  fun embed_is_deterministic() {
    val a = HashEmbedder().embed("hello world")
    val b = HashEmbedder().embed("hello world")
    assertEquals(a.size, b.size)
    for (i in a.indices) {
      // Deterministic — bytes match exactly.
      assertEquals(a[i], b[i], 0.0f)
    }
  }

  @Test
  fun embed_is_l2_normalized() {
    val v = HashEmbedder().embed("Mission College Spring 2026 enrollment")
    val norm = sqrt(v.sumOf { (it * it).toDouble() })
    assertTrue("L2 norm should be ≈ 1.0, was $norm", abs(norm - 1.0) < 1e-4)
  }

  @Test
  fun embed_distinguishes_different_inputs() {
    val a = HashEmbedder().embed("MATH 261")
    val b = HashEmbedder().embed("CHEM C211")
    var diffs = 0
    for (i in a.indices) if (a[i] != b[i]) diffs++
    assertTrue("expected most coordinates to differ, only $diffs/${a.size} did", diffs > a.size * 9 / 10)
  }

  @Test
  fun embed_dim_matches_pack_constant() {
    val v = HashEmbedder().embed("anything")
    assertEquals(KnowledgePack.EMBEDDING_DIM, v.size)
  }

  @Test
  fun embed_handles_empty_string_without_throwing() {
    val v = HashEmbedder().embed("")
    assertEquals(KnowledgePack.EMBEDDING_DIM, v.size)
  }

  @Test
  fun two_different_strings_produce_meaningfully_different_vectors() {
    val a = HashEmbedder().embed("MATH 261 calculus")
    val b = HashEmbedder().embed("ENG 101 composition")
    assertNotEquals(a.first(), b.first())
  }

  @Test
  fun selectEmbedderForPack_picks_hash_for_hash_meta() {
    val meta = PackMeta(
      schemaVersion = "1",
      campus = "LAMC",
      term = "2265",
      embedder = "hash-fallback-v1",
      embeddingDim = 100,
      builtAt = "2026-05-04T00:00:00Z",
    )
    val embedder = selectEmbedderForPack(meta) {
      throw AssertionError("MediaPipe factory must not be called for hash packs")
    }
    assertTrue(embedder is HashEmbedder)
  }

  @Test
  fun selectEmbedderForPack_picks_factory_for_mediapipe_meta() {
    val meta = PackMeta(
      schemaVersion = "1",
      campus = "LAMC",
      term = "2265",
      embedder = "mediapipe-use-lite::universal_sentence_encoder.tflite",
      embeddingDim = 100,
      builtAt = "2026-05-04T00:00:00Z",
    )
    var factoryCalled = false
    val factoryStub = object : Embedder {
      override val dim = KnowledgePack.EMBEDDING_DIM
      override val name = "factory-stub"
      override fun embed(text: String): FloatArray = FloatArray(dim)
    }
    val embedder = selectEmbedderForPack(meta) {
      factoryCalled = true
      factoryStub
    }
    assertTrue(factoryCalled)
    assertEquals(factoryStub, embedder)
  }
}
