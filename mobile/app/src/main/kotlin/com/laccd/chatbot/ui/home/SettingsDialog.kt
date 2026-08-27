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

package com.laccd.chatbot.ui.home

import com.google.android.gms.oss.licenses.OssLicensesMenuActivity
import android.app.UiModeManager
import android.content.Context
import android.content.Intent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MultiChoiceSegmentedButtonRow
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.laccd.chatbot.BuildConfig
import com.laccd.chatbot.R
import com.laccd.chatbot.data.campus.Campus
import com.laccd.chatbot.proto.LanguagePreference
import com.laccd.chatbot.proto.Theme
import com.laccd.chatbot.ui.common.ClickableLink
import com.laccd.chatbot.ui.common.LocalCampus
import com.laccd.chatbot.ui.common.tos.AppTosDialog
import com.laccd.chatbot.ui.modelmanager.ModelManagerViewModel
import com.laccd.chatbot.ui.theme.ThemeSettings
import com.laccd.chatbot.ui.theme.labelSmallNarrow

private val THEME_OPTIONS = listOf(Theme.THEME_AUTO, Theme.THEME_LIGHT, Theme.THEME_DARK)

// language toggle options. AUTO follows the system locale; the others force
// the matching values-{en,es}/strings.xml.
private val LANGUAGE_OPTIONS =
  listOf(
    LanguagePreference.LANGUAGE_AUTO,
    LanguagePreference.LANGUAGE_EN,
    LanguagePreference.LANGUAGE_ES,
  )

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsDialog(
  curThemeOverride: Theme,
  modelManagerViewModel: ModelManagerViewModel,
  onDismissed: () -> Unit,
  // when set, a "Switch campus" row is rendered. The caller is responsible
  // for navigating to the picker (the dialog dismisses itself first).
  onSwitchCampus: (() -> Unit)? = null,
) {
  var selectedTheme by remember { mutableStateOf(curThemeOverride) }
  var selectedLanguage by remember {
    mutableStateOf(modelManagerViewModel.readLanguageOverride())
  }
  val interactionSource = remember { MutableInteractionSource() }
  var showTos by remember { mutableStateOf(false) }
  var showNunitoLicense by remember { mutableStateOf(false) }
  // runtime cloud-tool bearer token state.
  var mobileToolTokenInput by remember { mutableStateOf("") }
  var mobileToolTokenSaved by remember {
    mutableStateOf(modelManagerViewModel.readMobileToolToken() != null)
  }

  Dialog(onDismissRequest = onDismissed) {
    val focusManager = LocalFocusManager.current
    Card(
      modifier =
        Modifier.fillMaxWidth().clickable(
          interactionSource = interactionSource,
          indication = null, // Disable the ripple effect
        ) {
          focusManager.clearFocus()
        },
      shape = RoundedCornerShape(16.dp),
    ) {
      Column(
        modifier = Modifier.padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
      ) {
        // Dialog title and subtitle.
        Column {
          Text(
            "Settings",
            style = MaterialTheme.typography.titleLarge,
            modifier = Modifier.padding(bottom = 8.dp),
          )
          // Subtitle.
          Text(
            "App version: ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
            style = labelSmallNarrow,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.offset(y = (-6).dp),
          )
        }

        Column(
          modifier = Modifier.verticalScroll(rememberScrollState()).weight(1f, fill = false),
          verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
          val context = LocalContext.current
          // Theme switcher.
          Column(modifier = Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}) {
            Text(
              "Theme",
              style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Medium),
            )
            MultiChoiceSegmentedButtonRow {
              THEME_OPTIONS.forEachIndexed { index, theme ->
                SegmentedButton(
                  shape =
                    SegmentedButtonDefaults.itemShape(index = index, count = THEME_OPTIONS.size),
                  onCheckedChange = {
                    selectedTheme = theme

                    // Update theme settings.
                    // This will update app's theme.
                    ThemeSettings.themeOverride.value = theme

                    // Save to data store.
                    modelManagerViewModel.saveThemeOverride(theme)

                    // Update ui mode.
                    //
                    // This is necessary to make other Activities launched from MainActivity to have
                    // the correct theme.
                    val uiModeManager =
                      context.applicationContext.getSystemService(Context.UI_MODE_SERVICE)
                        as UiModeManager
                    if (theme == Theme.THEME_AUTO) {
                      uiModeManager.setApplicationNightMode(UiModeManager.MODE_NIGHT_AUTO)
                    } else if (theme == Theme.THEME_LIGHT) {
                      uiModeManager.setApplicationNightMode(UiModeManager.MODE_NIGHT_NO)
                    } else {
                      uiModeManager.setApplicationNightMode(UiModeManager.MODE_NIGHT_YES)
                    }
                  },
                  checked = theme == selectedTheme,
                  label = { Text(themeLabel(theme)) },
                )
              }
            }
          }

          // Campus switcher. Shows the active campus's nickname + brand
          // primary as a swatch, plus a button to relaunch the first-run picker.
          if (onSwitchCampus != null) {
            val activeCampus: Campus = LocalCampus.current
            Column(modifier = Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}) {
              Text(
                stringResource(R.string.laccd_settings_campus_title),
                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Medium),
              )
              Row(
                modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp),
              ) {
                Box(
                  modifier =
                    Modifier.height(28.dp)
                      .padding(end = 4.dp)
                      .background(
                        color = activeCampus.primaryColor,
                        shape = androidx.compose.foundation.shape.RoundedCornerShape(8.dp),
                      ),
                ) {
                  Text(
                    text = activeCampus.code,
                    color = androidx.compose.ui.graphics.Color.White,
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
                  )
                }
                Text(
                  text = activeCampus.shortName,
                  style = MaterialTheme.typography.bodyMedium,
                  modifier = Modifier.weight(1f),
                )
                OutlinedButton(
                  onClick = {
                    onDismissed()
                    onSwitchCampus()
                  }
                ) {
                  Text(stringResource(R.string.laccd_settings_campus_switch))
                }
              }
            }
          }

          // Language switcher.
          Column(modifier = Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}) {
            Text(
              stringResource(R.string.laccd_settings_language_title),
              style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Medium),
            )
            MultiChoiceSegmentedButtonRow {
              LANGUAGE_OPTIONS.forEachIndexed { index, language ->
                SegmentedButton(
                  shape =
                    SegmentedButtonDefaults.itemShape(
                      index = index,
                      count = LANGUAGE_OPTIONS.size,
                    ),
                  onCheckedChange = {
                    selectedLanguage = language
                    modelManagerViewModel.saveLanguageOverride(language)
                  },
                  checked = language == selectedLanguage,
                  label = { Text(languageLabel(language)) },
                )
              }
            }
          }

          // Cloud tools access token. Authenticates the mobile
          // /api/tools/* calls (live schedule, ASSIST, programs, sequence,
          // eLumen). Stored in the encrypted secret store; read back on each
          // request by MobileToolTokenProvider. Until set, those cloud tools
          // fail closed and the app stays on on-device data only.
          Column(
            modifier = Modifier.fillMaxWidth().semantics(mergeDescendants = true) {},
            verticalArrangement = Arrangement.spacedBy(4.dp),
          ) {
            Text(
              "Cloud tools access token",
              style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Medium),
            )
            Text(
              if (mobileToolTokenSaved) {
                "A token is saved on this device."
              } else {
                "Not set — cloud tools (schedule, ASSIST, programs) stay offline until a token is entered."
              },
              style = MaterialTheme.typography.bodySmall,
              color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            val saveMobileToolToken = {
              modelManagerViewModel.saveMobileToolToken(mobileToolTokenInput)
              mobileToolTokenSaved = modelManagerViewModel.readMobileToolToken() != null
              mobileToolTokenInput = ""
              focusManager.clearFocus()
            }
            OutlinedTextField(
              value = mobileToolTokenInput,
              onValueChange = { mobileToolTokenInput = it },
              singleLine = true,
              label = { Text("Enter token") },
              visualTransformation = PasswordVisualTransformation(),
              keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
              keyboardActions = KeyboardActions(onDone = { saveMobileToolToken() }),
              modifier = Modifier.fillMaxWidth(),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
              OutlinedButton(
                onClick = {
                  modelManagerViewModel.clearMobileToolToken()
                  mobileToolTokenSaved = false
                  mobileToolTokenInput = ""
                  focusManager.clearFocus()
                },
                enabled = mobileToolTokenSaved,
              ) {
                Text("Clear")
              }
              Button(
                onClick = { saveMobileToolToken() },
                enabled = mobileToolTokenInput.isNotBlank(),
              ) {
                Text("Save")
              }
            }
          }

          // Third party licenses.
          Column(modifier = Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}) {
            Text(
              "Third-party libraries",
              style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Medium),
            )
            OutlinedButton(
              onClick = {
                // Create an Intent to launch a license viewer that displays a list of
                // third-party library names. Clicking a name will show its license content.
                val intent = Intent(context, OssLicensesMenuActivity::class.java)
                context.startActivity(intent)
              }
            ) {
              Text("View licenses")
            }
            OutlinedButton(onClick = { showNunitoLicense = true }) {
              Text("View Nunito font license")
            }
          }

          // Tos
          Column(modifier = Modifier.fillMaxWidth().semantics(mergeDescendants = true) {}) {
            Text(
              stringResource(R.string.settings_dialog_tos_title),
              style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Medium),
            )
            OutlinedButton(onClick = { showTos = true }) {
              Text(stringResource(R.string.settings_dialog_view_app_terms_of_service))
            }
            ClickableLink(
              url = "https://ai.google.dev/gemma/terms",
              linkText = stringResource(R.string.tos_dialog_title_gemma),
              modifier = Modifier.padding(top = 4.dp),
            )
            ClickableLink(
              url = "https://ai.google.dev/gemma/prohibited_use_policy",
              linkText = stringResource(R.string.settings_dialog_gemma_prohibited_use_policy),
              modifier = Modifier.padding(top = 8.dp),
            )
          }
        }

        // Button row.
        Row(
          modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
          horizontalArrangement = Arrangement.End,
        ) {
          // Close button
          Button(onClick = { onDismissed() }) { Text("Close") }
        }
      }
    }
  }

  if (showTos) {
    AppTosDialog(onTosAccepted = { showTos = false }, viewingMode = true)
  }
  if (showNunitoLicense) {
    val context = LocalContext.current
    val licenseText = remember {
      context.resources.openRawResource(R.raw.nunito_ofl).bufferedReader().use { it.readText() }
    }
    AlertDialog(
      onDismissRequest = { showNunitoLicense = false },
      title = { Text("Nunito font license") },
      text = {
        Text(
          licenseText,
          modifier = Modifier.verticalScroll(rememberScrollState()),
          style = MaterialTheme.typography.bodySmall,
        )
      },
      confirmButton = {
        TextButton(onClick = { showNunitoLicense = false }) { Text("Close") }
      },
    )
  }
}

private fun themeLabel(theme: Theme): String {
  return when (theme) {
    Theme.THEME_AUTO -> "Auto"
    Theme.THEME_LIGHT -> "Light"
    Theme.THEME_DARK -> "Dark"
    else -> "Unknown"
  }
}

@Composable
private fun languageLabel(language: LanguagePreference): String {
  val resId =
    when (language) {
      LanguagePreference.LANGUAGE_EN -> R.string.laccd_settings_language_en
      LanguagePreference.LANGUAGE_ES -> R.string.laccd_settings_language_es
      else -> R.string.laccd_settings_language_auto
    }
  return stringResource(resId)
}
