/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.common

import com.laccd.chatbot.BuildConfig

object MobileE2eMode {
  const val SMOKE = "smoke"
  const val REAL = "real"

  val current: String
    get() = BuildConfig.MOBILE_E2E_MODE.lowercase()

  val isSmoke: Boolean
    get() = BuildConfig.DEBUG && current == SMOKE

  val isReal: Boolean
    get() = current == REAL

  val useRecordedFixtures: Boolean
    get() = isSmoke && BuildConfig.MOBILE_E2E_USE_RECORDED_FIXTURES
}
