/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.io.IOException
import java.security.GeneralSecurityException

interface SecretStore {
  fun putString(key: String, value: String)
  fun getString(key: String): String?
  fun remove(key: String)
}

class EncryptedSecretStore(context: Context) : SecretStore {
  // L7: build the encrypted prefs defensively. A known OEM Android Keystore
  // invalidation bug can leave the master key unable to decrypt the stored Tink
  // keyset, so EncryptedSharedPreferences.create() throws on launch. Rather than
  // crashing the app in a field initializer with no recovery, [createEncryptedPrefs]
  // self-heals by dropping the corrupted prefs file and rebuilding once.
  private val prefs = createEncryptedPrefs(context)

  override fun putString(key: String, value: String) {
    prefs.edit().putString(key, value).apply()
  }

  override fun getString(key: String): String? = prefs.getString(key, null)

  override fun remove(key: String) {
    prefs.edit().remove(key).apply()
  }

  companion object {
    const val SECRET_PREFS_NAME = "laccd_encrypted_secrets"
    private const val TAG = "EncryptedSecretStore"

    private fun buildMasterKey(context: Context): MasterKey =
      MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private fun buildPrefs(context: Context): SharedPreferences =
      EncryptedSharedPreferences.create(
        context,
        SECRET_PREFS_NAME,
        buildMasterKey(context),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
      )

    /**
     * Builds the encrypted prefs, self-healing from the known keystore-invalidation
     * crash. If the master key can no longer decrypt the stored keyset,
     * [EncryptedSharedPreferences.create] throws a [GeneralSecurityException] /
     * [IOException]. We delete the corrupted prefs file (which holds the keyset)
     * and rebuild once, generating a fresh keyset under the current master key.
     *
     * Any previously stored secrets are lost (they were unrecoverable anyway),
     * but confidentiality is preserved — there is deliberately **no plaintext
     * fallback**. If the rebuild also fails we rethrow rather than degrade.
     */
    private fun createEncryptedPrefs(context: Context): SharedPreferences =
      try {
        buildPrefs(context)
      } catch (e: GeneralSecurityException) {
        recreatePrefs(context, e)
      } catch (e: IOException) {
        recreatePrefs(context, e)
      }

    private fun recreatePrefs(context: Context, cause: Exception): SharedPreferences {
      Log.w(TAG, "Encrypted secret store unreadable; recreating it (stored secrets cleared).", cause)
      runCatching { context.deleteSharedPreferences(SECRET_PREFS_NAME) }
      return buildPrefs(context)
    }
  }
}
