// sync/manager.py's behaviour on a virtual clock: the probe and heartbeat
// loops, PIN prompts and their snooze, the idle auto-off, transfers
// (auto-accept, the dialog, the background card, acks) and the Send to Mac
// worker. A fake client stands in for the Mac; the protocol itself is
// tested against Flet's server in companion_protocol_test.dart.
import 'dart:async';
import 'dart:io';

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/sync/companion_client.dart';
import 'package:mai_an_lab/sync/sync_manager.dart';

import 'sync_fakes.dart';

class _Host implements SyncHost {
  final prefs = <String, Object?>{};
  final snacks = <String>[];
  final pins = <PinPrompt>[];
  final transferPrompts = <TransferPrompt>[];
  FakeTransferQueue? q = FakeTransferQueue();
  bool background = false;
  late CompanionSyncManager manager;

  @override
  Object? pref(String key) => prefs[key];
  @override
  Future<void> savePref(String key, Object? value) async => prefs[key] = value;
  @override
  bool get inBackground => background;
  @override
  void snack(String text, {SyncIcon? icon, bool red = false}) =>
      snacks.add(red ? 'RED $text' : text);
  @override
  void showPinPrompt(PinPrompt prompt) => pins.add(prompt);
  @override
  void dismissPinPrompt(PinPrompt prompt) => manager.pinPromptClosed(prompt);
  @override
  void showTransferPrompt(TransferPrompt prompt) => transferPrompts.add(prompt);
  @override
  void dismissTransferPrompt(TransferPrompt prompt) =>
      manager.transferPromptClosed(prompt);
  @override
  TransferQueue? get queue => q;
  @override
  Future<Map<String, Object?>?> storedFeatures(String path) async =>
      path.endsWith('analysed.flac') ? {'bpm': 120.0} : null;
}

class _Clock extends Stopwatch {
  _Clock(this.async);
  final FakeAsync async;
  @override
  Duration get elapsed => async.elapsed;
}

Map<String, Object?> _t(
  String id, {
  String q = 'aac_vbr_max',
  bool dsp = false,
}) => {
  'id': id,
  'title': 'Title $id',
  'artist': 'Artist',
  'album': 'Album',
  'quality': q,
  'extension': 'flac',
  'stream_path': '/api/v1/tracks/stream?path=%2F$id&quality=$q',
  'dsp_features': dsp ? {'bpm': 1} : null,
};

