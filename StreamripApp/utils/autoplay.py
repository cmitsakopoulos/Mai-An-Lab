"""Auto-play: keep a rolling buffer of similar tracks queued after the current one.

The buffer is the run of `_autoplay`-tagged tracks ahead of the playing row. It
is inserted non-destructively (the rest of the queue, e.g. the library tail, is
kept below it) and topped up automatically whenever the playing track changes —
there is no manual "replenish" anywhere.

Two anchor modes decide where refills come from:

  FOLLOW  the station follows you. A track you choose restarts it from that
          track, and refills seed from the last track you LISTENED THROUGH — so
          it drifts, but only along songs you accepted (never from the unheard
          end of its own buffer, which is how chained refills used to wander).
  STAY    the station stays put. Choosing a track just plays it; refills keep
          paging deeper into the anchor's own ranking, so nothing drifts.

Feedback is implicit: an early skip (see play_ledger.is_listen) keeps a track
out for the session, and two early skips of auto-play tracks in a row re-plan
the buffer. Tracks started this session are never re-recommended.

Cost model: nothing runs on a timer. Work happens on a track change (a scan of
the queue tail) and, when the buffer runs low, one walk + one batched DB read.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque

from utils import play_ledger, track_graph

logger = logging.getLogger(__name__)

FOLLOW = "follow"
STAY = "stay"
ANCHOR_MODES = (FOLLOW, STAY)

BUFFER_TARGET = 8        # tracks kept queued ahead while in the foreground
BUFFER_LOW = 4           # refill when fewer than this remain
BACKGROUND_TARGET = 24   # ~1.5 h, topped up once before the UI goes away
HISTORY_LEN = 150        # tracks started this session, never re-recommended
REJECT_LEN = 50          # early skips / removals, never re-recommended
SKIPS_TO_REPLAN = 2      # consecutive early skips of auto-play tracks
SKIP_SIGNAL_MAX_AGE_S = 180  # ledger entries older than this don't re-plan


class AutoPlay:
    """Owns auto-play state and every queue edit it makes. `run_task(fn, *args)`
    schedules a coroutine function on the UI loop; `notify(message)` surfaces a
    user-facing hint (e.g. the seed has no DSP features yet)."""

    def __init__(self, engine, db, run_task, notify=None):
        self.engine = engine
        self.db = db
        self._run_task = run_task
        self._notify = notify or (lambda _msg: None)

        self.enabled = False
        self.anchor_mode = FOLLOW
        self.anchor = ""            # the track the station started from
        self._last_accepted = ""    # last track listened through (FOLLOW seed)
        self.history: deque[str] = deque(maxlen=HISTORY_LEN)
        self.rejected: deque[str] = deque(maxlen=REJECT_LEN)
        self._skip_run = 0
        # The anchor had no DSP features, so it can't seed anything: the next
        # track that starts takes over as the anchor.
        self._anchor_dead = False
        # Generation: bumped whenever the plan changes (toggle, restart,
        # re-plan) so a fill that was awaiting the walk bails instead of
        # inserting a stale block.
        self._gen = 0
        self._filling = False

    # ── mode ────────────────────────────────────────────────────────────────
    def set_enabled(self, on: bool, seed: str = "") -> None:
        if on == self.enabled:
            return
        self._new_plan()
        self.enabled = on
        if on:
            self._set_anchor(seed or self.engine.current_path)
            if self.anchor:
                self._schedule_fill(self.anchor, BUFFER_TARGET, first=True)
        else:
            self._set_anchor("")
            self.drop_buffer()

    def set_anchor_mode(self, mode: str) -> None:
        if mode in ANCHOR_MODES:
            self.anchor_mode = mode

    def _set_anchor(self, path: str) -> None:
        self.anchor = path or ""
        self._last_accepted = ""
        self._anchor_dead = False
        # The engine calls on_similar_continue at queue end only while this is set.
        self.engine.play_similar_seed_path = self.anchor

    def _new_plan(self) -> None:
        self._gen += 1
        self._filling = False
        self._skip_run = 0

    # ── events from the app ─────────────────────────────────────────────────
    def on_track_changed(self, path: str) -> None:
        """The playing track changed (skip, auto-advance or a tap)."""
        if path:
            self.history.append(path)
        if self.enabled and self._anchor_dead and path and path != self.anchor:
            self._new_plan()
            self._set_anchor(path)
            self._schedule_fill(path, BUFFER_TARGET, first=True)
            return
        self.ensure_buffer()

    def on_user_played(self, path: str) -> None:
        """The user explicitly chose `path`. FOLLOW restarts the station from
        it; STAY keeps the station, and the track just plays."""
        if not self.enabled or not path:
            return
        if self.anchor_mode == STAY and self.anchor:
            self.ensure_buffer()
            return
        self.restart_from(path)

    def restart_from(self, path: str) -> None:
        """Re-anchor the station on `path` and replace the pending buffer (both
        modes; this is also "Start radio")."""
        if not self.enabled or not path:
            return
        self._new_plan()
        self._set_anchor(path)
        # Claim the fill BEFORE dropping: the drop's queue-mutation event runs
        # ensure_buffer, which must not start its own refill meanwhile.
        self._schedule_fill(path, BUFFER_TARGET, first=True)
        self.drop_buffer()

    def adopt_anchor(self, path: str) -> None:
        """After a session restore: give an enabled station its anchor back
        without touching the restored queue."""
        if self.enabled and not self.anchor and path:
            self._set_anchor(path)

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
        """One finished listen. Accepted listens become the FOLLOW seed; early
        skips are excluded for the session, and a run of them re-plans."""
        if not path:
            return
        if play_ledger.is_listen(listened_s, duration_s):
            self._last_accepted = path
            self._skip_run = 0
            return
        self._remember_rejected(path)
        if not (self.enabled and fresh and self._is_autoplay(path)):
            return
        self._skip_run += 1
        if self._skip_run >= SKIPS_TO_REPLAN:
            self.replan()

    def reject(self, path: str) -> None:
        """"Not this": keep `path` out of every refill this session."""
        self._remember_rejected(path)

    def on_queue_dry(self) -> None:
        """The queue played out while enabled: append a block and play it."""
        if not self.enabled or self._filling:
            return
        seed = self._refill_seed()
        if not seed:
            self.engine.stop()
            return
        self._schedule_fill(seed, BUFFER_TARGET, append_and_play=True)

    def prefill_for_background(self) -> None:
        """Top the buffer up before the UI session may be torn down — with the
        session gone nothing refills, and playback would fall through to the
        rest of the queue."""
        self.ensure_buffer(BACKGROUND_TARGET)

    # ── buffer ──────────────────────────────────────────────────────────────
    def buffer_paths(self) -> list[str]:
        """The pending auto-play tracks ahead of the playing row, in order."""
        q = self.engine.queue
        return [t["path"] for t in q[self.engine.current_index + 1:]
                if t.get("_autoplay") and t.get("path")]

    def drop_buffer(self) -> int:
        """Remove every pending auto-play track (never the playing one)."""
        q = self.engine.queue
        ci = self.engine.current_index
        victims = [i for i in range(ci + 1, len(q)) if q[i].get("_autoplay")]
        if victims:
            self.engine.remove_indices(victims)
        return len(victims)

    def ensure_buffer(self, target: int = BUFFER_TARGET) -> None:
        if not self.enabled or self._filling:
            return
        have = len(self.buffer_paths())
        low = BUFFER_LOW if target == BUFFER_TARGET else target
        if have >= low:
            return
        seed = self._refill_seed()
        if seed:
            self._schedule_fill(seed, target - have)

    def replan(self) -> None:
        """The buffer is heading the wrong way: drop it and refill from the
        last accepted track (FOLLOW) or the anchor (STAY)."""
        if not self.enabled:
            return
        self._new_plan()
        seed = self._refill_seed()
        if seed:
            self._schedule_fill(seed, BUFFER_TARGET)  # before the drop; see on_user_played
        self.drop_buffer()

    # ── internals ───────────────────────────────────────────────────────────
    def _refill_seed(self) -> str:
        if self.anchor_mode == STAY:
            return self.anchor or self.engine.current_path
        return self._last_accepted or self.anchor or self.engine.current_path

    def _remember_rejected(self, path: str) -> None:
        if path and path not in self.rejected:
            self.rejected.append(path)

    def _is_autoplay(self, path: str) -> bool:
        return any(t.get("_autoplay") and t.get("path") == path for t in self.engine.queue)

    def _avoid(self) -> set[str]:
        """Everything a refill must not pick: queued auto-play tracks (ahead or
        already played), this session's history and rejections, and the current
        track. The library tail stays eligible — that is what gets promoted."""
        avoid = {t["path"] for t in self.engine.queue if t.get("_autoplay") and t.get("path")}
        avoid.update(self.history)
        avoid.update(self.rejected)
        if self.engine.current_path:
            avoid.add(self.engine.current_path)
        return avoid

    def _insert_after(self) -> int:
        """Below the last buffered track (keeps the buffer's order and stays
        ahead of the tail), or right after the playing row."""
        q = self.engine.queue
        after = self.engine.current_index
        for i in range(after + 1, len(q)):
            if q[i].get("_autoplay"):
                after = i
        return after

    def _schedule_fill(self, seed: str, n: int, *, first: bool = False,
                       append_and_play: bool = False) -> None:
        if self._filling or n <= 0:
            return
        self._filling = True
        self._run_task(self._fill, seed, n, self._gen, first, append_and_play)

    async def _fill(self, seed: str, n: int, gen: int, first: bool = False,
                    append_and_play: bool = False) -> None:
        try:
            batch = await self._walk_block(seed, n, gen)
            if batch is None:
                return  # superseded by a newer plan
            if not batch:
                if first:
                    await self._explain_empty(seed)
                elif append_and_play:
                    self.engine.stop()
                return
            if append_and_play:
                first_new = len(self.engine.queue)
                self.engine.queue_extend(batch)
                self.engine.play_track_at(first_new)
            else:
                self.engine.queue_after_current(batch, after_index=self._insert_after())
            logger.info("Auto-play: queued %d similar tracks from %s",
                        len(batch), os.path.basename(seed))
        except Exception:
            logger.exception("Auto-play: fill from %s failed", seed)
        finally:
            if gen == self._gen:
                self._filling = False

    async def _walk_block(self, seed: str, n: int, gen: int) -> list[dict] | None:
        """Walk from `seed` and build engine track dicts. None = the plan
        changed while awaiting (caller must not touch the queue)."""
        paths = await track_graph.walk(self.db, seed, length=n, avoid=self._avoid())
        if gen != self._gen or not self.enabled:
            return None
        if not paths:
            return []
        rows = await self.db.get_tracks_brief(paths)
        if gen != self._gen or not self.enabled:
            return None
        # Re-read: the queue or history may have changed during the awaits.
        avoid = self._avoid()
        batch = []
        for p in paths:
            row = rows.get(p)
            if row is None or p in avoid:
                continue
            avoid.add(p)
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
