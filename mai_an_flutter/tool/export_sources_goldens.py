#!/usr/bin/env python3
"""Golden values for Phase 6b source clients (PORT_MAP rule 3):
  - Deezer Blowfish key derivation, Blowfish-CBC chunk decryption, AES-128-ECB
    fallback URL encryption (against pycryptodomex)
  - Qobuz OrpheusDL MD5 API signing, getFileUrl signing, and QobuzSpoofer bundle parsing
  - streamrip_api._detect_type_and_id URL parsing
  - source_metadata.extract_tracks & normalize_track (Qobuz & Deezer)
  - search_results.SearchResults & StreamripSearcher._parse_results / _humanize_search_error

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_sources_goldens.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(HERE, ".pydeps"))
sys.path.insert(1, os.path.join(REPO, "StreamripApp"))

from utils.deezer import DeezerClient  # noqa: E402
from utils.downloadable import DeezerDownloadable  # noqa: E402
from utils.exceptions import InvalidAppSecretError  # noqa: E402
from utils.qobuz import QobuzClient, QobuzSpoofer  # noqa: E402
from utils.search_results import SearchResults  # noqa: E402
from utils.source_metadata import extract_tracks, normalize_track  # noqa: E402
from utils.streamrip_api import _detect_type_and_id  # noqa: E402
from utils.streamrip_search import StreamripSearcher  # noqa: E402


def _qobuz_sign_api(endpoint: str, params: dict, unix_ts: str, secret: str) -> str:
    to_hash = endpoint.replace("/", "")
    for key in sorted(params.keys()):
        if key not in ("app_id", "user_auth_token"):
            to_hash += key + str(params[key])
    to_hash += unix_ts + secret
    return hashlib.md5(to_hash.encode("utf-8")).hexdigest()


def _qobuz_sign_file_url(track_id: str, quality: int, unix_ts: str, secret: str) -> tuple[int, str]:
    fmt = QobuzClient.get_quality(quality)
    raw = f"trackgetFileUrlformat_id{fmt}intentstreamtrack_id{track_id}{unix_ts}{secret}"
    return fmt, hashlib.md5(raw.encode("utf-8")).hexdigest()


def main() -> None:
    # 1. Deezer Blowfish & AES goldens
    track_ids = ["3135556", "1234567890", "42", "999999999999"]
    bf_keys = {tid: DeezerDownloadable._generate_blowfish_key(tid).hex() for tid in track_ids}

    # Deterministic 2048-byte ciphertext pattern
    cipher_2048 = bytes(((i * 37 + 13) & 0xFF) for i in range(2048))
    bf_decrypts = []
    for tid in ["3135556", "42"]:
        key = DeezerDownloadable._generate_blowfish_key(tid)
        pt = DeezerDownloadable._decrypt_chunk(key, cipher_2048)
        bf_decrypts.append({
            "track_id": tid,
            "cipher_hex": cipher_2048.hex(),
            "plain_hex": pt.hex(),
        })

    dz_stub = object.__new__(DeezerClient)
    aes_urls = []
    for tid, md5_origin, media_ver in [
        ("3135556", "0123456789abcdef0123456789abcdef", "1"),
        ("987654321", "fedcba9876543210fedcba9876543210", "5"),
    ]:
        aes_urls.append({
            "track_id": tid,
            "md5_origin": md5_origin,
            "media_version": media_ver,
            "url": dz_stub._get_encrypted_file_url(tid, md5_origin, media_ver),
        })

    # 2. Qobuz signing + Spoofer goldens
    secret = "e79f8b9be485692b0e5f9dd895826368"
    qobuz_api_sigs = []
    for ep, params, ts in [
        ("catalog/search", {"query": "boards of canada", "type": "albums", "limit": "50", "offset": "0", "app_id": "312369995"}, "1700000000"),
        ("album/get", {"album_id": "0886443927087", "limit": "500", "offset": "0", "app_id": "312369995"}, "1700000123"),
        ("user/get", {"user_auth_token": "tok_secret", "app_id": "312369995"}, "1700000999"),
    ]:
        qobuz_api_sigs.append({
            "endpoint": ep,
            "params": params,
            "unix_ts": ts,
            "secret": secret,
            "sig": _qobuz_sign_api(ep, params, ts, secret),
        })

    qobuz_file_sigs = []
    for q in [1, 2, 3, 4]:
        fmt, sig = _qobuz_sign_file_url("19512574", q, "1700000500.25", secret)
        qobuz_file_sigs.append({
            "track_id": "19512574",
            "quality": q,
            "format_id": fmt,
            "unix_ts": "1700000500.25",
            "secret": secret,
            "sig": sig,
        })

    # Synthetic bundle.js fixture matching QobuzSpoofer regexes
    s1_raw = base64.standard_b64encode(b"secret_berlin_32bytes_padding_01").decode("ascii") + ("A" * 44)
    s2_raw = base64.standard_b64encode(b"secret_london_32bytes_padding_02").decode("ascii") + ("B" * 44)
    bundle_js = (
        'foo={production:{api:{appId:"312369995",appSecret:"0123456789abcdef0123456789abcdef"}}};'
        f'd.initialSeed("{s1_raw[:16]}",window.utimezone.berlin);'
        f'd.initialSeed("{s2_raw[:16]}",window.utimezone.london);'
        f'name:"Europe/Berlin",info:"{s1_raw[16:40]}",extras:"{s1_raw[40:]}";'
        f'name:"Europe/London",info:"{s2_raw[16:40]}",extras:"{s2_raw[40:]}"'
    )
    spoofer = QobuzSpoofer()
    # Run the exact spoofer regex parsing logic on bundle_js
    import re
    from collections import OrderedDict
    app_match = re.search(spoofer.app_id_regex, bundle_js)
    spoofer_app_id = str(app_match.group("app_id"))
    secrets_od = OrderedDict()
    for m in re.finditer(spoofer.seed_timezone_regex, bundle_js):
        seed, tz = m.group("seed", "timezone")
        secrets_od[tz] = [seed]
    kp = list(secrets_od.items())
    if len(kp) > 1:
        secrets_od.move_to_end(kp[1][0], last=False)
    ie_re = spoofer.info_extras_regex.format(timezones="|".join(tz.capitalize() for tz in secrets_od))
    for m in re.finditer(ie_re, bundle_js):
        tz, info, extras = m.group("timezone", "info", "extras")
        secrets_od[tz.lower()] += [info, extras]
    spoofer_secrets = [
        base64.standard_b64decode("".join(v)[:-44]).decode("utf-8")
        for v in secrets_od.values()
    ]

    # 3. URL detection goldens
    urls = [
        "https://www.qobuz.com/track/19512574",
        "https://www.qobuz.com/us-en/album/random-access-memories-daft-punk/0886443927087",
        "https://open.qobuz.com/album/0886443927087?foo=bar#frag",
        "https://play.qobuz.com/artist/36819",
        "https://www.qobuz.com/us-en/playlist/best-electronic/998877",
        "https://www.deezer.com/track/3135556",
        "https://www.deezer.com/en/album/302127",
        "https://www.deezer.com/fr/artist/27",
        "https://www.deezer.co.uk/playlist/908622995",
        "http://192.168.1.10:8765/api/v1/tracks/stream?path=a.flac",
        "companion://track/123",
        "https://deezer.page.link/abc123",
        "https://open.spotify.com/track/1234",
    ]
    url_cases = []
    for u in urls:
        mtype, mid, src = _detect_type_and_id(u)
        url_cases.append({"url": u, "media_type": mtype, "id": mid, "source": src})

    # 4. source_metadata goldens (Qobuz & Deezer fixtures)
    qobuz_album_payload = {
        "id": "0886443927087",
        "title": "Random Access Memories",
        "artist": {"id": 36819, "name": "Daft Punk"},
        "release_date": "2013-05-17",
        "tracks_count": 13,
        "media_count": 1,
        "genre": {"path": [{"name": "Electronic"}, {"name": "French House"}]},
        "copyright": "(P) 2013 Daft Life",
        "image": {"small": "https://img/s.jpg", "large": "https://img/l.jpg"},
        "tracks": {
            "items": [
                {
                    "id": 1001,
                    "title": "Give Life Back to Music",
                    "performer": {"name": "Daft Punk"},
                    "track_number": 1,
                    "media_number": 1,
                    "isrc": "USQX91300101",
                    "composer": {"name": "Thomas Bangalter"},
                },
                {
                    "id": 1002,
                    "title": "The Game of Love",
                    "artist": {"name": "Daft Punk"},
                    "media_number": 1,
                },
            ]
        },
    }
    deezer_album_payload = {
        "id": 302127,
        "title": "Discovery",
        "artist": {"id": 27, "name": "Daft Punk"},
        "release_date": "2001-03-07",
        "nb_tracks": 14,
        "track_total": 2,
        "cover_xl": "https://dz/xl.jpg",
        "cover_big": "https://dz/big.jpg",
        "genres": {"data": [{"id": 106, "name": "Electro"}, {"id": 113, "name": "Dance"}]},
        "tracks": [
            {
                "id": 3135556,
                "title": "One More Time",
                "artist": {"name": "Daft Punk"},
                "track_position": 1,
                "disk_number": 1,
                "isrc": "GBDUW0000059",
            },
            {
                "id": 3135557,
                "title": "Aerodynamic",
                "contributors": [{"name": "Daft Punk (Contrib)"}],
                "track_position": 2,
                "disk_number": 2,
            },
        ],
    }

    q_tracks = extract_tracks("qobuz", "album", json.loads(json.dumps(qobuz_album_payload)))
    q_norm = [normalize_track("qobuz", t, i + 1) for i, t in enumerate(q_tracks)]

    dz_tracks = extract_tracks("deezer", "album", json.loads(json.dumps(deezer_album_payload)))
    dz_norm = [normalize_track("deezer", t, i + 1) for i, t in enumerate(dz_tracks)]

    # 5. SearchResults & StreamripSearcher._parse_results goldens
    qobuz_raw_search = [
        {
            "_media_type": "track",
            "id": 1001,
            "title": "Give Life Back to Music",
            "performer": {"name": "Daft Punk"},
            "album": {
                "title": "Random Access Memories",
                "release_date_original": "2013-05-17",
                "image": {"large": "https://img/l.jpg"},
            },
        },
        # Duplicate on deluxe edition — should be deduped by _parse_results!
        {
            "_media_type": "track",
            "id": 1099,
            "title": "Give Life Back to Music",
            "performer": {"name": "Daft Punk"},
            "album": {
                "title": "Random Access Memories (10th Anniversary)",
                "release_date_original": "2023-05-12",
                "image": {"large": "https://img/l2.jpg"},
            },
        },
        {
            "_media_type": "track",
            "id": 1003,
            "title": "Single Title",
            "performer": {"name": "Daft Punk"},
            "album": {"title": "Single Title", "release_date_original": "2014-01-01"},
        },
        {
            "_media_type": "album",
            "id": "0886443927087",
            "title": "Random Access Memories",
            "version": "10th Anniversary",
            "artist": {"name": "Daft Punk"},
            "tracks_count": 22,
            "release_date_original": "2023-05-12",
            "image": {"medium": "https://img/m.jpg"},
        },
        {
            "_media_type": "artist",
            "id": 36819,
            "name": "Daft Punk",
            "albums_count": 18,
        },
        {
            "_media_type": "playlist",
            "id": "555",
            "name": "Electronic Essentials",
            "owner": {"name": "Qobuz"},
            "tracks_count": 40,
            "description": "Best cuts\n| all time",
        },
    ]

    deezer_raw_search = [
        {
            "_media_type": "track",
            "id": 3135556,
            "title": "One More Time",
            "artist": {"name": "Daft Punk"},
            "album": {"title": "Discovery", "cover_big": "https://dz/big.jpg"},
        },
        {
            "_media_type": "album",
            "id": 302127,
            "title": "Discovery",
            "artist": {"name": "Daft Punk"},
            "nb_tracks": 14,
            "release_date": "2001-03-07",
            "cover_xl": "https://dz/xl.jpg",
        },
    ]

    searcher = object.__new__(StreamripSearcher)
    qobuz_parsed = searcher._parse_results(qobuz_raw_search, "qobuz")
    deezer_parsed = searcher._parse_results(deezer_raw_search, "deezer")

    sr_q_tracks = SearchResults.from_pages("qobuz", "track", [{"tracks": {"items": qobuz_raw_search[:1]}}])
    sr_q_albums = SearchResults.from_pages("qobuz", "album", [{"albums": {"items": [qobuz_raw_search[3]]}}])
    sr_q_artists = SearchResults.from_pages("qobuz", "artist", [{"artists": {"items": [qobuz_raw_search[4]]}}])
    sr_q_playlists = SearchResults.from_pages("qobuz", "playlist", [{"playlists": {"items": [qobuz_raw_search[5]]}}])

    humanize_cases = []
    for label, exc in [
        ("invalid_app_secret", InvalidAppSecretError("bad")),
        ("status_401", AssertionError("Status: 401, Response: {'message': 'Invalid'}")),
        ("status_400", Exception("Status: 400, Response: {}")),
        ("status_429", Exception("Status: 429, Response: {}")),
        ("network_timeout", Exception("Connection timed out while connecting")),
        ("generic", Exception("unexpected boom")),
    ]:
        humanize_cases.append({
            "label": label,
            "input": str(exc),
            "is_invalid_secret": isinstance(exc, InvalidAppSecretError),
            "output": str(StreamripSearcher._humanize_search_error(exc)),
        })

    out = {
        "blowfish_keys": bf_keys,
        "blowfish_decrypts": bf_decrypts,
        "aes_encrypted_urls": aes_urls,
        "qobuz_api_sigs": qobuz_api_sigs,
        "qobuz_file_sigs": qobuz_file_sigs,
        "qobuz_spoofer": {
            "bundle_js": bundle_js,
            "app_id": spoofer_app_id,
            "secrets": spoofer_secrets,
        },
        "url_cases": url_cases,
        "source_metadata": {
            "qobuz_album_payload": qobuz_album_payload,
            "qobuz_normalized": q_norm,
            "deezer_album_payload": deezer_album_payload,
            "deezer_normalized": dz_norm,
        },
        "search_parsing": {
            "qobuz_raw": qobuz_raw_search,
            "qobuz_parsed": qobuz_parsed,
            "deezer_raw": deezer_raw_search,
            "deezer_parsed": deezer_parsed,
            "summaries": {
                "track": sr_q_tracks.summaries(),
                "track_preview": sr_q_tracks.results[0].preview(),
                "album": sr_q_albums.summaries(),
                "album_preview": sr_q_albums.results[0].preview(),
                "artist": sr_q_artists.summaries(),
                "artist_preview": sr_q_artists.results[0].preview(),
                "playlist": sr_q_playlists.summaries(),
                "playlist_preview": sr_q_playlists.results[0].preview(),
            },
        },
        "humanize_errors": humanize_cases,
    }

    path = os.path.join(PORT, "test", "goldens", "sources_phase6b.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
