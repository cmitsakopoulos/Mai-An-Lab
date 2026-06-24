# Playback-Queue Fixes (diagnosis + patches)

Scope: the **playback** queue in `StreamripApp/utils/audio_engine.py` (not the
download queue in `queue_controller.py`). Symptoms reported:

- Previously-played / reloaded track shows duration **0:00 / 0:00**.
- When a queue ends it's "dead" — you have to start a new one; no resume.
- Aggressive use (rapid skips / many mutations) → **"Playback error"** toasts and
  bad UI sync ("thread thrashing").

Fixes are ordered by risk. **Fix 1 and Fix 2 are safe and isolated. Fix 3 is the
real race fix and needs careful testing.**

> Line numbers below are approximate (match the current working tree, not HEAD).
> All edits are in `StreamripApp/utils/audio_engine.py` unless noted.

---

## Fix 1 — Duration stuck at 0:00 (safe, 1 line of logic)

### Root cause
`_sync_metadata_for_current()` hard-resets `duration` to `0.0` on every track
switch and **never seeds it from the track's own metadata**, relying entirely on
Dart's `durationStream` to re-report. When a track isn't actively (re)loaded —
e.g. reloading a **completed** queue without starting playback — Dart never
emits a duration, so the UI is stuck at `0:00 / 0:00`.

The track dicts already carry `duration` in **seconds** (see
`_track_to_playlist_item`, which converts `track.get("duration")` → `duration_ms`),
and `audio_engine.duration` is also in seconds — so the units match.

### Patch
In `_sync_metadata_for_current()`:

```python
        self._set("position", 0.0)
        self._set("duration", 0.0)
```

→

```python
        self._set("position", 0.0)
        # Seed duration from the track's own metadata so the slider shows the
        # correct length immediately — before Dart's durationStream reports, and
        # even when playback isn't (re)started (e.g. reloading a completed
        # queue, where Dart never re-emits duration → stuck at 0:00). Dart's
        # later duration_ms (exact, from the decoder) overwrites this via _set's
        # dirty-check if it differs.
        try:
            seeded_dur = float(track.get("duration") or 0.0)
        except (TypeError, ValueError):
            seeded_dur = 0.0
        self._set("duration", seeded_dur)
```

### Also apply to `restore_queue()` (already seeds from saved `duration`, but make
it prefer the per-track value if the saved snapshot duration is 0):
`restore_queue` sets `self._set("duration", max(0.0, float(duration)))`. If the
persisted `duration` was 0 (because it was saved mid-glitch), the per-track value
is a better fallback. Optional — Fix 1 in `_sync_metadata_for_current` covers most
cases because restore calls into the same metadata path on track changes.

---

## Fix 2 — "Dead" queue can't be replayed (safe, small)

### Root cause
On `processing_state == "completed"` (and not `repeat_mode == "all"`), the engine
just does `self._set("is_playing", False)`. The Dart `just_audio` player is now in
the **completed** state at the end of the last track. A bare `play()` won't resume
a completed player, so pressing play does nothing — you have to build a new queue.

### Patch — part A: mark the terminal state
In `_on_state_change`, the completion `else` branch:

```python
            else:
                self._set("is_playing", False)
                if getattr(self, "jarvis_controlled", False):
                    self.dispatch("on_jarvis_continue")
                elif getattr(self, "play_similar_seed_path", ""):
                    self.dispatch("on_similar_continue")
```

→

```python
            else:
                self._set("is_playing", False)
                if getattr(self, "jarvis_controlled", False):
                    self.dispatch("on_jarvis_continue")
                elif getattr(self, "play_similar_seed_path", ""):
                    self.dispatch("on_similar_continue")
                else:
                    # Terminal end of a finite queue: remember it so the next
                    # play() restarts from the top instead of no-opping on a
                    # 'completed' Dart player.
                    self._completed_terminal = True
```

Add `self._completed_terminal = False` to `__init__`.

### Patch — part B: make `play()` restart a finished queue
Replace `play()`:

```python
    def play(self):
        if self._audio and self._page:
            self._page.run_task(self._audio.play)
```

→

```python
    def play(self):
        if not (self._audio and self._page):
            return
        if getattr(self, "_completed_terminal", False) and self.queue:
            # Replay a finished queue from the top; a 'completed' player won't
            # resume on a bare play().
            self._completed_terminal = False
            self.current_index = 0
            self._sync_metadata_for_current()
            self._arm_queue_gate()
            self._page.run_task(self._push_queue_native, 0, True)
            return
        self._page.run_task(self._audio.play)
```

### Patch — part C: clear the flag whenever playback (re)starts
Set `self._completed_terminal = False` at the top of `set_queue()`,
`play_track_at()`, `next()`, and `previous()` so a normal skip/new-queue clears it.
(One line each.)

> UX note: with this, when a queue ends the last track stays shown; pressing play
> restarts from track 0. If you'd rather it visibly reset to track 0 the moment it
> ends, move the `current_index = 0` + `_sync_metadata_for_current()` into the
> completion branch instead of `play()`.

---

## Fix 3 — Race / "thread thrashing" on rapid skip + mutation (involved; test!)

