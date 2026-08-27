/*
 * Copyright 2025 Google LLC
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

package com.laccd.chatbot

import android.app.Application
import com.laccd.chatbot.data.DataStoreRepository
import com.laccd.chatbot.notifications.NotificationChannels
import com.laccd.chatbot.tools.MobileToolTokenProvider
import com.laccd.chatbot.ui.theme.ThemeSettings
import dagger.hilt.android.HiltAndroidApp
import javax.inject.Inject

@HiltAndroidApp
class GalleryApplication : Application() {

  @Inject lateinit var dataStoreRepository: DataStoreRepository

  override fun onCreate() {
    super.onCreate()

    // Wire the runtime mobile-tool bearer token into the LaccdApi client so
    // real /api/tools/* calls authenticate with the user-provisioned token from
    // the encrypted secret store. Reads live on each request, so a token saved
    // later in Settings is picked up without a restart. Unset -> null -> the
    // client fails closed before any network call.
    MobileToolTokenProvider.install { dataStoreRepository.readMobileToolToken() }

    // Load saved theme.
    ThemeSettings.themeOverride.value = dataStoreRepository.readTheme()

    // register notification channels once per process. Channel
    // creation is idempotent so subsequent process starts no-op.
    NotificationChannels.ensureCreated(this)
  }
}
