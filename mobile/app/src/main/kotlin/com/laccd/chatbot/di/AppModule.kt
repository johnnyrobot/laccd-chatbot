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

package com.laccd.chatbot.di

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.core.DataStoreFactory
import androidx.datastore.core.Serializer
import androidx.datastore.dataStoreFile
import androidx.room.Room
import com.laccd.chatbot.AppLifecycleProvider
import com.laccd.chatbot.BenchmarkResultsSerializer
import com.laccd.chatbot.GalleryLifecycleProvider
import com.laccd.chatbot.ProfileSerializer
import com.laccd.chatbot.SettingsSerializer
import com.laccd.chatbot.UserDataSerializer
import com.laccd.chatbot.data.DataStoreRepository
import com.laccd.chatbot.data.DefaultDataStoreRepository
import com.laccd.chatbot.data.EncryptedSecretStore
import com.laccd.chatbot.data.SecretStore
import com.laccd.chatbot.data.classes.ClassDao
import com.laccd.chatbot.data.classes.ClassDb
import com.laccd.chatbot.proto.BenchmarkResults
import com.laccd.chatbot.proto.Profile
import com.laccd.chatbot.proto.Settings
import com.laccd.chatbot.proto.UserData
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
internal object AppModule {

  // Provides the SettingsSerializer
  @Provides
  @Singleton
  fun provideSettingsSerializer(): Serializer<Settings> {
    return SettingsSerializer
  }

  @Provides
  @Singleton
  fun provideUserDataSerializer(): Serializer<UserData> = UserDataSerializer

  // Provides the BenchmarkResultsSerializer
  @Provides
  @Singleton
  fun provideBenchmarkResultsSerializer(): Serializer<BenchmarkResults> {
    return BenchmarkResultsSerializer
  }

  // Provides DataStore<Settings>
  @Provides
  @Singleton
  fun provideSettingsDataStore(
    @ApplicationContext context: Context,
    settingsSerializer: Serializer<Settings>,
  ): DataStore<Settings> {
    return DataStoreFactory.create(
      serializer = settingsSerializer,
      produceFile = { context.dataStoreFile("settings.pb") },
    )
  }

  @Provides
  @Singleton
  fun provideUserDataDataStore(
    @ApplicationContext context: Context,
    userDataSerializer: Serializer<UserData>,
  ): DataStore<UserData> =
    DataStoreFactory.create(
      serializer = userDataSerializer,
      produceFile = { context.dataStoreFile("user_data.pb") },
    )

  // Provides DataStore<BenchmarkResults>
  @Provides
  @Singleton
  fun provideBenchmarkResultsDataStore(
    @ApplicationContext context: Context,
    benchmarkResultsSerializer: Serializer<BenchmarkResults>,
  ): DataStore<BenchmarkResults> {
    return DataStoreFactory.create(
      serializer = benchmarkResultsSerializer,
      produceFile = { context.dataStoreFile("benchmark_results.pb") },
    )
  }

  // Provides AppLifecycleProvider
  @Provides
  @Singleton
  fun provideAppLifecycleProvider(): AppLifecycleProvider {
    return GalleryLifecycleProvider()
  }

  // Provides DataStoreRepository
  @Provides
  @Singleton
  fun provideSecretStore(@ApplicationContext context: Context): SecretStore {
    return EncryptedSecretStore(context)
  }

  @Provides
  @Singleton
  fun provideDataStoreRepository(
    dataStore: DataStore<Settings>,
    benchmarkResultsStore: DataStore<BenchmarkResults>,
    secretStore: SecretStore,
  ): DataStoreRepository {
    return DefaultDataStoreRepository(
      dataStore,
      benchmarkResultsStore,
      secretStore,
    )
  }

  // Profile DataStore.
  @Provides
  @Singleton
  fun provideProfileSerializer(): Serializer<Profile> = ProfileSerializer

  @Provides
  @Singleton
  fun provideProfileDataStore(
    @ApplicationContext context: Context,
    profileSerializer: Serializer<Profile>,
  ): DataStore<Profile> =
    DataStoreFactory.create(
      serializer = profileSerializer,
      produceFile = { context.dataStoreFile("profile.pb") },
    )

  // Room class planner.
  @Provides
  @Singleton
  fun provideClassDb(@ApplicationContext context: Context): ClassDb =
    Room.databaseBuilder(context, ClassDb::class.java, ClassDb.NAME).build()

  @Provides
  fun provideClassDao(db: ClassDb): ClassDao = db.classDao()
}