### Root cause
Queue **mutations** (`queue_next`, `remove_from_queue`, `move_queue_item`, …) are
serialized under `self._native_lock`. **Skips are not** — `next()`, `previous()`,
`play_track_at()` each:
1. mutate `self.current_index` locally,
2. fire a separate `self._page.run_task(self._audio.skip_to_*)`, and
3. re-arm the wall-clock gate `_arm_queue_gate(0.8)`.

Fire these quickly and you get: local-vs-Dart index divergence, a flurry of
competing `run_task`s, repeated `on_queue_mutated` → full `queue_sheet.refresh`
rebuilds, and — when a skip lands mid-mutation — `just_audio` throwing on its live
`ConcatenatingAudioSource`. Those throws surface as the **"Playback error:"**
snackbar (`main.py:458`, the `on_playback_error` handler). The wall-clock gate is
a band-aid that fast input defeats.

### Patch — part A (quick win): stop benign races from toasting
Most of these errors are transient just_audio load races, not real failures.
Filter them out of the user-facing snackbar. In `main.py` where the handler is
registered (~line 458):

```python
on_playback_error=lambda _, d: self.show_snackbar(f"Playback error: {d}", ...),
```

→ route through a filter:

```python
on_playback_error=lambda _, d: self._on_playback_error_toast(d),
```

and add:

```python
    def _on_playback_error_toast(self, detail: str):
        # Swallow transient just_audio load/abort races that fire during rapid
        # skip/mutation; they self-recover and shouldn't alarm the user. Only
        # surface genuine, sticky failures.
        benign = ("abort", "interrupted", "Source error", "Loading interrupted",
                  "Connection", "setAudioSource")
        if any(b.lower() in str(detail).lower() for b in benign):
            logger.warning("Suppressed transient playback error: %s", detail)
            return
        self.show_snackbar(f"Playback error: {detail}",
                           icon=ft.Icons.ERROR_OUTLINE, color="#FF4444")
```

### Patch — part B (the real fix): coalesce + serialize skips
Make rapid skips collapse into **one** `skip_to_index(final)` issued under the
same lock as mutations, instead of N competing skips.

Add to `__init__`:
```python
        self._skip_target: int | None = None   # native (post-shuffle) index
        self._skip_task = None
```

Add helpers:
```python
    def _request_skip(self, native_index: int):
        """Coalesce rapid skips: the UI index is already updated by the caller;
        debounce the actual Dart skip so a burst of taps issues ONE
        skip_to_index(final) under the native lock instead of N racing skips
        (the source of the 'malformed queue' errors)."""
        self._skip_target = native_index
        self._arm_queue_gate(0.8)
        if self._page and (self._skip_task is None or self._skip_task.done()):
            self._skip_task = self._page.run_task(self._run_coalesced_skip)

    async def _run_coalesced_skip(self):
        import asyncio
        # Settle: wait out the burst until the target stops moving.
        last = object()
        while last != self._skip_target:
            last = self._skip_target
            await asyncio.sleep(0.18)
        target = self._skip_target
        if target is None:
            return
        if self._native_lock is None:
            self._native_lock = asyncio.Lock()
        async with self._native_lock:        # serialize against mutations
            if self._audio:
                try:
                    await self._audio.skip_to_index(target)
                    await self._audio.play()
                except Exception as exc:
                    logger.warning("coalesced skip failed: %s", exc)
```

Then in `next()`, `previous()`, `play_track_at()`: keep the local
`current_index` / `_sync_metadata_for_current()` updates (so the UI is instant),
but replace the direct
`self._page.run_task(self._audio.skip_to_index, target)` /
`skip_to_next` / `skip_to_previous` calls with:

```python
        self._request_skip(target)   # target = the native/post-shuffle index
```

For `next()`/`previous()` compute `target` the same way the code already computes
the native index (the `_shuffle_order.index(...)` logic), then pass it to
`_request_skip`. This converts "N taps → N skips" into "N taps → 1 skip", which
removes both the thrashing and the mid-mutation throws.

> Test matrix: rapid next×5, rapid prev×5, next during an add, remove the playing
> track, shuffle on/off mid-burst, skip past end-of-queue (should hit the Fix-2
> terminal path), and skip into a just-added tail track.

### Optional part C: replace the wall-clock gate with index-trust
Longer-term, `_arm_queue_gate` (wall-clock suppression of Dart→Python index
mirroring) is the fragile bit. Once skips are coalesced+serialized (part B), you
can shorten the gate to ~0.3s, or drop it in favour of trusting Dart's
`currentIndex` once `processing_state == "ready"` (i.e. only accept queue_index
mirroring when the player has settled). Do this only after B is stable.

---

## Suggested apply order
1. **Fix 1** (duration) — ship + verify 0:00 is gone on reload.
2. **Fix 2** (replay finished queue) — verify play restarts an ended queue.
3. **Fix 3A** (toast filter) — quick relief from the error spam.
4. **Fix 3B** (coalesce skips) — the real race fix; test the matrix above.
5. **Fix 3C** (gate cleanup) — only after 3B is proven.

Remember: extension Dart/Kotlin/res changes need a **fresh** build
(`fresh_build_android.sh`); pure-Python changes (all of the above) work with the
incremental `build_android.sh`.
