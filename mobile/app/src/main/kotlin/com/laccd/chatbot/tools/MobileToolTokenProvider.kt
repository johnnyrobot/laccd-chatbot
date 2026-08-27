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

/**
 * Process-wide bridge that supplies the runtime mobile-tool bearer token to
 * [LaccdApi] from any call site, including the `LlmChatModelHelper` singleton
 * object that cannot receive Hilt injection.
 *
 * `GalleryApplication` installs a source backed by
 * `DataStoreRepository.readMobileToolToken()` (the encrypted secret store) once
 * per process. Until a source is installed — or when the user has not saved a
 * token — the provider yields `null`, which makes [LaccdApi]'s interceptor fail
 * closed before any network call.
 */
object MobileToolTokenProvider {

  @Volatile
  private var source: () -> String? = { null }

  /** Install the token source (called once from application startup). */
  fun install(source: () -> String?) {
    this.source = source
  }

  /** Current token, or `null` when unset. */
  fun current(): String? = source()

  /**
   * A stable [tokenProvider] lambda to hand to [LaccdApi.create]. It resolves
   * the *currently installed* source on every request, so a token saved after
   * the client was built is picked up without rebuilding the client.
   */
  val tokenProvider: () -> String? = { source() }

  /** Test hook: reset to the fail-closed default. */
  fun resetForTesting() {
    source = { null }
  }
}
