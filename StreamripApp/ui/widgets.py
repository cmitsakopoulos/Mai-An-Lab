import os
import re
import sys
import logging
import platform
import threading
import subprocess
import flet as ft
from ui.tokens import (
    BG, SURFACE, SURFACE2, SURFACE_ELEVATED, CYAN, AMBER, TEXT, DIM, TEXT_TERTIARY,
    BORDER, BORDER_SUBTLE, RADIUS_CARD, RADIUS_PILL, RADIUS_THUMB, SOURCE_COLORS, apply_opacity
)

logger = logging.getLogger(__name__)

class ArtworkCache:
    def __init__(self, max_size=50):
        self._cache = {}
        self._access_order = []
        self._max_size = max_size
        self._lock = threading.Lock()
        
    def get(self, key: str) -> str | None:
        with self._lock:
            if key in self._cache:
                self._access_order.remove(key)
                self._access_order.append(key)
                return self._cache[key]
            return None
        
    def put(self, key: str, path: str):
        with self._lock:
            if key in self._cache:
                self._access_order.remove(key)
            elif len(self._cache) >= self._max_size:
                oldest = self._access_order.pop(0)
                evicted_path = self._cache[oldest]
                del self._cache[oldest]
                try:
                    if evicted_path and os.path.exists(evicted_path):
                        os.remove(evicted_path)
                except Exception as exc:
                    logger.warning("Failed to delete evicted artwork: %s", exc)
            
            self._cache[key] = path
            self._access_order.append(key)
        
    def clear(self):
        with self._lock:
            for path in self._cache.values():
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
            self._cache.clear()
            self._access_order.clear()

_ARTWORK_CACHE = ArtworkCache(max_size=50)

def src_color(source: str) -> str:
    return SOURCE_COLORS.get((source or "").lower(), "#FFFFFF")

def fmt_time(s: float) -> str:
    m, s = divmod(int(s), 60)
    return f"{m}:{s:02d}"

def pick_folder(title="Select Folder") -> str | None:
    """Native folder picker fallback for desktop platforms."""
    system = platform.system()
    
    if system == "Darwin":  # macOS
        script = f'POSIX path of (choose folder with prompt "{title}")'
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode == 0:
                return result.stdout.strip() or None
        except Exception:
            pass
        return None
        
    elif system == "Linux":
        # Try zenity first, then kdialog
        for cmd in [
            ["zenity", "--file-selection", "--directory", f"--title={title}"],
            ["kdialog", "--getexistingdirectory", "."],
        ]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    return result.stdout.strip() or None
            except Exception:
                continue
        return None
        
    return None # Fallback to Flet FilePicker for Windows/Mobile

def strip_markup(text: str) -> str:
    """Remove Kivy-style [b]…[/b] markup tags that streamrip_search pre-computes."""
    return re.sub(r"\[/?[^\]]*\]", "", str(text))


def dialog_handoff(app, get_dialog):
    """Close a dialog, then open the next one only once the first is really gone.

    Returns `(on_dismiss, close)`. Wire `on_dismiss` into the dialog's
    `on_dismiss` and call `close(follow_up)` instead of `page.pop_dialog()`;
    `follow_up` runs when Flutter reports the dismissal, or immediately if there
    was nothing to close.

    Both halves exist because of Flet 0.86 route handling. `BottomSheetControl`
    closes itself with a bare `Navigator.pop()` and `AlertDialogControl` pops the
    topmost route once its own is active, so NEITHER can be trusted to take down
    the route it means to. Close a dialog and push another in the same Flutter
    frame — which `page.run_task` does NOT escape — and the outgoing dialog's pop
    takes the incoming one's route instead: Flutter keeps rendering the dialog
    Python has already recorded as closed, so `pop_dialog()` finds nothing open
    and nothing on screen can be dismissed again. Only `on_dismiss`, which
    `show_dialog()` fires after Flutter confirms the route is gone and unmounts
    the stack entry, gives a guaranteed ordering.

    `get_dialog` is a callable so callers can wire this up before the dialog
    itself is constructed.
    """
    pending = {"run": None}

    def on_dismiss(_e=None):
        follow_up = pending["run"]
        pending["run"] = None
        if follow_up:
            follow_up()

    def close(follow_up=None):
        pending["run"] = follow_up
        # dismiss_dialog() names the dialog rather than popping whatever sits on
        # top, so a toast raised by background work can't absorb the close.
        if not app.dismiss_dialog(get_dialog()):
            # Already gone, so on_dismiss will never arrive — run it now.
            on_dismiss()

    return on_dismiss, close


