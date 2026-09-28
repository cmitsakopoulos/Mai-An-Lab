"""Auto-play: a station that keeps a short buffer of similar tracks queued after
the playing one.

A station is opened from one track (the anchor) and is RESUMABLE
(`track_graph.Station`): each refill continues the same journey — legs of
LEG_LEN tracks inside a genre, then a hop into an adjacent one — and nothing the
station has queued is ever queued again, even after being dropped. That is what
stops a rebuild from re-serving songs the listener just removed (measured on the
device image: a fresh walk re-queued 85% of the old buffer, 46% by the artist
just removed).

Variety (the Deterministic / Random pills):

  DETERMINISTIC  the same start track on the same library, in a fresh session,
                 always gives the same station: nearest first, strongest genre
                 hop. Songs played or rejected this session are still excluded,
                 so a replay later in a session differs.
  RANDOM         every station draws a seed: rank-sampled picks inside legs
                 (`genre_graph.rank_pick`), a lift-weighted hop, a shuffled
                 entry track. The seed is logged, so a station can be replayed.

Choosing a track: a pending auto-play row just plays (the station carries on);
anything else starts a new station from it.

Negative feedback: removing a recommendation hides it from auto-play (kept in the
DB, reversible in Settings → Auto-play); an early skip of a recommendation (see
play_ledger.is_listen) counts too. NEGATIVES_TO_END_LEG of them in one leg end
the leg: the unheard buffer is replaced, in one op, by a leg that hops on — or,
in a genre with no exit, is steered away from the rejected tracks. An act
rejected ARTIST_NEGATIVES_TO_BAN times is banned for the station.

Learned genre hops: a rejection among the first tracks after a genre hop
(`genre_graph.HOP_WINDOW`) is also blamed on that hop, persistently. Each one
halves the hop's weight in every later station; HOP_VETO_REJECTS of them, by at
least HOP_VETO_ARTISTS different artists (one disliked act must not condemn a
genre), block the hop for good — the A → B hop only: B stays reachable from
elsewhere and as a station start. Blocked hops are listed and resettable in
Settings → Auto-play.

Cost: nothing runs on a timer. A track change may top the buffer up (one
resumable take + one batched DB read); a station opens once.
"""
from __future__ import annotations

import logging
import os
import random
import time
from collections import Counter, deque

from utils import genre_graph, play_ledger, track_graph

logger = logging.getLogger(__name__)

DETERMINISTIC = "deterministic"
RANDOM = "random"
VARIETY_MODES = (DETERMINISTIC, RANDOM)

BUFFER_TARGET = 4        # tracks kept queued ahead while in the foreground
BUFFER_LOW = 3           # top up when fewer than this remain
BACKGROUND_TARGET = 24   # ~1.5 h, topped up once before the UI goes away
HISTORY_LEN = 150        # tracks started this session, never re-recommended
SKIP_SIGNAL_MAX_AGE_S = 180  # older ledger entries are history, not feedback

# Station shape. Measured on the device image (2026-09-28): 5 per act / 1 per
# album keeps a same-artist run to ~4 across different albums (never inside one
# release) and cut stations that ran dry from 69/150 to 12/150 vs 2/1. The caps
# count over the last CAP_WINDOW tracks so a long station doesn't lock an act out.
LEG_LEN = 8
ARTIST_CAP = 5
ALBUM_CAP = 1
CAP_WINDOW = 24
NEGATIVES_TO_END_LEG = 2
ARTIST_NEGATIVES_TO_BAN = 2
HOP_REJECT_WEIGHT = 0.5
HOP_VETO_REJECTS = 3
HOP_VETO_ARTISTS = 2


def hop_blocked(rec: dict) -> bool:
    """A hop's rejection summary ({rejects, artists}) has earned a block."""
    return (rec.get("rejects", 0) >= HOP_VETO_REJECTS
            and rec.get("artists", 0) >= HOP_VETO_ARTISTS)


