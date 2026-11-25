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
| Blowfish block cipher (Schneier 1993) & AES-128 block cipher (FIPS 197) for Deezer CDN stream decryption and fallback URL encryption | `lib/sources/deezer_crypto.dart` | Standard published specifications (public domain / NIST FIPS 197) | Specifications only; clean-room pure Dart, bit-verified against `pycryptodomex` |
| On-phone DSP: radix-2 FFT with real-input packing (Cooley–Tukey 1965), Hann window, Slaney-style mel filterbank, orthonormal DCT-II, median-filter HPSS (Fitzgerald 2010), Krumhansl–Schmuckler key profiles (1982), spectral-flux onset autocorrelation tempo | `lib/analysis/fft.dart`, `lib/analysis/audio_features.dart` | Textbook algorithms, as used by this repository's `utils/dsp.py` | Algorithms only; clean-room pure Dart (no `fftea` or FFI needed), verified against `dsp.py` on synthetic and real audio |
| FLAC Vorbis Comment/PICTURE, ID3v2.3, and ISO BMFF MP4 `ilst`/`covr` container tag writing | `lib/sources/tagger.dart` | Standard container specs (Xiph FLAC/Vorbis, ID3v2.3, ISO/IEC 14496-12) | Specifications only; clean-room pure Dart (no external tagging package needed), round-trip verified against `tinytag` and `mutagen` |

## pub.dev dependencies

`sqlite3`, `path_provider`, `audio_service`, `just_audio`, `audio_session`, `permission_handler`, `toml`, `flutter_markdown_plus`, `url_launcher`, `flutter_tts`, `speech_to_text`, `crypto`.
Licences ship via Flutter's `LicenseRegistry` (`showLicensePage` in Settings → About).

## Candidates for upcoming phases (not yet evaluated)

None open. (Phase 6d's DSP candidate `fftea` was not needed: a small pure-Dart FFT matched `dsp.py` to ~3e-6 relative on real music.)
