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

import android.util.Log
import com.laccd.chatbot.BuildConfig
import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import java.io.IOException
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import retrofit2.http.GET
import retrofit2.http.Query
import java.util.concurrent.TimeUnit

/**
 * Retrofit interface for the cloud `/api/tools/...` endpoints implemented in
 * `backend/app/routes/mobile_tools.py`. Every call is authenticated by a runtime
 * bearer token injected by [mobileToolBearerInterceptor]; the client fails before
 * network when no token is provisioned.
 *
 * The response data classes mirror the `dict` shapes returned by the FastAPI
 * endpoints. Fields not present on the wire fall back to defaults — keeps
 * the client tolerant to backend additions.
 */
interface LaccdApi {

  @GET("api/tools/schedule")
  suspend fun schedule(
    @Query("campus") campus: String,
    @Query("term") term: String,
    @Query("subject") subject: String? = null,
    @Query("course_number") courseNumber: String? = null,
    @Query("keyword") keyword: String? = null,
  ): ScheduleResponse

  @GET("api/tools/assist")
  suspend fun assist(
    @Query("course") course: String,
    @Query("from") from: String,
    @Query("to") to: String,
  ): AssistResponse

  @GET("api/tools/programs")
  suspend fun programs(
    @Query("q") query: String,
    @Query("campus") campus: String? = null,
  ): ProgramsResponse

  @GET("api/tools/sequence")
  suspend fun sequence(
    @Query("program_id") programId: String,
    @Query("campus") campus: String = "LAMC",
    @Query("pace") pace: String = "full",
  ): SequenceResponse

  @GET("api/tools/elumen")
  suspend fun elumen(
    @Query("subject") subject: String,
    @Query("number") number: String,
    @Query("campus") campus: String = "LAMC",
  ): ElumenResponse

  companion object {
    private const val TAG = "LaccdApi"

    /** Builds a Retrofit-backed [LaccdApi] with runtime auth header injection. */
    fun create(
      baseUrl: String = BuildConfig.MOBILE_TOOL_BASE_URL,
      tokenProvider: () -> String? = { null },
      enableLogging: Boolean = BuildConfig.DEBUG,
    ): LaccdApi {
      val moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
      val httpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(45, TimeUnit.SECONDS)
        .addInterceptor(mobileToolBearerInterceptor(tokenProvider))
        .addInterceptor { chain ->
          if (enableLogging) {
            Log.d(TAG, "Sending authenticated cloud-tool request")
          }
          chain.proceed(chain.request())
        }
        .build()
      return Retrofit.Builder()
        .baseUrl(if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/")
        .client(httpClient)
        .addConverterFactory(MoshiConverterFactory.create(moshi))
        .build()
        .create(LaccdApi::class.java)
    }

    /**
     * Adds the bearer token to every outgoing request. Missing tokens stop the
     * request before network so the app falls back to on-device behavior.
     */
    fun mobileToolBearerInterceptor(tokenProvider: () -> String?): Interceptor = Interceptor { chain ->
      val req = chain.request()
      val token = tokenProvider()?.trim().orEmpty()
      if (token.isEmpty()) {
        throw IOException("Mobile tool token is not configured.")
      }
      val out = req.newBuilder().header("Authorization", "Bearer $token").build()
      chain.proceed(out)
    }
  }
}

// ── Response shapes (mirror backend/app/routes/mobile_tools.py) ────────────

@JsonClass(generateAdapter = true)
data class ScheduleResponse(
  val campus: String = "",
  @Json(name = "campus_name") val campusName: String = "",
  val term: String = "",
  @Json(name = "term_name") val termName: String = "",
  val subject: String? = null,
  @Json(name = "course_number") val courseNumber: String? = null,
  val keyword: String? = null,
  val sections: List<ScheduleSection> = emptyList(),
  @Json(name = "total_sections") val totalSections: Int = 0,
)

@JsonClass(generateAdapter = true)
data class ScheduleSection(
  @Json(name = "course_code") val courseCode: String = "",
  val component: String = "",
  @Json(name = "related_to") val relatedTo: String = "",
  val section: String = "",
  val instructor: String? = null,
  @Json(name = "days_times") val daysTimes: String? = null,
  val location: String? = null,
  val units: Double? = null,
  val ztc: Boolean? = null,
)

@JsonClass(generateAdapter = true)
data class AssistResponse(
  val course: String = "",
  val from: String = "",
  val to: String = "",
  @Json(name = "area_key") val areaKey: String = "",
  @Json(name = "year_id") val yearId: Int = 0,
  val matches: List<AssistMatch> = emptyList(),
  @Json(name = "all_transferable_count") val allTransferableCount: Int = 0,
  @Json(name = "filtered_count") val filteredCount: Int = 0,
  val source: String = "live",
)

@JsonClass(generateAdapter = true)
data class AssistMatch(
  val code: String? = null,
  val title: String? = null,
  @Json(name = "courseCode") val courseCode: String? = null,
  val units: Double? = null,
)

@JsonClass(generateAdapter = true)
data class ProgramsResponse(
  val query: String = "",
  val campus: String? = null,
  val programs: List<ProgramHit> = emptyList(),
  val count: Int = 0,
)

@JsonClass(generateAdapter = true)
data class ProgramHit(
  @Json(name = "masterRecordId") val masterRecordId: String? = null,
  val title: String = "",
  @Json(name = "award_type") val awardType: String? = null,
  @Json(name = "campus_code") val campusCode: String? = null,
  @Json(name = "campus_name") val campusName: String? = null,
)

@JsonClass(generateAdapter = true)
data class SequenceResponse(
  @Json(name = "program_name") val programName: String = "",
  @Json(name = "program_id") val programId: String = "",
  val campus: String = "",
  val pace: String = "full",
  @Json(name = "terms_to_complete") val termsToComplete: Int = 0,
  val sequence: List<SequenceTerm> = emptyList(),
  val warnings: List<String> = emptyList(),
)

@JsonClass(generateAdapter = true)
data class SequenceTerm(
  @Json(name = "term_no") val termNo: Int = 0,
  val name: String = "",
  val courses: List<SequenceCourse> = emptyList(),
  val units: Double = 0.0,
)

@JsonClass(generateAdapter = true)
data class SequenceCourse(
  val code: String = "",
  val title: String = "",
  val units: Double = 0.0,
  val notes: String? = null,
)

@JsonClass(generateAdapter = true)
data class ElumenResponse(
  val subject: String = "",
  val number: String = "",
  val campus: String = "",
  val course: ElumenCourse = ElumenCourse(),
)

@JsonClass(generateAdapter = true)
data class ElumenCourse(
  val code: String = "",
  val title: String = "",
  val description: String? = null,
  val units: Double? = null,
  @Json(name = "units_min") val unitsMin: Double? = null,
  @Json(name = "units_max") val unitsMax: Double? = null,
  val prerequisite: String? = null,
  val prerequisites: List<List<String>> = emptyList(),
  val corequisite: String? = null,
  val corequisites: List<List<String>> = emptyList(),
  val advisory: String? = null,
  val advisories: List<String> = emptyList(),
)
