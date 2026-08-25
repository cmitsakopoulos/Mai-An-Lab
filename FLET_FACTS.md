# Flet Architectural & Gesture Mechanics Reference

This document provides a technical overview of **Flet**, its Flutter-backed architecture, event system, and gesture/swipe mechanics with specific reference to navigation workflows in Mai An Lab.

---

## 1. Core Architecture of Flet

* **Flutter Runtime Foundation**: Flet applications run on top of the Flutter engine. The Python layer provides declarative state and control trees, while Flutter renders controls at native performance (60/120 fps) via Skia/Impeller.
* **Client-Server Event Bridge**: Communication between Python runtime and Flutter UI happens across an asynchronous JSON-RPC protocol over websockets or local sockets.
* **Control Hierarchy**: Python controls (`ft.Container`, `ft.Column`, `ft.ListView`, `ft.GestureDetector`) map directly to Flutter widgets (`Container`, `Column`, `ListView`, `GestureDetector`).
* **Page Lifecycle & Task Scheduling**:
  * Asynchronous tasks run in the Python event loop (`self.page.run_task(...)` or `asyncio.create_task(...)`).
  * UI mutations are synced via `page.update()` or guarded via `safe_update(fn)` to prevent race conditions during teardown or background transitions.

---

## 2. Gesture & Swipe Mechanics

Flet exposes Flutter's Gesture Arena system through two primary controls:

### A. `ft.GestureDetector`
`ft.GestureDetector` is the primary mechanism for detecting taps, long-presses, scale/pinch, and 1D/2D drags.

#### Key Event Handlers for Swiping:
* **`on_horizontal_drag_start`** (`ft.DragStartEvent`): Fired when a horizontal contact initiates.
* **`on_horizontal_drag_update`** (`ft.DragUpdateEvent`): Fired continuously as the pointer moves horizontally.
  * `e.primary_delta`: Movement along the primary horizontal axis since the last update.
  * `e.global_delta` / `e.local_delta`: 2D movement deltas.
* **`on_horizontal_drag_end`** (`ft.DragEndEvent`): Fired when contact ceases (finger lifted / mouse released).
  * `e.primary_velocity`: Velocity in pixels per second.
    * **Positive (`> 0`)**: Left-to-right swipe (standard "Back" gesture).
    * **Negative (`< 0`)**: Right-to-left swipe (standard "Forward / Next" gesture).

#### Safe Velocity Extraction Pattern:
```python
def handle_swipe(e: ft.DragEndEvent):
    # Protect against NoneType or missing attribute across platforms
    velocity = getattr(e, "primary_velocity", 0) or 0
    
    if velocity > 300:
        # User swiped left-to-right (Back)
        pass
    elif velocity < -300:
        # User swiped right-to-left (Forward)
        pass
```

### B. `ft.Dismissible`
A specialized widget designed for list item dismissal (e.g., swiping a track to remove it from a playlist/queue).
* `direction`: `ft.DismissDirection.START_TO_END`, `END_TO_START`, `HORIZONTAL`, etc.
* Automatically handles exit animations and trigger callbacks (`on_dismiss`).

---

## 3. Flutter Gesture Arena & Conflict Resolution

Flutter resolves competing pointer gestures using an arbitration mechanism called the **Gesture Arena**:

1. **Axis Disambiguation (Vertical vs Horizontal)**:
   * Vertical scrolling inside `ft.ListView` or `ft.Column(scroll=...)` does not conflict with a wrapping `on_horizontal_drag_*` detector.
   * Flutter resolves vertical drag vectors to the scroll controller and horizontal vectors to the horizontal drag recognizer once displacement exceeds the touch slop threshold (~18px on mobile).
2. **Interactive Child Precedence**:
   * Controls that consume horizontal drags internally (e.g., `ft.Slider`, interactive canvas nodes with `on_pan_*`, text selection handles in `ft.TextField`) win the gesture arena for touches starting inside their bounds.
   * Touches on empty space, labels, static cards, headers, or general container surfaces bubble to the parent `ft.GestureDetector`.

---

## 4. Swipe Navigation Patterns

### Pattern 1: Two-Tier Hierarchical Back-Navigation
When navigating nested views (such as Settings Hub and Settings Subpages):

```
┌────────────────────────────────────────────────────────┐
│                      MAIN PANES                        │
│   [0] Jarvis Pane  ──┐                                 │
│   [1] Search Pane  ──┼──► [3] SETTINGS HUB (Main Menu) │
│   [2] Library Pane ──┘               ▲                 │
└──────────────────────────────────────┼─────────────────┘
                                       │ Swipe Right (Tier 2)
                                       ▼
                              [3.x] SETTINGS SUBPAGE
                              (AI Assistant, Storage, etc.)
```

* **Tier 2 (Subpage $\to$ Hub)**:
  * If `self._current_subpage_name` is set, a right swipe executes `self._show_hub()`.
* **Tier 1 (Hub $\to$ Originating Pane)**:
  * If on the root Settings Hub, a right swipe executes `self.app._switch_tab(self.app._previous_tab)`.

### Pattern 2: Edge-Bezel Drag Detector (iOS/Android Native Style)
To avoid any potential gesture ambiguity with inner content sliders or wide interactive cards, an invisible edge detector can be placed along the left bezel in an `ft.Stack`:

```python
self._edge_back_detector = ft.GestureDetector(
    content=ft.Container(width=28, bgcolor="transparent"),
    left=0,
    top=0,
    bottom=0,
    on_horizontal_drag_end=self._on_swipe_back,
)
```

---

## 5. Platform Considerations

| Platform | Touch / Gesture Characteristics |
| :--- | :--- |
| **Android / Mobile** | High-velocity touch flings; gesture arena arbitration active; notch/insets handled by `SafeArea`. |
| **macOS / Desktop** | Trackpad horizontal scroll events and mouse drags map to drag events; higher velocity thresholds (`> 400`) prevent accidental triggers during trackpad scrolling. |
