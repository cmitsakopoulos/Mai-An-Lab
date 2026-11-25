import 'dart:io';

import 'package:flutter/widgets.dart';

import '../player/queue_store.dart' show Prefs;
import '../scan/storage_paths.dart';
import '../settings/config_controller.dart';
import '../sources/source_config.dart';

/// onboarding_modal.py's first-run state and its finish
/// (`_finish_onboarding`), without the UI: the `.onboarded` marker, the
/// config.toml update, the folder prefs and the tab to land on.

/// `ACCENT_OPTIONS`.
const kOnboardingAccents = [
  ('#FFD60A', 'Gold'),
  ('#64D2FF', 'Cyan'),
  ('#BF5AF2', 'Violet'),
  ('#FF375F', 'Rose'),
  ('#30D158', 'Mint'),
];

/// `STARTUP_OPTIONS` (key, label).
const kOnboardingStartups = [
  ('Library', 'Library'),
  ('Search', 'Search'),
  ('Jarvis', 'Jarvis'),
];

/// Opens the wizard; (startup page, saved folder) once finished.
typedef OnboardingFlow =
    Future<(String, String)?> Function(BuildContext context);

const kOnboardingWelcome =
    'Welcome to Mai-An Lab! Enjoy studio-grade listening.';

String onboardedMarkerPath(String dataDir) =>
    '$dataDir${Platform.pathSeparator}.onboarded';

/// main.py `check_onboarding`: a fresh install has no marker. (Flet also
/// looks beside config.toml; here that is the same folder.) A state-bundle
/// restore writes the marker, so a restored install skips the wizard.
Future<bool> needsOnboarding(String dataDir) async {
  try {
    return !await File(onboardedMarkerPath(dataDir)).exists();
  } catch (e) {
    debugPrint('Onboarding check failed: $e');
    return false;
  }
}

/// The wizard's choices.
class OnboardingChoices {
  const OnboardingChoices({
    this.accent = '#FFD60A',
    this.startup = 'Library',
    required this.folder,
    this.qobuzUser = '',
    this.qobuzToken = '',
    this.deezerArl = '',
  });

  final String accent;
  final String startup;
  final String folder;
  final String qobuzUser;
  final String qobuzToken;
  final String deezerArl;
}

/// The suggested library folder: the public Music folder's `Streamrip`,
/// else the Music folder itself (`__init__`; not the app's sandbox).
String suggestedMusicDir(StoragePaths sp) {
  final music = sp.defaultMusicDir();
  final candidate = '$music/Streamrip';
  return Directory(candidate).existsSync() ? candidate : music;
}

/// `_finish_onboarding` step 3–4: what config.toml gets. [folder] is the
/// already-checked one.
Map<String, Map<String, Object?>> onboardingConfigUpdate(
  OnboardingChoices c,
  String folder,
) {
  final update = <String, Map<String, Object?>>{
    'appearance': {
      'accent_color': c.accent,
      'show_jarvis': true,
      'show_network': false,
      'show_playlists': true,
      'show_artists': true,
      'show_albums': false,
      'show_tracks': true,
    },
    'general': {'startup_page': c.startup},
    'downloads': {'folder': folder},
  };
  final qUser = c.qobuzUser.trim();
  final qToken = c.qobuzToken.trim();
  if (qUser.isNotEmpty && qToken.isNotEmpty) {
    update['qobuz'] = {
      'use_auth_token': true,
      'email_or_userid': qUser,
      'password_or_token': qToken,
      'app_id': kQobuzDefaultAppId,
      'secrets': [kQobuzDefaultSecret],
    };
  }
  final arl = c.deezerArl.trim();
  if (arl.isNotEmpty) update['deezer'] = {'arl': arl};
  return update;
}

/// `_finish_onboarding`: the marker, then the choices saved. Returns the
/// folder actually saved (a sandboxed or relative path falls back to the
/// public Music folder: it would strand every download).
Future<String> finishOnboarding({
  required String dataDir,
  required ConfigController config,
  required StoragePaths storage,
  required OnboardingChoices choices,
}) async {
  try {
    final marker = File(onboardedMarkerPath(dataDir));
    await marker.parent.create(recursive: true);
    await marker.writeAsString('1');
  } catch (e) {
    debugPrint("Onboarding: couldn't write the marker: $e");
  }
  final folder = storage.usable(choices.folder)
      ? choices.folder
      : storage.defaultMusicDir();
  await config.update(onboardingConfigUpdate(choices, folder));
  final prefs = Prefs(dataDir);
  await prefs.load();
  await prefs.set('folder_path', folder);
  await prefs.set('library_path', folder);
  await prefs.flush();
  return folder;
}
