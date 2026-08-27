/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.tools

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MobileToolTokenProviderTest {

  @After
  fun tearDown() {
    MobileToolTokenProvider.resetForTesting()
  }

  @Test
  fun defaults_to_null_before_install() {
    MobileToolTokenProvider.resetForTesting()
    assertNull(MobileToolTokenProvider.current())
    assertNull(MobileToolTokenProvider.tokenProvider())
  }

  @Test
  fun install_supplies_current_token() {
    MobileToolTokenProvider.install { "abc123" }
    assertEquals("abc123", MobileToolTokenProvider.current())
    assertEquals("abc123", MobileToolTokenProvider.tokenProvider())
  }

  @Test
  fun token_provider_resolves_source_lazily_on_each_call() {
    var token: String? = "first"
    MobileToolTokenProvider.install { token }
    assertEquals("first", MobileToolTokenProvider.tokenProvider())
    token = "second"
    assertEquals("second", MobileToolTokenProvider.tokenProvider())
    token = null
    assertNull(MobileToolTokenProvider.tokenProvider())
  }

  @Test
  fun reset_restores_fail_closed_default() {
    MobileToolTokenProvider.install { "abc123" }
    MobileToolTokenProvider.resetForTesting()
    assertNull(MobileToolTokenProvider.current())
  }
}
