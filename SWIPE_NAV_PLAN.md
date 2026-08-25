# Swipe-to-Go-Back — Evaluation & Implementation Plan

Companion to `FLET_FACTS.md`. Part 1 audits that document against Flet 0.86.0 and
the current codebase. Part 2 is the feature plan.

---

# Part 1 — Critical Evaluation of `FLET_FACTS.md`

## 1.1 What checks out

Verified against `flet 0.86.0` (`miniconda3/.../flet/controls/core/gesture_detector.py`,
`flet/controls/events.py`):

| Claim | Verdict |
| :-- | :-- |
| `on_horizontal_drag_{down,start,update,end,cancel}` exist on `GestureDetector` | ✅ correct |
| `DragEndEvent.primary_velocity` | ✅ exists, `Optional[float]` — so the `or 0` guard is genuinely required, not cargo-cult |
| `DragUpdateEvent.primary_delta` / `local_delta` / `global_delta` | ✅ all present |
| `ft.Dismissible` with `direction` | ✅ present, and already used at `ui/player/queue_sheet.py:226` |
| `GestureDetector` accepts `left`/`top`/`bottom` for `Stack` positioning | ✅ it extends `LayoutControl`; those fields exist |
| Flutter touch slop ≈ 18 px | ✅ `kTouchSlop = 18.0` |
| Positive `primary_velocity` = left→right | ✅ correct, and matches the existing convention in `ui/player/now_playing.py:309` |

## 1.2 Factual corrections

1. **Transport is not JSON-RPC.** Flet 0.86 serialises control-tree patches and
   events with **msgpack**, not JSON-RPC. This is not pedantry — it is why a
   `set` in `SegmentedButton.selected` crashes `page.update()` in this app. Any
   claim about per-frame bridge cost has to be reasoned about in msgpack terms.

2. **The doc's desktop mitigation is the wrong lever.** It proposes raising the
   velocity threshold to `> 400` on macOS to survive trackpad scroll. Flet 0.86
   exposes `GestureDetector.allowed_devices: list[PointerDeviceType]`. Restricting
   the detector to `PointerDeviceType.TOUCH` removes trackpad and mouse from the
   gesture arena entirely — a hard guarantee instead of a tuned constant.

3. **Missing risk: there is no hit-test-behavior prop.** Flutter's
   `GestureDetector` takes `behavior: HitTestBehavior.opaque`; Flet 0.86's control
   has no such field (confirmed by dataclass field dump). The doc's Pattern 2 uses
   `ft.Container(width=28, bgcolor="transparent")` and assumes it receives
   pointers. That depends entirely on whether the Flet Dart side wraps the child
   opaquely. **This must be verified on device before the rest of the design is
   committed to.** If it does not hit-test, the fallback is a container with a
   near-zero-alpha but non-transparent bgcolor (e.g. `"#01000000"`).

4. **Missing knob: `drag_interval`.** If a live "peek" animation is ever driven
   from `on_horizontal_drag_update`, every pointer move becomes a msgpack round
   trip to Python and back. `drag_interval` throttles this. The doc discusses
   drag-update deltas without mentioning the throttle that makes them affordable.

## 1.3 The plan's central premise is already falsified in this repo

`StreamripApp/main.py:1035`:

```python
# Content area (removed swipe detector GestureDetector to prevent accidental tab switching)
self._swipe_content = self._tab_content
```

A full-pane horizontal detector **was already built, shipped, and reverted.**
Its handler `_on_swipe` still sits at `main.py:1135` as dead code, complete with a
comment recording that the threshold had already been pushed to `1000` px/s and
was *still* too twitchy:

```python
# Increased threshold to 1000 to make swiping less aggressive
if abs(vx) < 1000:
```

**Consequence:** Pattern 1 as written (a pane-wide detector) is not an untested
idea — it is a known regression. The plan must start from Pattern 2 (bounded
gesture surface), and the dead `_on_swipe` should be deleted so it stops reading
like live behaviour.