class NotificationSystem:
    def __init__(self, app):
        self.app = app
        self.page = app.page
        # The snackbar currently on screen, so a burst retires the old one
        # instead of stacking. Held directly (not via pop_dialog) so dismissing
        # a toast never pops an unrelated dialog off the stack.
        self._current: "ft.SnackBar | None" = None

    def show(self, text: str, icon=ft.Icons.NOTIFICATIONS_ROUNDED, color=CYAN):
        # Native ft.SnackBar via page.show_dialog. The previous implementation
        # stacked custom cards in a positioned Container appended to page.overlay;
        # on Flet 0.86 desktop that overlay structure blanked the ENTIRE view — a
        # silent Flutter render failure with nothing in the Python log — so any
        # toast took the app down. SnackBar is a DialogControl presented through
        # the same managed show_dialog path the dialogs use, so it renders and
        # auto-dismisses without blanking. The show(text, icon, color) API is
        # unchanged, so all call sites keep working.
        if self.app.is_background or not self.page:
            return

        def _present():
            prev = self._current
            if prev is not None:
                try:
                    prev.open = False
                    prev.update()
                except Exception:
                    pass
                self._current = None
            sb = ft.SnackBar(
                content=ft.Row(
                    [
                        ft.Icon(icon, color=color, size=20),
                        ft.Text(
                            text, color=TEXT, size=13, weight=ft.FontWeight.W_500,
                            expand=True, no_wrap=False, max_lines=3,
                        ),
                    ],
                    spacing=12, tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=SURFACE,
                behavior=ft.SnackBarBehavior.FLOATING,
                show_close_icon=True,
                close_icon_color=DIM,
                duration=3000,
                margin=ft.Margin.all(12),
                padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                shape=ft.RoundedRectangleBorder(radius=RADIUS_PILL, side=ft.BorderSide(1, BORDER_SUBTLE)),
            )
            self._current = sb
            try:
                self.page.show_dialog(sb)
            except Exception:
                logger.exception("snackbar show failed")

        # Route through safe_update so any thread can call show() and it lands in
        # the app's single coalesced page.update().
        self.app.safe_update(_present)


class AnimatedEntry(ft.Container):
    def __init__(self, content, target_height=56, depth=0, **kwargs):
        super().__init__(
            content=content,
            height=target_height,
            opacity=1.0,
            animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT_EXPO),
            animate_opacity=ft.Animation(200, ft.AnimationCurve.EASE_OUT_EXPO),
            **kwargs
        )
        self.target_height = target_height
        self.depth = depth

    def hide(self):
        """Trigger the slide-out animation."""
        self.height = 0
        self.opacity = 0
        self.update()


class ScaleButton(ft.GestureDetector):
    """Wraps content to provide 0.96 scale-down feedback on tap."""
    def __init__(self, content, on_tap=None, scale_to=0.96, **kwargs):
        # The container that will be scaled
        self._inner = ft.Container(
            content=content,
            scale=ft.Scale(1.0),
            animate_scale=ft.Animation(70, ft.AnimationCurve.EASE_OUT),
            expand_loose=False,   # prevent size collapsing
        )
        super().__init__(
            content=self._inner,
            **kwargs
        )
        self.on_tap = on_tap
        self.scale_to = scale_to
        self.on_tap_down = self._press
        self.on_tap_up = self._release
        self.on_tap_cancel = self._release

    def _press(self, e):
        self._inner.scale = ft.Scale(self.scale_to)
        try:
            self._inner.update()
        except Exception:
            pass

    def _release(self, e):
        self._inner.scale = ft.Scale(1.0)
        try:
            self._inner.update()
        except Exception:
            pass


