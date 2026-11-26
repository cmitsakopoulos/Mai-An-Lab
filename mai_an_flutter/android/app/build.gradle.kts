plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.mitsakopoulos.maianlab.mai_an_lab"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_17.toString()
    }

    defaultConfig {
        // Same base ID as the shipping Flet app, on purpose: at cutover the
        // Flutter build installs as an in-place UPDATE of it (same ID, same
        // signing key, higher versionCode) and inherits its data dir, library.db
        // included. See docs/PORT_MAP.md, "Cutover".
        applicationId = "com.mitsakopoulos.maianlab.mai_an_lab"
        // Until cutover, the port is a SEPARATE app. Without this suffix,
        // `flutter run` would install over the Flet app (both are signed with
        // ~/.android/debug.keystore at versionCode 1), or uninstall it and
        // wipe its data on a signature/version conflict.
        applicationIdSuffix = ".port"
        resValue("string", "app_name", "Mai An (port)")
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

flutter {
    source = "../.."
}
