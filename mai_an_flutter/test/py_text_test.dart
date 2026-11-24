// Python string primitives, pinned to values CPython printed.
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/py_text.dart';

void main() {
  test('str.title applies Final_Sigma', () {
    const cases = {
      'ΟΔΟΣ': 'Οδος',
      'ΟΔΟΣ ΟΔΟΣ': 'Οδος Οδος',
      'ΣΑΣ.': 'Σας.',
      'Σ': 'Σ',
      'aΣb': 'Aσb',
      'ΑΣ-ΒΣ': 'Ας-Βς',
      "ΟΔΟΣ'Σ": "Οδοσ'Σ",
      'ΑΣ1': 'Ας1',
      'hip hop': 'Hip Hop',
      "rock'n'roll": "Rock'N'Roll",
    };
    cases.forEach((s, want) => expect(pyTitle(s), want, reason: s));
  });

  test('str.lower applies Final_Sigma', () {
    const cases = {'ΟΔΟΣ': 'οδος', "ΟΔΟΣ'Σ": "οδοσ'ς", 'ΑΣ1': 'ας1', 'Σ': 'σ'};
    cases.forEach((s, want) => expect(pyLower(s), want, reason: s));
  });
}
