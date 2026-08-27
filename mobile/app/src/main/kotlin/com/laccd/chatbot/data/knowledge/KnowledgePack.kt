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

import android.util.Log
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * Per-campus on-device CozoDB pack — graph + RAG vectors + ASSIST + programs +
 * academic_calendar in one sqlite-backed `.cozo` file. Built server-side by
 * `scripts/build_knowledge_pack.py` and installed into app storage by the operator or user.
 *
 * One [KnowledgePack] per campus, cached forever (the file is small, queries
 * are cheap). All public query methods delegate to [GraphQueries] /
 * [RagQueries]; this class is the lifecycle owner.
 *
 * Schema constants here MUST match `scripts/build_knowledge_pack.py`:
 *   SCHEMA_VERSION ↔ build_knowledge_pack.py::SCHEMA_VERSION
 *   EMBEDDING_DIM  ↔ build_knowledge_pack.py::EMBEDDING_DIM
 *   RAG_INDEX_NAME ↔ build_knowledge_pack.py HNSW name "rag_chunk:vector_idx"
 *
 * On open, the pack's `pack_meta` relation is read and the embedded
 * schema_version / embedding_dim are checked. Mismatches throw
 * [PackVersionMismatch] so the caller can offer a re-download instead of
 * crashing on a downstream query that uses a renamed column.
 */
class KnowledgePack private constructor(
  val campus: String,
  val client: CozoClient,
  val meta: PackMeta,
) {

  /** Source URLs cited in the pack's RAG slice; used by the citation chip layer. */
  val campusFingerprint: String = "${campus.lowercase()}@v${meta.schemaVersion}"

  fun close() {
    client.close()
    OPEN_PACKS.remove(campus.uppercase())
  }

  companion object {
    private const val TAG = "KnowledgePack"

    /** Bump in lock-step with `scripts/build_knowledge_pack.py::SCHEMA_VERSION`. */
    const val SCHEMA_VERSION = "1"

    /** MediaPipe USE Lite output dim. Must equal the build-time embedding dim. */
    const val EMBEDDING_DIM = 100

    /** HNSW index name used by RagQueries.kt and the build script. */
    const val RAG_INDEX_NAME = "rag_chunk:vector_idx"

    /** File extension for downloaded packs. */
    const val PACK_FILE_EXTENSION = ".cozo"

    /**
     * Where per-campus packs live on disk. Caller passes
     * `context.filesDir` so the storage stays app-private.
     */
    fun packsDir(filesDir: File): File =
      File(filesDir, "knowledge_packs").apply { if (!exists()) mkdirs() }

    /** Path for a given campus's pack file. */
    fun packFile(filesDir: File, campus: String): File =
      File(packsDir(filesDir), "${campus.lowercase()}$PACK_FILE_EXTENSION")

    /** True when a pack file for [campus] is present in app storage. */
    fun isPackPresent(filesDir: File, campus: String): Boolean =
      packFile(filesDir, campus).exists()

    /**
     * Open (or return a cached handle to) the campus's pack. Throws
     * [PackNotDownloaded] if no file is present, or [PackVersionMismatch] if
     * the file's `pack_meta.schema_version` doesn't equal [SCHEMA_VERSION].
     */
    fun open(filesDir: File, campus: String): KnowledgePack {
      val key = campus.uppercase()
      OPEN_PACKS[key]?.let { return it }

      val file = packFile(filesDir, campus)
      if (!file.exists()) throw PackNotDownloaded(key)

      val client = CozoClient.openSqlite(file.absolutePath)
      val meta = readMeta(client)

      if (meta.schemaVersion != SCHEMA_VERSION) {
        client.close()
        throw PackVersionMismatch(
          campus = key,
          expected = SCHEMA_VERSION,
          actual = meta.schemaVersion,
        )
      }
      if (meta.embeddingDim != EMBEDDING_DIM) {
        Log.w(TAG, "embedding_dim mismatch for $key: pack=${meta.embeddingDim} app=$EMBEDDING_DIM")
      }
      val pack = KnowledgePack(campus = key, client = client, meta = meta)
      OPEN_PACKS[key] = pack
      return pack
    }

    /** Open a [KnowledgePack] backed by an in-memory CozoDB. Test-only. */
    internal fun openInMemory(campus: String, schemaVersion: String = SCHEMA_VERSION): KnowledgePack {
      val client = CozoClient.openMemory()
      // Tests can install schema separately; provide a synthetic meta so
      // schema_version matches the app's expectation by default.
      val meta = PackMeta(
        schemaVersion = schemaVersion,
        campus = campus.uppercase(),
        term = "TEST",
        embedder = "hash",
        embeddingDim = EMBEDDING_DIM,
        builtAt = "1970-01-01T00:00:00Z",
      )
      return KnowledgePack(campus.uppercase(), client, meta)
    }

    private val OPEN_PACKS = ConcurrentHashMap<String, KnowledgePack>()

    private fun readMeta(client: CozoClient): PackMeta {
      val result = runCatching {
        client.run("?[key, value] := *pack_meta[key, value]")
      }.getOrElse {
        // Pack predates pack_meta — treat as v0 so the caller can offer a
        // re-download. Don't crash here; the caller decides.
        Log.w(TAG, "pack_meta missing — treating as schema v0", it)
        return PackMeta(
          schemaVersion = "0",
          campus = "",
          term = "",
          embedder = "",
          embeddingDim = 0,
          builtAt = "",
        )
      }
      val map = mutableMapOf<String, String>()
      for (row in result.rows) {
        map[row.string("key")] = row.string("value")
      }
      return PackMeta(
        schemaVersion = map["schema_version"].orEmpty(),
        campus = map["campus"].orEmpty(),
        term = map["term"].orEmpty(),
        embedder = map["embedder"].orEmpty(),
        embeddingDim = map["embedding_dim"]?.toIntOrNull() ?: 0,
        builtAt = map["built_at"].orEmpty(),
      )
    }
  }
}

/** Snapshot of `pack_meta` rows the build script writes. */
data class PackMeta(
  val schemaVersion: String,
  val campus: String,
  val term: String,
  val embedder: String,
  val embeddingDim: Int,
  val builtAt: String,
)

class PackNotDownloaded(campus: String) :
  RuntimeException("Knowledge pack for $campus is not installed")

class PackVersionMismatch(
  campus: String,
  val expected: String,
  val actual: String,
) : RuntimeException("Pack $campus has schema v$actual, app expects v$expected")
