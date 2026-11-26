import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/library/track_query.dart';
import 'package:sqlite3/sqlite3.dart';

import 'fixtures.dart';

void main() {
  late Directory dir;
  late String path;

  setUp(() {
    dir = Directory.systemTemp.createTempSync('mai_an_browse');
    path = '${dir.path}/library.db';
    final db = sqlite3.open(path);
    createLibrarySchema(db);
    db.execute('''
      INSERT INTO artists (id, name, album_count, track_count) VALUES
        (1, 'Björk', 1, 1), (2, 'Radiohead', 2, 3);
      INSERT INTO albums (id, artist_id, title, genre, genre_bucket, track_count) VALUES
        (1, 1, 'Post', 'Electronic', 'Electronic,Pop', 1),
        (2, 2, 'OK Computer', 'Alternative Rock', 'Alt,Rock', 2),
        (3, 2, 'Kid A', 'Electronic', 'Electronic', 1);
      INSERT INTO tracks (id, album_id, title, track_num, path, added_date) VALUES
        (1, 1, 'Army of Me', 1, '/m/1', 100),
        (2, 2, 'Airbag', 1, '/m/2', 300),
        (3, 2, 'Let Down', 5, '/m/3', 200),
        (4, 3, 'Idioteque', 8, '/m/4', 50);
    ''');
    db.close();
  });

  tearDown(() => dir.deleteSync(recursive: true));

  Future<(LibraryController, BrowseController)> loaded(LibraryView v) async {
    final lib = LibraryController(() => LibraryRepository.open(path));
    final b = BrowseController(lib, views: LibraryView.values);
    await lib.load();
    b.setView(v);
    await pumpEventQueue();
    return (lib, b);
  }

  List<String> labels(BrowseController b) => [
    for (final n in b.rows)
      switch (n) {
        GenreNode(:final genre) => 'g:${genre.name}',
        ArtistNode(:final artist) => '${'  ' * n.depth}a:${artist.name}',
        AlbumNode(:final album) => '${'  ' * n.depth}al:${album.title}',
        TrackNode(:final track) => '${'  ' * n.depth}t:${track.title}',
        PlaylistNode(:final playlist) => 'p:${playlist.name}',
        EmptyPlaylistNode() => '${'  ' * n.depth}(empty)',
        NewPlaylistNode() => '+new',
      },
  ];

  test('config: bar order, defaults, initial view', () {
    expect(LibraryView.fromConfig(const LibraryTabs().visible), [
      LibraryView.playlists,
      LibraryView.genres,
      LibraryView.artists,
      LibraryView.tracks,
    ]);
    final all = LibraryTabs.fromToml({
      'show_albums': 'yes',
      'show_tracks': false,
      'show_network': true,
      'pill_style': 'Unified',
    });
    expect(all.visible, ['playlists', 'genres', 'artists', 'albums']);
    expect(all.unified, isTrue);
    final views = LibraryView.fromConfig(all.visible);
    expect(views, [
      LibraryView.playlists,
      LibraryView.genres,
      LibraryView.artists,
      LibraryView.albums,
    ]);
    // library.py: tracks, then albums, artists, genres, playlists.
    expect(LibraryView.initial(views), LibraryView.albums);
    expect(LibraryView.initial([LibraryView.playlists]), LibraryView.playlists);
    expect(LibraryView.fromConfig(['network']), [LibraryView.tracks]);
  });

  test(
    'albums follow the tracks sort; newest album first by default',
    () async {
      final (lib, b) = await loaded(LibraryView.albums);
      expect(labels(b), ['al:OK Computer', 'al:Post', 'al:Kid A']);
      lib.setSort(LibrarySort.album);
      await pumpEventQueue();
      expect(labels(b), ['al:Kid A', 'al:OK Computer', 'al:Post']);
      expect((b.shown, b.total), (3, 3));
      b.dispose();
      lib.dispose();
    },
  );

  test('genres open onto artists, albums in that genre, tracks', () async {
    final (lib, b) = await loaded(LibraryView.genres);
    expect(labels(b), ['g:Alt', 'g:Electronic', 'g:Pop', 'g:Rock']);
    final electronic = b.rows[1];
    await b.toggle(electronic);
    expect(labels(b), [
      'g:Alt',
      'g:Electronic',
      '  a:Björk',
      '  a:Radiohead',
      'g:Pop',
      'g:Rock', //
    ]);
    // Under a genre, an artist shows only that genre's albums.
    await b.toggle(b.rows[3]);
    expect(labels(b).sublist(3, 5), ['  a:Radiohead', '    al:Kid A']);
    await b.toggle(b.rows[4]);
    expect(labels(b)[5], '      t:Idioteque');
    final t = b.rows[5] as TrackNode;
    expect(t.context.map((t) => t.title), ['Idioteque']);
    // Closing the genre closes everything under it.
    await b.toggle(electronic);
    await b.toggle(electronic);
    expect(labels(b).sublist(1, 4), [
      'g:Electronic',
      '  a:Björk',
      '  a:Radiohead',
    ]);
    b.dispose();
    lib.dispose();
  });

  test('album tracks in disc order; a search collapses the tree', () async {
    final (lib, b) = await loaded(LibraryView.artists);
    expect(labels(b), ['a:Björk', 'a:Radiohead']);
    await b.toggle(b.rows[1]);
    await b.toggle(b.rows[2]); // Kid A (title order)
    await b.toggle(b.rows[4]); // OK Computer
    expect(labels(b), [
      'a:Björk', 'a:Radiohead', '  al:Kid A', '    t:Idioteque', //
      '  al:OK Computer', '    t:Airbag', '    t:Let Down',
    ]);
    lib.setQuery('radio');
    await pumpEventQueue();
    expect(labels(b), ['a:Radiohead']);
    b.dispose();
    lib.dispose();
  });

  test('artist and album search match through their tracks', () async {
    final (lib, b) = await loaded(LibraryView.artists);
    lib.setQuery('idiot');
    await pumpEventQueue();
    expect(labels(b), ['a:Radiohead']);
    expect(b.closestMatch, isFalse);
    b.setView(LibraryView.albums);
    await pumpEventQueue();
    expect(labels(b), ['al:Kid A']);
    lib.setQuery('bjork');
    await pumpEventQueue();
    expect(labels(b), ['al:Post']);
    lib.setQuery('Komputer');
    await pumpEventQueue();
    expect(b.closestMatch, isTrue);
    expect(labels(b).first, 'al:OK Computer');
    b.dispose();
    lib.dispose();
  });

  test('genre sort by tracks, ties by name; genre play queue', () async {
    final (lib, b) = await loaded(LibraryView.genres);
    b.setGenreSort(GenreSort.tracks);
    await pumpEventQueue();
    expect(labels(b), ['g:Alt', 'g:Electronic', 'g:Rock', 'g:Pop']);
    final tracks = await b.genreTracks('Electronic');
    expect(tracks.map((t) => t.title), ['Army of Me', 'Idioteque']);
    b.dispose();
    lib.dispose();
  });

  test(
    'playlists: create, open, add, reorder, remove, rename, delete',
    () async {
      final lib = LibraryController(
        () => LibraryRepository.open(path, writable: true),
      );
      final b = BrowseController(lib, views: LibraryView.values);
      await lib.load();
      b.setView(LibraryView.playlists);
      await pumpEventQueue();
      expect(labels(b), ['+new']);

      var (id, err) = await b.createPlaylist('  Mix ', paths: ['/m/2', '/m/1']);
      expect(err, isNull);
      expect(labels(b), ['p:Mix', '+new']);
      final (_, clash) = await b.createPlaylist('MIX');
      expect(clash, 'UNIQUE constraint failed: playlists.name');

      await b.toggle(b.rows.first);
      expect(labels(b), ['p:Mix', '  t:Airbag', '  t:Army of Me', '+new']);
      expect(await b.addToPlaylist(id!, ['/m/4', '/m/2']), isNull);
      expect(labels(b), [
        'p:Mix',
        '  t:Airbag',
        '  t:Army of Me',
        '  t:Idioteque',
        '+new',
      ]);
      expect((b.rows.first as PlaylistNode).playlist.trackCount, 3);

      // Last to first; the play context follows the new order.
      expect(await b.movePlaylistTrack(id, 2, 0), isNull);
      expect(labels(b).sublist(1, 4), [
        '  t:Idioteque',
        '  t:Airbag',
        '  t:Army of Me',
      ]);
      expect((b.rows[2] as TrackNode).context.map((t) => t.path), [
        '/m/4',
        '/m/2',
        '/m/1',
      ]);
      final repo = lib.repository!;
      expect((await repo.tracksInPlaylist(id)).map((t) => t.path), [
        '/m/4',
        '/m/2',
        '/m/1',
      ]);

      for (final p in ['/m/4', '/m/2', '/m/1']) {
        expect(await b.removeFromPlaylist(id, p), isNull);
      }
      expect(labels(b), ['p:Mix', '  (empty)', '+new']);
      expect((b.rows.first as PlaylistNode).playlist.trackCount, 0);

      expect(
        await b.updatePlaylist(id, name: 'Road', color: '#FF5555'),
        isNull,
      );
      expect(labels(b).first, 'p:Road');
      expect((b.rows.first as PlaylistNode).playlist.color, '#FF5555');

      lib.setQuery('rod');
      await pumpEventQueue();
      expect(labels(b), ['p:Road']); // closest match; no create row
      expect(b.closestMatch, isTrue);
      lib.setQuery('');
      await pumpEventQueue();

      expect(await b.deletePlaylist(id), isNull);
      expect(labels(b), ['+new']);
      b.dispose();
      lib.dispose();
    },
  );
}