class OnyxButton(ScaleButton):
    def __init__(self, text: str, icon: str = None, on_tap=None, height=44, width=None, text_size=13, padding=None, **kwargs):
        content_row = ft.Row(
            [
                ft.Icon(icon, color=BG, size=16 if text_size < 13 else 18 if text_size < 14 else 20) if icon else ft.Container(),
                ft.Text(text, color=BG, weight=ft.FontWeight.W_700, size=text_size),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            tight=True if width is None else False,
            spacing=6 if text_size < 13 else 8 if text_size < 14 else 10,
        )
        super().__init__(
            content=ft.Container(
                content=content_row,
                bgcolor=CYAN,
                height=height,
                width=width,
                padding=padding or (ft.Padding.symmetric(horizontal=16, vertical=8) if width is None else None),
                border_radius=RADIUS_PILL,
                alignment=ft.Alignment(0, 0),
            ),
            on_tap=on_tap,
            **kwargs
        )


class GlassCard(ft.Container):
    def __init__(self, content, **kwargs):
        super().__init__(
            content=content,
            bgcolor=SURFACE,
            border_radius=RADIUS_CARD,
            padding=20,
            border=ft.Border.all(1, BORDER_SUBTLE),
            **kwargs
        )


class MenuTextItem(ft.Container):
    def __init__(self, text: str, on_click=None, icon: str = None):
        super().__init__(
            content=ft.Row([
                ft.Icon(icon, color=DIM, size=20) if icon else ft.Container(),
                ft.Text(text, color=TEXT, size=14),
            ], spacing=12),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            on_click=on_click,
        )


class AppSearchBar(ft.Container):
    def __init__(self, hint: str, on_submit=None, on_change=None, on_clear=None):
        self._input = ft.TextField(
            hint_text=hint,
            hint_style=ft.TextStyle(color=DIM),
            text_style=ft.TextStyle(color=TEXT),
            border=ft.InputBorder.NONE,
            on_submit=on_submit,
            on_change=on_change,
            expand=True,
            content_padding=ft.Padding.only(left=10, right=10),
        )
        self._clear_btn = ft.IconButton(
            icon=ft.Icons.CLOSE, icon_color=DIM, icon_size=18,
            on_click=lambda _: self._clear(on_clear),
            visible=False
        )
        super().__init__(
            content=ft.Row([
                ft.Icon(ft.Icons.SEARCH, color=CYAN, size=20),
                self._input,
                self._clear_btn,
            ], spacing=0),
            bgcolor=SURFACE2,
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12),
            border=ft.Border.all(1, BORDER_SUBTLE),
            animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
        )
    def _clear(self, callback):
        self._input.value = ""
        self._clear_btn.visible = False
        self.update()
        if callback: callback()
    @property
    def value(self): return self._input.value
    @value.setter
    def value(self, val): 
        self._input.value = val
        self._clear_btn.visible = bool(val)


