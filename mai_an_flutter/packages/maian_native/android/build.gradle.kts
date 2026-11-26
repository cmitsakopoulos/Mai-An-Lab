// The native half of the player: Kotlin, resources and manifest under
// src/main (moved here from the Flet app's flet_audio_service extension).
group = "com.flet.flet_audio_service"
version = "1.0"

plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

android {
    // Kept: the Kotlin imports com.flet.flet_audio_service.R.
    namespace = "com.flet.flet_audio_service"
    compileSdk = 36

    defaultConfig {
        minSdk = 21
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("androidx.core:core:1.13.1")
}
