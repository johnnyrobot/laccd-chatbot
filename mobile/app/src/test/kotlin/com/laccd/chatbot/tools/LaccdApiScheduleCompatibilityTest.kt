/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.laccd.chatbot.tools

import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

class LaccdApiScheduleCompatibilityTest {

  @Test
  fun schedule_response_preserves_additive_section_metadata_and_ignores_unknown_keys() {
    val response = Moshi.Builder()
      .add(KotlinJsonAdapterFactory())
      .build()
      .adapter(ScheduleResponse::class.java)
      .fromJson(
        """
        {
          "campus": "LAMC",
          "campus_name": "Los Angeles Mission College",
          "term": "2026SP",
          "term_name": "Spring 2026",
          "subject": "MATH",
          "course_number": "261",
          "keyword": "calculus",
          "sections": [
            {
              "course_code": "MATH 261",
              "course_title": "Calculus I",
              "class_number": "12345",
              "section": "12345",
              "days": "MW",
              "time": "10:00 AM-12:05 PM",
              "instructor": "Ada Lovelace",
              "days_times": "MW 10:00 AM-12:05 PM",
              "location": "CMSC 101",
              "dates": "02/09/2026-06/08/2026",
              "class_type": ["OER"],
              "units": 4.0,
              "ztc": true,
              "component": "Lecture",
              "related_to": "12346",
              "future_backend_key": "ignored"
            }
          ],
          "total_sections": 1
        }
        """.trimIndent(),
      )

    assertNotNull(response)
    assertEquals("LAMC", response!!.campus)
    assertEquals("Los Angeles Mission College", response.campusName)
    assertEquals("2026SP", response.term)
    assertEquals("Spring 2026", response.termName)
    assertEquals("MATH", response.subject)
    assertEquals("261", response.courseNumber)
    assertEquals("calculus", response.keyword)
    assertEquals(1, response.totalSections)
    assertEquals(1, response.sections.size)

    val section = response.sections.single()
    assertEquals("MATH 261", section.courseCode)
    assertEquals("12345", section.section)
    assertEquals("Ada Lovelace", section.instructor)
    assertEquals("MW 10:00 AM-12:05 PM", section.daysTimes)
    assertEquals("CMSC 101", section.location)
    assertEquals(4.0, section.units)
    assertEquals(true, section.ztc)
    assertEquals("Lecture", section.component)
    assertEquals("12346", section.relatedTo)
  }
}
