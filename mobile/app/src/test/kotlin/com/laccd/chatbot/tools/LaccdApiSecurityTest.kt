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

import java.io.IOException
import okhttp3.Interceptor
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LaccdApiSecurityTest {
  @Test
  fun bearer_interceptor_adds_authorization_header() {
    val interceptor = LaccdApi.mobileToolBearerInterceptor { "runtime-token" }
    val chain = RecordingChain()

    interceptor.intercept(chain)

    assertEquals("Bearer runtime-token", chain.proceededRequest!!.header("Authorization"))
  }

  @Test
  fun bearer_interceptor_fails_before_network_when_token_missing() {
    val interceptor = LaccdApi.mobileToolBearerInterceptor { "" }
    val chain = RecordingChain()

    val error = runCatching { interceptor.intercept(chain) }.exceptionOrNull()

    assertTrue(error is IOException)
    assertFalse(chain.proceeded)
  }

  private class RecordingChain : Interceptor.Chain {
    var proceeded = false
    var proceededRequest: Request? = null

    private val request = Request.Builder().url("https://example.test/api/tools/schedule").build()

    override fun request(): Request = request

    override fun proceed(request: Request): Response {
      proceeded = true
      proceededRequest = request
      return Response.Builder()
        .request(request)
        .protocol(Protocol.HTTP_1_1)
        .code(200)
        .message("OK")
        .build()
    }

    override fun connection() = null
    override fun call() = throw UnsupportedOperationException()
    override fun connectTimeoutMillis() = 0
    override fun withConnectTimeout(timeout: Int, unit: java.util.concurrent.TimeUnit) = this
    override fun readTimeoutMillis() = 0
    override fun withReadTimeout(timeout: Int, unit: java.util.concurrent.TimeUnit) = this
    override fun writeTimeoutMillis() = 0
    override fun withWriteTimeout(timeout: Int, unit: java.util.concurrent.TimeUnit) = this
  }
}
