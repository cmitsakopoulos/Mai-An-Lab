// The phone app's pure-Dart DSP and file-name code, used as-is: the Mac must
// compute exactly what the phone does (same window, same maths, same
// features_version), so there is one copy, in mai_an_flutter/lib.
//
// Only files that import nothing but dart:* and each other may be listed
// here; anything touching Flutter would not compile in a plain Dart binary.
// At the v1.0.0 cutover these move into a shared package.
export '../../mai_an_flutter/lib/analysis/audio_features.dart'
    show
        AudioFeatures,
        extractFeaturesFromPcmBytes,
        kDspFeaturesVersion,
        kDspMaxSeconds,
        kDspSampleRate;
export '../../mai_an_flutter/lib/downloads/file_names.dart'
    show cleanFilename, normalizeNfc;
