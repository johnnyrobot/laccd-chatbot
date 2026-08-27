/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.common

import android.content.res.Configuration
import android.os.LocaleList
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.laccd.chatbot.data.DataStoreRepository
import com.laccd.chatbot.proto.LanguagePreference
import dagger.hilt.android.lifecycle.HiltViewModel
import java.util.Locale
import javax.inject.Inject
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn

/**
 * wraps a content tree with a [LocalConfiguration] / [LocalContext] override that
 * forces the user's chosen UI language. When [LanguagePreference.LANGUAGE_AUTO] (or the
 * proto3 default UNSPECIFIED), the system locale is used and no override is applied.
 *
 * `stringResource(...)` reads from `LocalContext.resources`, so swapping `LocalContext`
 * causes the entire downstream tree to pick up the matching `values-xx/strings.xml`.
 */
@Composable
fun AppLanguageProvider(content: @Composable () -> Unit) {
  val viewModel: AppLanguageViewModel = hiltViewModel()
  val language by viewModel.language.collectAsState()
  ProvideLocale(language = language, content = content)
}

@Composable
private fun ProvideLocale(language: LanguagePreference, content: @Composable () -> Unit) {
  val baseContext = LocalContext.current
  val baseConfig = LocalConfiguration.current
  val locale: Locale? =
    when (language) {
      LanguagePreference.LANGUAGE_EN -> Locale("en")
      LanguagePreference.LANGUAGE_ES -> Locale("es")
      else -> null
    }
  if (locale == null) {
    content()
    return
  }
  val updatedConfig =
    remember(locale, baseConfig.fontScale, baseConfig.densityDpi) {
      Configuration(baseConfig).apply {
        setLocale(locale)
        setLocales(LocaleList(locale))
      }
    }
  val updatedContext = remember(updatedConfig) { baseContext.createConfigurationContext(updatedConfig) }
  CompositionLocalProvider(
    LocalContext provides updatedContext,
    LocalConfiguration provides updatedConfig,
    content = content,
  )
}

@HiltViewModel
class AppLanguageViewModel @Inject constructor(repository: DataStoreRepository) : ViewModel() {
  val language: StateFlow<LanguagePreference> =
    repository.observeLanguage().stateIn(
      scope = viewModelScope,
      started = SharingStarted.WhileSubscribed(5_000L),
      initialValue = LanguagePreference.LANGUAGE_AUTO,
    )
}
