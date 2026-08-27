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
import org.cozodb.CozoJavaBridge
import org.json.JSONArray
import org.json.JSONObject

/**
 * Thin wrapper around `org.cozodb.CozoJavaBridge`, the string-only JSON API
 * exposed by the `io.github.cozodb:cozo_android` AAR. The bridge takes a
 * Cozo script and a JSON parameter string and returns a JSON result string —
 * we own the JSON parsing in [parseResult] so callers see typed
 * [CozoResult] / [CozoRow] values.
 *
 * One [CozoClient] per opened pack; [KnowledgePack] enforces a per-campus
 * singleton so we don't open the same `.cozo` file twice.
 */
class CozoClient private constructor(
  val engine: String,
  val path: String,
  private val bridge: CozoJavaBridge,
) {

  /** Run a CozoScript with no parameters. */
  fun run(script: String): CozoResult = runWithParams(script, emptyMap())

  /** Run a CozoScript with named parameters. */
  fun runWithParams(script: String, params: Map<String, Any?>): CozoResult {
    val paramsJson =
      if (params.isEmpty()) "{}"
      else JSONObject(params.toMutableMap() as Map<String, *>).toString()
    val raw = runRaw(script, paramsJson)
    return parseResult(raw)
  }

  /** Run a CozoScript and return the raw JSON string from the bridge. */
  fun runRaw(script: String, paramsJson: String = "{}"): String =
    bridge.query(script, paramsJson)

  /** Idempotent — repeated calls are no-ops. */
  fun close() {
    runCatching { bridge.close() }
      .onFailure { Log.w(TAG, "CozoJavaBridge.close failed", it) }
  }

  companion object {
    private const val TAG = "CozoClient"

    /**
     * Open a Cozo database. [engine] is one of `mem`, `sqlite`, or `rocksdb`
     * (the latter two persist to [path]). [options] is a JSON string accepted
     * by the engine — empty `{}` is fine for sqlite/mem; rocksdb accepts
     * tuning knobs.
     */
    fun open(engine: String, path: String, options: String = ""): CozoClient {
      val bridge =
        if (options.isEmpty()) CozoJavaBridge(engine, path)
        else CozoJavaBridge(engine, path, options)
      return CozoClient(engine, path, bridge)
    }

    /** Convenience: sqlite-backed Cozo at [path]. */
    fun openSqlite(path: String): CozoClient = open("sqlite", path)

    /** Convenience: in-memory Cozo (test-only). */
    fun openMemory(): CozoClient = open("mem", "")

    private fun parseResult(raw: String): CozoResult {
      val obj = JSONObject(raw)
      val ok = obj.optBoolean("ok", true)
      if (!ok) {
        val message = obj.optString("display", obj.optString("message", "unknown CozoDB error"))
        throw CozoException(message)
      }
      val headersArr = obj.optJSONArray("headers") ?: JSONArray()
      val rowsArr = obj.optJSONArray("rows") ?: JSONArray()
      val headers = List(headersArr.length()) { headersArr.optString(it, "") }
      val rows = List(rowsArr.length()) { i ->
        val row = rowsArr.optJSONArray(i) ?: JSONArray()
        CozoRow(values = List(row.length()) { row.opt(it) }, headers = headers)
      }
      return CozoResult(headers = headers, rows = rows)
    }
  }
}

/** Decoded result of a CozoScript run. */
data class CozoResult(val headers: List<String>, val rows: List<CozoRow>) {
  val isEmpty: Boolean get() = rows.isEmpty()
  val size: Int get() = rows.size
}

/**
 * One row from a Cozo result. [values] are raw JSON-decoded primitives
 * accessed by header name or by position. Cozo never re-orders columns
 * within a result, so positional access is safe across runs.
 */
data class CozoRow(val values: List<Any?>, val headers: List<String>) {
  fun string(name: String, default: String = ""): String =
    valueOf(name)?.toString() ?: default

  fun stringOrNull(name: String): String? = valueOf(name)?.toString()

  fun int(name: String, default: Int = 0): Int = (valueOf(name) as? Number)?.toInt() ?: default

  fun long(name: String, default: Long = 0L): Long =
    (valueOf(name) as? Number)?.toLong() ?: default

  fun double(name: String, default: Double = 0.0): Double =
    (valueOf(name) as? Number)?.toDouble() ?: default

  fun bool(name: String, default: Boolean = false): Boolean =
    (valueOf(name) as? Boolean) ?: default

  fun array(name: String): JSONArray? = valueOf(name) as? JSONArray

  private fun valueOf(name: String): Any? {
    val idx = headers.indexOf(name)
    if (idx < 0) return null
    return values.getOrNull(idx)
  }
}

/** Raised when Cozo returns `{"ok": false, ...}`. */
class CozoException(message: String) : RuntimeException(message)
