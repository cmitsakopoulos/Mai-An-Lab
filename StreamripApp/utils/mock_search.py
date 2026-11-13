"""Lightweight mock search package generator for offline/testing/preview modes.

Provides realistic tracks, albums, and artist metadata with high-res album covers,
bitrates, durations, and audio quality tags so UI layouts, typography, and animations
can be tested and evaluated visually without active API credentials.

Used by Search's opt-in demo mode (Settings → Appearance → Demo Search Results)
and the tests. Every record carries "demo": True, which is how the Search view
knows not to hand one to the downloader or the preview player.
"""

from __future__ import annotations

# High quality sample covers from Deezer public CDN
SAMPLE_COVERS = [
    "https://e-cdns-images.dzcdn.net/images/cover/6c651817c7dcfca5de51c72b22fbf6e8/250x250-000000-80-0-0.jpg",
    "https://e-cdns-images.dzcdn.net/images/cover/4d0a927952a251c6b12a87d0c75ce2cb/250x250-000000-80-0-0.jpg",
    "https://e-cdns-images.dzcdn.net/images/cover/2c55b6a3ce91bf6eb3a7c6422b31a5eb/250x250-000000-80-0-0.jpg",
    "https://e-cdns-images.dzcdn.net/images/cover/b41a9ce6e9491d904c0dfd535b757e7d/250x250-000000-80-0-0.jpg",
    "https://e-cdns-images.dzcdn.net/images/cover/1ef2e864be735fec42e3184ec6be91e7/250x250-000000-80-0-0.jpg",
    "https://e-cdns-images.dzcdn.net/images/cover/cb72bf27d2cce77f724490f8983995f7/250x250-000000-80-0-0.jpg",
]

SAMPLE_ARTIST_AVATAR = "https://e-cdns-images.dzcdn.net/images/artist/9c2d7faad7dfa50b7ef11ef7ff419cf9/250x250-000000-80-0-0.jpg"


def get_mock_search_results(query: str, source: str = "qobuz") -> list[dict]:
    """Generates a rich, realistic search package matching the query term."""
    q = (query or "").strip()
    if not q:
        return []

    q_title = q.title()
    source_lower = (source or "qobuz").lower()

    # Tracks
    track_names = [
        f"{q_title}",
        "Archangel",
        "Ghost Hardware",
        "Near Dark",
        "Rival Dealer",
    ]
    formats = [
        ("FLAC 24-bit/96kHz", 96000),
        ("FLAC 24-bit/44.1kHz", 44100),
        ("FLAC 16-bit/44.1kHz", 44100),
        ("MP3 320kbps", 320),
        ("FLAC 24-bit/192kHz", 192000),
    ]

    tracks = []
    for i, name in enumerate(track_names, start=1):
        fmt, bitrate = formats[(i - 1) % len(formats)]
        tracks.append({
            "id": f"mock_t_{i}",
            "media_type": "track",
            "title": name,
            "name": name,
            "ui_title": name,
            "artist": q_title,
            "ui_subtitle": q_title,
            "album": f"{q_title} (Selected Works)",
            "ui_detail": fmt,
            "duration": 195 + (i * 35),
            "cover": SAMPLE_COVERS[(i - 1) % len(SAMPLE_COVERS)],
            "source": source_lower,
            "format": fmt,
            "bitrate": bitrate,
            "year": "2023",
            "track_number": i,
            "is_in_library": (i == 1),
        })

    # Albums
    album_descriptors = [
        ("Untrue", "2007", 13),
        ("Antidawn", "2022", 5),
        ("Rival Dealer", "2013", 3),
        ("Kindred", "2012", 3),
        ("Street Halo", "2011", 3),
        ("Tunes 2011 to 2019", "2019", 17),
    ]

    albums = []
    for i, (title, year, count) in enumerate(album_descriptors, start=1):
        albums.append({
            "id": f"mock_a_{i}",
            "media_type": "album",
            "title": f"{q_title} - {title}",
            "name": f"{q_title} - {title}",
            "ui_title": f"{title}",
            "artist": q_title,
            "ui_subtitle": f"{q_title} · {year}",
            "ui_detail": f"{count} tracks · Hi-Res",
            "year": year,
            "cover": SAMPLE_COVERS[(i - 1) % len(SAMPLE_COVERS)],
            "source": source_lower,
            "num_tracks": count,
            "type": "album" if count > 4 else "ep",
            "is_in_library": (i == 1),
            "owned_tracks": 13 if i == 1 else 0,
        })

    # Artists
    artists = [
        {
            "id": "mock_art_1",
            "media_type": "artist",
            "name": q_title,
            "ui_title": q_title,
            "artist": q_title,
            "ui_subtitle": "Electronic · Ambient Dubstep",
            "ui_detail": "14 Albums · 128 Tracks",
            "cover": SAMPLE_ARTIST_AVATAR,
            "source": source_lower,
            "num_albums": 14,
            "num_tracks": 128,
            "genres": "Electronic, Ambient, Dubstep",
            "is_in_library": False,
            "owned_tracks": 13,
        },
        {
            "id": "mock_art_2",
            "media_type": "artist",
            "name": f"{q_title} & Four Tet",
            "ui_title": f"{q_title} & Four Tet",
            "artist": f"{q_title} & Four Tet",
            "ui_subtitle": "Collaboration",
            "ui_detail": "3 Singles",
            "cover": SAMPLE_COVERS[2],
            "source": source_lower,
            "num_albums": 3,
            "num_tracks": 6,
            "genres": "Electronic",
            "is_in_library": False,
            "owned_tracks": 0,
        }
    ]

    results = tracks + albums + artists
    for r in results:
        r["demo"] = True
    return results


def get_mock_children(record: dict) -> list[dict]:
    """What expanding a demo artist or album row shows: the artist's albums,
    or the album's tracks. Anything else has no children."""
    m_type = record.get("media_type")
    name = record.get("artist") or record.get("name") or ""
    results = get_mock_search_results(name, source=record.get("source") or "qobuz")
    if m_type == "artist":
        return [r for r in results if r["media_type"] == "album"]
    if m_type == "album":
        return [r for r in results if r["media_type"] == "track"]
    return []

