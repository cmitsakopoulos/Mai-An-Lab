# Mai-An Lab

A music player and streamrip-style download client for Qobuz and Deezer. It has an
on-device DSP analysis engine, an auto-playlist engine based on acoustic
similarity, and the Jarvis LLM assistant. It's written in Dart: a Flutter app for
Android, plus a command-line companion for the Mac.

> [!NOTE]
> I am a bioinformatician by training, not a professional software engineer. This is a passion project; advice and contributions are welcome.

## Layout

| Path | What it is |
|---|---|
| [`mai_an_flutter/`](mai_an_flutter/) | The app (Flutter, Android). Native player and notification code is in `packages/maian_native`. |
| [`mai_an_mac/`](mai_an_mac/) | `maian`, the Mac companion: pairs with the phone over Wi-Fi, offloads DSP analysis to ffmpeg, and moves tracks both ways. |

The design notes are in [`mai_an_flutter/docs/`](mai_an_flutter/docs/): [PORT_MAP.md](mai_an_flutter/docs/PORT_MAP.md) (data contract and decisions), [PORT_HISTORY.md](mai_an_flutter/docs/PORT_HISTORY.md), and [THIRD_PARTY.md](mai_an_flutter/docs/THIRD_PARTY.md).

## Quick start

```sh
# Phone
cd mai_an_flutter
flutter test
tool/install_phone.sh            # build and install over adb

# Mac companion
cd mai_an_mac
dart test
tool/make_app.sh --install       # "Mai An Companion" in ~/Applications and on the Desktop
```

## History

Before version 1.0 of the Dart app, Mai-An Lab was a Python/[Flet](https://flet.dev) app (`StreamripApp/`). That code, its docs and its releases (`v.1.0.0` to `v.2.0.0`) are on the [`flet`](../../tree/flet) branch and in the tags.

## License

GPL-3.0, see [LICENSE](LICENSE). Third-party notices are in [THIRD_PARTY.md](mai_an_flutter/docs/THIRD_PARTY.md).