class CupertinoSegmentedBar(ft.Container):
    """Apple-style unified segmented control capsule."""
    def __init__(self, segments: list[tuple[str, str, str | None, str | None]], 
                 selected_key: str, on_change, height: int = 42, **kwargs):
        """
        segments: list of (key, label, icon_name, opt_color)
        """
        self.segments = segments
        self.selected_key = selected_key
        self.on_change = on_change
        self._buttons = []
        
        button_controls = []
        for key, label, icon, col in segments:
            is_active = (key == selected_key)
            accent = col or CYAN
            
            icon_ctrl = ft.Icon(icon, color=accent if is_active else DIM, size=15) if icon else None
            text_ctrl = ft.Text(
                label,
                size=11,
                weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.W_500,
                color=TEXT if is_active else DIM,
                no_wrap=True,
            )
            
            inner_row = ft.Row(
                [icon_ctrl, text_ctrl] if icon_ctrl else [text_ctrl],
                spacing=4,
                alignment=ft.MainAxisAlignment.CENTER,
                tight=True,
            )
            
            btn = ft.Container(
                content=inner_row,
                bgcolor=apply_opacity(0.14, accent) if is_active else "transparent",
                border=ft.Border.all(1, apply_opacity(0.35, accent)) if is_active else None,
                border_radius=RADIUS_PILL - 4,
                padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                alignment=ft.Alignment(0, 0),
                expand=True,
                animate=ft.Animation(140, ft.AnimationCurve.EASE_OUT),
                on_click=lambda e, k=key: self._select(k),
            )
            self._buttons.append((key, btn, inner_row, col))
            button_controls.append(btn)
            
        super().__init__(
            content=ft.Row(button_controls, spacing=2, expand=True),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER_SUBTLE),
            border_radius=RADIUS_PILL,
            padding=3,
            height=height,
            **kwargs
        )
        
    def _apply_styles(self):
        for k, btn, row, col in self._buttons:
            active = (k == self.selected_key)
            accent = col or CYAN
            btn.bgcolor = apply_opacity(0.14, accent) if active else "transparent"
            btn.border = ft.Border.all(1, apply_opacity(0.35, accent)) if active else None
            for c in row.controls:
                if isinstance(c, ft.Icon):
                    c.color = accent if active else DIM
                elif isinstance(c, ft.Text):
                    c.color = TEXT if active else DIM
                    c.weight = ft.FontWeight.W_600 if active else ft.FontWeight.W_500

    def _select(self, key: str):
        if key == self.selected_key:
            return
        self.selected_key = key
        self._apply_styles()
        self.update()
        if self.on_change:
            self.on_change(key)
            
    def set_selected(self, key: str):
        self.selected_key = key
        self._apply_styles()
        self.update()


class SourceSegment(ScaleButton):
    def __init__(self, text: str, selected=False, on_tap=None, **kwargs):
        self.selected = selected
        self.text_control = ft.Text(
            text.upper(), color=TEXT if selected else DIM, weight=ft.FontWeight.W_700 if selected else ft.FontWeight.W_500, size=11
        )
        super().__init__(
            content=ft.Container(
                content=self.text_control,
                bgcolor=SURFACE_ELEVATED if selected else "transparent",
                border=ft.Border.all(1, CYAN if selected else BORDER_SUBTLE),
                border_radius=RADIUS_PILL,
                padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                alignment=ft.Alignment(0, 0),
            ),
            on_tap=on_tap,
            **kwargs
        )
    def update_state(self, selected: bool):
        self.selected = selected
        self.content.bgcolor = SURFACE_ELEVATED if selected else "transparent"
        self.content.border = ft.Border.all(1, CYAN if selected else BORDER_SUBTLE)
        self.text_control.color = TEXT if selected else DIM
        self.text_control.weight = ft.FontWeight.W_700 if selected else ft.FontWeight.W_500
        self.update()


class SettingsHeader(ft.Row):
    def __init__(self, title: str, on_back=None):
        super().__init__(
            controls=[
                ft.IconButton(icon=ft.Icons.ARROW_BACK_IOS_NEW_ROUNDED, icon_color=CYAN, icon_size=18, on_click=on_back),
                ft.Text(title, size=22, weight=ft.FontWeight.W_700, color=TEXT),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )


class HubSettingItem(ScaleButton):
    def __init__(self, icon: str, title: str, subtitle: str, on_tap=None):
        super().__init__(
            content=ft.Container(
                content=ft.Row([
                    ft.Icon(icon, color=CYAN, size=22),
                    ft.Column([
                        ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_600),
                        ft.Text(subtitle, color=DIM, size=12),
                    ], spacing=2, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT_ROUNDED, color=DIM, opacity=0.4, size=18),
                ], spacing=16),
                bgcolor=SURFACE,
                border=ft.Border.all(1, BORDER_SUBTLE),
                padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                border_radius=RADIUS_CARD,
            ),
            on_tap=on_tap,
        )


