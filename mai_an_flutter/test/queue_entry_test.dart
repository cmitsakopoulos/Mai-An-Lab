import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/player/queue_entry.dart';

/// Reference values printed by Python: pathlib.Path(p).as_uri() and
/// utils/play_ledger.src_to_path. The URI is the MediaItem id, so it is what
/// play_ledger.jsonl and native_resume.json store; both apps must agree.
const _roundTrips = [
  (
    '/storage/emulated/0/Music/Sigur Rós/Ágætis byrjun/01 #.flac',
    'file:///storage/emulated/0/Music/Sigur%20R%C3%B3s/%C3%81g%C3%A6tis%20byrjun/01%20%23.flac',
  ),
  (
    "/m/(Live) it's #1 & a+b;c=d@e~f,!.flac",
    'file:///m/%28Live%29%20it%27s%20%231%20%26%20a%2Bb%3Bc%3Dd%40e~f%2C%21.flac',
  ),
  (
    '/m/日本語/曲 ?.mp3',
    'file:///m/%E6%97%A5%E6%9C%AC%E8%AA%9E/%E6%9B%B2%20%3F.mp3',
  ),
  ('/m/emoji 🎵.m4a', 'file:///m/emoji%20%F0%9F%8E%B5.m4a'),
  ('/m/100% pure.flac', 'file:///m/100%25%20pure.flac'),
];

void main() {
  test('pathToUri matches pathlib.as_uri and srcToPath inverts it', () {
    for (final (path, uri) in _roundTrips) {
      expect(pathToUri(path), uri, reason: path);
      expect(srcToPath(uri), path, reason: uri);
    }
  });

  test('srcToPath matches play_ledger.src_to_path on odd input', () {
    expect(srcToPath('file:///m/bad%zz%C3.flac'), '/m/bad%zz�.flac');
    expect(srcToPath('file://host/m/a%20b.flac'), '/m/a b.flac');
    expect(srcToPath('/plain/path'), '/plain/path');
    expect(srcToPath('https://x/y'), '');
    expect(srcToPath('file:///m/a.flac?x=1#f'), '/m/a.flac');
    expect(srcToPath(''), '');
  });

  test('network and relative sources pass through _to_uri unchanged', () {
    expect(pathToUri('https://cdn/x.flac'), 'https://cdn/x.flac');
    expect(pathToUri('file:///a'), 'file:///a');
    expect(pathToUri('rel/x.flac'), 'rel/x.flac');
    expect(pathToUri(''), '');
  });

  test('row fields fall back like the Flet engine', () {
    final bare = QueueEntry({'path': '/m/x/song.flac'});
    expect(bare.title, 'song.flac');
    expect(bare.artist, 'Unknown Artist');
    expect(bare.album, 'Unknown Album');
    expect(bare.duration, 0);
    // track.get("artist_name", default): present-but-empty stays empty.
    expect(QueueEntry({'path': '/a', 'artist_name': ''}).artist, '');
  });

  test('as_queued copies and drops the recommendation tag', () {
    final src = QueueEntry({'path': '/x', kAutoplay: true});
    final out = src.asQueued();
    expect(out.data, {'path': '/x', kQueued: true});
    expect(src.data, {'path': '/x', kAutoplay: true});
  });

  test('unknown keys survive (queue_state.json round trip)', () {
    final e = QueueEntry({'path': '/x', 'image_url': 'u', 'custom': 1});
    expect(e.asQueued().toJson()['custom'], 1);
  });
}
