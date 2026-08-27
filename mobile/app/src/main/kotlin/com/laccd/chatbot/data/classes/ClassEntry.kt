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

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey
import java.util.UUID

/**
 * local class planner row. Field shape mirrors Companero's
 * `student_planner.classes.v1` localStorage entry exactly so a JSON export from one side
 * imports cleanly into the other:
 *
 *   { id, name, section, daysTimes, location, notes }
 *
 * `orderIndex` is our DB-side equivalent of Companero's implicit array position. Importer
 * and exporter convert between the two.
 */
@Entity(tableName = "class_entries")
data class ClassEntry(
  @PrimaryKey val id: String = UUID.randomUUID().toString(),
  @ColumnInfo(name = "name") val name: String,
  @ColumnInfo(name = "section") val section: String = "",
  @ColumnInfo(name = "days_times") val daysTimes: String = "",
  @ColumnInfo(name = "location") val location: String = "",
  @ColumnInfo(name = "notes") val notes: String = "",
  @ColumnInfo(name = "order_index") val orderIndex: Int = 0,
)
