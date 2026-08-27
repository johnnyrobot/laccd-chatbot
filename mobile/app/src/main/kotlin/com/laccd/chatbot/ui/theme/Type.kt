/*
 * Copyright 2025 Google LLC
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

package com.laccd.chatbot.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.laccd.chatbot.R

/**
 * The public app uses only bundled Nunito fonts. Typography never triggers a font-provider
 * network request and remains available offline.
 */

/** Display serif for headlines, the campus-picker monogram, the home-page title. */
val displayFontFamily =
  FontFamily(
    Font(R.font.nunito_regular, FontWeight.Normal),
    Font(R.font.nunito_medium, FontWeight.Medium),
    Font(R.font.nunito_semibold, FontWeight.SemiBold),
    Font(R.font.nunito_bold, FontWeight.Bold),
    Font(R.font.nunito_extrabold, FontWeight.ExtraBold),
    Font(R.font.nunito_black, FontWeight.Black),
  )

/** Body and label family. */
val bodyFontFamily =
  FontFamily(
    Font(R.font.nunito_light, FontWeight.Light),
    Font(R.font.nunito_regular, FontWeight.Normal),
    Font(R.font.nunito_medium, FontWeight.Medium),
    Font(R.font.nunito_semibold, FontWeight.SemiBold),
    Font(R.font.nunito_bold, FontWeight.Bold),
  )

/**
 * Backwards-compatible alias used by existing call sites. Continues to point
 * at the body family so existing call sites pick up the editorial sans automatically.
 */
val appFontFamily = bodyFontFamily

private val baseline = Typography()

val AppTypography =
  Typography(
    // Display + headline lean editorial: serif, slightly tighter tracking.
    displayLarge =
      baseline.displayLarge.copy(fontFamily = displayFontFamily, letterSpacing = (-0.5).sp),
    displayMedium =
      baseline.displayMedium.copy(fontFamily = displayFontFamily, letterSpacing = (-0.5).sp),
    displaySmall =
      baseline.displaySmall.copy(fontFamily = displayFontFamily, letterSpacing = (-0.25).sp),
    headlineLarge =
      baseline.headlineLarge.copy(fontFamily = displayFontFamily, letterSpacing = (-0.25).sp),
    headlineMedium = baseline.headlineMedium.copy(fontFamily = displayFontFamily),
    headlineSmall = baseline.headlineSmall.copy(fontFamily = displayFontFamily),
    // Title + body + label use the cleaner sans for legibility.
    titleLarge = baseline.titleLarge.copy(fontFamily = bodyFontFamily),
    titleMedium = baseline.titleMedium.copy(fontFamily = bodyFontFamily),
    titleSmall = baseline.titleSmall.copy(fontFamily = bodyFontFamily),
    bodyLarge = baseline.bodyLarge.copy(fontFamily = bodyFontFamily),
    bodyMedium = baseline.bodyMedium.copy(fontFamily = bodyFontFamily),
    bodySmall = baseline.bodySmall.copy(fontFamily = bodyFontFamily),
    labelLarge = baseline.labelLarge.copy(fontFamily = bodyFontFamily),
    labelMedium = baseline.labelMedium.copy(fontFamily = bodyFontFamily),
    labelSmall = baseline.labelSmall.copy(fontFamily = bodyFontFamily),
  )

val titleMediumNarrow =
  baseline.titleMedium.copy(fontFamily = bodyFontFamily, letterSpacing = 0.0.sp)

val titleSmaller =
  baseline.titleSmall.copy(
    fontFamily = bodyFontFamily,
    fontSize = 12.sp,
    fontWeight = FontWeight.Bold,
  )

val labelSmallNarrow = baseline.labelSmall.copy(fontFamily = bodyFontFamily, letterSpacing = 0.0.sp)

val labelSmallNarrowMedium =
  baseline.labelSmall.copy(
    fontFamily = bodyFontFamily,
    fontWeight = FontWeight.Medium,
    letterSpacing = 0.0.sp,
  )

val bodySmallNarrow = baseline.bodySmall.copy(fontFamily = bodyFontFamily, letterSpacing = 0.0.sp)

val bodySmallMediumNarrow =
  baseline.bodySmall.copy(fontFamily = bodyFontFamily, letterSpacing = 0.0.sp, fontSize = 14.sp)

val bodySmallMediumNarrowBold =
  baseline.bodySmall.copy(
    fontFamily = bodyFontFamily,
    letterSpacing = 0.0.sp,
    fontSize = 14.sp,
    fontWeight = FontWeight.Bold,
  )

val homePageTitleStyle =
  baseline.displayMedium.copy(
    fontFamily = displayFontFamily,
    fontSize = 48.sp,
    lineHeight = 48.sp,
    letterSpacing = (-1).sp,
    fontWeight = FontWeight.Medium,
  )

val bodyLargeNarrow = baseline.bodyLarge.copy(letterSpacing = 0.2.sp, fontFamily = bodyFontFamily)
val bodyMediumMedium = baseline.bodyMedium.copy(fontWeight = FontWeight.Medium, fontFamily = bodyFontFamily)

val headlineLargeMedium =
  baseline.headlineLarge.copy(fontWeight = FontWeight.Medium, fontFamily = displayFontFamily)

val emptyStateTitle =
  baseline.headlineSmall.copy(fontSize = 37.sp, lineHeight = 50.sp, fontFamily = displayFontFamily)
val emptyStateContent =
  baseline.headlineSmall.copy(fontSize = 16.sp, lineHeight = 22.sp, fontFamily = bodyFontFamily)

/** Display style reserved for the campus-picker monogram cards. Heavy serif at large size. */
val campusMonogramStyle =
  baseline.displayLarge.copy(
    fontFamily = displayFontFamily,
    fontSize = 56.sp,
    lineHeight = 56.sp,
    fontWeight = FontWeight.Bold,
    letterSpacing = (-1).sp,
  )
