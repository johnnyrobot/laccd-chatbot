/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot

import androidx.datastore.core.CorruptionException
import androidx.datastore.core.Serializer
import com.laccd.chatbot.proto.Profile
import com.google.protobuf.InvalidProtocolBufferException
import java.io.InputStream
import java.io.OutputStream

object ProfileSerializer : Serializer<Profile> {
  override val defaultValue: Profile = Profile.getDefaultInstance()

  override suspend fun readFrom(input: InputStream): Profile {
    try {
      return Profile.parseFrom(input)
    } catch (exception: InvalidProtocolBufferException) {
      throw CorruptionException("Cannot read Profile proto.", exception)
    }
  }

  override suspend fun writeTo(t: Profile, output: OutputStream) = t.writeTo(output)
}
