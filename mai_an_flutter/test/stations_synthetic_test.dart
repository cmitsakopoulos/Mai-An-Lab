// Parity with the Python station code over synthetic inputs. The reference
// values are in goldens/stations_synthetic.json, written by
// tool/export_station_goldens.py from the Flet sources.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/stations/artist_graph.dart';
import 'package:mai_an_lab/stations/credit_keys.dart';
import 'package:mai_an_lab/stations/genre_taxonomy.dart';

final Map<String, dynamic> golden =
    jsonDecode(File('test/goldens/stations_synthetic.json').readAsStringSync())
        as Map<String, dynamic>;

List<String> _strs(Object? l) => [for (final x in l! as List) x as String];

void main() {
  test('taxonomy', () {
    for (final c in golden['taxonomy'] as List) {
      final tag = c['tag'] as String;
      expect(genreBucket(tag), c['bucket'], reason: tag);
      expect(
        genreTokens(tag).toList()..sort(),
        _strs(c['tokens']),
        reason: tag,
      );
      expect(isRegionalTag(tag), c['regional'], reason: tag);
      expect(genreDisplayLabel(tag), c['display'], reason: tag);
      expect(tagCulture(tag), c['culture'], reason: tag);
      expect(isCultureWord(tag), c['culture_word'], reason: tag);
    }
    for (final c in golden['script_culture'] as List) {
      expect(
        scriptCulture([c['text'] as String]),
        c['culture'],
        reason: '${c['text']}',
      );
    }
  });

  test('credit keys', () {
    for (final c in golden['credit_keys'] as List) {
      final name = c['name'] as String;
      expect(splitArtistCredits(name), _strs(c['split']), reason: name);
      expect(creditKeys(name).toList()..sort(), _strs(c['keys']), reason: name);
    }
  });

  test('artist culture', () {
    for (final c in golden['artist_culture'] as List) {
      expect(
        artistCulture(c['country'] as String?, _strs(c['genres']), [
          for (final n in c['names'] as List) n as String?,
        ]),
        c['culture'],
        reason: '$c',
      );
    }
  });

  test('tag similarity', () {
    final ts = golden['tag_similarity'] as Map<String, dynamic>;
    final tags = _strs(ts['tags']);
    final npmi = {
      for (final e in (ts['npmi'] as Map).entries)
        e.key as String: (e.value as num).toDouble(),
    };
    void check(Float32List s, List<Object?> expected) {
      for (var i = 0; i < tags.length; i++) {
        for (var j = 0; j < tags.length; j++) {
          expect(
            s[i * tags.length + j],
            closeTo((expected[i] as List)[j] as num, 1e-6),
            reason: '${tags[i]} ~ ${tags[j]}',
          );
        }
      }
    }

    check(tagSimilarity(tags, npmi), ts['matrix'] as List);
    check(tagSimilarity(tags), ts['matrix_no_npmi'] as List);
  });

  for (final name in ['library', 'library_no_tags']) {
    group(name, () {
      final lib = golden[name] as Map<String, dynamic>;
      final meta = [
        for (final m in lib['meta'] as List)
          TrackMeta(
            artist: m['artist'] as String?,
            credit: m['credit'] as String?,
            genres: _strs(m['genres']),
            country: m['country'] as String?,
            manual: m['manual'] as bool,
            names: [for (final n in m['names'] as List) n as String?],
          ),
      ];
      final rows = lib['X'] as List;
      final dim = (rows.first as List).length;
      final x = Float32List.fromList([
        for (final r in rows)
          for (final v in r as List) (v as num).toDouble(),
      ]);
      final npmi = {
        for (final e in (lib['npmi'] as Map).entries)
          e.key as String: (e.value as num).toDouble(),
      };
      ArtistGraph build() =>
          ArtistGraph(x, dim, meta, npmi: npmi, creditKeys: creditKeys);
      final want = lib['graph'] as Map<String, dynamic>;

      test('graph structure', () {
        final g = build();
        expect(g.units, _strs(want['units']));
        expect(g.unitOf.toList(), want['unit_of']);
        expect(g.culture, want['culture']);
        expect(g.inferred, want['inferred']);
        expect(g.namesake, want['namesake']);
        expect(g.hasTags, want['has_tags']);
        expect(g.tags, _strs(want['tags']));
        Map<String, List<int>> links(List<Set<int>> l) => {
          for (var u = 0; u < l.length; u++)
            if (l[u].isNotEmpty) '$u': l[u].toList()..sort(),
        };
        expect(links(g.sameAct), want['same_act']);
        expect(links(g.collab), want['collab']);
        expect(
          g.flavour(List.generate(g.length, (i) => i)),
          want['flavour_all'],
        );
      });

      test('profiles and affinity rows', () {
        final g = build();
        final t = g.tags.length;
        final w = want['W'] as List;
        for (var u = 0; u < g.length; u++) {
          for (var c = 0; c < t; c++) {
            expect(
              g.w[u * t + c],
              closeTo((w[u] as List)[c] as num, 1e-5),
              reason: 'W[$u, ${g.tags[c]}]',
            );
          }
        }
        final r = want['rows'] as List;
        for (var u = 0; u < g.length; u++) {
          final row = g.row(u);
          for (var v = 0; v < g.length; v++) {
            expect(
              row[v],
              closeTo((r[u] as List)[v] as num, 1e-5),
              reason: 'row($u)[$v] ${g.units[u]} ~ ${g.units[v]}',
            );
          }
        }
      });

      test('drift runs', () {
        final bpm = Float64List.fromList([
          for (final v in lib['bpm'] as List) (v as num).toDouble(),
        ]);
        final energy = Float64List.fromList([
          for (final v in lib['energy'] as List) (v as num).toDouble(),
        ]);
        final keys = [for (final m in meta) creditKeys(m.credit)];
        final albums = [
          for (final m in lib['meta'] as List) m['album'] as String?,
        ];
        for (final run in lib['runs'] as List) {
          final caps = run['caps'] as List;
          final d = Drift(
            build(),
            x,
            run['seed'] as int,
            artistKeys: keys,
            albumKeys: albums,
            caps: RollingCaps(
              maxPerArtist: caps[0] as int,
              maxPerAlbum: caps[1] as int,
              window: caps[2] as int,
            ),
            focused: run['focused'] as bool,
            bpm: bpm,
            energy: energy,
          );
          final label =
              'seed ${run['seed']} focused ${run['focused']} caps $caps';
          for (final s in run['steps'] as List) {
            switch (s['op']) {
              case 'take':
                expect(d.take(s['n'] as int), s['out'], reason: label);
                expect(d.flavour(), s['flavour'], reason: label);
              case 'accept':
                d.accept(s['idx'] as int);
              case 'reject':
                d.reject([for (final i in s['idx'] as List) i as int]);
              case 'exclude':
                d.exclude([for (final i in s['idx'] as List) i as int]);
            }
          }
        }
      });
    });
  }
}
