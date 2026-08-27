/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data.classes

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(entities = [ClassEntry::class], version = 1, exportSchema = false)
abstract class ClassDb : RoomDatabase() {
  abstract fun classDao(): ClassDao

  companion object {
    const val NAME = "laccd_classes.db"
  }
}
