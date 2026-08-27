/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.security

import java.io.File

object SafePaths {
  private val SAFE_FILE_NAME = Regex("""[A-Za-z0-9._-]+""")

  fun safeFileName(value: String): String {
    val normalized = value.trim().replace("\\s+".toRegex(), "-")
    require(normalized.isNotEmpty()) { "File name must not be empty." }
    require(normalized != "." && normalized != "..") { "File name must not be relative." }
    require(!normalized.contains('\u0000')) { "File name must not contain NUL." }
    require(!normalized.contains('/') && !normalized.contains('\\')) {
      "File name must not contain path separators."
    }
    require(!File(normalized).isAbsolute) { "File name must not be absolute." }
    require(SAFE_FILE_NAME.matches(normalized)) {
      "File name contains unsupported characters."
    }
    return normalized
  }

  fun resolveContained(root: File, candidate: String): File {
    require(candidate.isNotBlank()) { "Path must not be empty." }
    require(!candidate.contains('\u0000')) { "Path must not contain NUL." }
    require(!candidate.contains('\\')) { "Path must use forward slashes only." }
    require(!File(candidate).isAbsolute) { "Path must not be absolute." }

    val base = root.canonicalFile
    val resolved = File(base, candidate).canonicalFile
    require(resolved.toPath().startsWith(base.toPath())) {
      "Path escapes the allowed directory."
    }
    return resolved
  }

  fun resolveContained(root: File, candidate: File): File =
    resolveContained(root, candidate.path)

  fun resolveZipEntry(root: File, entryName: String): File {
    require(entryName.isNotBlank()) { "ZIP entry must not be empty." }
    require(!entryName.contains('\u0000')) { "ZIP entry must not contain NUL." }
    require(!entryName.contains('\\')) { "ZIP entry must use forward slashes only." }
    require(!File(entryName).isAbsolute) { "ZIP entry must not be absolute." }

    val parts = entryName.split("/")
    require(parts.all { it.isNotEmpty() && it != "." && it != ".." }) {
      "ZIP entry contains an unsafe path segment."
    }
    return resolveContained(root, entryName)
  }
}
