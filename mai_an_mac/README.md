# maian: Mai An Companion for the Mac

Pure Dart, no Flutter/Xcode/Python. Pairs the phone, runs DSP offload, moves
files both ways, and ships new builds of the phone app to the phone. Speaks
the same Companion protocol as the Flet app.

    tool/make_app.sh --install   # build "Mai An Companion.app" into ~/Applications

    dart compile exe cli/maian.dart -o build/maian
    ./build/maian                # pair, offload, receive; opens the control page
    ./build/maian send A.flac B.flac   # queue files for the phone
    ./build/maian publish app.apk      # offer a phone app build
    ./build/maian status
    ./build/maian quit

Needs `ffmpeg` (brew install ffmpeg) for offload and AAC conversion.
Files live in `~/Library/Application Support/MaiAn` (`MAIAN_HOME` to move);
tracks from the phone go to `~/Music/MaiAn` (changeable on the page). The
app runs in the background; its log is `~/Library/Logs/MaiAn/companion.log`.

## Phone app updates

1. On the control page, **Phone app → Build & offer to phone**
   runs `flutter build apk --release` (64-bit ARM only by default; optionally
   raising pubspec's `+N` first) with your login shell's environment, shows
   the output, and offers the APK when it's done. Or build in a terminal:
   `flutter build apk --release` in `mai_an_flutter`. The app watches
   `build/app/outputs/flutter-apk/app-release.apk` there and offers each new
   build (never an older versionCode). Or drop any APK on the control page,
   or `maian publish`.
2. The phone sees the offer in its next heartbeat (15 s while Companion Sync
   is on), asks, downloads, checks the SHA-256 and hands it to Android's
   installer. Settings → Companion & Sync → App Updates shows it too.
3. The control page follows it: Offered → Downloaded → Installing → Installed.

Android only installs a build signed with the same key as the installed app
(here `~/.android/debug.keystore`), and only after the user confirms. A
same-version rebuild is offered too (the phone compares hashes), so bumping
`version:` is optional during development. The phone needs a build with this
feature installed once over USB before it can update itself.

## Background services

**Settings → Background** switches each one off and remembers it: the
phone connection (port 8945), discovery (UDP beacon every 2 s), analysis for
the phone (503 to the phone when off; the phone stops its run), low-power
analysis (one core), keeping the Mac awake during jobs, the build-folder
watcher (every 4 s), and stopping Gradle after builds (frees 1-2 GB). The
page's refresh timer only runs while a page is open.

## Notes

- `src/shared.dart` re-exports the phone app's pure-Dart DSP and file-name
  code, so Mac and phone compute identical features.
- Opening the app while an older version runs replaces it (by PID if it
  predates the Quit command); `make_app.sh --install` does the same.
- Tests: `dart test` here; `flutter test test/mac_companion_interop_test.dart`
  in `mai_an_flutter` runs the phone's real client (and app updater) against
  `maian serve`.
- On a phone hotspot the Mac beacons by unicast to its default gateway (the
  phone), since Android may drop broadcasts.
- `tool/make_icon.dart` draws the app icon; `make_app.sh` packs it.
