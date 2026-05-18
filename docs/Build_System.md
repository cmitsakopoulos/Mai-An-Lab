# Build System: Serious Python & Android Deployment

This document details the build pipeline and mandatory Android configuration required to deploy Mai-An Lab.

## 1. Serious Python Pipeline

Mai-An Lab uses the **Serious Python** plugin to embed a CPython interpreter within the Flutter application.

### Packaging Flow
1. **Dependency Collection**; all Python requirements are listed in `requirements.txt` and collected by the build engine.
2. **Asset Bundling**; the `StreamripApp` source and its dependencies are zipped into a `app.zip` and placed in the Flutter `assets/` directory.
3. **Native Invocation**; at startup, the Flutter app extracts the Python environment and launches the `main.py` entry point.

### Binary Compatibility
- **ARM64 Only**; the project is strictly targeted at ARM64 Android devices. The build system (`flet build apk`) automatically bundles the necessary ARM64 binaries for dependencies like `numpy` during the packaging process.
- **DNS Handling**; the `aiodns` package is stripped from the mobile build as it relies on native resolvers that often conflict with Android's networking stack.

---

## 2. Mandatory Build Patches

Because Flet's default Android template is generic, several manual patches are applied to the `android/` directory during the build process to support background audio.

### AudioServiceFragmentActivity Patch
By default, Flet uses a vanilla `FlutterActivity`. To support the `audio_service` plugin, the `MainActivity.kt` (or the `AndroidManifest.xml` activity declaration) must be patched to use `AudioServiceFragmentActivity`.
- **Purpose**; ensures that the Flutter engine is correctly shared between the UI and the background service.
- **Location**; `android/app/src/main/kotlin/.../MainActivity.kt`.

### WindowSoftInputMode Patch
To ensure the layout remains responsive when the keyboard appears, the `AndroidManifest.xml` is configured to use `adjustResize`.
- **Purpose**; allows the Flutter view to shrink and scroll, keeping the search bar and input fields visible while the keyboard is active.
- **Location**; `android/AndroidManifest.xml`.

---

## 3. ProGuard & R8 Rules

To prevent the Android build system from "optimizing away" the Python binary or its related native libraries, specific rules are added to `proguard-rules.pro`:

```pro
# Keep Serious Python native methods
-keep class com.flet.serious_python.** { *; }

# Keep audio_service and background execution classes
-keep class com.ryanheise.audioservice.** { *; }
```

## 4. Permissions

The `AndroidManifest.xml` must include the following permissions for full functionality:

| Permission | Purpose | Android Version |
|------------|---------|-----------------|
| `INTERNET` | Streaming metadata and downloading media. | All |
| `READ_MEDIA_AUDIO` | Required for indexing and accessing local music files. | 13+ |
| `MANAGE_EXTERNAL_STORAGE` | Required for recursive indexing of music folders outside the app's sandbox (All Files Access). | 11+ |
| `FOREGROUND_SERVICE` | Required for persistent background playback. | All |
| `FOREGROUND_SERVICE_MEDIA_PLAYBACK` | Specific service type required for background media playback. | 14+ |
| `POST_NOTIFICATIONS` | Required to show playback controls in the notification shade. | 13+ |
| `WAKE_LOCK` | Prevents the CPU from sleeping during high-fidelity downloads. | All |
| `RECORD_AUDIO` | Mandatory permission required for the Jarvis Voice Assistant speech recognizer. | All |
| `READ_EXTERNAL_STORAGE` | Legacy filesystem access (superseded by `READ_MEDIA_AUDIO`). | < 13 |
| `WRITE_EXTERNAL_STORAGE` | Legacy filesystem write access for older devices. | < 10 |

---

## 5. Runtime Environment & Android Hacks

To ensure stability on modern Android (11+) and facilitate a clean CPython environment, the `main.py` entry point applies several runtime workarounds:

### SELinux & Memory Allocation
On Android 11+, the default Python memory allocator can trigger SELinux denials when attempting to map large memory regions.
- **Fix**; `os.environ["PYTHONMALLOC"] = "malloc"` is set before importing any high-level modules. This forces Python to use the system `malloc` which is permitted by the OS policy.

### Path & XDG Hijacking
Android's internal `FILES_DIR` is the only reliably writable location for the app.
- **Path Hijacking**; `pathlib.Path.home()` is monkey-patched at runtime to return the app's internal files directory. This prevents libraries from attempting to write to `/data` or other restricted paths.
- **XDG Variables**; `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` are explicitly set to subdirectories within the internal storage to ensure that `streamrip` and `mutagen` cache their data in valid locations.

### Logging Redirection
Serious Python redirects `sys.stdout` and `sys.stderr` to the Android **Logcat**.
- **Usage**; Python `logging` calls can be viewed in real-time using `adb logcat -s python`. This is the primary diagnostic tool for debugging backend logic on the device.

---

---

## 6. Automation & Fresh Rebuilds

To avoid build locks and ensure metadata consistency, the project uses automated build scripts (`build_android.sh` for MacOS/Linux and `build_android.ps1` for Windows).

### Fresh Rebuild Feature
Every execution of the build script performs a **thorough wipe** of the environment:
- **Process Cleanup**; any hung `java` or Gradle processes are forcefully terminated to release file locks on the Android NDK and SDK components.
- **Artifact Purge**; the `build/` directory and `.gradle/` cache are recursively deleted. This ensures that stale assets or old Python environment zips are not accidentally included in the new APK.
- **Clean Deployment**; the existing application is uninstalled from the connected device via `adb uninstall` before the new one is installed. This prevents signature mismatch errors common during development.

> [!TIP]
> Always use these scripts instead of raw `flet build` commands to ensure that local extensions like `flet_audio_service` are correctly resolved via `configure_paths.py`.
