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

import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Update
import kotlinx.coroutines.flow.Flow

@Dao
abstract class ClassDao {

  @Query("SELECT * FROM class_entries ORDER BY order_index ASC, name COLLATE NOCASE ASC")
  abstract fun observeAll(): Flow<List<ClassEntry>>

  @Query("SELECT * FROM class_entries ORDER BY order_index ASC, name COLLATE NOCASE ASC")
  abstract suspend fun listAll(): List<ClassEntry>

  @Query("SELECT * FROM class_entries WHERE id = :id LIMIT 1")
  abstract suspend fun findById(id: String): ClassEntry?

  @Query("SELECT COALESCE(MAX(order_index), -1) FROM class_entries")
  abstract suspend fun maxOrderIndex(): Int

  @Insert(onConflict = OnConflictStrategy.REPLACE) abstract suspend fun upsert(entry: ClassEntry)

  @Insert(onConflict = OnConflictStrategy.REPLACE)
  abstract suspend fun upsertAll(entries: List<ClassEntry>)

  @Update abstract suspend fun update(entry: ClassEntry)

  @Delete abstract suspend fun delete(entry: ClassEntry)

  @Query("DELETE FROM class_entries") abstract suspend fun clearAll()
}
