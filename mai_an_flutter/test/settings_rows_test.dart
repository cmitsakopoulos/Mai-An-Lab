// Row-level Settings search: every catalogued row has its anchor in a
// page, and the matching.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/settings/settings_search.dart';

void main() {
  test('every row id has exactly one SettingsAnchor in lib/settings', () {
    final source = [
      for (final f in Directory('lib/settings').listSync())
        if (f is File && f.path.endsWith('.dart')) f.readAsStringSync(),
    ].join('\n');
    final ids = {for (final r in kSettingRows) r.id};
    expect(ids.length, kSettingRows.length, reason: 'ids are unique');
    for (final id in ids) {
      final n = RegExp(
        "SettingsAnchor\\(\\s*id: '${RegExp.escape(id)}'",
      ).allMatches(source).length;
      expect(n, 1, reason: id);
    }
  });

  test('rows match on every word, in title or keywords', () {
    List<String> ids(String q) => [for (final r in searchSettingRows(q)) r.id];
    expect(ids(''), isEmpty);
    expect(ids('e'), isEmpty);
    expect(ids('equalizer'), contains('audio.eq'));
    expect(ids('qobuz app id'), ['account.qobuz_keys']);
    expect(ids('flac'), ['storage.quality']);
    expect(ids('xyzzy'), isEmpty);
  });
}
