/// Port of `StreamripApp/utils/metadata_objects.py`.
class AlbumMetadata {
  const AlbumMetadata({
    required this.id,
    required this.album,
    required this.artist,
    required this.year,
    required this.tracktotal,
    required this.disctotal,
    this.genres,
    this.copyright,
  });

  final String id;
  final String album;
  final String artist;
  final String year;
  final int tracktotal;
  final int disctotal;
  final List<String>? genres;
  final String? copyright;

  String getGenres() =>
      genres != null && genres!.isNotEmpty ? genres!.join(', ') : '';

  String getCopyright() => copyright ?? '';
}

class TrackMetadata {
  const TrackMetadata({
    required this.id,
    required this.title,
    required this.artist,
    required this.album,
    required this.tracknumber,
    required this.discnumber,
    this.isrc,
    this.composer,
    this.lyrics,
  });

  final String id;
  final String title;
  final String artist;
  final AlbumMetadata album;
  final int tracknumber;
  final int discnumber;
  final String? isrc;
  final String? composer;
  final String? lyrics;
}
