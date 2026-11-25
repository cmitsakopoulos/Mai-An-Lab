/// Single source of truth for the Flutter app's displayed version.
///
/// Kept in lockstep with `pubspec.yaml` (`version: $kAppVersion+$kAppBuildNumber`)
/// and enforced by `test/app_version_test.dart`.
///
/// The Flutter port uses standard SemVer 2.0.0 chained to a `0.x.y` zero
/// release (Git tags `vMAJOR.MINOR.PATCH`, no dot after `v`, separating it
/// cleanly from legacy Flet's `v.1.x` / `v.2.0.0` tags):
/// - `0.1.0`: Phases 0–5 baseline
/// - `0.2.0`: Phase 6 (Sources, Downloads, New Releases & On-Phone DSP)
/// - `0.3.0`: Phase 7 (Metadata Workbench, Enrichment & Lean Onboarding)
/// - `0.4.0`: Pre-cutover UI Polish pass
/// - `1.0.0`: Android cutover (replaces Flet in-place on `master`)
/// - `1.1.0`: Phase 8 (macOS desktop + sync server)
///
/// Build numbers start at `2` (strictly above Flet's `versionCode = 1`) and
/// increase monotonically on every release build.
const String kAppVersion = '0.3.0';
const int kAppBuildNumber = 4;
