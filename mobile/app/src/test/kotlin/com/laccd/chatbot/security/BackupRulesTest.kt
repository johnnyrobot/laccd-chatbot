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
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Test

class BackupRulesTest {
  @Test
  fun manifest_disables_android_backup() {
    val root = File("src/main/AndroidManifest.xml")
    val xml = parse(root)
    val application = xml.getElementsByTagName("application").item(0) as org.w3c.dom.Element
    assertEquals("false", application.getAttribute("android:allowBackup"))
    assertEquals("", application.getAttribute("android:dataExtractionRules"))
    assertEquals("", application.getAttribute("android:fullBackupContent"))
  }

  private fun parse(file: File) =
    DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(file).apply {
      documentElement.normalize()
    }
}
