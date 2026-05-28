import sys
import logging
import flet as ft

from ui.tokens import BG, SURFACE, SURFACE2, CYAN, TEXT, DIM, BORDER, LIB_TRACK_COLOR, apply_opacity
from ui.widgets import AnimatedEntry

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logger = logging.getLogger(__name__)


class QueueSheet:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._initialized = False
        self.container = None

    def _ensure_initialized(self):
        if self._initialized:
            return

        self._count_text = ft.Text("", color=DIM, size=11, weight=ft.FontWeight.W_700)
        self._queue_list = ft.ListView(expand=True, spacing=4,
                                        padding=ft.Padding.symmetric(horizontal=12))
        self._empty_label = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.QUEUE_MUSIC, color=DIM, size=48),
                    ft.Text("Queue is empty", color=DIM, size=13, text_align=ft.TextAlign.CENTER),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
            visible=False,
        )

        self._status_icon = ft.Icon(ft.Icons.REPEAT_ONE_ROUNDED, color=CYAN, size=16)
        self._status_text = ft.Text(
            "",
            color=CYAN,
            size=12,
            weight=ft.FontWeight.W_600,
            expand=True,
        )
        self._status_notice = ft.Container(
            content=ft.Row(
                [
                    self._status_icon,
                    self._status_text,
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=8,
            ),
            bgcolor=apply_opacity(0.1, CYAN),
            padding=ft.Padding.symmetric(vertical=8, horizontal=12),
            border_radius=8,
            margin=ft.Margin.symmetric(horizontal=12, vertical=8),
            visible=False,
        )

        # 1. Migrate to native BottomSheet for reliable mobile expansion
        self.container = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        # Re-add custom visual drag handle since show_drag_handle=False
                        ft.Row([ft.Container(width=40, height=4, bgcolor=BORDER, border_radius=2)],
                               alignment=ft.MainAxisAlignment.CENTER),
                        ft.Container(
                            content=ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("UP NEXT", color=TEXT, size=13, weight=ft.FontWeight.W_700),
                                            self._count_text,
                                        ],
                                        spacing=1,
                                    ),
                                    ft.Container(expand=True),
                                    ft.TextButton(
                                        content=ft.Text("CLEAR ALL", color=CYAN, size=11, weight=ft.FontWeight.W_700),
                                        on_click=lambda e: self._clear_all(),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.CLOSE, icon_color=DIM, icon_size=18,
                                        on_click=lambda e: self.collapse(),
                                    ),
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=ft.Padding.symmetric(horizontal=20),
                        ),
                        ft.Divider(color=BORDER),
                        self._empty_label,
                        self._status_notice,
                        self._queue_list,
                    ],
                    spacing=0,
                    expand=True,
                ),
                bgcolor=SURFACE,
                border_radius=ft.BorderRadius.only(top_left=20, top_right=20),
                expand=True, # FIX: Expand container to fill the screen
            ),
            fullscreen=True,
            scrollable=False, # CRITICAL FIX: Let the ListView scroll, not the sheet
            show_drag_handle=False, # CRITICAL FIX: Prevents scroll controller conflict
            draggable=True,
            use_safe_area=True, 
            bgcolor=SURFACE,
        )
        self._initialized = True

    def build(self) -> ft.Control:
        self._ensure_initialized()
        return self.container

    def expand(self):
        def _mutate():
            self._ensure_initialized()
            self.refresh()
            # FIX: Removed manual height constraints here as well
            self.container.open = True
        self.app.safe_update(_mutate)

    def collapse(self):
        def _mutate():
            self._ensure_initialized()
            self.container.open = False
        self.app.safe_update(_mutate)

    def refresh(self):
        cur_idx    = audio_engine.current_index
        cur_artist = audio_engine.current_artist
        remaining  = max(0, len(audio_engine.queue) - cur_idx - 1)
        is_repeat_one = (audio_engine.repeat_mode == "one")
        is_repeat_all = (audio_engine.repeat_mode == "all")
        is_shuffle = bool(audio_engine.is_shuffle)

        is_similar = bool(getattr(self.app, "play_similar_mode", False))

        self._count_text.value = (
            f"{remaining} track{'s' if remaining != 1 else ''} remaining"
            if audio_engine.queue else "Nothing queued"
        )

        def track_row(i: int, t: dict, position_offset: int) -> ft.Control:
            is_active = (i == cur_idx)
            same_art  = (not is_active and bool(cur_artist)
                         and t.get("artist_name", "") == cur_artist)
            position  = position_offset  # 0 = now playing, 1+ = up next

            accent = CYAN if is_active else (LIB_TRACK_COLOR if same_art else "transparent")
            bg     = apply_opacity(0.1, CYAN) if is_active else (
                     apply_opacity(0.05, LIB_TRACK_COLOR) if same_art else SURFACE)

            pos_label = ft.Container(
                content=ft.Text(
                    "▶" if is_active else f"+{position}",
                    color=CYAN if is_active else DIM,
                    size=10, weight=ft.FontWeight.W_700,
                ),
                width=28,
                alignment=ft.Alignment(0, 0),
            )

            card = ft.Container(
                content=ft.Row(
                    [
                        ft.Container(width=3, bgcolor=accent, border_radius=2),
                        pos_label,
                        ft.Column(
                            [
                                ft.Text(t.get("track_title", "Unknown"),
                                        color=CYAN if is_active else TEXT,
                                        size=13,
                                        weight=ft.FontWeight.W_700 if is_active else None,
                                        overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                                ft.Text(
                                    t.get("artist_name", "Unknown"),
                                    color=CYAN if is_active else (LIB_TRACK_COLOR if same_art else DIM),
                                    size=11,
                                ),
                            ],
                            spacing=1, expand=True,
                        ),
                        ft.Row(
                            [
                                ft.IconButton(icon=ft.Icons.ARROW_UPWARD, icon_color=DIM, icon_size=16,
                                              visible=not is_active and not is_shuffle and i > cur_idx + 1,
                                              on_click=lambda e, idx=i: self._move(idx, idx - 1)),
                                ft.IconButton(icon=ft.Icons.ARROW_DOWNWARD, icon_color=DIM, icon_size=16,
                                              visible=not is_active and not is_shuffle,
                                              on_click=lambda e, idx=i: self._move(idx, idx + 1)),
                                ft.IconButton(icon=ft.Icons.REMOVE_CIRCLE_OUTLINE, icon_color="#FF4444",
                                              icon_size=16,
                                              on_click=lambda e, idx=i: self._remove(idx)),
                            ],
                            spacing=0,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=bg,
                border=ft.Border.all(1, apply_opacity(0.4, CYAN) if is_active else BORDER),
                border_radius=10,
                height=60,
                padding=ft.Padding.only(left=0, right=4, top=4, bottom=4),
                opacity=0.4 if is_repeat_one else 1.0,
            )

            return AnimatedEntry(
                ft.Dismissible(
                    content=card,
                    # Background exposed when swiping RIGHT (START_TO_END)
                    background=ft.Container(
                        content=ft.Row(
                            [ft.Icon(ft.Icons.DELETE_OUTLINE, color=BG, size=20)],
                            alignment=ft.MainAxisAlignment.START,
                        ),
                        bgcolor="#FF4444",
                        border_radius=10,
                        padding=ft.Padding.only(left=20),
                    ),
                    # Background exposed when swiping LEFT (END_TO_START)
                    secondary_background=ft.Container(
                        content=ft.Row(
                            [ft.Icon(ft.Icons.DELETE_OUTLINE, color=BG, size=20)],
                            alignment=ft.MainAxisAlignment.END,
                        ),
                        bgcolor="#FF4444",
                        border_radius=10,
                        padding=ft.Padding.only(right=20),
                    ),
                    dismiss_direction=ft.DismissDirection.HORIZONTAL, # Enables swiping in both directions
                    on_dismiss=lambda e, idx=i: self._remove(idx),
                ),
                target_height=60,
            )

        shuf_order = getattr(audio_engine, "_shuffle_order", None)
        upcoming = []
        if is_shuffle and shuf_order and len(shuf_order) == len(audio_engine.queue):
            try:
                curr_shuf_idx = shuf_order.index(cur_idx)
            except ValueError:
                curr_shuf_idx = -1
            
            if curr_shuf_idx != -1:
                shuffled_indices = shuf_order[curr_shuf_idx:]
                for idx in shuffled_indices:
                    upcoming.append((idx, audio_engine.queue[idx]))
        
        if not upcoming:
            for idx in range(cur_idx, len(audio_engine.queue)):
                upcoming.append((idx, audio_engine.queue[idx]))

        rows = [
            track_row(idx, t, position_offset=pos)
            for pos, (idx, t) in enumerate(upcoming[:15])
        ]

        is_empty = len(rows) == 0
        self._empty_label.visible = is_empty

        if is_repeat_one:
            self._status_icon.name = ft.Icons.REPEAT_ONE_ROUNDED
            self._status_text.value = "Repeat Current Song is active. Normal queue progression is paused."
            self._status_notice.visible = True
        elif is_similar:
            self._status_icon.name = ft.Icons.ALL_INCLUSIVE_ROUNDED
            self._status_text.value = "Similar Tracks Walk is active. Jarvis will dynamically append acoustically matching recommendations."
            self._status_notice.visible = True
        elif is_shuffle and is_repeat_all:
            self._status_icon.name = ft.Icons.SHUFFLE_ROUNDED
            self._status_text.value = "Shuffle & Repeat Queue is active. Tracks will play in a randomized loop indefinitely."
            self._status_notice.visible = True
        elif is_shuffle:
            self._status_icon.name = ft.Icons.SHUFFLE_ROUNDED
            self._status_text.value = "Shuffle Play is active. Tracks will play in a randomized order."
            self._status_notice.visible = True
        elif is_repeat_all:
            self._status_icon.name = ft.Icons.REPEAT_ROUNDED
            self._status_text.value = "Repeat Queue is active. The queue will loop back to the beginning after the last song."
            self._status_notice.visible = True
        else:
            self._status_notice.visible = False

        try:
            self._status_icon.update()
            self._status_text.update()
            self._status_notice.update()
        except Exception:
            pass

        # Single synchronous assignment; no async chunking.
        self._queue_list.controls = rows
        self._queue_list.update()

    def _move(self, from_idx: int, to_idx: int):
        audio_engine.move_queue_item(from_idx, to_idx)
        self.app.safe_update(self.refresh)

    def _remove(self, idx: int):
        audio_engine.remove_from_queue(idx)
        self.app.safe_update(self.refresh)

    def _clear_all(self):
        audio_engine.clear_queue()
        self.collapse()
        self.app.show_snackbar("Playback queue cleared.")
