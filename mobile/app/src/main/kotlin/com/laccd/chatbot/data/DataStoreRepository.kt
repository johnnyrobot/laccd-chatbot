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

package com.laccd.chatbot.data

import androidx.datastore.core.DataStore
import com.laccd.chatbot.proto.BenchmarkResult
import com.laccd.chatbot.proto.BenchmarkResults
import com.laccd.chatbot.proto.ImportedModel
import com.laccd.chatbot.proto.LanguagePreference
import com.laccd.chatbot.proto.Settings
import com.laccd.chatbot.proto.Theme
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.runBlocking

// TODO(b/423700720): Change to async (suspend) functions
interface DataStoreRepository {
  fun saveTextInputHistory(history: List<String>)

  fun readTextInputHistory(): List<String>

  fun saveTheme(theme: Theme)

  fun readTheme(): Theme

  fun saveLanguage(language: LanguagePreference)

  fun readLanguage(): LanguagePreference

  fun observeLanguage(): Flow<LanguagePreference>

  fun saveSelectedCampus(campusCode: String)

  fun readSelectedCampus(): String

  fun observeSelectedCampus(): Flow<String>

  fun saveMobileToolToken(token: String)

  fun readMobileToolToken(): String?

  fun deleteMobileToolToken()

  fun saveImportedModels(importedModels: List<ImportedModel>)

  fun readImportedModels(): List<ImportedModel>

  fun isTosAccepted(): Boolean

  fun acceptTos()

  fun setHasSeenBenchmarkComparisonHelp(seen: Boolean)

  fun getHasSeenBenchmarkComparisonHelp(): Boolean

  fun addBenchmarkResult(result: BenchmarkResult)

  fun getAllBenchmarkResults(): List<BenchmarkResult>

  fun deleteBenchmarkResult(index: Int)

  /** Records that a promo with the specified ID has been viewed. */
  fun addViewedPromoId(promoId: String)

  /** Removes a viewed promo record. */
  fun removeViewedPromoId(promoId: String)

  /** Returns whether a promo with the specified ID has been viewed. */
  fun hasViewedPromo(promoId: String): Boolean
}

/** Repository for managing data using Proto DataStore. */
class DefaultDataStoreRepository(
  private val dataStore: DataStore<Settings>,
  private val benchmarkResultsDataStore: DataStore<BenchmarkResults>,
  private val secretStore: SecretStore,
) : DataStoreRepository {
  override fun saveTextInputHistory(history: List<String>) {
    runBlocking {
      dataStore.updateData { settings ->
        settings.toBuilder().clearTextInputHistory().addAllTextInputHistory(history).build()
      }
    }
  }

  override fun readTextInputHistory(): List<String> {
    return runBlocking {
      val settings = dataStore.data.first()
      settings.textInputHistoryList
    }
  }

  override fun saveTheme(theme: Theme) {
    runBlocking {
      dataStore.updateData { settings -> settings.toBuilder().setTheme(theme).build() }
    }
  }

  override fun readTheme(): Theme {
    return runBlocking {
      val settings = dataStore.data.first()
      val curTheme = settings.theme
      // Use "auto" as the default theme.
      if (curTheme == Theme.THEME_UNSPECIFIED) Theme.THEME_AUTO else curTheme
    }
  }

  override fun saveLanguage(language: LanguagePreference) {
    runBlocking {
      dataStore.updateData { settings -> settings.toBuilder().setLanguage(language).build() }
    }
  }

  override fun readLanguage(): LanguagePreference {
    return runBlocking {
      val cur = dataStore.data.first().language
      // Both UNSPECIFIED (the proto3 default) and AUTO mean "follow the system locale".
      if (cur == LanguagePreference.LANGUAGE_UNSPECIFIED) LanguagePreference.LANGUAGE_AUTO else cur
    }
  }

  override fun observeLanguage(): Flow<LanguagePreference> =
    dataStore.data.map { settings ->
      val cur = settings.language
      if (cur == LanguagePreference.LANGUAGE_UNSPECIFIED) LanguagePreference.LANGUAGE_AUTO else cur
    }

  override fun saveSelectedCampus(campusCode: String) {
    runBlocking {
      dataStore.updateData { settings ->
        settings.toBuilder().setSelectedCampusCode(campusCode).build()
      }
    }
  }

  override fun readSelectedCampus(): String =
    runBlocking { dataStore.data.first().selectedCampusCode }

  override fun observeSelectedCampus(): Flow<String> =
    dataStore.data.map { it.selectedCampusCode }

  override fun saveMobileToolToken(token: String) {
    secretStore.putString(KEY_MOBILE_TOOL_TOKEN, token)
  }

  override fun readMobileToolToken(): String? =
    secretStore.getString(KEY_MOBILE_TOOL_TOKEN)?.takeIf { it.isNotEmpty() }

  override fun deleteMobileToolToken() {
    secretStore.remove(KEY_MOBILE_TOOL_TOKEN)
  }

  override fun saveImportedModels(importedModels: List<ImportedModel>) {
    runBlocking {
      dataStore.updateData { settings ->
        settings.toBuilder().clearImportedModel().addAllImportedModel(importedModels).build()
      }
    }
  }

  override fun readImportedModels(): List<ImportedModel> {
    return runBlocking {
      val settings = dataStore.data.first()
      settings.importedModelList
    }
  }

  override fun isTosAccepted(): Boolean {
    return runBlocking {
      val settings = dataStore.data.first()
      settings.isTosAccepted
    }
  }

  override fun acceptTos() {
    runBlocking {
      dataStore.updateData { settings -> settings.toBuilder().setIsTosAccepted(true).build() }
    }
  }

  override fun setHasSeenBenchmarkComparisonHelp(seen: Boolean) {
    runBlocking {
      dataStore.updateData { settings ->
        settings.toBuilder().setHasSeenBenchmarkComparisonHelp(seen).build()
      }
    }
  }

  override fun getHasSeenBenchmarkComparisonHelp(): Boolean {
    return runBlocking {
      val settings = dataStore.data.first()
      settings.hasSeenBenchmarkComparisonHelp
    }
  }

  override fun addBenchmarkResult(result: BenchmarkResult) {
    runBlocking {
      benchmarkResultsDataStore.updateData { results ->
        results.toBuilder().addResult(0, result).build()
      }
    }
  }

  override fun getAllBenchmarkResults(): List<BenchmarkResult> {
    return runBlocking { benchmarkResultsDataStore.data.first().resultList }
  }

  override fun deleteBenchmarkResult(index: Int) {
    runBlocking {
      benchmarkResultsDataStore.updateData { results ->
        val newResults = results.toBuilder().removeResult(index).build()
        newResults
      }
    }
  }

  override fun addViewedPromoId(promoId: String) {
    runBlocking {
      dataStore.updateData { settings ->
        if (settings.viewedPromoIdList.contains(promoId)) {
          settings
        } else {
          settings.toBuilder().addViewedPromoId(promoId).build()
        }
      }
    }
  }

  override fun removeViewedPromoId(promoId: String) {
    runBlocking {
      dataStore.updateData { settings ->
        val newList = settings.viewedPromoIdList.filter { it != promoId }
        settings.toBuilder().clearViewedPromoId().addAllViewedPromoId(newList).build()
      }
    }
  }

  override fun hasViewedPromo(promoId: String): Boolean {
    return runBlocking {
      val settings = dataStore.data.first()
      settings.viewedPromoIdList.contains(promoId)
    }
  }

  companion object {
    private const val KEY_MOBILE_TOOL_TOKEN = "mobile_tool_token"
  }
}
