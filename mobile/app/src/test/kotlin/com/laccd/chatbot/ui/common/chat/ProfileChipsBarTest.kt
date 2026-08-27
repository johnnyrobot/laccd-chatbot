/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.common.chat

import org.junit.Assert.assertEquals
import org.junit.Test

class ProfileChipsBarTest {

  @Test
  fun emptyComposer_replacesWithToken() {
    assertEquals("[my email: x@y]", appendChipToken("", "[my email: x@y]"))
  }

  @Test
  fun trailingWhitespace_appendsWithoutExtraSpace() {
    assertEquals("Hi [my ID: 1]", appendChipToken("Hi ", "[my ID: 1]"))
    assertEquals("Hi\n[my ID: 1]", appendChipToken("Hi\n", "[my ID: 1]"))
  }

  @Test
  fun noTrailingWhitespace_insertsSeparatorSpace() {
    assertEquals("Hello [my email: x@y]", appendChipToken("Hello", "[my email: x@y]"))
  }
}
