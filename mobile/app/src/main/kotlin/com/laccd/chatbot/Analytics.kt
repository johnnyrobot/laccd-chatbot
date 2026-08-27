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

import android.os.Bundle

// LACCD fork: Firebase Analytics stripped per `feedback_no_firebase_no_accounts`. The original
// Edge Gallery `firebaseAnalytics?.logEvent(...)` call sites are preserved unchanged so the
// upstream merge story stays simple; the property here always returns null, so every safe-call
// short-circuits and no telemetry leaves the device. The `FirebaseAnalytics` shim type
// satisfies the existing `FirebaseAnalytics.Event.APP_OPEN` reference in MainActivity.
class FirebaseAnalytics private constructor() {
  fun logEvent(@Suppress("UNUSED_PARAMETER") name: String, @Suppress("UNUSED_PARAMETER") bundle: Bundle?) {
    // No-op.
  }

  object Event {
    const val APP_OPEN = "app_open"
  }
}

val firebaseAnalytics: FirebaseAnalytics?
  get() = null

enum class GalleryEvent(val id: String) {
  CAPABILITY_SELECT(id = "capability_select"),
  GENERATE_ACTION(id = "generate_action"),
  BUTTON_CLICKED(id = "button_clicked"),
}