## 1.4 Pattern 1's tier discriminator does not work as written

The doc keys the two tiers off `self._current_subpage_name`. In
`ui/views/settings.py`:

- `:1222` — `_show_sub_page()` **sets** `self._current_subpage_name = title`
- `:1211` — `_show_hub()` **never clears it**

So after the user opens any subpage once and returns to the hub, the flag stays
truthy forever. Tier 2 would fire on every subsequent right-swipe and Tier 1
(hub → originating pane) would become unreachable. The user would be trapped in
Settings with a gesture that silently does nothing.

## 1.5 `self.app._previous_tab` does not exist

Grepped across `main.py` and `ui/`: nothing reads or writes `_previous_tab`.
Meanwhile Settings (absolute tab index `3`) is entered from at least six call
sites, each from a different origin:

- `ui/views/assistant.py:114`
- `ui/views/search.py:232`, `:285`, `:489`, `:914`
- `ui/views/library.py:536`, `:561`, `:563`, `:565` (the last also deep-links to the Storage subpage)
- `main.py:713` (onboarding completion)

The originating tab is currently discarded by `_switch_tab` (`main.py:1187`),
which overwrites `self._current_tab` with no history. This has to be added as a
prerequisite, not assumed.

## 1.6 Pattern 2's left bezel collides with the Android system back gesture

This is the largest omission. On Android 10+ with gesture navigation, the OS
reserves roughly the leftmost and rightmost 20 dp for the system back gesture and
consumes those pointers **before the Flutter view sees them**. A 28 px
left-bezel detector is therefore mostly unreachable on the exact platform it was
designed for.

The deeper issue this exposes: **the app has no back handling at all.** There is
no `page.views` stack, no `page.on_view_pop` wiring, no lifecycle back hook
(`page.on_app_lifecycle_state_change` at `main.py:277` is the only page-level
handler). Today, pressing system back inside a Settings subpage backgrounds or
closes the whole app rather than going up one level. Swipe-back is a *workaround
for a missing back handler*, and the plan should say so explicitly.

Two ways forward, and they are not equivalent:

- **(A) Inset gesture surface** — place the strip inboard of the OS zone (start
  at ~24 px from the edge, ~40 px wide) or accept a right-edge/full-width-header
  surface instead. Cheap, no architectural change, but it is a second back
  affordance that behaves differently from the system one.
- **(B) Migrate Settings to `page.views`** so `page.on_view_pop` receives the
  hardware/gesture back. Correct long-term, but it touches the view cache
  (`_view_cache`), the mini-player and nav-bar layout (`main.py:1070`), and the
  `_tab_content` content-swap model. Large.

**Recommendation: ship (A) now, log (B) as the real fix.** The plan below is
structured so that (B) can be adopted later without rewriting the navigation
logic — because the logic is deliberately kept out of the gesture handler.

## 1.7 Conflict inventory the doc does not have

Panes where a horizontal back-swipe must **not** be live:

| Surface | File | Existing horizontal-drag owner |
| :-- | :-- | :-- |
| Now Playing artwork | `ui/player/now_playing.py:59` | `on_horizontal_drag_end` → prev/next track |
| Now Playing sheet | `ui/player/now_playing.py:265` | `draggable=True` — native swipe-down dismiss |
| Queue rows | `ui/player/queue_sheet.py:226` | `ft.Dismissible`, both directions |
| Network graph | `ui/views/library.py:1397` | `InteractiveViewer` claims pan natively |
| EQ curve | `ui/views/settings.py:54` | `on_pan_start/update/end` on the canvas |

Two notes on that table:

- `now_playing.py:309` `_handle_swipe` has **no velocity threshold** — any
  non-zero `primary_velocity` skips a track. That is a pre-existing sensitivity
  bug and it is directly relevant: it is the same class of failure that got the
  tab-swipe reverted.
