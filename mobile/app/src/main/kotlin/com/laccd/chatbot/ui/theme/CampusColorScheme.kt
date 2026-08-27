/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.theme

import androidx.compose.material3.ColorScheme
import androidx.compose.ui.graphics.Color

/**
 * derive a Material 3 [ColorScheme] from a campus's brand colors.
 *
 * The cloud chatbot already canonicalizes per-campus brand colors in
 * `shared/campus_config.py`; we mirror them here. Material 3's `dynamicColorScheme(seed)` is
 * gated on API 31+ via system colors and always uses Material You — neither is what we want
 * for a brand-faithful look. So [seededLightScheme] / [seededDarkScheme] take a base scheme
 * (with all the surface/background/error tokens already polished) and overlay only the
 * accent triplet (primary/secondary/tertiary) and their on-/container roles with brand-
 * derived colors. The result preserves Edge Gallery's careful surface tuning while shifting
 * the brand accent per campus.
 */

private fun Color.toward(target: Color, amount: Float): Color {
  val a = amount.coerceIn(0f, 1f)
  return Color(
    red = red + (target.red - red) * a,
    green = green + (target.green - green) * a,
    blue = blue + (target.blue - blue) * a,
    alpha = alpha,
  )
}

/** Relative luminance per WCAG; uses sRGB linearization. */
internal fun Color.luminance(): Float {
  fun chan(c: Float): Float =
    if (c <= 0.03928f) c / 12.92f else Math.pow(((c + 0.055f) / 1.055f).toDouble(), 2.4).toFloat()
  return 0.2126f * chan(red) + 0.7152f * chan(green) + 0.0722f * chan(blue)
}

private fun pickOnColor(bg: Color): Color =
  if (bg.luminance() < 0.45f) Color.White else Color(0xFF1A1A1A)

fun seededLightScheme(
  base: ColorScheme,
  primary: Color,
  secondary: Color,
  accent: Color,
): ColorScheme =
  base.copy(
    primary = primary,
    onPrimary = pickOnColor(primary),
    primaryContainer = primary.toward(Color.White, 0.82f),
    onPrimaryContainer = primary.toward(Color.Black, 0.55f),
    secondary = secondary,
    onSecondary = pickOnColor(secondary),
    secondaryContainer = secondary.toward(Color.White, 0.82f),
    onSecondaryContainer = secondary.toward(Color.Black, 0.55f),
    tertiary = accent,
    onTertiary = pickOnColor(accent),
    tertiaryContainer = accent.toward(Color.White, 0.82f),
    onTertiaryContainer = accent.toward(Color.Black, 0.55f),
    inversePrimary = primary.toward(Color.White, 0.55f),
  )

fun seededDarkScheme(
  base: ColorScheme,
  primary: Color,
  secondary: Color,
  accent: Color,
): ColorScheme {
  val brightPrimary = primary.toward(Color.White, 0.45f)
  val brightSecondary = secondary.toward(Color.White, 0.45f)
  val brightAccent = accent.toward(Color.White, 0.45f)
  return base.copy(
    primary = brightPrimary,
    onPrimary = primary.toward(Color.Black, 0.55f),
    primaryContainer = primary.toward(Color.Black, 0.45f),
    onPrimaryContainer = primary.toward(Color.White, 0.78f),
    secondary = brightSecondary,
    onSecondary = secondary.toward(Color.Black, 0.55f),
    secondaryContainer = secondary.toward(Color.Black, 0.45f),
    onSecondaryContainer = secondary.toward(Color.White, 0.78f),
    tertiary = brightAccent,
    onTertiary = accent.toward(Color.Black, 0.55f),
    tertiaryContainer = accent.toward(Color.Black, 0.45f),
    onTertiaryContainer = accent.toward(Color.White, 0.78f),
    inversePrimary = primary,
  )
}
