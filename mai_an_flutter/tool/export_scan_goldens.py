#!/usr/bin/env python3
"""Golden values for the port's library scan (PORT_MAP rule 3): what tinytag
reads from each kind of audio file, and what LibraryScanner writes into a
database. No personal data, so the output is committed:

  test/goldens/tags/              a small corpus of audio files, made here
                                  with ffmpeg, mutagen and hand-built bytes:
                                  every container the scanner accepts, ID3
                                  v1/v2.2/v2.3/v2.4 in each text encoding,
                                  vorbis comments, MP4 atoms, RIFF INFO, and
                                  broken files.
  test/goldens/scan_synthetic.json
      tags   for each corpus file: tinytag's attributes and `other`, or the
             exception it raised.
      scan   folder layouts built from the corpus (nested folders, symlinks,
             unsupported files, an artist alias, a large scan that takes the
             bulk path), scanned by LibraryScanner into a fresh database,
             then changed (deleted, replaced, touched, added) and scanned
             again: the count each scan reported and every table after it.

    flutter test test/tinytag_test.dart test/scan_test.dart

And, when the Mac's music folder is at hand (gitignored, personal data):

  tools/state_backups/goldens/mac_music/tags.json   tinytag on every file
  tools/state_backups/goldens/mac_music/scan.json   the scanned tables
      test/scan_real_test.dart scans the same folder and compares.

tinytag is pinned to the version the Android build bundles (2.3.2, see
StreamripApp/build/site-packages), installed apart from the dev env:

    ~/miniconda3/bin/pip install --target tool/.pydeps tinytag==2.3.2

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_scan_goldens.py [--no-corpus] [--music DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(HERE, ".pydeps"))
sys.path.insert(1, os.path.join(REPO, "StreamripApp"))
sys.path.insert(2, HERE)

import tinytag  # noqa: E402

TT_VERSION = "2.3.2"
assert tinytag.__file__.startswith(os.path.join(HERE, ".pydeps")), tinytag.__file__

from export_db_goldens import dump  # noqa: E402
from utils.db_manager import DatabaseManager  # noqa: E402
from utils.library_scanner import LibraryScanner  # noqa: E402
import utils.library_scanner as _ls  # noqa: E402

_ls.TinyTag = tinytag.TinyTag  # the scanner imported the dev env's copy

CORPUS = os.path.join(PORT, "test", "goldens", "tags")
OUT = os.path.join(PORT, "test", "goldens", "scan_synthetic.json")
REAL = os.path.join(REPO, "tools", "state_backups", "goldens", "mac_music")

# ── corpus ──────────────────────────────────────────────────────────────────


def ffmpeg(out: str, *args: str, dur: str = "0.4") -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
         "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
         *args, out],
        check=True,
    )


def synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3(version: int, frames: list[bytes], ext: bytes = b"") -> bytes:
    body = b"".join(frames)
    flags = 0x40 if ext else 0
    return b"ID3" + bytes([version, 0, flags]) + synchsafe(len(ext) + len(body)) + ext + body


def f22(fid: str, data: bytes) -> bytes:
    return fid.encode() + len(data).to_bytes(3, "big") + data


def f23(fid: str, data: bytes) -> bytes:
    return fid.encode("latin-1") + len(data).to_bytes(4, "big") + b"\0\0" + data


def f24(fid: str, data: bytes) -> bytes:
    return fid.encode() + synchsafe(len(data)) + b"\0\0" + data


def txt(enc: int, *values: str) -> bytes:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}[enc]
    sep = b"\0\0" if enc in (1, 2) else b"\0"
    return bytes([enc]) + sep.join(v.encode(codec) for v in values)


def comm(enc: int, desc: str, text: str) -> bytes:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}[enc]
    sep = b"\0\0" if enc in (1, 2) else b"\0"
    return bytes([enc]) + b"eng" + desc.encode(codec) + sep + text.encode(codec)


def id3v1(title=b"", artist=b"", album=b"", year=b"", comment=b"", track=None, genre=255) -> bytes:
    def pad(b, n):
        return b[:n].ljust(n, b"\0")
    c = pad(comment, 30)
    if track is not None:
        c = c[:28] + b"\0" + bytes([track])
    return b"TAG" + pad(title, 30) + pad(artist, 30) + pad(album, 30) + pad(year, 4) + c + bytes([genre])


def riff_info(fields: list[tuple[bytes, bytes]]) -> bytes:
    body = b"INFO"
    for k, v in fields:
        body += k + struct.pack("<I", len(v)) + v + (b"\0" if len(v) % 2 else b"")
    return b"LIST" + struct.pack("<I", len(body)) + body


def append_chunk(wav: bytes, chunk: bytes) -> bytes:
    out = wav + chunk
    return out[:4] + struct.pack("<I", len(out) - 8) + out[8:]


def build_corpus() -> None:
    from mutagen import id3 as M
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    from mutagen.oggflac import OggFLAC
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE

    if os.path.isdir(CORPUS):
        shutil.rmtree(CORPUS)
    os.makedirs(CORPUS)
    tmp = tempfile.mkdtemp()

    def base(name, *args, dur="0.4"):
        p = os.path.join(tmp, name)
        ffmpeg(p, *args, dur=dur)
        return open(p, "rb").read()

    mp3_cbr = base("cbr.mp3", "-ac", "1", "-ar", "22050", "-c:a", "libmp3lame", "-b:a", "32k",
                   "-write_xing", "0", "-id3v2_version", "0")
    mp3_vbr = base("vbr.mp3", "-ac", "1", "-ar", "22050", "-c:a", "libmp3lame", "-q:a", "9",
                   "-id3v2_version", "0")
    flac = base("a.flac", "-ac", "1", "-ar", "8000", "-c:a", "flac")
    ogg = base("a.ogg", "-ac", "1", "-ar", "22050", "-c:a", "libvorbis", "-q:a", "0")
    opus = base("a.opus", "-ac", "1", "-c:a", "libopus", "-b:a", "16k")
    oggflac = base("af.oga", "-ac", "1", "-ar", "8000", "-c:a", "flac", "-f", "ogg")
    m4a = base("a.m4a", "-ac", "1", "-ar", "22050", "-c:a", "aac", "-b:a", "32k")
    m4a_fast = base("b.m4a", "-ac", "1", "-ar", "22050", "-c:a", "aac", "-b:a", "32k",
                    "-movflags", "+faststart")
    alac = base("c.m4a", "-ac", "1", "-ar", "8000", "-c:a", "alac")
    wav = base("a.wav", "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le", dur="0.2")
    adts = base("a.aac", "-ac", "1", "-ar", "22050", "-c:a", "aac", "-b:a", "32k", "-f", "adts")

    def put(name: str, data: bytes) -> str:
        p = os.path.join(CORPUS, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    # Plain containers, no tags.
    put("plain_cbr.mp3", mp3_cbr)
    put("plain.aac", adts)

    # ID3v2.3, UTF-16 with BOM, the common iTunes frames.
    put("id3v23_utf16.mp3", id3(3, [
        f23("TIT2", txt(1, "Ψυχή μου")),
        f23("TPE1", txt(1, "Χάρις Αλεξίου")),
        f23("TPE2", txt(1, "Various Artists")),
        f23("TALB", txt(1, "Η Αγάπη #1")),
        f23("TYER", txt(0, "1998")),
        f23("TCON", txt(1, "(13)")),
        f23("TRCK", txt(0, "3/12")),
        f23("TPOS", txt(0, "1/2")),
        f23("COMM", comm(1, "", "a plain comment")),
        f23("COMM", comm(1, "iTunNORM", " 0000044E 00000000")),
        f23("TXXX", txt(1, "ARTISTS", "Second Artist")),
        f23("TXXX", txt(3, "Custom Thing", "x")),
        f23("TBPM", txt(0, "120")),
        f23("GRP1", txt(0, "grouping")),
        f23("TIT1", txt(0, "the work")),
        f23("WOAR", b"https://example.com"),
        f23("PRIV", b"XMP\0<x:xmpmeta/>\0\0"),
        f23("APIC", b"\0image/png\0\x03\0" + b"\x89PNG" * 4),
        f23("ZZZZ", txt(0, "unknown frame")),
    ]) + mp3_vbr)

    # ID3v2.4, UTF-8, several values in one frame, a genre number, padding.
    put("id3v24_utf8.mp3", id3(4, [
        f24("TIT2", txt(3, "Ünïcödé Title")),
        f24("TPE1", txt(3, "Artist One", "Artist Two")),
        f24("TALB", txt(3, "Album ⁄ Slash")),
        f24("TDRC", txt(3, "2019-05-03")),
        f24("TCON", txt(3, "17", "Electronic")),
        f24("TRCK", txt(3, "7")),
        f24("USLT", comm(3, "", "la la la")),
        f24("SYLT", b"\x03eng\x02\x01desc\0" + b"line one\0" + struct.pack(">I", 1500)
            + b"\nline two\0" + struct.pack(">I", 61250)),
    ]) + b"\0" * 64 + mp3_cbr)

    # ID3v2.4 UTF-16BE without BOM; an extended header.
    put("id3v24_be_ext.mp3", id3(4, [
        f24("TIT2", txt(2, "Big Endian")),
        f24("TPE1", txt(2, "BE Artist")),
        f24("TCON", txt(2, "(255)Weird")),
        f24("TRCK", txt(2, "x/9")),
    ], ext=synchsafe(10) + b"\x01\x00\0\0\0\0") + mp3_cbr)

    # ID3v2.2 (three-letter frames).
    put("id3v22.mp3", id3(2, [
        f22("TT2", txt(0, "Old Title")),
        f22("TP1", txt(0, "Old Artist")),
        f22("TAL", txt(0, "Old Album")),
        f22("TYE", txt(0, "1987")),
        f22("TCO", txt(0, "Rock")),
        f22("TRK", txt(0, "02/10")),
        f22("COM", comm(0, "", "two-two comment")),
    ]) + mp3_cbr)

    # ID3v1 only, with a track number and a genre; latin-1 text.
    put("id3v1_only.mp3", mp3_cbr + id3v1(
        b"V1 Title", "Caf\xe9 Artist".encode("latin-1"), b"V1 Album", b"2001",
        b"v1 comment", track=5, genre=17))

    # Both: ID3v2 wins, ID3v1 fills the gaps.
    put("id3v2_and_v1.mp3", id3(3, [f23("TIT2", txt(0, "From V2"))]) + mp3_cbr
        + id3v1(b"From V1", b"V1 Only Artist", b"V1 Only Album", b"1999", genre=0))

    # Only an artist (the scanner keeps it; title from the file name).
    put("artist_only.mp3", id3(3, [f23("TPE1", txt(0, "Lonely Artist"))]) + mp3_cbr)

    # A frame whose size overruns the tag: parsing stops there.
    put("id3_bad_frame.mp3", id3(3, [f23("TIT2", txt(0, "Before")),
                                      b"TPE1" + (10 ** 6).to_bytes(4, "big") + b"\0\0x"]) + mp3_cbr)

    # FLAC with vorbis comments and a picture block.
    p = put("tagged.flac", flac)
    f = FLAC(p)
    f["TITLE"] = "Flac Title"
    f["ARTIST"] = ["Flac Artist", "Featured"]
    f["ALBUMARTIST"] = "Flac Album Artist"
    f["ALBUM"] = "Flac Album"
    f["DATE"] = "2020"
    f["GENRE"] = "Ambient"
    f["TRACKNUMBER"] = "4/10"
    f["DISCNUMBER"] = "2"
    f["TOTALDISCS"] = "3"
    f["COMMENT"] = "flac comment"
    f["MY_CUSTOM"] = "custom value"
    f["BPM"] = "98"
    pic = Picture()
    pic.type = 3
    pic.mime = "image/png"
    pic.desc = "cover"
    pic.data = b"\x89PNG" * 8
    f.add_picture(pic)
    f.save()

    # FLAC with an ID3v2 tag in front (some rippers do this).
    tagged = open(p, "rb").read()
    put("id3_prefixed.flac", id3(3, [f23("TIT2", txt(0, "ID3 Title")),
                                      f23("TPE1", txt(0, "ID3 Artist"))]) + tagged)
    # Upper-case extension; a FLAC named .mp3 is still sniffed as FLAC only
    # after ID3, so it goes through the MPEG path.
    put("UPPER.FLAC", tagged)
    put("flac_named.mp3", tagged)

    # Untagged FLAC: no title, no artist (the scanner skips it).
    put("untagged.flac", flac)

    # Ogg Vorbis, Opus, Ogg FLAC.
    p = put("tagged.ogg", ogg)
    v = OggVorbis(p)
    v["title"] = "Vorbis Title"
    v["artist"] = "Vorbis Artist"
    v["album"] = "Vorbis Album"
    v["date"] = "2011"
    v["tracknumber"] = "9"
    v["tracktotal"] = "12"
    v["genre"] = "Jazz"
    v.save()
    p = put("tagged.opus", opus)
    o = OggOpus(p)
    o["title"] = "Opus Title"
    o["artist"] = "Opus Artist"
    o["albumartist"] = "Opus AA"
    o["discnumber"] = "1/1"
    o.save()
    p = put("tagged.oga", oggflac)
    of = OggFLAC(p)
    of["title"] = "OggFlac Title"
    of["artist"] = "OggFlac Artist"
    of.save()

    # MP4: AAC (moov at the end and at the front), ALAC.
    p = put("tagged.m4a", m4a)
    m = MP4(p)
    m["\xa9nam"] = "MP4 Title"
    m["\xa9ART"] = "MP4 Artist"
    m["aART"] = "MP4 Album Artist"
    m["\xa9alb"] = "MP4 Album"
    m["\xa9day"] = "2015-01-01T00:00:00Z"
    m["\xa9gen"] = "Soul"
    m["trkn"] = [(3, 11)]
    m["disk"] = [(1, 2)]
    m["tmpo"] = [128]
    m["\xa9cmt"] = "mp4 comment"
    m["----:com.apple.iTunes:ARTISTS"] = [MP4FreeForm(b"Second MP4 Artist")]
    m["----:com.apple.iTunes:MOOD"] = [MP4FreeForm(b"calm")]
    m["covr"] = [MP4Cover(b"\x89PNG" * 8, imageformat=MP4Cover.FORMAT_PNG)]
    m.save()
    p = put("faststart.m4a", m4a_fast)
    m = MP4(p)
    m["\xa9nam"] = "Fast Title"
    m["\xa9ART"] = "Fast Artist"
    m["xyzw"] = "unknown atom"
    m.save()
    p = put("alac.m4a", alac)
    m = MP4(p)
    m["\xa9nam"] = "ALAC Title"
    m["\xa9ART"] = "ALAC Artist"
    m["\xa9alb"] = "ALAC Album"
    m.save()

    # WAV: RIFF INFO; an ID3 chunk.
    put("info.wav", append_chunk(wav, riff_info([
        (b"INAM", b"Wav Title"), (b"IART", b"Wav Artist"), (b"IPRD", b"Wav Album"),
        (b"ICRD", b"2003"), (b"IGNR", b"Folk"), (b"ITRK", b"6"), (b"IXYZ", b"odd"),
    ])))
    p = put("id3.wav", wav)
    w = WAVE(p)
    w.add_tags()
    w.tags.add(M.TIT2(encoding=3, text="Wav ID3 Title"))
    w.tags.add(M.TPE1(encoding=3, text="Wav ID3 Artist"))
    w.save()

    # Broken files.
    whole = open(os.path.join(CORPUS, "id3v23_utf16.mp3"), "rb").read()
    put("truncated_tag.mp3", whole[:60])
    put("garbage.flac", b"not a flac file at all" * 4)
    put("empty.ogg", b"")
    put("truncated.m4a", open(os.path.join(CORPUS, "tagged.m4a"), "rb").read()[:40])
    put("garbage.wav", b"RIFF\x10\0\0\0WAVEjunk")
    put("plain.ogg", ogg)
    shutil.rmtree(tmp)


# ── tinytag ─────────────────────────────────────────────────────────────────


def tag_fields(path: str) -> dict:
    try:
        tag = tinytag.TinyTag.get(path)
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}
    out = {}
    for k, v in tag.__dict__.items():
        if k.startswith("_") or k in ("images", "filename") or v is None:
            continue
        out[k] = dict(v) if k == "other" else v
    return out


def tags_case() -> dict:
    return {name: tag_fields(os.path.join(CORPUS, name)) for name in sorted(os.listdir(CORPUS))}


# ── scan ────────────────────────────────────────────────────────────────────

T0 = 1_700_000_000

LAYOUT = [
    {"path": "Music/Χάρις/Album/01 Ψυχή.mp3", "src": "id3v23_utf16.mp3", "mtime": T0 + 1},
    {"path": "Music/Χάρις/Album/02.mp3", "src": "id3v24_utf8.mp3", "mtime": T0 + 2},
    {"path": "Music/Χάρις/album/03.mp3", "src": "id3v22.mp3", "mtime": T0 + 3},
    {"path": "Music/flac/tagged.flac", "src": "tagged.flac", "mtime": T0 + 4},
    {"path": "Music/flac/UPPER.FLAC", "src": "UPPER.FLAC", "mtime": T0 + 5},
    {"path": "Music/flac/prefixed.flac", "src": "id3_prefixed.flac", "mtime": T0 + 6},
    {"path": "Music/flac/untagged.flac", "src": "untagged.flac", "mtime": T0 + 7},
    {"path": "Music/flac/garbage.flac", "src": "garbage.flac", "mtime": T0 + 8},
    {"path": "Music/ogg/a.ogg", "src": "tagged.ogg", "mtime": T0 + 9},
    {"path": "Music/ogg/b.opus", "src": "tagged.opus", "mtime": T0 + 10},
    {"path": "Music/ogg/c.oga", "src": "tagged.oga", "mtime": T0 + 11},
    {"path": "Music/mp4/a.m4a", "src": "tagged.m4a", "mtime": T0 + 12},
    {"path": "Music/mp4/b.m4a", "src": "faststart.m4a", "mtime": T0 + 13},
    {"path": "Music/mp4/c.mp4", "src": "alac.m4a", "mtime": T0 + 14},
    {"path": "Music/wav/info.wav", "src": "info.wav", "mtime": T0 + 15},
    {"path": "Music/wav/id3.wav", "src": "id3.wav", "mtime": T0 + 16},
    {"path": "Music/misc/v1.mp3", "src": "id3v1_only.mp3", "mtime": T0 + 17},
    {"path": "Music/misc/artist_only.mp3", "src": "artist_only.mp3", "mtime": T0 + 18},
    {"path": "Music/misc/plain.aac", "src": "plain.aac", "mtime": T0 + 19},
    {"path": "Music/misc/empty.ogg", "src": "empty.ogg", "mtime": T0 + 20},
    {"path": "Music/misc/.hidden.mp3", "src": "id3v2_and_v1.mp3", "mtime": T0 + 21},
    {"path": "Music/misc/notes.txt", "src": "plain.ogg", "mtime": T0 + 22},
    {"path": "Music/misc/cover.jpg", "src": "plain.ogg", "mtime": T0 + 23},
    {"path": "Music/alias/one.mp3", "src": "alias_artist.mp3", "mtime": T0 + 24},
    {"path": "Music/alias/two.mp3", "src": "spaced_artist.mp3", "mtime": T0 + 25},
    {"path": "Music/link_dir", "symlink": "flac"},
    {"path": "Music/link_file.mp3", "symlink": "misc/v1.mp3"},
]

# Made per case, not in the corpus: their tags are what matters here.
EXTRA_SRC = {
    "alias_artist.mp3": [("TIT2", "Merged"), ("TPE1", "alias artist")],
    "spaced_artist.mp3": [("TIT2", "Spaced"), ("TPE1", "  Spaced Artist  "), ("TALB", " Spaced Album ")],
}

STEP2 = [
    {"delete": "Music/Χάρις/Album/02.mp3"},
    {"copy": {"path": "Music/flac/tagged.flac", "src": "tagged.ogg", "mtime": T0 + 100}},
    {"touch": {"path": "Music/ogg/a.ogg", "mtime": T0 + 9.5}},
    {"copy": {"path": "Music/new/new.m4a", "src": "faststart.m4a", "mtime": T0 + 101}},
    {"delete": "Music/flac/untagged.flac"},
]

BULK_SRC = ["id3v23_utf16.mp3", "tagged.flac", "tagged.ogg", "tagged.m4a", "id3v22.mp3", "info.wav"]
BULK = [
    {"path": f"Bulk/d{i // 100}/t{i:03d}{os.path.splitext(BULK_SRC[i % len(BULK_SRC)])[1]}",
     "src": BULK_SRC[i % len(BULK_SRC)], "mtime": T0 + 1000 + i}
    for i in range(205)
]


def extra_sources(tmp: str) -> dict[str, str]:
    base = open(os.path.join(CORPUS, "plain_cbr.mp3"), "rb").read()
    out = {}
    for name, frames in EXTRA_SRC.items():
        p = os.path.join(tmp, name)
        with open(p, "wb") as fh:
            fh.write(id3(3, [f23(fid, txt(0, val)) for fid, val in frames]) + base)
        out[name] = p
    return out


def apply(root: str, entries: list[dict], extra: dict[str, str]) -> None:
    for e in entries:
        if "delete" in e:
            os.remove(os.path.join(root, e["delete"]))
            continue
        if "touch" in e:
            t = e["touch"]
            os.utime(os.path.join(root, t["path"]), (t["mtime"], t["mtime"]))
            continue
        e = e.get("copy", e)
        dst = os.path.join(root, e["path"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if "symlink" in e:
            os.symlink(e["symlink"], dst)
            continue
        src = extra.get(e["src"]) or os.path.join(CORPUS, e["src"])
        shutil.copyfile(src, dst)
        os.utime(dst, (e["mtime"], e["mtime"]))


async def _scan(target: str, db: str, aliases: list[list[str]]) -> int | None:
    dbm = DatabaseManager(db)
    await dbm.initialize()
    if aliases:
        conn = await dbm.get_connection()
        for alias, canonical in aliases:
            await conn.execute(
                "INSERT OR IGNORE INTO artist_aliases (alias, canonical, created_at) VALUES (?, ?, 0)",
                (alias, canonical))
        await conn.commit()
        await dbm._load_aliases(conn)
    done = []
    scanner = LibraryScanner(target, dbm, completion_callback=lambda c, s: done.append(c))
    await scanner.run()
    await dbm.close()
    return done[0] if done else None


def relocate(v, root: str):
    """Paths under the temp folder as `$ROOT/...`, so the port can compare."""
    if isinstance(v, str):
        return v.replace(root, "$ROOT")
    if isinstance(v, list):
        return [relocate(x, root) for x in v]
    if isinstance(v, dict):
        return {k: relocate(x, root) for k, x in v.items()}
    return v


def scan_case(name: str, layout: list[dict], target: str, steps: list[list[dict]],
              aliases: list[list[str]]) -> dict:
    tmp = tempfile.mkdtemp()
    try:
        extra = extra_sources(tmp)
        root = os.path.join(tmp, "root")
        os.makedirs(root)
        db = os.path.join(tmp, "library.db")
        results = []
        for i, step in enumerate([layout, *steps]):
            apply(root, step, extra)
            count = asyncio.run(_scan(os.path.join(root, target), db, aliases if i == 0 else []))
            results.append({"count": count, "db": relocate(dump(db), root)})
        return {
            "name": name, "target": target, "layout": layout, "steps": steps,
            "aliases": aliases, "extra": EXTRA_SRC, "results": results,
        }
    finally:
        shutil.rmtree(tmp)


def scan_cases() -> list[dict]:
    return [
        scan_case("layout", LAYOUT, "Music", [STEP2], [["Alias Artist", "Canonical Artist"]]),
        scan_case("bulk", BULK, "Bulk", [[{"delete": BULK[0]["path"]}]], []),
    ]


# ── real library ────────────────────────────────────────────────────────────


def real(music: str) -> None:
    os.makedirs(REAL, exist_ok=True)
    exts = LibraryScanner(music, None).supported_extensions
    tags = {}
    for dirpath, dirnames, filenames in os.walk(music):
        dirnames.sort()
        for f in sorted(filenames):
            if f.lower().endswith(exts):
                p = os.path.join(dirpath, f)
                tags[os.path.relpath(p, music)] = tag_fields(p)
    with open(os.path.join(REAL, "tags.json"), "w") as fh:
        json.dump({"music": music, "tinytag": TT_VERSION, "tags": tags}, fh, ensure_ascii=False, indent=0)
    tmp = tempfile.mkdtemp()
    try:
        db = os.path.join(tmp, "library.db")
        count = asyncio.run(_scan(music, db, []))
        d = dump(db)
        keep = {"artists", "albums", "tracks", "fts_search"}
        d["tables"] = {k: v for k, v in d["tables"].items() if k in keep}
        with open(os.path.join(REAL, "scan.json"), "w") as fh:
            json.dump({"music": music, "count": count, "db": d}, fh, ensure_ascii=False)
    finally:
        shutil.rmtree(tmp)
    print(f"{REAL}: {len(tags)} files, scan count {count}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-corpus", action="store_true", help="keep test/goldens/tags as is")
    ap.add_argument("--music", default=os.path.expanduser("~/Desktop/Music"))
    a = ap.parse_args()
    if not a.no_corpus:
        build_corpus()
    out = {"tinytag": TT_VERSION, "tags": tags_case(), "scan": scan_cases()}
    with open(OUT, "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"{OUT}: {len(out['tags'])} files, {len(out['scan'])} scan cases")
    if os.path.isdir(a.music):
        real(a.music)


if __name__ == "__main__":
    main()
