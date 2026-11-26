# maian: Mai An Companion for the Mac

Pure Dart, no Flutter/Xcode/Python. Pairs the phone, runs DSP offload, moves
files both ways. Speaks the same Companion protocol as the Flet app, so the
phone app needs no changes.

    dart compile exe cli/maian.dart -o build/maian
    ./build/maian            # pair, offload, receive; opens the control page
    ./build/maian send A.flac B.flac   # queue files for the phone
    ./build/maian status

Needs `ffmpeg` (brew install ffmpeg) for offload and AAC conversion.
Files live in `~/Library/Application Support/MaiAn` (`MAIAN_HOME` to move);
tracks from the phone go to `~/Music/MaiAn` (`--receive-dir`).

- `src/shared.dart` re-exports the phone app's pure-Dart DSP and file-name
  code, so Mac and phone compute identical features.
- Tests: `dart test` here; `flutter test test/mac_companion_interop_test.dart`
  in `mai_an_flutter` runs the phone's real client against `maian serve`.
- On a phone hotspot the Mac beacons by unicast to its default gateway (the
  phone), since Android may drop broadcasts.
