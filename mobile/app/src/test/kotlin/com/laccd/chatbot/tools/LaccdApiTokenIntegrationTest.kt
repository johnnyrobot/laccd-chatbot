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
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * end-to-end proof that the provisioned mobile-tool token actually
 * reaches the wire as `Authorization: Bearer <token>`, and that an empty/unset
 * token fails closed *before* any network call. Closes L5 (the token store was
 * never exercised end-to-end).
 */
class LaccdApiTokenIntegrationTest {

  private lateinit var server: MockWebServer

  @Before
  fun setUp() {
    server = MockWebServer()
    server.start()
  }

  @After
  fun tearDown() {
    server.shutdown()
    MobileToolTokenProvider.resetForTesting()
  }

  private fun api(tokenProvider: () -> String?): LaccdApi =
    LaccdApi.create(
      baseUrl = server.url("/").toString(),
      tokenProvider = tokenProvider,
      enableLogging = false,
    )

  @Test
  fun saved_token_flows_through_as_bearer_authorization_header() {
    server.enqueue(
      MockResponse()
        .setResponseCode(200)
        .setBody("""{"subject":"MATH","number":"261","campus":"LAMC","course":{}}"""),
    )

    val resp = runBlocking {
      api { "saved-token-xyz" }.elumen(subject = "MATH", number = "261", campus = "LAMC")
    }

    assertEquals("MATH", resp.subject)
    val recorded = server.takeRequest()
    assertEquals("Bearer saved-token-xyz", recorded.getHeader("Authorization"))
    assertEquals(
      "/api/tools/elumen?subject=MATH&number=261&campus=LAMC",
      recorded.path,
    )
  }

  @Test
  fun installed_provider_token_flows_through() {
    MobileToolTokenProvider.install { "provider-token" }
    server.enqueue(MockResponse().setResponseCode(200).setBody("""{"course":{}}"""))

    runBlocking {
      api(MobileToolTokenProvider.tokenProvider)
        .elumen(subject = "MATH", number = "261", campus = "LAMC")
    }

    assertEquals("Bearer provider-token", server.takeRequest().getHeader("Authorization"))
  }

  @Test
  fun empty_token_fails_closed_before_any_network_call() {
    val error = runBlocking {
      runCatching {
        api { "" }.elumen(subject = "MATH", number = "261", campus = "LAMC")
      }.exceptionOrNull()
    }

    assertNotNull(error)
    assertTrue("expected IOException, was $error", error is IOException)
    assertEquals("no request should reach the network", 0, server.requestCount)
  }

  @Test
  fun unset_provider_defaults_to_null_and_fails_closed() {
    MobileToolTokenProvider.resetForTesting()

    val error = runBlocking {
      runCatching {
        api(MobileToolTokenProvider.tokenProvider)
          .elumen(subject = "MATH", number = "261", campus = "LAMC")
      }.exceptionOrNull()
    }

    assertNotNull(error)
    assertTrue("expected IOException, was $error", error is IOException)
    assertEquals(0, server.requestCount)
  }
}
