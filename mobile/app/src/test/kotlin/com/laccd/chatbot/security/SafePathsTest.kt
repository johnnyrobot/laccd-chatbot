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
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SafePathsTest {
  @Test
  fun safe_file_name_normalizes_spaces() {
    assertEquals("My-Model.gguf", SafePaths.safeFileName(" My Model.gguf "))
  }

  @Test(expected = IllegalArgumentException::class)
  fun safe_file_name_rejects_separators() {
    SafePaths.safeFileName("../secret.pb")
  }

  @Test(expected = IllegalArgumentException::class)
  fun safe_file_name_rejects_empty() {
    SafePaths.safeFileName("   ")
  }

  @Test
  fun resolve_contained_accepts_child_path() {
    val root = File(System.getProperty("java.io.tmpdir"), "safe-paths-root")
    val resolved = SafePaths.resolveContained(root, "models/gemma/file.bin")
    assertTrue(resolved.toPath().startsWith(root.canonicalFile.toPath()))
  }

  @Test(expected = IllegalArgumentException::class)
  fun resolve_contained_rejects_traversal() {
    val root = File(System.getProperty("java.io.tmpdir"), "safe-paths-root")
    SafePaths.resolveContained(root, "../outside.bin")
  }

  @Test(expected = IllegalArgumentException::class)
  fun resolve_contained_rejects_absolute() {
    val root = File(System.getProperty("java.io.tmpdir"), "safe-paths-root")
    SafePaths.resolveContained(root, "/tmp/outside.bin")
  }

  @Test
  fun resolve_zip_entry_accepts_nested_file() {
    val root = File(System.getProperty("java.io.tmpdir"), "safe-zip-root")
    val resolved = SafePaths.resolveZipEntry(root, "weights/model.bin")
    assertTrue(resolved.toPath().startsWith(root.canonicalFile.toPath()))
  }

  @Test(expected = IllegalArgumentException::class)
  fun resolve_zip_entry_rejects_traversal() {
    val root = File(System.getProperty("java.io.tmpdir"), "safe-zip-root")
    SafePaths.resolveZipEntry(root, "weights/../../datastore/user_data.pb")
  }

  @Test(expected = IllegalArgumentException::class)
  fun resolve_zip_entry_rejects_backslash() {
    val root = File(System.getProperty("java.io.tmpdir"), "safe-zip-root")
    SafePaths.resolveZipEntry(root, "weights\\model.bin")
  }
}
