"""Drain the native player's play ledger into the database.

The Android audio handler (flet_audio_service.dart, "Play ledger") appends one
JSON line per finished listen to LEDGER_NAME in the app data dir:

    {"id": "<src uri>", "ms": <audible ms>, "dur": <ms or 0>, "start": <unix s>, "end": <unix s>}

It does this independently of the Flet session, so listens that happen while
Python is deaf (app suspended long enough for the session to be torn down)
still land on disk. `drain_ledger` moves the file aside, parses it, and hands
the batch to `DatabaseManager.record_listens` in one transaction.

Crash safety: the file is renamed to `<ledger>.draining` before reading and only
deleted after the DB commit. A leftover `.draining` from an interrupted drain is
ingested first on the next call; record_listens ignores rows it already has, so
a replay never double-counts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

LEDGER_NAME = "play_ledger.jsonl"

# After renaming the ledger aside, wait this long before reading so an append
# the handler had already opened against the old name finishes landing in the
# renamed file (appends are ~150 bytes; this is generous).
_SETTLE_S = 0.5

_drain_lock = asyncio.Lock()


def is_listen(listened_s: float, duration_s: float, min_s: float = 30.0) -> bool:
    """The scrobble rule: a listen counts after `min_s` seconds, or half the
    track for short tracks. Anything less is an early skip."""
    return listened_s >= min_s or (duration_s > 0.0 and listened_s >= 0.5 * duration_s)


def ledger_path(data_dir: str) -> str:
    return os.path.join(data_dir, LEDGER_NAME)


def _src_to_path(src: str) -> str:
    """Invert AudioEngine._to_uri: file URIs and bare paths map back to the
    library path; network streams have no library identity and are dropped."""
    if not src:
        return ""
    if src.startswith(("http://", "https://")):
        return ""
    if src.startswith("file://"):
        return unquote(urlparse(src).path)
    return src


def parse_lines(lines) -> list[dict]:
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # a torn final line from a killed process
        path = _src_to_path(str(raw.get("id") or ""))
        if not path:
            continue
        try:
            entries.append({
                "path": path,
                "listened_s": float(raw.get("ms") or 0) / 1000.0,
                "duration_s": float(raw.get("dur") or 0) / 1000.0,
                "ended_at": int(raw.get("end") or 0) or None,
            })
        except (TypeError, ValueError):
            continue
    return entries


def _read(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


async def drain_ledger(data_dir: str, db, on_entries=None) -> int:
    """Ingest every pending ledger line. Returns the number of listens that
    counted toward play_counts. Cheap no-op (one stat) when nothing is pending.
    `on_entries(entries)`, if given, sees each parsed batch in play order (the
    auto-play skip signal) before it is recorded."""
    live = ledger_path(data_dir)
    draining = live + ".draining"
    total = 0
    async with _drain_lock:
        # Two passes: a leftover .draining from an interrupted drain first,
        # then whatever the handler has appended since.
        for _ in range(2):
            if not os.path.exists(draining):
                if not os.path.exists(live):
                    break
                try:
                    os.replace(live, draining)
                except FileNotFoundError:
                    break
                await asyncio.sleep(_SETTLE_S)
            lines = await asyncio.to_thread(_read, draining)
            entries = parse_lines(lines)
            if entries and on_entries is not None:
                try:
                    on_entries(entries)
                except Exception:
                    logger.exception("play ledger: on_entries callback failed")
            counted = await db.record_listens(entries) if entries else 0
            try:
                os.remove(draining)
            except FileNotFoundError:
                pass
            if entries:
                logger.info("play ledger: ingested %d listens (%d counted)",
                            len(entries), counted)
            total += counted
    return total