def hop_weight(rec: dict) -> float:
    return 0.0 if hop_blocked(rec) else HOP_REJECT_WEIGHT ** rec.get("rejects", 0)


class AutoPlay:
    """Owns auto-play state and every queue edit it makes. `run_task(fn, *args)`
    schedules a coroutine function on the UI loop; `notify(message)` surfaces a
    user-facing hint (e.g. the seed has no DSP features yet)."""

    def __init__(self, engine, db, run_task, notify=None):
        self.engine = engine
        self.db = db
        self._run_task = run_task
        self._notify = notify or (lambda _msg: None)

        self._enabled = False
        self.variety = DETERMINISTIC
        self.anchor = ""                 # the track the station started from
        self.station: track_graph.Station | None = None
        self.station_seed: int | None = None   # RANDOM only; replays the station
        self.history: deque[str] = deque(maxlen=HISTORY_LEN)
        self.hidden: set[str] = set()    # removed by the listener (persisted)
        # (from, to) genre hop -> {rejects, artists}, persisted (see module doc).
        self.hop_rejects: dict[tuple[str, str], dict] = {}
        self._leg_negatives: list[str] = []
        self._artist_negatives: Counter = Counter()
        # Artist of every track this station queued: a rejection often arrives
        # after its row is gone (removed from the queue, or a ledger skip).
        self._artist_by_path: dict[str, str] = {}
        # The anchor had no DSP features, so it can't seed anything: the next
        # track that starts takes over as the anchor.
        self._anchor_dead = False
        # Generation: bumped whenever the plan changes (toggle, new station, leg
        # end) so a fill that was awaiting the station bails instead of
        # inserting a stale block.
        self._gen = 0
        self._filling = False

    # ── mode ────────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, on: bool) -> None:
        # The engine asks auto-play for more at the end of the queue only while on.
        self._enabled = bool(on)
        self.engine.continues_at_end = self._enabled

    def set_enabled(self, on: bool, seed: str = "") -> None:
        if on == self.enabled:
            return
        self.enabled = on
        if on:
            self.start(seed or self.engine.current_path)
        else:
            self._reset("")
            self.drop_buffer()

    def set_variety(self, mode: str) -> None:
        """Switch Deterministic / Random. Takes effect at once: the pending
        buffer is rebuilt as a new station from the playing track."""
        if mode not in VARIETY_MODES or mode == self.variety:
            return
        self.variety = mode
        if self.enabled and self.engine.current_path:
            self.start(self.engine.current_path)

    def start(self, path: str) -> None:
        """Start a new station from `path`, replacing the pending buffer (a
        chosen track, "Start radio", or a variety switch)."""
        if not self.enabled or not path:
            return
        self._reset(path)
        self._schedule_fill(BUFFER_TARGET, replace=True)

    def resume(self, path: str) -> None:
        """After a session restore: give an enabled station its anchor back
        without touching the restored queue. The station itself opens on the
        next refill (its state isn't persisted)."""
        if self.enabled and not self.anchor and path:
            self.anchor = path

    def _reset(self, anchor: str) -> None:
        self._gen += 1
        self._filling = False
        self.anchor = anchor or ""
        self.station = None
        self.station_seed = None
        self._leg_negatives = []
        self._artist_negatives.clear()
        self._artist_by_path.clear()
        self._anchor_dead = False

    @property
    def current_genre(self) -> str | None:
        """Genre of the station's current leg, for display (None for a radius
        station)."""
        node = self.station.node if self.station is not None else None
        return genre_graph.node_display(node) or None

    # ── events from the app ─────────────────────────────────────────────────
    def on_track_changed(self, path: str) -> None:
        """The playing track changed (skip, auto-advance or a tap)."""
        if path:
            self.history.append(path)
            if self.station is not None:
                self.station.exclude([path])
        if self.enabled and self._anchor_dead and path and path != self.anchor:
            self.start(path)
            return
        self.ensure_buffer()

    def on_user_chose(self, path: str) -> None:
        """The user explicitly chose `path`; the engine is already on it. A
        pending recommendation just plays — the station carries on; anything
        else starts a new station from it."""
        if not self.enabled or not path:
            return
        q, ci = self.engine.queue, self.engine.current_index
        if (self.station is not None and 0 <= ci < len(q)
                and q[ci].get("_autoplay") and q[ci].get("path") == path):
            self.ensure_buffer()
            return
        self.start(path)

    def on_listens(self, entries: list[dict]) -> None:
        """Ledger batch (play order): {path, listened_s, duration_s, ended_at}."""
        now = time.time()
        for e in entries:
            ended = e.get("ended_at") or now
            self.on_listen(e.get("path") or "", float(e.get("listened_s") or 0.0),
                           float(e.get("duration_s") or 0.0),
                           fresh=(now - ended) <= SKIP_SIGNAL_MAX_AGE_S)

    def on_listen(self, path: str, listened_s: float, duration_s: float,
                  fresh: bool = True) -> None:
        """One finished listen. A track listened through becomes the leg's
        reference; an early skip of a recommendation is a negative."""
        if not path:
            return
        if play_ledger.is_listen(listened_s, duration_s):
            if self.station is not None:
                self.station.accept(path)
            return
        if self.enabled and fresh and self._is_autoplay(path):
            self._negative(path)

    def reject(self, path: str) -> None:
        """The listener removed a recommendation (call AFTER the row is gone):
        hide it from auto-play for good (reversible in Settings), count it
        against the current leg, and top the buffer back up."""
        if not path:
            return
        self.hidden.add(path)
        self._run_task(self._persist_hidden, path)
        if self.station is not None:
            self.station.exclude([path])
        if self.enabled and not self._negative(path):
            have = len(self._pending_rows())
            if not self._filling and have < BUFFER_TARGET:
                self._schedule_fill(BUFFER_TARGET - have)

    def on_queue_dry(self) -> None:
        """The queue played out while enabled: append a block and play it."""
        if not self.enabled or self._filling:
            return
        self._schedule_fill(BUFFER_TARGET, append_and_play=True)

    def prefill_for_background(self) -> None:
        """Top the buffer up before the UI session may be torn down — with the
        session gone nothing refills, and playback would fall through to the
        rest of the queue."""
        self.ensure_buffer(BACKGROUND_TARGET)

    # ── listener feedback (Settings → Auto-play) ────────────────────────────
    async def load_feedback(self) -> None:
        """Hidden tracks and learned genre-hop penalties, from the DB."""
        try:
            self.hidden = {r["path"] for r in await self.db.get_autoplay_hidden()}
        except Exception:
            logger.warning("Auto-play: could not load hidden tracks", exc_info=True)
        await self._load_hop_rejects()

    async def _load_hop_rejects(self) -> None:
        try:
            rows = await self.db.get_hop_rejects()
        except Exception:
            logger.warning("Auto-play: could not load genre-hop feedback", exc_info=True)
            return
        self.hop_rejects = {(r["src"], r["dst"]): r for r in rows}

    def hop_weights(self) -> dict[tuple[str, str], float]:
        return {pair: hop_weight(rec) for pair, rec in self.hop_rejects.items()}

    def blocked_hops(self) -> list[dict]:
        """Blocked hops, most recently rejected first."""
        return [rec for rec in self.hop_rejects.values() if hop_blocked(rec)]

    async def unblock_hops(self, pairs: list[tuple[str, str]] | None = None) -> None:
        """Forget the rejections of `pairs` (None = every hop): they are taken
        at full weight again, including by the running station."""
        await self.db.clear_hop_rejects(pairs)
        cleared = list(self.hop_rejects) if pairs is None else [tuple(p) for p in pairs]
        for pair in cleared:
            self.hop_rejects.pop(pair, None)
            if self.station is not None:
                self.station.weight_hop(*pair, 1.0)

    async def _record_hop_reject(self, hop: tuple[str, str], path: str, artist: str) -> None:
        try:
            await self.db.record_hop_reject(hop[0], hop[1], path, artist)
        except Exception:
            logger.warning("Auto-play: could not persist genre-hop feedback", exc_info=True)
            return
        await self._load_hop_rejects()
        rec = self.hop_rejects.get(hop)
        if rec is None:
            return
        if self.station is not None:
            self.station.weight_hop(*hop, hop_weight(rec))
        if hop_blocked(rec):
            logger.info("Auto-play: genre hop %s → %s blocked (%d rejections, %d artists)",
                        hop[0], hop[1], rec["rejects"], rec["artists"])

    async def unhide(self, paths: list[str] | None = None) -> None:
        """Let `paths` (None = every hidden track) be recommended again."""
        await self.db.unhide_from_autoplay(paths)
        if paths is None:
            self.hidden.clear()
        else:
            self.hidden.difference_update(paths)

    async def _persist_hidden(self, path: str) -> None:
        try:
            await self.db.hide_from_autoplay(path)
        except Exception:
            logger.warning("Auto-play: could not persist hidden track", exc_info=True)

    # ── buffer ──────────────────────────────────────────────────────────────
    def _pending_rows(self) -> list[int]:
        q = self.engine.queue
        return [i for i in range(self.engine.current_index + 1, len(q))
                if q[i].get("_autoplay")]

    def buffer_paths(self) -> list[str]:
        """The pending auto-play tracks ahead of the playing row, in order."""
        q = self.engine.queue
        return [q[i]["path"] for i in self._pending_rows() if q[i].get("path")]

    def drop_buffer(self) -> int:
        """Remove every pending auto-play track (never the playing one)."""
        victims = self._pending_rows()
        if victims:
            self.engine.remove_indices(victims)
        return len(victims)

    def ensure_buffer(self, target: int = BUFFER_TARGET) -> None:
        if not self.enabled or self._filling:
            return
        have = len(self._pending_rows())
        low = BUFFER_LOW if target == BUFFER_TARGET else target
        if have < low:
            self._schedule_fill(target - have)

    # ── internals ───────────────────────────────────────────────────────────
    def _negative(self, path: str) -> bool:
        """Count a rejection; True when it ended the leg (the buffer is being
        replaced)."""
        self._leg_negatives.append(path)
        artist = self._artist_of(path)
        hop = self.station.hop_of(path) if self.station is not None else None
        if hop is not None:
            self._run_task(self._record_hop_reject, hop, path, artist)
        if artist:
            self._artist_negatives[artist] += 1
            if (self._artist_negatives[artist] >= ARTIST_NEGATIVES_TO_BAN
                    and self.station is not None):
                self.station.ban_artist_of(path)
        if len(self._leg_negatives) >= NEGATIVES_TO_END_LEG:
            self._end_leg()
            return True
        return False

    def _end_leg(self) -> None:
        """The leg is heading the wrong way: move the station on and replace the
        unheard buffer (it came from the same leg) in one op."""
        rejected, self._leg_negatives = self._leg_negatives, []
        if self.station is None:
            return
        self.station.end_leg(rejected)
        self._gen += 1            # supersede a fill already in flight
        self._filling = False
        self._schedule_fill(BUFFER_TARGET, replace=True)

    def _is_autoplay(self, path: str) -> bool:
        return any(t.get("_autoplay") and t.get("path") == path for t in self.engine.queue)

    def _artist_of(self, path: str) -> str:
        return self._artist_by_path.get(path) or next(
            (t.get("artist_name") or "" for t in self.engine.queue if t.get("path") == path), "")

    def _avoid(self) -> set[str]:
        """Everything a new station must not pick: queued recommendations,
        this session's history, hidden tracks, and the playing track. The
        library tail stays eligible — that is what gets promoted."""
        avoid = {t["path"] for t in self.engine.queue if t.get("_autoplay") and t.get("path")}
        avoid.update(self.history)
        avoid.update(self.hidden)
        if self.engine.current_path:
            avoid.add(self.engine.current_path)
        return avoid

    def _insert_after(self) -> int:
        """Below the last pending recommendation (keeps the buffer's order and
        stays ahead of the tail), or right after the playing row."""
        rows = self._pending_rows()
        return rows[-1] if rows else self.engine.current_index

    def _schedule_fill(self, n: int, *, replace: bool = False,
                       append_and_play: bool = False) -> None:
        if n <= 0 or (self._filling and not replace):
            return
        self._filling = True
        self._run_task(self._fill, n, self._gen, replace, append_and_play)

    async def _open(self, gen: int) -> tuple[track_graph.Station | None, bool]:
        """The current station, opening it on first use. (station, just_opened)."""
        if self.station is not None:
            return self.station, False
        seed = self.anchor or self.engine.current_path
        if not seed:
            return None, False
        self.anchor = seed
        rng = None
        if self.variety == RANDOM:
            self.station_seed = random.randrange(1 << 31)
            rng = random.Random(self.station_seed)
        station = await track_graph.open_station(
            self.db, seed, avoid=self._avoid(), rng=rng, leg_len=LEG_LEN,
            max_per_artist=ARTIST_CAP, max_per_album=ALBUM_CAP, window=CAP_WINDOW,
            hop_weights=self.hop_weights(),
        )
        if gen != self._gen:
            return None, False
        self.station = station
        logger.info("Auto-play: station from %s (%s%s)", os.path.basename(seed),
                    self.variety,
                    f", seed {self.station_seed}" if self.station_seed is not None else "")
        return station, True

    async def _fill(self, n: int, gen: int, replace: bool = False,
                    append_and_play: bool = False) -> None:
        try:
            station, opened = await self._open(gen)
            if station is None:
                return
            paths = await station.take(n)
            if gen != self._gen or not self.enabled:
                return
            batch = await self._tracks(paths)
            if gen != self._gen or not self.enabled:
                return
            if not batch and opened:
                await self._explain_empty(self.anchor)
            if append_and_play:
                if not batch:
                    self.engine.stop()
                    return
                first_new = len(self.engine.queue)
                self.engine.queue_extend(batch)
                self.engine.play_track_at(first_new)
            elif replace:
                # Even an empty batch drops the old buffer: it belonged to the
                # previous station or to the leg just rejected.
                self.engine.replace_rows(self._pending_rows(), batch)
            elif batch:
                self.engine.queue_after_current(batch, after_index=self._insert_after())
            if batch:
                logger.info("Auto-play: queued %d from %s", len(batch),
                            os.path.basename(self.anchor))
        except Exception:
            logger.exception("Auto-play: fill failed")
        finally:
            if gen == self._gen:
                self._filling = False

    async def _tracks(self, paths: list[str]) -> list[dict]:
        """Engine track dicts for `paths`, tagged as recommendations."""
        if not paths:
            return []
        rows = await self.db.get_tracks_brief(paths)
        batch = []
        for p in paths:
            row = rows.get(p)
            if row is None or p in self.hidden:
                continue
            self._artist_by_path[p] = row.get("artist") or ""
            batch.append({
                "path":        p,
                "track_title": row.get("title") or os.path.basename(p),
                "artist_name": row.get("artist") or "Unknown Artist",
                "album_title": row.get("album") or "Unknown Album",
                "duration":    row.get("duration") or 0.0,
                "_autoplay":   True,
            })
        return batch

    async def _explain_empty(self, seed: str) -> None:
        try:
            analysed = await self.db.has_current_features(seed, track_graph.FEATURES_VERSION)
        except Exception:
            analysed = True
        if not analysed:
            self._anchor_dead = True
            self._notify("This track isn't analysed yet — auto-play will pick up "
                         "from the next track that is.")
        else:
            self._notify("No similar tracks found for this one.")
