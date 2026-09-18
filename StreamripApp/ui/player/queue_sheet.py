import sys
import logging
import flet as ft

from ui.tokens import (
    BG, SURFACE, SURFACE2, SURFACE_ELEVATED, CYAN, TEXT, DIM, BORDER,
    BORDER_SUBTLE, RADIUS_CARD, RADIUS_PILL, LIB_TRACK_COLOR, apply_opacity
)
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

        self._count_text = ft.Text("", color=DIM, size=12, weight=ft.FontWeight.W_400)
        self._queue_list = ft.ReorderableListView(
            expand=True,
            spacing=6,
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            show_default_drag_handles=False,
            on_reorder=self._handle_queue_reorder,
        )
        self._empty_label = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.QUEUE_MUSIC_ROUNDED, color=DIM, size=48),
                    ft.Text("Queue is empty", color=DIM, size=14, text_align=ft.TextAlign.CENTER),
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
            weight=ft.FontWeight.W_500,
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
            border=ft.Border.all(1, apply_opacity(0.2, CYAN)),
            padding=ft.Padding.symmetric(vertical=8, horizontal=12),
            border_radius=RADIUS_CARD,
            margin=ft.Margin.symmetric(horizontal=12, vertical=6),
            visible=False,
        )

        # Native BottomSheet for reliable mobile expansion with Apple styling
        self.container = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        # Apple grab handle pill
                        ft.Container(
                            content=ft.Row([ft.Container(width=36, height=5, bgcolor=SURFACE_ELEVATED, border_radius=3)],
                                           alignment=ft.MainAxisAlignment.CENTER),
                            padding=ft.Padding.only(top=10, bottom=6),
                        ),
                        ft.Container(
                            content=ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("Playing Next", color=TEXT, size=17, weight=ft.FontWeight.W_600),
                                            self._count_text,
                                        ],
                                        spacing=2,
                                    ),
                                    ft.Container(expand=True),
                                    ft.TextButton(
                                        content=ft.Text("Clear", color=CYAN, size=13, weight=ft.FontWeight.W_600),
                                        on_click=lambda e: self._clear_all(),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.CLOSE_ROUNDED, icon_color=DIM, icon_size=18,
                                        on_click=lambda e: self.collapse(),
                                    ),
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=ft.Padding.only(left=20, right=14, top=4, bottom=6),
                        ),
                        ft.Divider(color=BORDER_SUBTLE, height=1),
                        self._empty_label,
                        self._status_notice,
                        self._queue_list,
                    ],
                    spacing=0,
                    expand=True,
                ),
                bgcolor=SURFACE,
                border_radius=ft.BorderRadius.only(top_left=20, top_right=20),
                expand=True,
            ),
            fullscreen=True,
            scrollable=False,
            show_drag_handle=False,
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

            bg = apply_opacity(0.12, CYAN) if is_active else (
                 apply_opacity(0.06, LIB_TRACK_COLOR) if same_art else SURFACE2)
            border_color = apply_opacity(0.35, CYAN) if is_active else BORDER_SUBTLE

            if is_active:
                pos_indicator = ft.Icon(ft.Icons.PLAY_ARROW_ROUNDED, color=CYAN, size=16)
            else:
                pos_indicator = ft.Text(
                    f"{position}",
                    color=DIM,
                    size=12,
                    weight=ft.FontWeight.W_500,
                )

            pos_label = ft.Container(
                content=pos_indicator,
                width=28,
                alignment=ft.Alignment(0, 0),
            )

            card = ft.Container(
                content=ft.Row(
                    [
                        pos_label,
                        ft.Column(
                            [
                                ft.Text(
                                    t.get("track_title", "Unknown"),
                                    color=CYAN if is_active else TEXT,
                                    size=13,
                                    weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.W_500,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    max_lines=1,
                                ),
                                ft.Text(
                                    t.get("artist_name", "Unknown"),
                                    color=CYAN if is_active else (LIB_TRACK_COLOR if same_art else DIM),
                                    size=11,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    max_lines=1,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.Row(
                            [
                                ft.ReorderableDragHandle(
                                    content=ft.Icon(ft.Icons.REORDER_ROUNDED, color=DIM, size=18),
                                    visible=not is_active and not is_shuffle,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED,
                                    icon_color="#FF453A",
                                    icon_size=16,
                                    on_click=lambda e, idx=i: self._remove(idx),
                                ),
                            ],
                            spacing=0,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=bg,
                border=ft.Border.all(1, border_color),
                border_radius=RADIUS_CARD,
                height=56,
                padding=ft.Padding.only(left=8, right=6, top=4, bottom=4),
                opacity=0.4 if is_repeat_one else 1.0,
            )

            return AnimatedEntry(
                ft.Dismissible(
                    key=f"qd_{i}",
                    content=card,
                    background=ft.Container(
                        content=ft.Row(
                            [ft.Icon(ft.Icons.DELETE_OUTLINE_ROUNDED, color="#FFFFFF", size=20)],
                            alignment=ft.MainAxisAlignment.START,
                        ),
                        bgcolor="#FF453A",
                        border_radius=RADIUS_CARD,
                        padding=ft.Padding.only(left=20),
                    ),
                    secondary_background=ft.Container(
                        content=ft.Row(
                            [ft.Icon(ft.Icons.DELETE_OUTLINE_ROUNDED, color="#FFFFFF", size=20)],
                            alignment=ft.MainAxisAlignment.END,
                        ),
                        bgcolor="#FF453A",
                        border_radius=RADIUS_CARD,
                        padding=ft.Padding.only(right=20),
                    ),
                    dismiss_direction=ft.DismissDirection.HORIZONTAL,
                    on_dismiss=lambda e, idx=i: self._on_dismiss(idx),
                ),
                target_height=56,
                key=f"q_{i}",
            )

        shuf_order = audio_engine.shuffle_indices
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
        try:
            self._queue_list.update()
        except Exception:
            pass

    def _handle_queue_reorder(self, e: ft.OnReorderEvent):
        old_idx = e.old_index
        new_idx = e.new_index
        if new_idx > old_idx:
            new_idx -= 1

        cur_idx    = audio_engine.current_index
        is_shuffle = bool(audio_engine.is_shuffle)
        shuf_order = audio_engine.shuffle_indices
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

        visible_upcoming = upcoming[:15]
        if not (0 <= old_idx < len(visible_upcoming) and 0 <= new_idx < len(visible_upcoming)):
            self.refresh()
            return

        if old_idx == 0 or new_idx == 0:
            self.refresh()
            return

        from_queue_idx = visible_upcoming[old_idx][0]
        to_queue_idx = visible_upcoming[new_idx][0]

        # Update visual controls list in-place first to prevent flicker
        controls = e.control.controls
        item = controls.pop(e.old_index)
        controls.insert(new_idx, item)

        audio_engine.move_queue_item(from_queue_idx, to_queue_idx)
        self.app.safe_update(self.refresh)

    def _move(self, from_idx: int, to_idx: int):
        audio_engine.move_queue_item(from_idx, to_idx)
        self.app.safe_update(self.refresh)

    def _remove(self, idx: int):
        audio_engine.remove_from_queue(idx)
        self.app.safe_update(self.refresh)

    def _on_dismiss(self, idx: int):
        self.app.trigger_haptic("swipe_dismiss")
        self._remove(idx)

    def _clear_all(self):
        audio_engine.clear_queue()
        self.collapse()
