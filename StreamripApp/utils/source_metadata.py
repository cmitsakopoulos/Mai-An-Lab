"""Per-source normalization of raw API payloads.

Qobuz and Deezer describe the same concepts with different field names and
different container shapes, e.g. an album's track list is
``album["tracks"]["items"]`` on Qobuz but a plain ``album["tracks"]`` list on
Deezer; disc number is ``media_number`` vs ``disk_number``; cover art is a
nested ``image`` dict vs flat ``cover_*`` keys.

The download pipeline in ``streamrip_api`` used to read Qobuz field names
inline. Rather than thread ``if source == ...`` through the tagging loop, each
source maps its payload onto one common shape here. The Qobuz branch reproduces
the previous inline expressions exactly, including its fallbacks, so its output
is unchanged.
"""
from typing import Any, Optional

# What every adapter must return for a track. Album fields are nested under
# "album" and mirror AlbumMetadata's constructor arguments, plus "cover_url".
#   id, title, artist, track_number, disc_number, isrc, composer,
#   album: {id, album, artist, year, tracktotal, disctotal, genres,
#           copyright, cover_url}


def extract_tracks(source: str, media_type: str, meta: dict) -> list[dict]:
    """Pull the track dicts out of a metadata payload, attaching album context.

    Returns tracks with an ``album`` key populated, which is what the tagging
    step expects. ``artist`` payloads are not handled here: they need further
    per-album API calls and stay in the caller.
    """
    if source == "deezer":
        if media_type == "track":
            return [meta]
        # get_album/get_playlist on the Deezer client both flatten the paged
        # response into a plain "tracks" list.
        tracks = meta.get("tracks", [])
        if media_type == "album":
            for t in tracks:
                t.setdefault("album", meta)
        return list(tracks)

    # qobuz
    if media_type == "track":
        return [meta]
    tracks = meta.get("tracks", {}).get("items", [])
    if media_type == "album":
        for t in tracks:
            if "album" not in t:
                t["album"] = meta
    return list(tracks)


def normalize_track(source: str, track_meta: dict, fallback_number: int) -> dict:
    """Map one raw track payload onto the common shape described above."""
    if source == "deezer":
        return _normalize_deezer(track_meta, fallback_number)
    return _normalize_qobuz(track_meta, fallback_number)


def _normalize_qobuz(track_meta: dict, fallback_number: int) -> dict:
    album = track_meta.get("album", {}) or {}

    image_raw = album.get("image")
    cover_url = ""
    if isinstance(image_raw, str):
        cover_url = image_raw
    elif isinstance(image_raw, dict):
        cover_url = (
            image_raw.get("large")
            or image_raw.get("extralarge")
            or image_raw.get("medium")
            or image_raw.get("small")
            or ""
        )

    artist_dict = album.get("artist", {})
    album_artist = (
        artist_dict.get("name", "Unknown Artist")
        if isinstance(artist_dict, dict)
        else str(artist_dict)
    )

    genre_data = album.get("genre", {})
    genres = (
        [g.get("name") for g in genre_data.get("path", []) if isinstance(g, dict)]
        if isinstance(genre_data, dict)
        else []
    )

    track_artist = (
        track_meta.get("performer", {}).get("name")
        or track_meta.get("artist", {}).get("name")
        or "Unknown Artist"
    )
    composer_dict = track_meta.get("composer", {})

    return {
        "id": str(track_meta.get("id", "")),
        "title": track_meta.get("title", "Unknown"),
        "artist": track_artist,
        "track_number": track_meta.get("track_number", fallback_number),
        "disc_number": track_meta.get("media_number", 1),
        "isrc": track_meta.get("isrc"),
        "composer": composer_dict.get("name")
        if isinstance(composer_dict, dict)
        else None,
        "album": {
            "id": str(album.get("id", "")),
            "album": album.get("title", "Unknown Album"),
            "artist": album_artist,
            "year": str(album.get("release_date", "")).split("-")[0][:4],
            "tracktotal": album.get("tracks_count", 1),
            "disctotal": album.get("media_count", 1),
            "genres": genres if genres else None,
            "copyright": album.get("copyright"),
            "cover_url": cover_url,
        },
    }


def _normalize_deezer(track_meta: dict, fallback_number: int) -> dict:
    album = track_meta.get("album", {}) or {}

    # Deezer exposes flat cover fields, largest first.
    cover_url = (
        album.get("cover_xl")
        or album.get("cover_big")
        or album.get("cover_medium")
        or album.get("cover")
        or ""
    )

    album_artist = _deezer_artist_name(album.get("artist")) or _deezer_artist_name(
        track_meta.get("artist")
    ) or "Unknown Artist"

    genre_data = album.get("genres", {})
    genres = (
        [g.get("name") for g in genre_data.get("data", []) if isinstance(g, dict)]
        if isinstance(genre_data, dict)
        else []
    )

    # Deezer has no disc-count field. Infer it from the track list the client
    # attached, so multi-disc releases still tag a correct total.
    disctotal = 1
    album_tracks = album.get("tracks")
    if isinstance(album_tracks, list) and album_tracks:
        disc_numbers = [
            t.get("disk_number", 1) for t in album_tracks if isinstance(t, dict)
        ]
        if disc_numbers:
            disctotal = max(disctotal, max(disc_numbers))

    track_artist = (
        _deezer_artist_name(track_meta.get("artist"))
        or _deezer_contributor_name(track_meta)
        or "Unknown Artist"
    )

    return {
        "id": str(track_meta.get("id", "")),
        "title": track_meta.get("title", "Unknown"),
        "artist": track_artist,
        "track_number": track_meta.get("track_position", fallback_number),
        "disc_number": track_meta.get("disk_number", 1),
        "isrc": track_meta.get("isrc"),
        # Deezer's public API exposes no composer field.
        "composer": None,
        "album": {
            "id": str(album.get("id", "")),
            "album": album.get("title", "Unknown Album"),
            "artist": album_artist,
            "year": str(album.get("release_date", "")).split("-")[0][:4],
            # track_total is set by DeezerClient; nb_tracks is the API's own name.
            "tracktotal": album.get("track_total") or album.get("nb_tracks", 1),
            "disctotal": disctotal,
            "genres": genres if genres else None,
            # Not provided by the Deezer API.
            "copyright": None,
            "cover_url": cover_url,
        },
    }


def _deezer_artist_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("name")
    if isinstance(value, str) and value:
        return value
    return None


def _deezer_contributor_name(track_meta: dict) -> Optional[str]:
    contributors = track_meta.get("contributors")
    if isinstance(contributors, list):
        for c in contributors:
            if isinstance(c, dict) and c.get("name"):
                return c["name"]
    return None
