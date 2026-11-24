// Fakes shared by the Companion tests: the Mac as the fake clients see
// it, a beacon listener that only logs, the download queue's transfer side
// and a notification bridge that records.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:maian_native/maian_native.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/sync/companion_client.dart';
import 'package:mai_an_lab/sync/discovery.dart';
import 'package:mai_an_lab/sync/sync_manager.dart';

/// The Mac, as the fake clients see it.
class FakeMac {
  String name = 'Test Mac';
  String token = 'tok';
  String pin = '4321';
  bool up = true;
  String host = '192.168.1.7';
  int port = 8945;
  List<Map<String, Object?>> pending = [];
  final acked = <String>[];
  final pushed = <String>[];
  final pushErrors = <String, Object>{};
  final duplicates = <String>{};
  int pings = 0;

  /// Bundles received ("Send Backup to Mac"), and an error to answer with.
  final uploads = <String>[];
  Object? uploadError;

  bool reachable(String h, int p) => up && h == host && p == port;
}

class FakeCompanionClient extends CompanionClient {
  FakeCompanionClient(this.mac, super.host, int port, String? token)
    : super(port: port, authToken: token, deviceName: 'Pixel');
  final FakeMac mac;

  void _reach() {
    if (!mac.reachable(host, port)) {
      throw const SocketException(
        'refused',
        osError: OSError('Connection refused', 61),
      );
    }
  }

  @override
  Future<Map<String, Object?>> ping({Duration? timeout}) async {
    mac.pings++;
    _reach();
    return {
      'status': 'ok',
      'name': mac.name,
      'features_version': 5,
      'authorized': authToken == mac.token,
    };
  }

  @override
  Future<String?> pair(String pin, {String? deviceName}) async {
    _reach();
    if (pin != mac.pin) throw const CompanionAuthError('Incorrect PIN');
    return authToken = mac.token;
  }

  @override
  Future<List<Map<String, Object?>>> getPendingTransfers() async {
    _reach();
    if (authToken != mac.token) {
      throw const CompanionAuthError('Pending transfers: not paired');
    }
    return [for (final t in mac.pending) Map.of(t)];
  }

  @override
  Future<bool> ackTransfers(List<String> ids) async {
    _reach();
    mac.acked.addAll(ids);
    mac.pending.removeWhere((t) => ids.contains(t['id']));
    return true;
  }

  @override
  Future<Map<String, Object?>> uploadStateBundle(String bundlePath) async {
    _reach();
    if (mac.uploadError case final e?) throw e;
    if (!File(bundlePath).existsSync()) throw StateError('no bundle');
    mac.uploads.add(bundlePath);
    return {
      'status': 'ok',
      'saved_path': '/Users/me/backups/${bundlePath.split('/').last}',
    };
  }

  @override
  Future<Map<String, Object?>> pushTrack(
    String path,
    Map<String, Object?> meta,
  ) async {
    _reach();
    if (mac.pushErrors[path] case final e?) throw e;
    mac.pushed.add('$path|${meta['remaining']}|${meta['dsp_features']}');
    return {'status': 'ok', 'duplicate': mac.duplicates.contains(path)};
  }
}

class FakeListener extends BeaconListener {
  FakeListener(DeviceFound f, this.log) : super(onDeviceFound: f);
  final List<String> log;
  @override
  Future<void> start() async => log.add('start');
  @override
  void stop() => log.add('stop');
}

class FakeTransferQueue implements TransferQueue {
  final items = <Map<String, Object?>>[];
  @override
  void enqueueMany(
    List<Map<String, Object?>> items, {
    String qualityTier = 'mp3',
  }) {
    // Mac transfers are queued at their original quality.
    if (qualityTier != 'original') throw StateError('tier $qualityTier');
    this.items.addAll(items);
  }
}

class RecordingBridge implements NotificationBridge {
  final calls = <Map<String, Object?>>[];
  @override
  Stream<NotificationAction> get actions => const Stream.empty();
  @override
  Future<void> showProgress({
    required String job,
    required String title,
    required String content,
    required int progress,
    required int total,
    required bool done,
    required bool cancellable,
    String? actionLabel,
    String? actionTarget,
  }) async =>
      calls.add({'job': job, 'title': title, 'content': content, 'done': done});
  @override
  Future<void> showAlert({
    required String key,
    required String title,
    required String content,
    String navigateTo = '',
    String? actionLabel,
    String? actionTarget,
    bool urgent = false,
    String icon = '',
  }) async =>
      calls.add({'alert': key, 'title': title, 'navigate_to': navigateTo});
  @override
  Future<void> cancelAlert(String key) async => calls.add({'cancel': key});
}
