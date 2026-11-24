// Phase 4f: state bundles. Flet's test_state_export.py, ported; the ZIP
// reader and writer; and bundles crossing between the apps: the port
// imports what Flet's own state_export.py exports and the other way
// round (needs python3 with the standard library only; skipped without).
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/zip_archive.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/data/state_export.dart';
import 'package:sqlite3/sqlite3.dart';

final _flet = '${Directory.current.parent.path}/StreamripApp';

String? _findPython() {
  for (final p in [
    '${Platform.environment['HOME']}/miniconda3/bin/python',
    '/usr/bin/python3',
  ]) {
    if (File(p).existsSync()) return p;
  }
  return null;
}

final _python = _findPython();

/// Runs [code] with Flet's `utils.state_export` importable; returns stdout.
String _py(String code) {
  final r = Process.runSync(_python!, [
    '-c',
    'import sys; sys.path.insert(0, ${jsonEncode(_flet)})\n'
        'from utils import state_export\n$code',
  ]);
  if (r.exitCode != 0) throw StateError('python: ${r.stderr}');
  return '${r.stdout}';
}

late Directory tmp;
late String dbPath, configPath, bundleDir;

Future<void> _makeDb(String path, {String title = 'Song'}) async {
  final repo = await LibraryRepository.open(path, writable: true);
  await repo.close();
  final db = sqlite3.open(path);
  db.execute('PRAGMA journal_mode=WAL');
  db.execute("INSERT INTO artists(name) VALUES ('Artist')");
  db.execute("INSERT INTO albums(title, artist_id) VALUES ('Album', 1)");
  db.execute(
    "INSERT INTO tracks(path, title, album_id) VALUES ('/m/a.flac', ?, 1)",
    [title],
  );
  db.close();
}

List<Object?> _titles(String path) {
  final db = sqlite3.open(path, mode: OpenMode.readOnly);
  try {
    return [for (final r in db.select('SELECT title FROM tracks')) r['title']];
  } finally {
    db.close();
  }
}