- The EQ curve and the network graph both live *inside* Settings and Library
  respectively. An edge strip stacked above them steals the leftmost EQ band and
  left-edge pan starts. Visibility gating is mandatory, not optional polish.

No conflict: the view-mode chips (`library.py:2514`, `search.py:1073`) are
`on_tap` only.

## 1.8 Where the feature actually pays off

Ranked by value, having traced the real navigation graph:

1. **Settings subpage → hub.** The only genuine hierarchical nesting in the app.
   The current affordance is a 16 px `ARROW_BACK_IOS_NEW_ROUNDED` icon at
   `settings.py:1227` — a small target at the top of a scrollable page.
2. **Settings hub → originating tab.** No exit exists at all. The nav bar has
   only three destinations and `_switch_tab` sets `indicator_color` to
   `"transparent"` when on Settings (`main.py:1214`), so nothing is highlighted.
   The only way out is guessing which tab to tap.
3. **Metadata Workbench / Enrichment Wizard** (`settings.py:1867`, `:1876`) —
   both are constructed with an `on_back` callback already, so they slot straight
   into the same resolver.
4. **Not the three main tabs.** They are lateral siblings with a persistent nav
   bar. That is precisely what was tried and reverted.

## 1.9 Assets the doc missed that the plan should use

- `trigger_haptic(action)` (`main.py:837`) with per-action intensity from config.
  Swipe haptics are already a modelled concept: `swipe_queue_intensity` and
  `swipe_dismiss_intensity` have Settings dropdowns at `settings.py:530`/`:547`.
- `safe_update(fn)` (`main.py:920`) — the coalesced, session-guarded update path.
  Every mutation from a gesture handler must go through it.
- `page.on_keyboard_event` — gives desktop an ESC binding to the same intent,
  which is the correct desktop affordance rather than a tuned drag threshold.

---

# Part 2 — Plan

## 2.0 Feature definition

> A touch-only edge drag, and on desktop the ESC key, resolves **one level of
> back-navigation** in the pane that owns it. Scope for v1 is the Settings
> hierarchy — subpage → hub → the tab the user came from. Main tabs are
> explicitly out of scope.

**Design principle that makes the rest of this tractable:** the gesture is only a
*trigger*. All resolution lives in one function on the app. Swipe, ESC, and — if
option (B) is ever adopted — hardware back, all call the same entry point.

## Phase 0 — Fix the broken preconditions

These are bugs today, independent of the gesture. Do them first; each is testable
on its own.

1. **`main.py:1211` `_show_hub`** — set `self._current_subpage_name = None`.
   Also clears the stale `_baseline_subpage_state`. Fixes §1.4.
2. **`main.py:1187` `_switch_tab`** — record the outgoing tab before the
   overwrite. Only remember non-Settings tabs, so entering Settings from Settings
   cannot make it self-referential:
   ```python
   if index == 3 and self._current_tab != 3:
       self._previous_tab = self._current_tab
   ```
   Initialise `self._previous_tab = 2` (Library) alongside `_current_tab`. Fixes §1.5.
3. **Delete `_on_swipe` (`main.py:1135`)** — dead since the detector was pulled at
   `:1035`. Leaving it invites someone to re-wire the reverted regression.

## Phase 1 — One back-intent resolver

Add to `StreamripFletApp`:

```python
def navigate_back(self) -> bool:
    """Resolve one level of back-navigation. Returns True if consumed."""
```

Resolution order, most-nested first:

1. An open sheet or dialog → dismiss it by name via `dismiss_dialog(...)`, never
   `page.pop_dialog()` — the toast-eats-the-pop hazard is documented at
   `main.py:2834`. Requires tracking the topmost owned sheet; if that bookkeeping
   is not already available, **skip this rung in v1** rather than guess.
2. `_current_tab == 3` and `settings_view._current_subpage_name` → `_show_hub()`.
3. `_current_tab == 3` (hub) → `_switch_tab(self._previous_tab)`.
4. Otherwise → return `False` (nothing consumed; no visual response).