class AccordionCard(ft.Column):
    def __init__(self, icon: str, title: str, subtitle: str, content_controls: list,
                 header_actions: list | None = None, initially_open: bool = False,
                 on_toggle: callable = None):
        self.is_open = initially_open
        self.on_toggle = on_toggle
        self.content_area = ft.Container(
            content=ft.Column(content_controls, spacing=6),
            visible=initially_open,
            padding=ft.Padding.only(left=16, right=16, bottom=14, top=4),
        )
        self.chevron = ft.Icon(
            ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED if initially_open else ft.Icons.CHEVRON_RIGHT_ROUNDED,
            color=DIM, opacity=0.4, size=18
        )
        toggle_zone = ft.Container(
            content=ft.Row([
                ft.Icon(icon, color=CYAN, size=22),
                ft.Column([
                    ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_600),
                    ft.Text(subtitle, color=DIM, size=12),
                ], spacing=2, expand=True),
                self.chevron,
            ], spacing=16),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            on_click=self.toggle,
            expand=True,
        )
        if header_actions:
            header = ft.Row(
                [toggle_zone, ft.Container(
                    content=ft.Row(header_actions, spacing=2, tight=True),
                    padding=ft.Padding.only(right=8),
                )],
                spacing=0,
            )
        else:
            header = toggle_zone
        super().__init__(
            controls=[ft.Container(
                content=ft.Column([header, self.content_area], spacing=0),
                bgcolor=SURFACE,
                border=ft.Border.all(1, BORDER_SUBTLE),
                border_radius=RADIUS_CARD,
            )],
            spacing=0,
        )

    def toggle(self, _e):
        self.is_open = not self.is_open
        self.content_area.visible = self.is_open
        self.chevron.icon = ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED if self.is_open else ft.Icons.CHEVRON_RIGHT_ROUNDED
        if self.on_toggle:
            self.on_toggle(self.is_open)
        self.update()


class SkeletonRow(ft.Container):
    def __init__(self, delay: float = 0):
        super().__init__(
            content=ft.Shimmer(
                base_color=apply_opacity(0.15, DIM),
                highlight_color=apply_opacity(0.4, CYAN),
                content=ft.Row(
                    [
                        ft.Container(width=52, height=52, bgcolor=SURFACE2, border_radius=10),
                        ft.Column(
                            [
                                ft.Container(width=200, height=13, bgcolor=SURFACE2, border_radius=6),
                                ft.Container(width=140, height=11, bgcolor=SURFACE2, border_radius=6),
                                ft.Container(width=90,  height=9,  bgcolor=SURFACE2, border_radius=6),
                            ],
                            spacing=6,
                            expand=True,
                            tight=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ),
            bgcolor=SURFACE,
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            height=64,
        )


def build_page_ghost_top(on_click) -> ft.Control:
    return ft.Container(
        content=ft.Row(
            [
                ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_UP_ROUNDED, color=CYAN, size=16),
                ft.Text("Tap here to load previous page", color=TEXT, size=11, weight=ft.FontWeight.W_500),
                ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_UP_ROUNDED, color=CYAN, size=16),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=10,
        ),
        height=48,
        alignment=ft.Alignment(0, 0),
        bgcolor=apply_opacity(0.10, CYAN),
        border=ft.Border.all(1, apply_opacity(0.25, CYAN)),
        border_radius=12,
        margin=ft.Margin.only(bottom=12),
        on_click=on_click,
    )


def build_page_ghost_bottom(on_click) -> ft.Control:
    return ft.Container(
        content=ft.Row(
            [
                ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED, color=CYAN, size=16),
                ft.Text("Tap here to load next page", color=TEXT, size=11, weight=ft.FontWeight.W_500),
                ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED, color=CYAN, size=16),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=10,
        ),
        height=48,
        alignment=ft.Alignment(0, 0),
        bgcolor=apply_opacity(0.10, CYAN),
        border=ft.Border.all(1, apply_opacity(0.25, CYAN)),
        border_radius=12,
        margin=ft.Margin.only(top=12),
        on_click=on_click,
    )