void main() {
  late FakeMac mac;
  late _Host host;
  late RecordingBridge bridge;
  late List<String> listenerLog;
  late List<String> sweeps;

  setUp(() {
    mac = FakeMac();
    host = _Host();
    bridge = RecordingBridge();
    NotificationBridge.instance = bridge;
    listenerLog = [];
    sweeps = [];
  });

  CompanionSyncManager make(FakeAsync async, {List<String>? sweepFinds}) {
    final m = CompanionSyncManager(
      host,
      clientFactory: (h, p, t, n) => FakeCompanionClient(mac, h, p, t),
      listenerFactory: (f) => FakeListener(f, listenerLog),
      localIp: () async => '192.168.1.20',
      sweep: (ip, port) async {
        sweeps.add('$ip:$port');
        return sweepFinds ?? const [];
      },
      deviceName: () async => 'Pixel',
      clock: _Clock(async),
    );
    return host.manager = m;
  }

  void pair() {
    host.prefs['companion_paired_token'] = 'tok';
    host.prefs['companion_paired_host'] = mac.host;
    host.prefs['companion_paired_port'] = mac.port;
  }

  group('connection', () {
    test('a saved token connects on the first probe; Wi-Fi address kept', () {
      fakeAsync((async) {
        pair();
        final m = make(async);
        expect(m.enabled, isFalse);
        m.start();
        async.flushMicrotasks();
        expect(host.prefs['companion_sync_enabled'], true);
        expect(m.isConnected, isTrue);
        expect(m.discoveredServer, {
          'name': 'Test Mac',
          'ip': mac.host,
          'port': 8945,
          'features_version': 5,
        });
        expect(host.prefs['companion_paired_name'], 'Test Mac');
        // Listening stopped once connected.
        expect(listenerLog, ['start', 'stop']);
        m.stop();
        async.flushTimers();
      });
    });

    test('the USB tunnel is tried first and is never saved', () {
      fakeAsync((async) {
        mac.host = '127.0.0.1';
        host.prefs['companion_paired_token'] = 'tok';
        final m = make(async)..start();
        async.flushMicrotasks();
        expect(m.isConnected, isTrue);
        expect(host.prefs.containsKey('companion_paired_host'), isFalse);
        m.stop();
        async.flushTimers();
      });
    });

    test('no token: the PIN prompt, then two minutes of snooze', () {
      fakeAsync((async) {
        host.prefs['companion_paired_host'] = mac.host;
        final m = make(async)..start();
        async.flushMicrotasks();
        expect(host.pins, hasLength(1));
        final p = host.pins.single;
        expect((p.name, p.host, p.via), ('Test Mac', mac.host, mac.host));
        // Open: the probe doesn't ask again.
        async.elapse(const Duration(seconds: 9));
        expect(host.pins, hasLength(1));
        // "Not now": snoozed for 120 s, then asked again.
        host.dismissPinPrompt(p);
        async.elapse(const Duration(seconds: 100));
        expect(host.pins, hasLength(1));
        async.elapse(const Duration(seconds: 25));
        expect(host.pins, hasLength(2));
        m.stop();
        async.flushTimers();
      });
    });

    test('pairing: bad input, a wrong PIN, the right one', () {
      fakeAsync((async) {
        host.prefs['companion_paired_host'] = mac.host;
        final m = make(async)..start();
        async.flushMicrotasks();
        final p = host.pins.single;
        String? err;
        p.submit('12a4').then((e) => err = e);
        async.flushMicrotasks();
        expect(err, 'Please enter the 4-digit PIN.');
        p.submit('1111').then((e) => err = e);
        async.flushMicrotasks();
        expect(err, startsWith('Incorrect PIN. Check the code on your Mac'));
        p.submit(' 4321 ').then((e) => err = e);
        async.flushMicrotasks();
        expect(err, isNull);
        expect(m.isConnected, isTrue);
        expect(m.pinPromptOpen, isFalse);
        expect(host.prefs['companion_paired_token'], 'tok');
        expect(host.snacks, contains('Connected to Test Mac'));
        m.stop();
        async.flushTimers();
      });
    });

    test('heartbeat: two misses drop the link; a revoked token too', () {
      fakeAsync((async) {
        pair();
        final m = make(async)..start();
        async.flushMicrotasks();
        mac.up = false;
        async.elapse(const Duration(seconds: 15));
        expect(m.isConnected, isTrue); // one miss is Wi-Fi power save
        async.elapse(const Duration(seconds: 15));
        expect(m.isConnected, isFalse);
        expect(m.lastError, 'Mac stopped responding');
        expect(m.isSearching, isTrue);

        mac.up = true;
        async.elapse(const Duration(seconds: 4)); // the probe reconnects
        expect(m.isConnected, isTrue);
        mac.token = 'rotated';
        async.elapse(const Duration(seconds: 15));
        expect(m.isConnected, isFalse);
        expect(m.lastError, 'Pairing was revoked on the Mac');
        expect(host.prefs['companion_paired_token'], '');
        m.stop();
        async.flushTimers();
      });
    });

    test('idle for 15 minutes: asleep, loops stopped', () {
      fakeAsync((async) {
        pair();
        final m = make(async)..start();
        async.flushMicrotasks();
        async.elapse(const Duration(minutes: 14));
        expect(m.isAsleep, isFalse);
        async.elapse(const Duration(minutes: 1, seconds: 15));
        expect(m.isAsleep, isTrue);
        expect(m.isConnected, isFalse);
        expect(m.enabled, isTrue);
        final pings = mac.pings;
        async.elapse(const Duration(minutes: 5));
        expect(mac.pings, pings);
        async.flushTimers();
      });
    });

    test('unpair forgets the Mac and snoozes it', () {
      fakeAsync((async) {
        pair();
        final m = make(async)..start();
        async.flushMicrotasks();
        m.unpair();
        async.flushMicrotasks();
        expect(m.isConnected, isFalse);
        expect(host.prefs['companion_paired_token'], '');
        expect(host.prefs['companion_paired_host'], '');
        expect(host.prefs['companion_paired_port'], 8945);
        expect(host.snacks.last, 'Unpaired from Mac Companion.');
        m.stop();
        async.flushTimers();
      });
    });
  });

  group('Find Mac and manual connect', () {
    test('connected: the round trip', () {
      fakeAsync((async) {
        pair();
        final m = make(async)..start();
        async.flushMicrotasks();
        String? out;
        m.findMac().then((s) => out = s);
        async.flushMicrotasks();
        expect(out, 'Connected to Test Mac (${mac.host}) · answered in 0 ms.');
        m.stop();
        async.flushTimers();
      });
    });

    test('found only by the sweep', () {
      fakeAsync((async) {
        mac.host = '192.168.1.33';
        final m = make(async, sweepFinds: ['192.168.1.33']);
        host.prefs['companion_paired_token'] = 'tok';
        String? out;
        m.findMac().then((s) => out = s);
        async.elapse(const Duration(seconds: 4));
        expect(sweeps, ['192.168.1.20:8945']);
        expect(
          out,
          'Connected to Test Mac (192.168.1.33) by scanning: this network '
          'blocks discovery broadcasts, so the address is saved for next '
          'time.',
        );
        expect(host.prefs['companion_paired_host'], '192.168.1.33');
        m.stop();
        async.flushTimers();
      });
    });

    test('nothing answers', () {
      fakeAsync((async) {
        mac.up = false;
        final m = make(async);
        String? out;
        m.findMac().then((s) => out = s);
        async.elapse(const Duration(seconds: 4));
        expect(out, startsWith('No Mac answered. This phone is 192.168.1.20.'));
        m.stop();
        async.flushTimers();
      });
    });

    test('manual: refused, then a PIN prompt', () {
      fakeAsync((async) {
        final m = make(async);
        String? out;
        m.connectManual('192.168.1.99', 8945).then((s) => out = s);
        async.flushMicrotasks();
        expect(
          out,
          startsWith('192.168.1.99 is reachable, but nothing answers'),
        );
        m.connectManual(mac.host, 8945).then((s) => out = s);
        async.flushMicrotasks();
        expect(
          out,
          'Found Test Mac (${mac.host}). Enter the PIN shown on the Mac.',
        );
        m.stop();
        async.flushTimers();
      });
    });
  });

  group('transfers', () {
    test('auto-accepted on connect, acked once on disk', () {
      fakeAsync((async) {
        pair();
        mac.pending = [_t('a'), _t('b', q: 'original')];
        final m = make(async)..start();
        async.flushMicrotasks();
        final items = host.q!.items;
        expect(items.map((i) => i['id']), ['a', 'b']);
        expect(items.first, {
          'source': 'companion',
          'media_type': 'track',
          'id': 'a',
          'companion_transfer_id': 'a',
          'url':
              'http://${mac.host}:8945/api/v1/tracks/stream?path=%2Fa'
              '&quality=aac_vbr_max&token=tok',
          'ui_title': 'Title a',
          'ui_subtitle': 'Artist',
          'title': 'Title a',
          'artist': 'Artist',
          'album': 'Album',
          'dsp_features': null,
          'quality_label': 'AAC VBR',
        });
        expect(items.last['quality_label'], 'FLAC');
        expect(host.snacks, contains('Receiving 2 tracks from Test Mac'));
        // In flight: the next poll doesn't queue them again.
        async.elapse(const Duration(seconds: 15));
        expect(items, hasLength(2));

        m.onTransferFinished('a', ok: true);
        async.flushMicrotasks();
        expect(mac.acked, ['a']);
        m.stop();
        async.flushTimers();
      });
    });

    test('a transfer failing twice falls back to the dialog', () {
      fakeAsync((async) {
        pair();
        mac.pending = [_t('a')];
        final m = make(async)..start();
        async.flushMicrotasks();
        m.onTransferFinished('a', ok: false);
        async.elapse(const Duration(seconds: 15));
        expect(host.q!.items, hasLength(2)); // retried once
        m.onTransferFinished('a', ok: false);
        async.elapse(const Duration(seconds: 15));
        expect(host.q!.items, hasLength(2));
        expect(host.transferPrompts, hasLength(1));
        m.stop();
        async.flushTimers();
      });
    });

    test('ask first: the dialog, once per set of transfers', () {
      fakeAsync((async) {
        pair();
        host.prefs[askBeforeDownloadPref] = true;
        mac.pending = [_t('a', dsp: true), _t('b', q: 'aac_320k')];
        final m = make(async)..start();
        async.flushMicrotasks();
        expect(host.q!.items, isEmpty);
        final p = host.transferPrompts.single;
        expect(p.macName, 'Test Mac');
        expect(p.summary, '2 tracks · AAC 320, AAC VBR · features for 1');
        expect(bridge.calls, contains(equals({'cancel': 'transfers'})));
        host.dismissTransferPrompt(p); // "Later"
        async.elapse(const Duration(seconds: 15));
        expect(host.transferPrompts, hasLength(1)); // not asked again
        mac.pending.add(_t('c'));
        async.elapse(const Duration(seconds: 15));
        expect(host.transferPrompts, hasLength(2));
        host.transferPrompts.last.download();
        expect(host.q!.items.map((i) => i['id']), ['a', 'b', 'c']);
        expect(host.snacks.last, 'Queued 3 tracks in Download Dock');
        m.stop();
        async.flushTimers();
      });
    });

    test('in the background: one card, then the queue', () {
      fakeAsync((async) {
        pair();
        host.prefs[askBeforeDownloadPref] = true;
        final m = make(async)..start();
        async.flushMicrotasks();
        host.background = true;
        m.handleAppBackground();
        mac.pending = [_t('a'), _t('b')];
        async.elapse(const Duration(seconds: 15));
        expect(bridge.calls.where((c) => c['alert'] == 'transfers').toList(), [
          {
            'alert': 'transfers',
            'title': 'Test Mac has 2 tracks for you',
            'navigate_to': 'sync:transfers',
          },
        ]);
        async.elapse(const Duration(seconds: 120));
        expect(bridge.calls.where((c) => c['alert'] != null), hasLength(1));
        expect(host.transferPrompts, isEmpty);
        m.stop();
        async.flushTimers();
      });
    });
  });

  group('Send to Mac', () {
    test('sent, duplicate and failed; features sent along', () {
      fakeAsync((async) {
        pair();
        final m = make(async)..start();
        async.flushMicrotasks();
        mac.duplicates.add('/m/dup.flac');
        mac.pushErrors['/m/bad.flac'] = const CompanionHttpError('HTTP 415');
        final n = m.sendTracks([
          {'path': '/m/analysed.flac', 'title': 'A', 'artist': 'X'},
          {'path': '/m/dup.flac'},
          {'path': '/m/bad.flac'},
          // Twice in one call: queued twice, as Python does (it only
          // skips paths already waiting from an earlier call).
          {'path': '/m/analysed.flac'},
          {'title': 'no path'},
        ]);
        expect(n, 4);
        expect(
          m.sendTracks([
            {'path': '/m/dup.flac'},
          ]),
          0,
        ); // waiting
        async.flushMicrotasks();
        expect(mac.pushed, [
          '/m/analysed.flac|3|{bpm: 120.0}',
          '/m/dup.flac|2|null',
          '/m/analysed.flac|0|{bpm: 120.0}',
        ]);
        expect(
          host.snacks.last,
          'Sent 2 tracks to Test Mac · 1 already there · 1 failed',
        );
        final done = bridge.calls.lastWhere((c) => c['job'] == 'sync');
        expect(done['done'], true);
        expect(done['title'], 'Sending to Test Mac');
        m.stop();
        async.flushTimers();
      });
    });

    test('three network failures end the batch', () {
      fakeAsync((async) {
        pair();
        final m = make(async)..start();
        async.flushMicrotasks();
        for (final p in ['/1', '/2', '/3']) {
          mac.pushErrors[p] = const SocketException('gone');
        }
        m.sendTracks([
          for (final p in ['/1', '/2', '/3', '/4', '/5']) {'path': p},
        ]);
        async.flushMicrotasks();
        expect(mac.pushed, isEmpty);
        expect(host.snacks.last, 'RED 5 failed (Lost connection to Test Mac)');
        m.stop();
        async.flushTimers();
      });
    });

    test('not connected: nothing queued', () {
      fakeAsync((async) {
        final m = make(async);
        expect(
          m.sendTracks([
            {'path': '/a'},
          ]),
          0,
        );
      });
    });
  });

  group('texts', () {
    test('quality labels', () {
      expect(transferQualityLabel({'quality': 'aac_256k'}), 'AAC 256');
      expect(
        transferQualityLabel({'quality': 'original', 'extension': 'mp3'}),
        'MP3',
      );
      expect(transferQualityLabel({}), 'ORIGINAL');
      expect(
        transferQualityLabel({'quality': null, 'extension': ''}),
        'ORIGINAL',
      );
    });

    test('connect errors', () {
      expect(
        explainConnectError(TimeoutException('x'), 'h', 1),
        startsWith('No answer from h.'),
      );
      expect(
        explainConnectError(
          const SocketException('x', osError: OSError('', 113)),
          'h',
          1,
        ),
        startsWith('No answer from h.'),
      );
      expect(
        explainConnectError(const CompanionHttpError('HTTP 404'), 'h', 1),
        "Something answered at h:1, but it isn't the Mai-An Companion Server.",
      );
      expect(
        explainConnectError(const FormatException('bad'), 'h', 1),
        'Could not reach h:1 (FormatException: bad).',
      );
    });

    test('is_private as Python', () {
      // ipaddress.ip_address(x).is_private, printed by Python (3.13; the
      // same for these as the APK's 3.12).
      const want = {
        '10.1.2.3': true,
        '172.16.0.1': true,
        '172.32.0.1': false,
        '192.168.1.20': true,
        '100.64.0.1': false,
        '169.254.3.4': true,
        '127.0.0.1': true,
        '8.8.8.8': false,
        '198.18.5.5': true,
        '240.1.1.1': true,
        '192.0.0.9': false,
        'x': false,
      };
      for (final e in want.entries) {
        expect(isPrivateIpv4(e.key), e.value, reason: e.key);
      }
    });
  });
}
