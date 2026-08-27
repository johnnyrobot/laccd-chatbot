/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.ui.classes

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDownward
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Upload
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import com.laccd.chatbot.R
import com.laccd.chatbot.data.classes.ClassEntry
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private const val MAX_CLASS_IMPORT_BYTES = 2 * 1024 * 1024

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ClassesScreen(onBack: () -> Unit, viewModel: ClassesViewModel = hiltViewModel()) {
  val items by viewModel.items.collectAsState()
  val query by viewModel.query.collectAsState()
  val context = LocalContext.current
  val scope = rememberCoroutineScope()
  val snackbarHostState = remember { SnackbarHostState() }
  var editing by remember { mutableStateOf<ClassEntry?>(null) }
  var showAdd by remember { mutableStateOf(false) }
  var showOverflow by remember { mutableStateOf(false) }

  val exportLauncher =
    rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/json")) {
      uri ->
      if (uri == null) return@rememberLauncherForActivityResult
      scope.launch {
        runCatching {
            val payload = viewModel.exportJson()
            withContext(Dispatchers.IO) {
              context.contentResolver.openOutputStream(uri)?.use { it.write(payload.toByteArray()) }
            }
          }
          .onFailure { snackbarHostState.showSnackbar(it.message ?: "Export failed") }
          .onSuccess {
            snackbarHostState.showSnackbar(context.getString(R.string.laccd_classes_export_ok))
          }
      }
    }

  val importLauncher =
    rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
      if (uri == null) return@rememberLauncherForActivityResult
      scope.launch {
        val raw =
          runCatching {
              withContext(Dispatchers.IO) {
                context.contentResolver.openInputStream(uri)?.use { stream ->
                  val bytes = stream.readNBytes(MAX_CLASS_IMPORT_BYTES + 1)
                  if (bytes.size > MAX_CLASS_IMPORT_BYTES) null else bytes.toString(Charsets.UTF_8)
                }
              }
            }
            .getOrNull()
        if (raw.isNullOrBlank()) {
          snackbarHostState.showSnackbar(context.getString(R.string.laccd_classes_import_fail))
          return@launch
        }
        when (val result = viewModel.importJson(raw)) {
          is ClassesViewModel.ImportResult.Success ->
            snackbarHostState.showSnackbar(
              context.getString(R.string.laccd_classes_import_ok, result.count)
            )
          ClassesViewModel.ImportResult.Failure ->
            snackbarHostState.showSnackbar(context.getString(R.string.laccd_classes_import_fail))
        }
      }
    }

  Scaffold(
    topBar = {
      TopAppBar(
        title = { Text(stringResource(R.string.laccd_classes_title)) },
        navigationIcon = {
          IconButton(onClick = onBack) {
            Icon(
              Icons.AutoMirrored.Filled.ArrowBack,
              contentDescription = stringResource(R.string.cd_menu),
            )
          }
        },
        actions = {
          IconButton(onClick = { showOverflow = true }) {
            Icon(Icons.Default.MoreVert, contentDescription = stringResource(R.string.cd_menu))
          }
          DropdownMenu(expanded = showOverflow, onDismissRequest = { showOverflow = false }) {
            DropdownMenuItem(
              text = { Text(stringResource(R.string.laccd_classes_export)) },
              leadingIcon = { Icon(Icons.Default.Download, null) },
              onClick = {
                showOverflow = false
                exportLauncher.launch(defaultExportFilename())
              },
            )
            DropdownMenuItem(
              text = { Text(stringResource(R.string.laccd_classes_import)) },
              leadingIcon = { Icon(Icons.Default.Upload, null) },
              onClick = {
                showOverflow = false
                importLauncher.launch(arrayOf("application/json", "text/plain", "*/*"))
              },
            )
          }
        },
      )
    },
    floatingActionButton = {
      ExtendedFloatingActionButton(
        onClick = { showAdd = true },
        icon = { Icon(Icons.Default.Add, null) },
        text = { Text(stringResource(R.string.laccd_classes_add)) },
      )
    },
    snackbarHost = { SnackbarHost(snackbarHostState) },
  ) { padding ->
    Column(modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp)) {
      OutlinedTextField(
        value = query,
        onValueChange = viewModel::setQuery,
        leadingIcon = { Icon(Icons.Default.Search, null) },
        placeholder = { Text(stringResource(R.string.laccd_classes_search_hint)) },
        singleLine = true,
        modifier = Modifier.fillMaxWidth().padding(top = 12.dp, bottom = 8.dp),
      )

      if (items.isEmpty()) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
          val msg =
            if (query.isBlank()) R.string.laccd_classes_empty
            else R.string.laccd_classes_no_results
          Text(
            text = stringResource(msg),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
          )
        }
      } else {
        LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
          items(items, key = { it.id }) { entry ->
            ClassRow(
              entry = entry,
              onEdit = { editing = entry },
              onDelete = { viewModel.delete(entry) },
              onMoveUp = { viewModel.moveUp(entry.id) },
              onMoveDown = { viewModel.moveDown(entry.id) },
            )
          }
        }
      }
    }
  }

  if (showAdd) {
    ClassEditorDialog(
      initial = null,
      onDismiss = { showAdd = false },
      onSave = { name, section, daysTimes, location, notes ->
        viewModel.add(name, section, daysTimes, location, notes)
        showAdd = false
      },
    )
  }
  editing?.let { current ->
    ClassEditorDialog(
      initial = current,
      onDismiss = { editing = null },
      onSave = { name, section, daysTimes, location, notes ->
        viewModel.update(
          current.copy(
            name = name,
            section = section,
            daysTimes = daysTimes,
            location = location,
            notes = notes,
          )
        )
        editing = null
      },
    )
  }
}

