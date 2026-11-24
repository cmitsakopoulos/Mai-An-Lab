import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/app_version.dart';

void main() {
  test('kAppVersion and kAppBuildNumber match pubspec.yaml', () {
    final pubspec = File('pubspec.yaml').readAsStringSync();
    final match = RegExp(
      r'^version:\s*([^\s#]+)',
      multiLine: true,
    ).firstMatch(pubspec);
    expect(match, isNotNull, reason: 'pubspec.yaml must declare version:');
    expect(match!.group(1), '$kAppVersion+$kAppBuildNumber');
  });

  test('version follows SemVer and stays above Flet versionCode 1', () {
    expect(
      RegExp(r'^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$').hasMatch(kAppVersion),
      isTrue,
      reason: 'kAppVersion must be valid SemVer 2.0.0',
    );
    expect(
      kAppBuildNumber,
      greaterThan(1),
      reason: 'Android versionCode must exceed Flet (1) for in-place cutover',
    );
  });
}