On a `True` return, fire `trigger_haptic("swipe_back")` and add a
`swipe_back_intensity` dropdown next to the two existing swipe haptic dropdowns
at `settings.py:530`.

This function is pure Python over app state, which is what makes Phase 3's tests
possible without a running Flutter engine.

## Phase 2 — The gesture surface

Add one detector to `_root_stack` (`main.py:1103`), above `safe_root` and below
`error_boundary._error_view`:

```python
self._edge_back = ft.GestureDetector(
    content=ft.Container(width=40, bgcolor="#01000000"),
    left=24,            # inboard of the Android system back-gesture zone
    top=0,
    bottom=140,         # clear of mini-player + NavigationBar
    visible=False,      # gated by _switch_tab
    allowed_devices=[ft.PointerDeviceType.TOUCH],
    on_horizontal_drag_end=self._on_edge_back,
)
```

Handler:

```python
def _on_edge_back(self, e):
    v = getattr(e, "primary_velocity", 0) or 0
    if v > 250:
        self.navigate_back()
```

Gating — in `_switch_tab`'s `_mutate()`, set `self._edge_back.visible = (index == 3)`.
This keeps the strip off the network graph's `InteractiveViewer` and off the
Library and Search panes entirely. Inside Settings, the EQ curve
(`settings.py:54`) is the one remaining overlap; `left=24, width=40` clips the
leftmost ~64 px of the EQ canvas, so **confirm on device whether the lowest band
is still draggable**, and if not, narrow the strip or gate it off the DSP subpage
by name.

Desktop: bind `page.on_keyboard_event` → on `Escape`, call `navigate_back()`.
No drag path on desktop at all, because `allowed_devices` excludes the trackpad.

**Deferred (Phase 2b):** live drag-tracking "peek" via `on_horizontal_drag_update`
plus `offset`/`animate_offset` on `_tab_content`. Real polish, but it puts a
msgpack round-trip on every pointer move. If attempted, set `drag_interval=16`
first. Ship velocity-only and judge whether the peek is missed.

## Phase 3 — Verification

**Unit** — new `StreamripApp/tests/test_swipe_navigation.py`, following the style
of the existing `test_settings_search.py` / `test_assistant_view.py`:

- `navigate_back()` on a Settings subpage calls `_show_hub` and does not switch tabs.
- `navigate_back()` on the hub switches to `_previous_tab`.
- Entering Settings from Library, then from Search, yields the right
  `_previous_tab` each time.
- `_show_hub()` clears `_current_subpage_name` (the Phase 0 regression guard).
- A `DragEndEvent` stub with `primary_velocity=None` does not raise.
- Velocities of `+100` (below threshold) and `-400` (wrong direction) are no-ops.

Run under **miniconda base**, not py310.

**On device** — this is a Python-only change, so no Dart rebuild is needed;
`install -r` preserves data. Three things can only be answered on hardware, in
this order — if #1 fails the whole Pattern-2 approach needs rethinking, so check
it first with a throwaway build:

1. Does a near-transparent `Container` inside a `GestureDetector` receive
   pointers at all, given Flet 0.86 exposes no `HitTestBehavior`? (§1.2.3)
2. Does Android gesture navigation still eat the strip at `left=24`? Sweep the
   inset if so. (§1.6)
3. Is the EQ curve's lowest band still draggable? (§2 gating note)

## Open decisions for you

- **Sheets in the resolver (rung 1).** Should the gesture also close Now Playing
  and the Queue sheet? Both already have native swipe-down dismiss
  (`draggable=True`), and both bind horizontal drags to other actions, so my
  recommendation is **no** — leave them out and keep the strip Settings-only in v1.
- **Option (B).** Worth scheduling the `page.views` migration so that hardware
  back works? That is the difference between an extra gesture and a correct app.
- **`now_playing.py:309`.** The unthresholded track-skip swipe is a live bug in
  the same family. Fold a threshold into this work, or track separately?
