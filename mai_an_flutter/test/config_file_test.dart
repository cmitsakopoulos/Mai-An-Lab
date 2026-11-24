// config.toml writes against Python's (PORT_MAP §0 rule 3): Settings' write
// sequences on the template and on a real config, and hand-written edge
// files, each step's whole file compared with what tomlkit 0.15.2 wrote.
//
//   ~/miniconda3/bin/python tool/export_config_goldens.py
//   flutter test test/config_file_test.dart
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/core/config_file.dart';

Map<String, Map<String, Object?>> _params(Object? p) => {
  for (final e in (p as Map<String, dynamic>).entries)
    e.key: (e.value as Map<String, dynamic>).cast<String, Object?>(),
};

void main() {
  final golden =
      jsonDecode(File('test/goldens/config_synthetic.json').readAsStringSync())
          as Map<String, dynamic>;

  void runCases(String group) {
    for (final c in (golden[group] as List).cast<Map<String, dynamic>>()) {
      test(c['name'] as String, () {
        var text = c['src'] as String;
        var i = 0;
        for (final step in (c['steps'] as List).cast<Map<String, dynamic>>()) {
          text = updateConfigText(text, _params(step['params']));
          expect(text, step['out'], reason: 'step $i: ${step['params']}');
          i++;
        }
      });
    }
  }

  group('Settings flows', () => runCases('flows'));
  group('edges', () => runCases('edges'));

  test('the bundled template is utils/config.toml', () {
    expect(
      File('assets/config_template.toml').readAsStringSync(),
      golden['template'],
    );
  });

  test('invalid TOML is refused, the file untouched', () async {
    final dir = Directory.systemTemp.createTempSync('config');
    addTearDown(() => dir.deleteSync(recursive: true));
    final store = ConfigStore(dir.path, template: () async => '');
    await File(store.path).create(recursive: true);
    await File(store.path).writeAsString('[a\nx = 1\n');
    expect(
      await store.update({
        'a': {'x': 2},
      }),
      isFalse,
    );
    expect(await File(store.path).readAsString(), '[a\nx = 1\n');
  });

  test('a missing file starts as the template', () async {
    final dir = Directory.systemTemp.createTempSync('config');
    addTearDown(() => dir.deleteSync(recursive: true));
    final template = golden['template'] as String;
    final store = ConfigStore(dir.path, template: () async => template);
    expect(
      await store.update({
        'general': {'library_sort': 'artist'},
      }),
      isTrue,
    );
    final text = await File(store.path).readAsString();
    expect(text, startsWith(template));
    expect(AppConfig.parse(text).librarySort.name, 'artist');
  });
}
