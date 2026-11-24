"""Flet's own CompanionServer, hermetic, for test/companion_protocol_test.dart.

    ~/miniconda3/bin/python tool/companion_test_server.py <scratch dir>

Runs utils/sync/server.py unchanged except for what would touch the real
machine: the feature cache and the bundle folder live under <scratch dir>
(both are found from the module's __file__), the beacon goes to 127.0.0.1 on
a private UDP port, and feature extraction is a deterministic stand-in (the
protocol is under test, not dsp.py).

Prints one JSON line with the ports, PIN and token, then answers JSON
commands on stdin, one per line:
  {"cmd": "state"}                      server-side state
  {"cmd": "py_offload", "paths": [...]} the Python client's offload_dsp,
                                        with the rows it stored
  {"cmd": "py_push", "path": p, "meta": {...}}   Python's push_track
  {"cmd": "py_pending"}                 Python's get_pending_transfers
  {"cmd": "py_stream_url", "transfer": {...}, "token": t}
  {"cmd": "py_quote", "values": [...]}  urllib.parse.quote(v)
  {"cmd": "reset_cache"}                the feature cache back to its seed rows
  {"cmd": "rotate_pin"}                 as after five wrong PINs
  {"cmd": "queue_files", "files": [{"name": n, "size": k, "fill": b}],
   "quality": q}                        write files on the "Mac" and queue
                                        them for the phone
  {"cmd": "py_download", "url": u, "dir": d}   streamrip_api.download, the
                                        companion branch (app-private check
                                        off: the temp dir counts as private)
  {"cmd": "quit"}
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "StreamripApp"))
sys.path.insert(0, ROOT)

SCRATCH = os.path.abspath(sys.argv[1])

import utils.sync.discovery as disc  # noqa: E402
import utils.sync.server as srv  # noqa: E402
from utils.sync.client import CompanionClient  # noqa: E402
from urllib.parse import quote  # noqa: E402

# repo_root = parents[3] of __file__: <scratch>/tools/offload_cache and
# <scratch>/tools/state_backups.
srv.__file__ = os.path.join(SCRATCH, "a", "b", "c", "server.py")


def _free_port(kind=socket.SOCK_STREAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


UDP = _free_port(socket.SOCK_DGRAM)
disc.UDP_PORT = UDP
srv.BeaconBroadcaster._broadcast_targets = lambda self: ["127.0.0.1"]


class _Feats:
    """Stand-in for dsp.extract_features_from_pcm's result: values derived
    from the uploaded bytes, so each file gets its own."""

    def __init__(self, data: bytes):
        n = len(data)
        self.bpm = 60.0 + n % 97
        self.energy = (n % 13) / 13.0
        self.brightness = 1000.0 + n
        self.rolloff = 0.25
        self.beat_strength = (n % 7) / 7.0
        self.spectral_flatness = 0.125
        self.spectral_contrast = 3.5
        self.key_index = n % 24
        self._blob = bytes((n + i) % 256 for i in range(16))

    def timbre_blob(self) -> bytes:
        return self._blob


def _fake_extract(path: str):
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"BAD"):
        raise ValueError("cannot decode")
    return _Feats(data)


srv.decode_and_extract = _fake_extract


def _seed(conn):
    conn.execute(
        "INSERT OR REPLACE INTO feature_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("/phone/Music/cached.flac", 128.0, 0.5, 2000.0, 0.3, 0.7, 0.1, 4.0, 9,
         bytes(range(8)), srv.FEATURES_VERSION))
    conn.execute(
        "INSERT OR REPLACE INTO feature_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("/phone/Music/stale.flac", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1,
         b"", srv.FEATURES_VERSION - 1))


class FakeDb:
    def __init__(self):
        self.features: list[dict] = []
        self.batches: list[list] = []

    async def get_total_tracks(self):
        return 7

    async def update_track_features(self, **kw):
        kw = dict(kw)
        if kw.get("timbre_blob") is not None:
            kw["timbre_blob"] = base64.b64encode(kw["timbre_blob"]).decode()
        self.features.append(kw)

    async def update_tracks_features_batch(self, batch):
        self.batches.append([_wire_row(r) for r in batch])


def _wire_row(r) -> list:
    r = list(r)
    r[9] = base64.b64encode(r[9]).decode()
    return r


async def main():
    port = _free_port()
    db = FakeDb()
    server = srv.CompanionServer(
        db_manager=db, port=port, device_name="Test Mac", pin="4321",
        auth_token="tok-123", trust_loopback=False,
        transfers_path=os.path.join(SCRATCH, "companion_transfers.json"),
    )
    recv_dir = os.path.join(SCRATCH, "recv")
    os.makedirs(recv_dir, exist_ok=True)
    server.receive_dir = lambda: recv_dir
    received: list = []
    server.on_tracks_received = lambda paths, sender: received.append([paths, sender])
    await server.start()

    # The Mac's feature cache: one track already analysed (and one with an
    # older features_version, which must count as missing).
    def seed_cache():
        conn = sqlite3.connect(server._cache_db_path)
        conn.execute("DELETE FROM feature_cache")
        _seed(conn)
        conn.commit()
        conn.close()

    seed_cache()

    # Tracks the Mac has queued for the phone.
    mac_dir = os.path.join(SCRATCH, "mac", "Άλμπουμ")
    os.makedirs(mac_dir, exist_ok=True)
    tracks = []
    for i, name in enumerate(["01 Ένα.flac", "02 Two & Three.mp3"]):
        p = os.path.join(mac_dir, name)
        with open(p, "wb") as f:
            f.write(bytes([i + 1]) * (1000 + i))
        tracks.append({"path": p, "title": name[3:].rsplit(".", 1)[0],
                       "artist": "Καλλιτέχνης", "album": "Άλμπουμ"})
    await server.queue_tracks_for_transfer(tracks, quality="original", precompute_dsp=False)

    print(json.dumps({"port": server.bound_port, "udp_port": UDP, "pin": server.pin,
                      "token": server.auth_token, "scratch": SCRATCH}), flush=True)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    def client(token=None):
        return CompanionClient("127.0.0.1", server.bound_port,
                               auth_token=token or server.auth_token,
                               device_name="Pixel 9 Pro")

    while True:
        line = await reader.readline()
        if not line:
            break
        msg = json.loads(line)
        cmd = msg.get("cmd")
        out: dict = {}
        try:
            if cmd == "quit":
                break
            elif cmd == "state":
                await asyncio.sleep(0.05)  # let a zero-delay receive flush run
                out = {
                    "pending": [t["id"] for t in server._pending_transfers],
                    "transfers": server._pending_transfers,
                    "last_client": list(server.last_client[:2]) if server.last_client else None,
                    "pin": server.pin,
                    "received": received,
                    "features": db.features,
                    "recv_files": sorted(
                        os.path.relpath(os.path.join(d, f), recv_dir)
                        for d, _, fs in os.walk(recv_dir) for f in fs),
                    "bundles": sorted(os.listdir(os.path.join(SCRATCH, "tools", "state_backups")))
                    if os.path.isdir(os.path.join(SCRATCH, "tools", "state_backups")) else [],
                }
            elif cmd == "py_offload":
                db.batches.clear()
                res = await client().offload_dsp(msg["paths"], db)
                out = {"result": res, "batches": db.batches}
            elif cmd == "py_push":
                out = {"reply": await client().push_track(msg["path"], msg["meta"])}
            elif cmd == "py_pending":
                out = {"transfers": await client().get_pending_transfers()}
            elif cmd == "py_stream_url":
                c = CompanionClient(msg.get("host", "127.0.0.1"), msg.get("port", 8945),
                                    auth_token=msg.get("token"))
                out = {"url": c.stream_url(msg["transfer"])}
            elif cmd == "py_quote":
                out = {"quoted": [quote(v) for v in msg["values"]]}
            elif cmd == "reset_cache":
                seed_cache()
            elif cmd == "queue_files":
                qdir = os.path.join(SCRATCH, "mac", "queued")
                os.makedirs(qdir, exist_ok=True)
                batch = []
                for f in msg["files"]:
                    p = os.path.join(qdir, f["name"])
                    with open(p, "wb") as fh:
                        fh.write(bytes([f.get("fill", 7)]) * f["size"])
                    batch.append({"path": p, "title": f["name"], "artist": "A"})
                queued = await server.queue_tracks_for_transfer(
                    batch, quality=msg.get("quality", "original"), precompute_dsp=False)
                out = {"queued": [t["id"] for t in queued]}
            elif cmd == "py_download":
                import utils.storage_paths as sp
                from utils.streamrip_api import download
                orig = sp.is_app_private
                sp.is_app_private = lambda _p: False
                events = []
                try:
                    paths = await download(msg["url"], msg["dir"],
                                           progress_callback=events.append)
                    out = {"paths": paths, "events": events}
                except Exception as ex:
                    out = {"exc_type": type(ex).__name__, "exc": str(ex), "events": events}
                finally:
                    sp.is_app_private = orig
            elif cmd == "rotate_pin":
                server._rotate_pin()
                out = {"pin": server.pin}
        except Exception as ex:  # reported, not fatal
            out = {"error": f"{type(ex).__name__}: {ex}"}
        print(json.dumps(out, ensure_ascii=False), flush=True)

    await server.stop()


asyncio.run(main())
