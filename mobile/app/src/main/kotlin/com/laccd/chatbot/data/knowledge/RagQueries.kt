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

import org.json.JSONArray

/**
 * RAG retrieval over the per-campus pack's `rag_chunk` relation. Queries the
 * HNSW vector index `rag_chunk:vector_idx` (matching the index name written
 * by `scripts/build_knowledge_pack.py::HNSW_INDEX_STATEMENTS`).
 *
 * Two-stage retrieval:
 *  1. Embed the user's question via [Embedder] (matches the embedder used at
 *     build time per [PackMeta.embedder]).
 *  2. ANN-search the HNSW index with `~rag_chunk:vector_idx { ... | query, k,
 *     ef, bind_distance: dist }` — the canonical Cozo vector-query form.
 *
 * Returns [RagHit]s with the source URL preserved so the citation chip layer
 * can underline the source under each grounded answer.
 *
 * No separate FTS index is built into the pack in v1 — text-only fallback
 * search uses substring `is_in` checks, which is acceptable for the small
 * per-pack corpus (~ thousands of chunks per campus).
 */
class RagQueries(
  private val pack: KnowledgePack,
  private val embedder: Embedder,
) {

  /**
   * ANN-search the campus's RAG corpus. Returns [k] hits ordered by cosine
   * distance ascending. [efSearch] tunes recall vs latency — values in
   * 32–100 are sane on-device.
   */
  fun search(query: String, k: Int = 6, efSearch: Int = 64): List<RagHit> {
    if (query.isBlank()) return emptyList()
    val vec = embedder.embed(query)
    require(vec.size == KnowledgePack.EMBEDDING_DIM) {
      "Embedder produced ${vec.size}-d vector, pack expects ${KnowledgePack.EMBEDDING_DIM}-d"
    }
    val script = """
      ?[id, source_url, source_kind, source_title, text, dist] :=
        ~rag_chunk:vector_idx { id, source_url, source_kind, source_title, text |
          query: v,
          k: $k,
          ef: $efSearch,
          bind_distance: dist,
          radius: 2.0
        }, v = vec(${'$'}q_vec);
      :order dist;
      :limit $k;
    """.trimIndent()
    val params = mapOf("q_vec" to vec.toJsonArray())
    val rs = pack.client.runWithParams(script, params)
    return rs.rows.map { row ->
      RagHit(
        id = row.string("id"),
        sourceUrl = row.string("source_url"),
        sourceKind = row.string("source_kind", "web"),
        sourceTitle = row.string("source_title"),
        text = row.string("text"),
        distance = row.double("dist"),
      )
    }
  }

  /**
   * Substring fallback when [search] is unavailable (e.g., HNSW index missing
   * because the pack was built without it, or vector embedding failed). Walks
   * `rag_chunk` and returns up to [k] chunks containing every word of [query]
   * (case-insensitive). Slower than HNSW but bounded by pack size.
   */
  fun keywordSearch(query: String, k: Int = 6): List<RagHit> {
    val needles = query.lowercase()
      .split(Regex("\\s+"))
      .filter { it.length >= 3 }
      .distinct()
    if (needles.isEmpty()) return emptyList()
    val firstNeedle = needles.first()
    val script = """
      ?[id, source_url, source_kind, source_title, text] :=
        *rag_chunk{id, source_url, source_kind, source_title, text},
        is_in(${'$'}needle, lowercase(text));
      :limit ${k * 4};
    """.trimIndent()
    val rs = pack.client.runWithParams(script, mapOf("needle" to firstNeedle))
    val rest = needles.drop(1)
    return rs.rows
      .asSequence()
      .map { row ->
        RagHit(
          id = row.string("id"),
          sourceUrl = row.string("source_url"),
          sourceKind = row.string("source_kind", "web"),
          sourceTitle = row.string("source_title"),
          text = row.string("text"),
          distance = Double.NaN,
        )
      }
      .filter { hit -> rest.all { hit.text.lowercase().contains(it) } }
      .take(k)
      .toList()
  }

  private fun FloatArray.toJsonArray(): JSONArray {
    val arr = JSONArray()
    for (v in this) arr.put(v.toDouble())
    return arr
  }
}

/** One hit from the RAG corpus. */
data class RagHit(
  val id: String,
  val sourceUrl: String,
  /** "web", "catalog_pdf", "addendum", etc. — set at build time. */
  val sourceKind: String,
  val sourceTitle: String,
  val text: String,
  /**
   * Cosine distance returned by the HNSW index (lower = more similar). NaN
   * when the row came from [RagQueries.keywordSearch].
   */
  val distance: Double,
)
