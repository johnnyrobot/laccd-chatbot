plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
    alias(libs.plugins.protobuf)
    alias(libs.plugins.oss.licenses)
}

val releaseKeystorePath = providers.environmentVariable("LACCD_RELEASE_KEYSTORE").orNull
val releaseKeystorePassword = providers.environmentVariable("LACCD_RELEASE_STORE_PASSWORD").orNull
val releaseKeyAlias = providers.environmentVariable("LACCD_RELEASE_KEY_ALIAS").orNull
val releaseKeyPassword = providers.environmentVariable("LACCD_RELEASE_KEY_PASSWORD").orNull
val releaseSigningConfigured =
    listOf(
        releaseKeystorePath,
        releaseKeystorePassword,
        releaseKeyAlias,
        releaseKeyPassword,
    ).all { !it.isNullOrBlank() }

android {
    namespace = "com.laccd.chatbot"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.laccd.chatbot"
        minSdk = 31
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        manifestPlaceholders["applicationName"] = "com.laccd.chatbot.GalleryApplication"
        manifestPlaceholders["appIcon"] = "@mipmap/ic_launcher"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        vectorDrawables { useSupportLibrary = true }

        // cloud REST tool API base URL. Auth uses a runtime token
        // provider; release builds must not bake mobile tool secrets into
        // BuildConfig.
        val toolBaseUrl =
            (project.findProperty("mobile.tool.base.url") as String?)
                ?: "https://lamc-chat.johnnyrobot.ai/"
        val legacyToolKey = (project.findProperty("mobile.tool.key") as String?)?.trim().orEmpty()
        require(
            legacyToolKey.isEmpty() ||
                gradle.startParameter.taskNames.none { it.contains("Release", ignoreCase = true) },
        ) {
            "mobile.tool.key is no longer supported for release builds. Provision the runtime token after install."
        }
        if (legacyToolKey.isNotEmpty()) {
            logger.warn("Ignoring legacy -Pmobile.tool.key; cloud tools now require a runtime bearer token.")
        }
        buildConfigField("String", "MOBILE_TOOL_BASE_URL", "\"$toolBaseUrl\"")

        // E2E mode is intentionally explicit:
        // - smoke: allows the debug-only low-memory emulator smoke runtime and recorded fixtures.
        // - real: requires the real LiteRT-LM/backend path; recorded fixtures are rejected.
        val e2eMode =
            ((project.findProperty("mobile.e2e.mode") as String?) ?: "real").trim().lowercase()
        require(e2eMode == "smoke" || e2eMode == "real") {
            "Unsupported -Pmobile.e2e.mode=$e2eMode. Use smoke or real."
        }
        // Smoke mode relies on BuildConfig.DEBUG (see MobileE2eMode.isSmoke); forbid it on
        // release builds so we never produce an undefined runtime state.
        require(
            e2eMode != "smoke" ||
                gradle.startParameter.taskNames.none { it.contains("Release", ignoreCase = true) },
        ) {
            "Smoke mode (-Pmobile.e2e.mode=smoke) is only allowed in debug builds."
        }
        val requestedRecordedFixtures =
            (project.findProperty("mobile.e2e.recorded.fixtures") as String?)
                ?.trim()
                ?.lowercase()
                ?.toBooleanStrictOrNull()
        val useRecordedFixtures = requestedRecordedFixtures ?: (e2eMode == "smoke")
        require(e2eMode == "smoke" || !useRecordedFixtures) {
            "Recorded E2E fixtures are only allowed with -Pmobile.e2e.mode=smoke."
        }
        buildConfigField("String", "MOBILE_E2E_MODE", "\"$e2eMode\"")
        buildConfigField("boolean", "MOBILE_E2E_USE_RECORDED_FIXTURES", useRecordedFixtures.toString())

    }

    signingConfigs {
        if (releaseSigningConfigured) {
            create("release") {
                storeFile = file(releaseKeystorePath!!)
                storePassword = releaseKeystorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (releaseSigningConfigured) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.process)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons.extended)
    implementation(libs.androidx.compose.navigation)
    debugImplementation(libs.androidx.compose.ui.tooling)

    implementation(libs.kotlin.reflect)
    implementation(libs.kotlinx.serialization.json)

    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.androidx.hilt.navigation.compose)

    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)

    implementation(libs.androidx.datastore)

    implementation(libs.androidx.work.runtime)
    implementation(libs.androidx.splashscreen)
    implementation(libs.androidx.exifinterface)
    implementation(libs.androidx.security.crypto)

    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)

    implementation(libs.protobuf.javalite)
    implementation(libs.play.services.oss.licenses)

    implementation(libs.richtext.commonmark)
    implementation(libs.richtext.ui.material3)

    implementation(libs.moshi.kotlin)
    ksp(libs.moshi.kotlin.codegen)

    implementation(libs.litertlm.android)
    implementation(libs.cozo.android)

    // cloud REST tool client (LaccdApi.kt).
    implementation(libs.retrofit)
    implementation(libs.retrofit.converter.moshi)
    implementation(libs.okhttp)

    // on-device USE Lite query embedder (Embedder.kt).
    implementation(libs.mediapipe.tasks.text)

    // JVM unit tests for ToolCallParser, ToolRouter, GraphQueries,
    // and the hash-fallback Embedder. These don't need an Android device.
    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.serialization.json)
    testImplementation(libs.moshi.kotlin)
    testImplementation(libs.okhttp.mockwebserver)
    // The Android `org.json` is stubbed at unit-test time (every method throws);
    // pull in the real library so JSONObject / JSONArray actually parse.
    testImplementation(libs.json.org)
}

protobuf {
    protoc { artifact = "com.google.protobuf:protoc:4.26.1" }
    generateProtoTasks {
        all().forEach { it.plugins { create("java") { option("lite") } } }
    }
}

gradle.taskGraph.whenReady {
    val requestedRelease = allTasks.any { it.name.contains("Release", ignoreCase = true) }
    require(!requestedRelease || releaseSigningConfigured) {
        "Release signing is not configured. Set LACCD_RELEASE_KEYSTORE, " +
            "LACCD_RELEASE_STORE_PASSWORD, LACCD_RELEASE_KEY_ALIAS, and " +
            "LACCD_RELEASE_KEY_PASSWORD. Debug signing is never used for release artifacts."
    }
}