void main() {
  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('bundle_test_');
    dbPath = '${tmp.path}/library.db';
    configPath = '${tmp.path}/config.toml';
    bundleDir = '${tmp.path}/bundles';
    Directory(bundleDir).createSync();
    File(configPath).writeAsStringSync('# test config\n');
    await _makeDb(dbPath);
  });
  tearDown(() => tmp.delete(recursive: true));

  Future<String> export() =>
      exportState(dbPath: dbPath, configPath: configPath, outDir: bundleDir);

  group('zip', () {
    test('crc32 is zlib.crc32', () {
      expect(crc32(utf8.encode('hello')), 0x3610a686);
      expect(crc32(const []), 0);
      expect(
        crc32(utf8.encode('world'), crc32(utf8.encode('hello '))),
        crc32(utf8.encode('hello world')),
      );
    });

    test('round trip: files, bytes, empty, larger than a chunk', () {
      final big = File('${tmp.path}/big.bin')
        ..writeAsBytesSync(List.generate(300000, (i) => (i * 7) % 251));
      final path = '${tmp.path}/a.zip';
      ZipWriter(path)
        ..addFile('big.bin', big.path)
        ..addString('m.json', '{"é": 1}')
        ..addBytes('empty', const [])
        ..close();
      final z = ZipReader(path);
      expect(z.names, ['big.bin', 'm.json', 'empty']);
      expect(z.read('big.bin'), big.readAsBytesSync());
      expect(utf8.decode(z.read('m.json')!), '{"é": 1}');
      expect(z.read('empty'), isEmpty);
      expect(z.read('missing'), isNull);
      expect(z.extractTo('big.bin', '${tmp.path}/out.bin'), isTrue);
      expect(
        File('${tmp.path}/out.bin').readAsBytesSync(),
        big.readAsBytesSync(),
      );
      z.close();
    });

    test('not a zip, and a corrupted member', () {
      final junk = File('${tmp.path}/junk.zip')..writeAsStringSync('nope');
      expect(
        () => ZipReader(junk.path),
        throwsA(
          isA<BadZipFile>().having(
            (e) => '$e',
            'text',
            'File is not a zip file',
          ),
        ),
      );
      final path = '${tmp.path}/b.zip';
      ZipWriter(path)
        ..addString('x', 'a' * 1000)
        ..close();
      final bytes = File(path).readAsBytesSync();
      bytes[35] ^= 0xFF; // inside the deflated data
      File(path).writeAsBytesSync(bytes);
      final z = ZipReader(path);
      expect(() => z.read('x'), throwsA(anything));
      z.close();
    });
  });

  group("Flet's test_state_export.py", () {
    test('a bundle of another version is refused', () async {
      final bundle = await export();
      final forged = '${tmp.path}/forged.zip';
      final src = ZipReader(bundle);
      final w = ZipWriter(forged);
      for (final n in src.names) {
        var data = src.read(n)!;
        if (n == 'manifest.json') {
          final m = jsonDecode(utf8.decode(data)) as Map<String, Object?>;
          m['bundle_version'] = 1;
          data = utf8.encode(jsonEncode(m));
        }
        w.addBytes(n, data);
      }
      w.close();
      src.close();
      expect(
        () => importState(
          zipPath: forged,
          dbPath: dbPath,
          configPath: configPath,
        ),
        throwsA(
          isA<BundleError>().having(
            (e) => e.message,
            'text',
            'bundle_version 1 != expected 3',
          ),
        ),
      );
    });

    test('the snapshot has its fixed name and imports', () async {
      final out = await exportStateSnapshot(
        dbPath: dbPath,
        configPath: configPath,
        outDir: bundleDir,
        name: 'mai_an_lab_state_latest.zip',
      );
      expect(out.split('/').last, 'mai_an_lab_state_latest.zip');
      final manifest = inspectBundle(out);
      expect(manifest['bundle_version'], kBundleVersion);
      expect(manifest['auto_snapshot'], isTrue);
      final r = importState(
        zipPath: out,
        dbPath: '${tmp.path}/imported.db',
        configPath: '${tmp.path}/imported.toml',
      );
      expect(r.replaced.keys, ['library.db', 'config.toml']);
      expect(_titles('${tmp.path}/imported.db'), ['Song']);
      expect(
        File('${tmp.path}/imported.toml').readAsStringSync(),
        '# test config\n',
      );
    });

    test('overlapping snapshots always carry the library', () async {
      final outs = await Future.wait([
        for (var i = 0; i < 8; i++)
          exportStateSnapshot(
            dbPath: dbPath,
            configPath: configPath,
            outDir: bundleDir,
            name: 'mai_an_lab_state_latest.zip',
          ),
      ]);
      for (final out in outs.toSet()) {
        final z = ZipReader(out);
        expect(z.names, contains('library.db'));
        z.close();
      }
      expect(
        Directory(bundleDir).listSync().map((e) => e.path.split('/').last),
        ['mai_an_lab_state_latest.zip'],
      );
    });

    test('a snapshot replaces the previous one', () async {
      Future<String> snap() => exportStateSnapshot(
        dbPath: dbPath,
        configPath: configPath,
        outDir: bundleDir,
        name: 'mai_an_lab_state_latest.zip',
      );
      final f = File(await snap());
      final first = f.lastModifiedSync();
      await Future<void>.delayed(const Duration(milliseconds: 1100));
      await snap();
      expect(f.lastModifiedSync().isAfter(first), isTrue);
      expect(
        Directory(bundleDir).listSync().where((e) => e.path.endsWith('.zip')),
        hasLength(1),
      );
    });

    test('import deletes the stale WAL and SHM', () async {
      final bundle = await export();
      final target = '${tmp.path}/imported.db';
      File('$target-wal').writeAsStringSync('dummy wal data');
      File('$target-shm').writeAsStringSync('dummy shm data');
      importState(
        zipPath: bundle,
        dbPath: target,
        configPath: '${tmp.path}/imported.toml',
      );
      expect(File(target).existsSync(), isTrue);
      expect(File('$target-wal').existsSync(), isFalse);
      expect(File('$target-shm').existsSync(), isFalse);
    });
  });

  group('export', () {
    test('the snapshot holds what is committed in the WAL', () async {
      // A live connection with the change still in the WAL, not yet
      // checkpointed: the online backup sees it.
      final live = sqlite3.open(dbPath);
      live.execute("UPDATE tracks SET title = 'Live'");
      final out = await export();
      live.close();
      final r = importState(
        zipPath: out,
        dbPath: '${tmp.path}/x.db',
        configPath: null,
      );
      expect(r.replaced.keys, ['library.db']);
      expect(_titles('${tmp.path}/x.db'), ['Live']);
    });

    test('name, manifest and members; no temp files left', () async {
      final now = DateTime(2026, 11, 24, 9, 5, 7);
      File('${tmp.path}/recent_searches.json').writeAsStringSync('["a"]');
      final out = await exportState(
        dbPath: dbPath,
        configPath: configPath,
        searchHistoryPath: '${tmp.path}/recent_searches.json',
        outDir: bundleDir,
        now: now,
      );
      expect(out, '$bundleDir/mai_an_lab_state_20261124_090507.zip');
      final z = ZipReader(out);
      expect(z.names, [
        'library.db',
        'config.toml',
        'recent_searches.json',
        'manifest.json',
      ]);
      final manifest = utf8.decode(z.read('manifest.json')!);
      z.close();
      final dbBytes =
          (jsonDecode(manifest) as Map)['contents']['library.db']['bytes']
              as int;
      expect(
        manifest,
        '{\n  "bundle_version": 3,\n  "exported_at": "20261124_090507",\n'
        '  "contents": {\n    "library.db": {\n      "bytes": $dbBytes\n'
        '    },\n    "config.toml": {\n      "bytes": 14\n    },\n'
        '    "recent_searches.json": {\n      "bytes": 5\n    }\n  }\n}',
      );
      expect(Directory(bundleDir).listSync(), hasLength(1));
    });

    test('keeps the newest five bundles', () async {
      for (var i = 0; i < 7; i++) {
        final f = File('$bundleDir/mai_an_lab_state_old$i.zip')
          ..writeAsStringSync('x');
        f.setLastModifiedSync(DateTime(2020, 1, 1 + i));
      }
      File('$bundleDir/other.zip').writeAsStringSync('x');
      final out = await export();
      final left = Directory(
        bundleDir,
      ).listSync().map((e) => e.path.split('/').last).toList()..sort();
      expect(
        left,
        [
          'mai_an_lab_state_old3.zip',
          'mai_an_lab_state_old4.zip',
          'mai_an_lab_state_old5.zip',
          'mai_an_lab_state_old6.zip',
          out.split('/').last,
          'other.zip',
        ]..sort(),
      );
    });

    test('a missing database exports without one', () async {
      File(dbPath).deleteSync();
      final out = await export();
      expect(inspectBundle(out)['contents'], {
        'config.toml': {'bytes': 14},
      });
    });
  });

  group('import', () {
    test("a bundle's errors, in Python's words", () async {
      final noManifest = '${tmp.path}/n.zip';
      ZipWriter(noManifest)
        ..addString('library.db', '')
        ..close();
      expect(
        () => importState(
          zipPath: noManifest,
          dbPath: dbPath,
          configPath: configPath,
        ),
        throwsA(
          isA<BundleError>().having(
            (e) => e.message,
            'text',
            'missing manifest.json',
          ),
        ),
      );
      final noVersion = '${tmp.path}/v.zip';
      ZipWriter(noVersion)
        ..addString('manifest.json', '{}')
        ..close();
      expect(
        () => importState(
          zipPath: noVersion,
          dbPath: dbPath,
          configPath: configPath,
        ),
        throwsA(
          isA<BundleError>().having(
            (e) => e.message,
            'text',
            'bundle_version None != expected 3',
          ),
        ),
      );
      expect(_titles(dbPath), ['Song']);
    });

    test('only the members present are replaced', () async {
      final b = '${tmp.path}/c.zip';
      ZipWriter(b)
        ..addString('config.toml', 'new\n')
        ..addString('manifest.json', '{"bundle_version": 3}')
        ..close();
      final r = importState(
        zipPath: b,
        dbPath: dbPath,
        configPath: configPath,
        searchHistoryPath: '${tmp.path}/recent_searches.json',
      );
      expect(r.replaced, {'config.toml': configPath});
      expect(File(configPath).readAsStringSync(), 'new\n');
      expect(_titles(dbPath), ['Song']);
      expect(File('${tmp.path}/recent_searches.json').existsSync(), isFalse);
    });
  });

  group('with Flet', () {
    // The phone bundles kept for the goldens (gitignored: personal data).
    final backups = Directory(
      '${Directory.current.parent.path}/tools/state_backups',
    );
    test(
      'every kept phone bundle reads as Python reads it',
      () {
        final zips = [
          for (final e in backups.listSync())
            if (e is File && e.path.endsWith('.zip')) e.path,
        ]..sort();
        expect(zips, isNotEmpty);
        for (final zip in zips) {
          final out = '${tmp.path}/py_${zips.indexOf(zip)}';
          _py('''
import zipfile
zipfile.ZipFile(${jsonEncode(zip)}).extractall(${jsonEncode(out)})
''');
          final z = ZipReader(zip);
          for (final name in z.names) {
            expect(
              z.read(name),
              File('$out/$name').readAsBytesSync(),
              reason: '$zip: $name',
            );
          }
          z.close();
          expect(inspectBundle(zip)['bundle_version'], isNotNull, reason: zip);
        }
      },
      skip: _python == null || !backups.existsSync()
          ? 'no python3 or no kept bundles'
          : false,
    );

    test(
      'Python imports what the port exports',
      () async {
        File('${tmp.path}/recent_searches.json').writeAsStringSync('["q"]');
        final out = await exportState(
          dbPath: dbPath,
          configPath: configPath,
          searchHistoryPath: '${tmp.path}/recent_searches.json',
          outDir: bundleDir,
        );
        final res = _py('''
import json, sqlite3, zipfile
z = zipfile.ZipFile(${jsonEncode(out)})
assert z.testzip() is None
r = state_export.import_state(${jsonEncode(out)}, ${jsonEncode('${tmp.path}/py.db')},
    ${jsonEncode('${tmp.path}/py.toml')}, ${jsonEncode('${tmp.path}/py.json')})
db = sqlite3.connect(${jsonEncode('${tmp.path}/py.db')})
print(json.dumps({
  "replaced": list(r["replaced"]),
  "manifest": state_export.inspect_bundle(${jsonEncode(out)}),
  "titles": [t for (t,) in db.execute("SELECT title FROM tracks")],
  "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
  "toml": open(${jsonEncode('${tmp.path}/py.toml')}).read(),
  "hist": open(${jsonEncode('${tmp.path}/py.json')}).read(),
}))
''');
        final got = jsonDecode(res) as Map<String, Object?>;
        expect(got['replaced'], [
          'library.db',
          'config.toml',
          'recent_searches.json',
        ]);
        expect(got['manifest'], inspectBundle(out));
        expect(got['titles'], ['Song']);
        expect(got['integrity'], 'ok');
        expect(got['toml'], '# test config\n');
        expect(got['hist'], '["q"]');
      },
      skip: _python == null ? 'no python3' : false,
    );

    test('the port imports what Python exports, and its manifest is '
        "Python's", () async {
      final res = _py('''
import json
out = state_export.export_state(${jsonEncode(dbPath)}, ${jsonEncode(configPath)},
    None, ${jsonEncode('${tmp.path}/py')})
snap = state_export.export_state_snapshot(${jsonEncode(dbPath)},
    ${jsonEncode(configPath)}, ${jsonEncode('${tmp.path}/py')})
print(json.dumps([out, snap]))
''');
      final [out, snap] = (jsonDecode(res) as List).cast<String>();
      for (final b in [out, snap]) {
        final target = '${tmp.path}/dart.db';
        final r = importState(
          zipPath: b,
          dbPath: target,
          configPath: '${tmp.path}/dart.toml',
        );
        expect(r.replaced.keys, ['library.db', 'config.toml']);
        expect(_titles(target), ['Song']);
      }
      // The same manifest text, byte for byte, from the same contents.
      final z = ZipReader(out);
      final pyManifest = utf8.decode(z.read('manifest.json')!);
      z.close();
      final m = jsonDecode(pyManifest) as Map<String, Object?>;
      expect(const JsonEncoder.withIndent('  ').convert(m), pyManifest);
      expect(inspectBundle(snap)['auto_snapshot'], isTrue);
    }, skip: _python == null ? 'no python3' : false);
  });
}
