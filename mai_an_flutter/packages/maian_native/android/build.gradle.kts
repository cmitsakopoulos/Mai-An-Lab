// No sources of its own: this compiles the Flet extension's Kotlin, resources
// and manifest where they live, so the shipping app and the port can never
// drift apart. When the Flet app is retired (PORT_MAP §5), move that
// directory here and drop the path.
//
// The Flet extension's own build.gradle also patches the Flet-generated app
// (MainActivity, libc++ dedupe). None of that applies here: the port's
// MainActivity is written by hand, and the port bundles no CPython/numpy.
group = "com.flet.flet_audio_service"
version = "1.0"

plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

// Relative to this directory (packages/maian_native/android). The Kotlin comes
// in through src/main/kotlin, a symlink to the shared directory: the Flutter
// tool looks for the plugin class there, and the Kotlin Gradle plugin compiles
// that directory by default. Resources and the manifest are read in place.
val shared = file(
    "../../../../flet_audio_service/src/flutter/flet_audio_service/android/src/main"
)
check(shared.isDirectory) { "Shared Kotlin sources not found at $shared" }

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

    sourceSets {
        getByName("main") {
            res.setSrcDirs(listOf(File(shared, "res")))
            manifest.srcFile(File(shared, "AndroidManifest.xml"))
            // Not jniLibs: those libc++ copies exist for the Flet build's numpy.
        }
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
