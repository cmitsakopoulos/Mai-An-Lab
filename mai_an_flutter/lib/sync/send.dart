import 'package:flutter/material.dart';

import '../data/library_repository.dart';
import 'sync_manager.dart';

/// utils/sync/send.py, the phone's half: "Send to the Mac", the one library
/// action that moves tracks to the other device. Menus ask
/// [companionTarget] what to call it; the action is always offered, and
/// [sendTracks] either sends or says why it can't (not paired, not
/// reachable). The Mac's half (preparing tracks for the phone) is phase 8.

/// send.py `CompanionTarget` for `kind == "mac"`.
class CompanionTarget {
  const CompanionTarget(this.name, {required this.paired});
  final String name;
  final bool paired;

  String get label => 'Send to $name';
  IconData get icon => Icons.laptop_mac_rounded;
}

/// send.py `companion_target`: named as well as we know it, paired or not.
CompanionTarget companionTarget(CompanionSyncManager m) =>
    CompanionTarget(m.macName('Mac'), paired: m.isPaired);

/// send.py `_reach_mac`: the phone isn't linked, so wake the link and try
/// the USB tunnel and the saved address once. A failure says what to do;
/// tapping Send again retries.
Future<bool> _reachMac(CompanionSyncManager m, CompanionTarget target) async {
  if (!target.paired) {
    m.host.snack(
      'Not paired with a Mac yet. Turn on Companion Server on the Mac, then '
      'pair in Settings → Companion & Sync.',
      icon: SyncIcon.linkOff,
      red: true,
    );
    return false;
  }
  m.host.snack('Connecting to ${target.name}…', icon: SyncIcon.sync);
  try {
    if (!m.enabled || m.isAsleep) await m.start();
    m.touch();
    await m.probeOnce();
  } catch (e) {
    debugPrint('reconnect before send failed: $e');
  }
  if (m.isConnected) return true;
  m.host.snack(
    "Can't reach ${target.name}. Check Companion Server is on and both "
    'devices are on the same Wi-Fi, then tap Send again.',
    icon: SyncIcon.wifiOff,
    red: true,
  );
  return false;
}

/// send.py `send_tracks` on the phone: progress lives in the `sync`
/// notification, the result in a snackbar. Returns how many were handed
/// to the worker.
Future<int> sendTracks(CompanionSyncManager m, List<Track> tracks) async {
  final items = [
    for (final t in tracks)
      if (t.path.isNotEmpty)
        <String, Object?>{
          'path': t.path,
          'title': t.title,
          'artist': t.artist,
          'album': t.album,
        },
  ];
  if (items.isEmpty) return 0;
  final noun = '${items.length} track${items.length != 1 ? 's' : ''}';
  var target = companionTarget(m);
  if (!m.isConnected && !await _reachMac(m, target)) return 0;
  target = companionTarget(m); // the reply may have renamed the Mac
  final n = m.sendTracks(items);
  if (n > 0) {
    m.host.snack('Sending $noun to ${target.name}…', icon: SyncIcon.upload);
  } else {
    m.host.snack('Already sending to ${target.name}.');
  }
  return n;
}
