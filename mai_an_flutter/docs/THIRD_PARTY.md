# Third-Party Code, Sources & Package Candidates (§12)

Code the port copies, translates, or depends on, for attribution in Settings → About and repository notices. Add a row whenever outside code or a published template comes in, before committing.

## Ported or copied into the source tree

| What | Where in the port | Source | License |
|---|---|---|---|
| tinytag 2.3.2: ID3, MPEG, FLAC, Ogg, WAVE, MP4 parsers (translated line-by-line) | `lib/scan/tinytag.dart` (licence text in header) | https://github.com/tinytag/tinytag (PyPI `tinytag==2.3.2`) | MIT, © 2014–2026 Tom Wallroth, Mat (mathiascode) et al. |
| `flet_audio_service` extension (audio handler, Kotlin notifications, artwork, PCM decoder) | `lib/player/audio_handler.dart`, `packages/maian_native` | This repository (the Flet app's own extension) | Project's license |
| Unicode data tables (NFKD fold, casefold, lower/title, decimal/non-decimal digits, canonical decompositions/combining classes) generated from Python's `unicodedata` & `str` | `lib/core/nfkd_fold_table.dart`, `lib/core/nfkd_casefold_table.dart`, `lib/core/py_unicode_tables.dart`, `lib/core/nfc_table.dart` | Unicode Character Database 15.1, via CPython | Unicode License v3 (data) |
| tomlkit 0.15.2: `InlineTable.as_string` translated; remaining editing behaviour reproduced from outputs | `lib/core/config_file.dart` (licence text above `_InlineTable`) | https://github.com/python-poetry/tomlkit (PyPI `tomlkit==0.15.2`) | MIT, © 2018 Sébastien Eustace |
| CPython `math.hypot` (`vector_norm` in `Modules/mathmodule.c`) translated | `lib/core/py_num.dart` `pyHypot` | https://github.com/python/cpython (3.13) | PSF License v2 |
| CPython `difflib.SequenceMatcher.ratio` translated | `lib/core/py_difflib.dart` | https://github.com/python/cpython (3.13, `Lib/difflib.py`) | PSF License v2 |
| `config.toml` template: streamrip's `config.toml` plus app sections (`utils/config.toml`, copied as-is) | `assets/config_template.toml` | https://github.com/nathom/streamrip (via Flet app) | GPL-3.0 (streamrip). ⚠️ Decide before release ([PORT_MAP.md §6](PORT_MAP.md#6-decisions)) |
| streamrip-derived filename cleaning (`clean_filename`: behaviour-only, no code copied) | `lib/downloads/file_names.dart` | https://github.com/nathom/streamrip | GPL-3.0 upstream; clean-room implementation. Check before porting anything else from streamrip |
| CPython `re` semantics (`\s`, `\w`, `\b`, `IGNORECASE`, `.`, `$`, `MULTILINE`) & `json.dumps` defaults (behaviour-only, no code copied) | `lib/core/py_re.dart`, `lib/core/py_re_tables.dart`, `lib/core/py_json.dart` | CPython `re` / `json` | PSF License v2; no code copied |
| numpy float32 reduction order (`mean`/`std`) and `np.corrcoef` arithmetic (behaviour-only, bit-checked, no code copied) | `lib/stations/acoustic_geometry.dart` | https://github.com/numpy/numpy (2.4) | BSD-3-Clause; no numpy code copied |

## Templates, algorithms & guidance adopted

| What | Where | Source | License |
|---|---|---|---|
| Strict analyzer modes (`strict-casts`, `strict-inference`, `strict-raw-types`) on `flutter_lints` | `analysis_options.yaml` | dart.dev, "Customizing static analysis" | BSD-3-Clause |
| `ReorderableListView` with `ReorderableDragStartListener` handles | `lib/library/browse_view.dart` | Flutter SDK (Material) | BSD-3-Clause |
| Cyclic Jacobi eigenvalue method (Jacobi 1846; Press et al., *Numerical Recipes* §11.1) | `lib/stations/acoustic_geometry.dart` `_symmetricEigen` | Textbook algorithm | Algorithm only; no code copied |
| SVD sign convention (`svd_flip`, `u_based_decision=False`) | `lib/stations/acoustic_geometry.dart` | scikit-learn docs | BSD-3-Clause; convention only |

## pub.dev dependencies

`sqlite3`, `path_provider`, `audio_service`, `just_audio`, `audio_session`, `permission_handler`, `toml`, `flutter_markdown_plus`, `url_launcher`, `flutter_tts`, `speech_to_text`.
Licences ship via Flutter's `LicenseRegistry` (`showLicensePage` in Settings → About).

## Candidates for upcoming phases (not yet evaluated)

| Need | Phase | Candidates | Notes |
|---|---|---|---|
| Tag writing (`tagger.py`) | 6c | [`metatagger`](https://pub.dev/packages/metatagger) (pure Dart: ID3v2, Vorbis comments, iTunes `ilst`); [`metadata_god`](https://pub.dev/documentation/metadata_god/latest/) / [`haudiotagger`](https://pub.dev/packages/haudiotagger) (Rust `lofty`); [`flutter_taglib`](https://pub.dev/documentation/flutter_taglib/1.3.2/) (TagLib FFI) | Prefer pure Dart (`metatagger`) if it round-trips identically through `tinytag` and `mutagen` to keep the build free of Rust/native toolchains. |
| On-phone DSP math (`dsp.py`) | 6d | Pure Dart FFT/STFT (e.g. [`fftea`](https://pub.dev/packages/fftea)) vs native C/FFI | Evaluate pure Dart first (`fftea` / typed `Float64List` / `Float32List` on an isolate) against Python's `dsp.py` accuracy and phone runtime before considering native code. |
| Crypto for Deezer / Qobuz | 6b | [`pointycastle`](https://pub.dev/packages/pointycastle) / [`crypto`](https://pub.dev/packages/crypto) (MD5, Blowfish CBC for Deezer chunks) | Verify Blowfish CBC compatibility with `pycryptodomex` / `deezer-py` via golden byte tests. |
