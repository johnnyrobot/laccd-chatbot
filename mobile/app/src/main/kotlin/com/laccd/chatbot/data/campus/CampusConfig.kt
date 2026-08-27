/*
 * Copyright 2026 LACCD Chatbot contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package com.laccd.chatbot.data.campus

import androidx.compose.ui.graphics.Color

/**
 * full mirror of `shared/campus_config.py`. The 9 LACCD campuses, each with the
 * brand seed colors that drive Material 3 theming (`primaryColor` is the canonical seed),
 * the website + voice agent name we surface in the on-device system prompt, and a `nickname`
 * used for the first-run picker monogram grid.
 *
 * Keep this in lockstep with the Python source of truth. A unit test in CampusColorSchemeTest
 * verifies that every campus produces a non-throwing light + dark scheme.
 */
enum class Campus(
  val code: String,
  val displayName: String,
  val shortName: String,
  val nickname: String,
  val primaryColor: Color,
  val secondaryColor: Color,
  val accentColor: Color,
  val accentHover: Color,
  val website: String,
  val subdomain: String,
  val phoneNumber: String,
  val lkAgentName: String,
) {
  ELAC(
    code = "ELAC",
    displayName = "East Los Angeles College",
    shortName = "East LA College",
    nickname = "East LA",
    primaryColor = Color(0xFF4A7729),
    secondaryColor = Color(0xFF32511C),
    accentColor = Color(0xFF32511C),
    accentHover = Color(0xFF274511),
    website = "www.elac.edu",
    subdomain = "elac-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "elac-assistant",
  ),
  LACC(
    code = "LACC",
    displayName = "Los Angeles City College",
    shortName = "LA City College",
    nickname = "City",
    primaryColor = Color(0xFF305589),
    secondaryColor = Color(0xFF122744),
    accentColor = Color(0xFFAE3539),
    accentHover = Color(0xFF90292C),
    website = "www.lacitycollege.edu",
    subdomain = "lacc-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "lacc-assistant",
  ),
  LAHC(
    code = "LAHC",
    displayName = "Los Angeles Harbor College",
    shortName = "LA Harbor College",
    nickname = "Harbor",
    primaryColor = Color(0xFF001B71),
    secondaryColor = Color(0xFFFFC52E),
    accentColor = Color(0xFF0444BC),
    accentHover = Color(0xFF003290),
    website = "www.lahc.edu",
    subdomain = "lahc-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "lahc-assistant",
  ),
  LAMC(
    code = "LAMC",
    displayName = "Los Angeles Mission College",
    shortName = "LA Mission College",
    nickname = "Mission",
    primaryColor = Color(0xFF00538A),
    secondaryColor = Color(0xFF004070),
    accentColor = Color(0xFF00538A),
    accentHover = Color(0xFF004070),
    website = "www.lamission.edu",
    subdomain = "lamc-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "lamc-assistant",
  ),
  LAPC(
    code = "LAPC",
    displayName = "Los Angeles Pierce College",
    shortName = "Pierce College",
    nickname = "Pierce",
    primaryColor = Color(0xFF090909),
    secondaryColor = Color(0xFF1F1F1F),
    accentColor = Color(0xFFBF2117),
    accentHover = Color(0xFF880F07),
    website = "www.piercecollege.edu",
    subdomain = "lapc-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "lapc-assistant",
  ),
  LASC(
    code = "LASC",
    displayName = "Los Angeles Southwest College",
    shortName = "LA Southwest College",
    nickname = "Southwest",
    primaryColor = Color(0xFF002856),
    secondaryColor = Color(0xFF00A0DD),
    accentColor = Color(0xFF002856),
    accentHover = Color(0xFF001B3A),
    website = "www.lasc.edu",
    subdomain = "lasc-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "lasc-assistant",
  ),
  LATTC(
    code = "LATTC",
    displayName = "Los Angeles Trade-Technical College",
    shortName = "LA Trade-Tech College",
    nickname = "Trade-Tech",
    primaryColor = Color(0xFF552285),
    secondaryColor = Color(0xFFFFB800),
    accentColor = Color(0xFF3A3458),
    accentHover = Color(0xFF32237A),
    website = "www.lattc.edu",
    subdomain = "lattc-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "lattc-assistant",
  ),
  LAVC(
    code = "LAVC",
    displayName = "Los Angeles Valley College",
    shortName = "LA Valley College",
    nickname = "Valley",
    primaryColor = Color(0xFF2F3F3A),
    secondaryColor = Color(0xFF00513F),
    accentColor = Color(0xFF114E3E),
    accentHover = Color(0xFF2F3F3A),
    website = "www.lavc.edu",
    subdomain = "lavc-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "lavc-assistant",
  ),
  WLAC(
    code = "WLAC",
    displayName = "West Los Angeles College",
    shortName = "West LA College",
    nickname = "West LA",
    primaryColor = Color(0xFF00347B),
    secondaryColor = Color(0xFF1B3667),
    accentColor = Color(0xFF00347B),
    accentHover = Color(0xFF1B3667),
    website = "www.wlac.edu",
    subdomain = "wlac-chat",
    phoneNumber = "+18182145505",
    lkAgentName = "wlac-assistant",
  );

  companion object {
    val DEFAULT: Campus = LAMC

    fun byCode(code: String): Campus =
      entries.firstOrNull { it.code.equals(code, ignoreCase = true) } ?: DEFAULT

    /** Returns the campus matching `code`, or null if `code` is blank/unknown. */
    fun byCodeOrNull(code: String): Campus? =
      if (code.isBlank()) null
      else entries.firstOrNull { it.code.equals(code, ignoreCase = true) }
  }
}
