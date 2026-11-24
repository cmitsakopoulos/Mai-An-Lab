import 'py_num.dart';

/// Python's value semantics for code ported over JSON-shaped data (dicts as
/// `Map<String, Object?>`): truthiness, `a or b`, `str(x)` in f-strings, and
/// `str.format` with keyword arguments.

/// `bool(x)`: None, False, zero, and empty strings and containers are false.
bool pyTruthy(Object? v) => switch (v) {
  null => false,
  bool b => b,
  num n => n != 0,
  String s => s.isNotEmpty,
  Iterable<Object?> i => i.isNotEmpty,
  Map<Object?, Object?> m => m.isNotEmpty,
  _ => true,
};

/// `a or b or …`: the first truthy value, else the last one.
Object? pyOr(List<Object?> values) {
  for (final v in values) {
    if (pyTruthy(v)) return v;
  }
  return values.last;
}

/// `str(x)` as an f-string writes it, for the scalar types JSON carries.
String pyStr(Object? v) => switch (v) {
  null => 'None',
  bool b => b ? 'True' : 'False',
  int i => '$i',
  double d => pyFloatRepr(d),
  String s => s,
  _ => throw UnsupportedError('pyStr: ${v.runtimeType}'),
};

/// `template.format(**kwargs)` for templates whose only fields are
/// `{name}` (no format specs, no doubled braces).
String pyFormat(String template, Map<String, Object?> kwargs) =>
    template.replaceAllMapped(RegExp(r'\{(\w+)\}'), (m) {
      final key = m[1]!;
      if (!kwargs.containsKey(key)) throw ArgumentError('KeyError: $key');
      return pyStr(kwargs[key]);
    });

/// `dict.get(key)` on a value that should be a dict; null when it isn't.
Object? pyGet(Object? map, String key) =>
    map is Map<String, Object?> ? map[key] : null;

/// Python's `ValueError`, for the few messages the ported code shows.
class PyValueError implements Exception {
  PyValueError(this.message);
  final String message;

  @override
  String toString() => message;
}
