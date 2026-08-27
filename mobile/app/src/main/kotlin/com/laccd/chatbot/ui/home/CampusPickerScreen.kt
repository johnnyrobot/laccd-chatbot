/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.home

import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.ViewModel
import com.laccd.chatbot.R
import com.laccd.chatbot.data.DataStoreRepository
import com.laccd.chatbot.data.campus.Campus
import com.laccd.chatbot.ui.theme.bodyFontFamily
import com.laccd.chatbot.ui.theme.campusMonogramStyle
import com.laccd.chatbot.ui.theme.displayFontFamily
import com.laccd.chatbot.ui.theme.luminance
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject

/**
 * First-run campus picker. The grid uses [LazyVerticalGrid] (rather than nested Rows) so it adapts to the 9-campus
 * fixed list while still rendering a 3×3 layout on phones. Cards reveal with a staggered
 * cascade — each card waits index*60ms before fading in, mirroring how a yearbook page
 * fills row-by-row.
 *
 * Reachable from two entry points:
 *  - First launch: nav graph routes here when [DataStoreRepository.readSelectedCampus] is
 *    blank. On select, persists the choice and pops itself off the back stack.
 *  - Settings → "Switch campus": navigates here from the SettingsDialog. Same on-select flow.
 */
@Composable
fun CampusPickerScreen(onCampusPicked: (Campus) -> Unit, modifier: Modifier = Modifier) {
  val viewModel: CampusPickerViewModel = hiltViewModel()
  val campuses = remember { Campus.entries.toList() }

  Box(
    modifier =
      modifier
        .fillMaxSize()
        .background(MaterialTheme.colorScheme.background)
        .statusBarsPadding(),
    contentAlignment = Alignment.TopCenter,
  ) {
    Column(modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp)) {
      // Header: serif title + sans subtitle, generous vertical rhythm.
      Column(
        modifier = Modifier.fillMaxWidth().padding(top = 32.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
      ) {
        Text(
          text = stringResource(R.string.laccd_campus_picker_title),
          style =
            MaterialTheme.typography.displaySmall.copy(
              fontFamily = displayFontFamily,
              fontWeight = FontWeight.SemiBold,
              fontSize = 36.sp,
              lineHeight = 40.sp,
              letterSpacing = (-0.5).sp,
            ),
          color = MaterialTheme.colorScheme.onBackground,
        )
        Text(
          text = stringResource(R.string.laccd_campus_picker_subtitle),
          style =
            MaterialTheme.typography.bodyMedium.copy(
              fontFamily = bodyFontFamily,
              fontSize = 15.sp,
              lineHeight = 22.sp,
            ),
          color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
      }

      // 3-column grid of monograms.
      LazyVerticalGrid(
        columns = GridCells.Fixed(3),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        modifier = Modifier.fillMaxWidth().padding(bottom = 32.dp),
      ) {
        itemsIndexed(items = campuses, key = { _, c -> c.code }) { index, campus ->
          CampusMonogramCard(
            campus = campus,
            index = index,
            onClick = {
              viewModel.persistSelection(campus.code)
              onCampusPicked(campus)
            },
          )
        }
      }
    }
  }
}

@Composable
private fun CampusMonogramCard(campus: Campus, index: Int, onClick: () -> Unit) {
  // Stagger the fade-in by index. Each card waits 60ms more than the previous one before
  // animating from invisible+slightly-down to fully-revealed. Mirrors a yearbook-plate fill.
  var revealStarted by remember { mutableStateOf(false) }
  val staggerDelay = remember(index) { 80L + index * 60L }
  LaunchedEffect(Unit) {
    kotlinx.coroutines.delay(staggerDelay)
    revealStarted = true
  }
  val progress by animateFloatAsState(
    targetValue = if (revealStarted) 1f else 0f,
    animationSpec = tween(durationMillis = 420, easing = LinearOutSlowInEasing),
    label = "campusReveal:${campus.code}",
  )

  // Pick a foreground that contrasts against the campus brand. Most LACCD primaries are dark
  // navy/maroon/forest, so white reads cleanly. The few lighter accents (LAHC's gold-ish
  // secondary, LATTC's purple) still get white on primary.
  val foreground =
    if (campus.primaryColor.luminance() < 0.45f) Color.White else Color(0xFF1A1A1A)
  val foregroundMuted = foreground.copy(alpha = 0.72f)

  Box(
    modifier =
      Modifier.aspectRatio(1f)
        .graphicsLayer {
          alpha = progress
          translationY = (12.dp * (1 - progress)).toPx()
        }
        // Sharp top-left corner echoes the chat-bubble shape language for the assistant
        // messages — a small editorial signature carried through the app.
        .clip(RoundedCornerShape(topStart = 4.dp, topEnd = 18.dp, bottomEnd = 18.dp, bottomStart = 18.dp))
        .background(campus.primaryColor)
        .clickable(onClick = onClick),
  ) {
    // Code in the upper-left as a label tag.
    Text(
      text = campus.code,
      style = MaterialTheme.typography.labelSmall.copy(letterSpacing = 1.6.sp),
      color = foregroundMuted,
      modifier = Modifier.align(Alignment.TopStart).padding(start = 12.dp, top = 12.dp),
    )

    // Display-serif nickname in the center. Auto-shrinks for long nicknames ("Trade-Tech",
    // "Southwest") via per-card font size adjustment.
    val configuration = LocalConfiguration.current
    val baseFontSize = (configuration.screenWidthDp / 14f).sp.value.coerceIn(28f, 40f).sp
    val nicknameFontSize =
      if (campus.nickname.length > 7) baseFontSize * 0.78f else baseFontSize
    Text(
      text = campus.nickname,
      style =
        campusMonogramStyle.copy(
          fontSize = nicknameFontSize,
          lineHeight = nicknameFontSize * 1.05f,
        ),
      color = foreground,
      textAlign = TextAlign.Center,
      modifier = Modifier.align(Alignment.Center).padding(horizontal = 8.dp),
    )

    // Short hint at the bottom — the school name in body sans, more readable than the formal
    // "Los Angeles X College" full strings inside a small card.
    Text(
      text = campus.shortName,
      style =
        MaterialTheme.typography.labelSmall.copy(
          fontFamily = bodyFontFamily,
          fontSize = 10.sp,
          lineHeight = 12.sp,
        ),
      color = foregroundMuted,
      maxLines = 2,
      textAlign = TextAlign.Start,
      modifier =
        Modifier.align(Alignment.BottomStart).padding(start = 12.dp, bottom = 12.dp, end = 12.dp),
    )
  }
}

@HiltViewModel
class CampusPickerViewModel @Inject constructor(private val repository: DataStoreRepository) :
  ViewModel() {
  fun persistSelection(campusCode: String) {
    repository.saveSelectedCampus(campusCode)
  }
}