@Composable
private fun ClassRow(
  entry: ClassEntry,
  onEdit: () -> Unit,
  onDelete: () -> Unit,
  onMoveUp: () -> Unit,
  onMoveDown: () -> Unit,
) {
  var confirmDelete by remember { mutableStateOf(false) }
  Card(
    modifier = Modifier.fillMaxWidth(),
    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
  ) {
    Column(modifier = Modifier.padding(14.dp)) {
      Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.weight(1f)) {
          Text(
            text = entry.name,
            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
          )
          val meta =
            buildList {
                if (entry.section.isNotBlank()) add(entry.section)
                if (entry.daysTimes.isNotBlank()) add(entry.daysTimes)
                if (entry.location.isNotBlank()) add(entry.location)
              }
              .joinToString(" · ")
          if (meta.isNotEmpty()) {
            Text(
              text = meta,
              style = MaterialTheme.typography.bodySmall,
              color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
          }
          if (entry.notes.isNotBlank()) {
            Text(
              text = entry.notes,
              style = MaterialTheme.typography.bodySmall,
              color = MaterialTheme.colorScheme.onSurfaceVariant,
              modifier = Modifier.padding(top = 4.dp),
            )
          }
        }
        Column {
          IconButton(onClick = onMoveUp) {
            Icon(
              Icons.Default.ArrowUpward,
              contentDescription = stringResource(R.string.laccd_classes_move_up),
            )
          }
          IconButton(onClick = onMoveDown) {
            Icon(
              Icons.Default.ArrowDownward,
              contentDescription = stringResource(R.string.laccd_classes_move_down),
            )
          }
        }
      }
      Row(horizontalArrangement = Arrangement.End, modifier = Modifier.fillMaxWidth()) {
        TextButton(onClick = onEdit) {
          Icon(Icons.Default.Edit, null, modifier = Modifier.padding(end = 4.dp))
          Text(stringResource(R.string.laccd_classes_edit))
        }
        TextButton(onClick = { confirmDelete = true }) {
          Icon(Icons.Default.Delete, null, modifier = Modifier.padding(end = 4.dp))
          Text(stringResource(R.string.laccd_classes_delete))
        }
      }
    }
  }
  if (confirmDelete) {
    AlertDialog(
      onDismissRequest = { confirmDelete = false },
      title = { Text(stringResource(R.string.laccd_classes_delete_title)) },
      text = { Text(stringResource(R.string.laccd_classes_delete_body, entry.name)) },
      confirmButton = {
        TextButton(
          onClick = {
            confirmDelete = false
            onDelete()
          }
        ) {
          Text(stringResource(R.string.laccd_classes_delete))
        }
      },
      dismissButton = {
        TextButton(onClick = { confirmDelete = false }) {
          Text(stringResource(R.string.laccd_classes_cancel))
        }
      },
    )
  }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ClassEditorDialog(
  initial: ClassEntry?,
  onDismiss: () -> Unit,
  onSave: (
    name: String,
    section: String,
    daysTimes: String,
    location: String,
    notes: String,
  ) -> Unit,
) {
  var name by remember { mutableStateOf(initial?.name ?: "") }
  var section by remember { mutableStateOf(initial?.section ?: "") }
  var daysTimes by remember { mutableStateOf(initial?.daysTimes ?: "") }
  var location by remember { mutableStateOf(initial?.location ?: "") }
  var notes by remember { mutableStateOf(initial?.notes ?: "") }

  AlertDialog(
    onDismissRequest = onDismiss,
    title = {
      Text(
        if (initial == null) stringResource(R.string.laccd_classes_dialog_add_title)
        else stringResource(R.string.laccd_classes_dialog_edit_title)
      )
    },
    text = {
      Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedTextField(
          value = name,
          onValueChange = { name = it.take(120) },
          label = { Text(stringResource(R.string.laccd_classes_field_name)) },
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
          value = section,
          onValueChange = { section = it.take(40) },
          label = { Text(stringResource(R.string.laccd_classes_field_section)) },
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
          value = daysTimes,
          onValueChange = { daysTimes = it.take(80) },
          label = { Text(stringResource(R.string.laccd_classes_field_days_times)) },
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
          value = location,
          onValueChange = { location = it.take(80) },
          label = { Text(stringResource(R.string.laccd_classes_field_location)) },
          singleLine = true,
          modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
          value = notes,
          onValueChange = { notes = it.take(500) },
          label = { Text(stringResource(R.string.laccd_classes_field_notes)) },
          modifier = Modifier.fillMaxWidth(),
        )
      }
    },
    confirmButton = {
      TextButton(
        onClick = { onSave(name, section, daysTimes, location, notes) },
        enabled = name.isNotBlank(),
      ) {
        Text(stringResource(R.string.laccd_classes_save))
      }
    },
    dismissButton = {
      TextButton(onClick = onDismiss) { Text(stringResource(R.string.laccd_classes_cancel)) }
    },
  )
}

private fun defaultExportFilename(): String {
  val today = LocalDate.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd"))
  return "laccd-class-planner-$today.json"
}
