import os
import re
import sys
import logging
import asyncio
import platform
import threading
import subprocess
import flet as ft
from ui.tokens import BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, SOURCE_COLORS, apply_opacity

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
        script = f'''
        tell application "System Events" to activate
        set f to choose folder with prompt "{title}"
        return POSIX path of f
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=30
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


class NotificationSystem:
    def __init__(self, app):
        self.app = app
        self.page = app.page
        self._initialized = False
        self.container = None
        self.wrapper = None
        self._active_notifications = []

    def _ensure_initialized(self):
        if self._initialized:
            return
        self.container = ft.Column(
            tight=True,
            spacing=10,
            width=320,
        )
        self.wrapper = ft.Container(
            content=self.container,
            top=40,
            right=20,
        )
        self.page.overlay.append(self.wrapper)
        self._initialized = True

    def show(self, text: str, icon=ft.Icons.NOTIFICATIONS_ROUNDED, color=CYAN):
        # Don't try to animate UI elements into a suspended/hidden client;
        # it causes buffer back-pressure and spurious 120 Hz wakeups.
        if self.app.is_background:
            return
        self._ensure_initialized()

        # Limit to at most 3 active notifications
        while len(self._active_notifications) >= 3:
            oldest = self._active_notifications.pop(0)
            if oldest.data and callable(oldest.data):
                oldest.data()

        # Create a sleek notification card
        notification = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(icon, color=color, size=20),
                        bgcolor=apply_opacity(0.1, color),
                        padding=10,
                        border_radius=8,
                    ),
                    ft.Text(
                        text, 
                        color=TEXT, 
                        size=13, 
                        weight=ft.FontWeight.W_500, 
                        expand=True,
                        no_wrap=False,
                        max_lines=3,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=SURFACE2,
            border=ft.Border.all(1, apply_opacity(0.1, TEXT)),
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            shadow=ft.BoxShadow(
                blur_radius=20,
                color=apply_opacity(0.3, BG),
                offset=ft.Offset(0, 10),
            ),
            animate_opacity=300,
            opacity=0,
            offset=ft.Offset(0.3, 0),
            animate_offset=ft.Animation(400, ft.AnimationCurve.DECELERATE),
        )

        dismissed = [False]

        def _do_dismiss_immediate():
            if dismissed[0]: return
            dismissed[0] = True
            if dismissible in self._active_notifications:
                self._active_notifications.remove(dismissible)
            def _remove():
                if dismissible in self.container.controls:
                    self.container.controls.remove(dismissible)
            self.app.safe_update(_remove)

        def _do_dismiss():
            if dismissed[0]: return
            dismissed[0] = True
            if dismissible in self._active_notifications:
                self._active_notifications.remove(dismissible)
            # If the app is in the background we can't drive animations;
            # skip straight to an immediate removal instead.
            if self.app.is_background:
                _do_dismiss_immediate()
                return
            def _fade_out():
                notification.opacity = 0
                notification.offset = ft.Offset(0.3, 0)
            self.app.safe_update(_fade_out)

            async def _remove_after():
                await asyncio.sleep(0.4)
                def _remove():
                    if dismissible in self.container.controls:
                        self.container.controls.remove(dismissible)
                self.app.safe_update(_remove)
            asyncio.create_task(_remove_after())

        dismissible = ft.Dismissible(
            content=notification,
            dismiss_direction=ft.DismissDirection.HORIZONTAL,
            on_dismiss=lambda e: _do_dismiss_immediate(),
        )
        dismissible.data = _do_dismiss
        self._active_notifications.append(dismissible)

        def _add():
            self.container.controls.insert(0, dismissible)
            notification.opacity = 1
            notification.offset = ft.Offset(0, 0)

        self.app.safe_update(_add)

        async def _dismiss():
            await asyncio.sleep(4)
            _do_dismiss()

        notification.on_click = lambda _: _do_dismiss()

        asyncio.create_task(_dismiss())


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
            animate_scale=ft.Animation(50, ft.AnimationCurve.EASE_OUT_QUAD),
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
        if self.page:
            self.page.update()

    def _release(self, e):
        self._inner.scale = ft.Scale(1.0)
        if self.page:
            self.page.update()


class OnyxButton(ScaleButton):
    def __init__(self, text: str, icon: str = None, on_tap=None, height=50, width=None, **kwargs):
        content_row = ft.Row(
            [
                ft.Icon(icon, color=BG, size=20) if icon else ft.Container(),
                ft.Text(text, color=BG, weight=ft.FontWeight.W_700, size=14),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=10,
        )
        super().__init__(
            content=ft.Container(
                content=content_row,
                bgcolor=CYAN,
                height=height,
                width=width,
                border_radius=12,
                alignment=ft.Alignment(0, 0),
            ),
            on_tap=on_tap,
            **kwargs
        )


class GlassCard(ft.Container):
    def __init__(self, content, **kwargs):
        super().__init__(
            content=content,
            bgcolor="#0DFFFFFF",
            border_radius=16,
            padding=20,
            border=ft.Border.all(1, "#1AFFFFFF"),
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
            border=ft.Border.all(1, BORDER),
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


class SourceSegment(ScaleButton):
    def __init__(self, text: str, selected=False, on_tap=None, **kwargs):
        self.selected = selected
        self.text_control = ft.Text(
            text.upper(), color=BG if selected else TEXT, weight=ft.FontWeight.W_700, size=11
        )
        super().__init__(
            content=ft.Container(
                content=self.text_control,
                bgcolor=CYAN if selected else "transparent",
                border=ft.Border.all(1, CYAN if selected else BORDER),
                border_radius=8,
                padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                alignment=ft.Alignment(0, 0),
            ),
            on_tap=on_tap,
            **kwargs
        )
    def update_state(self, selected: bool):
        self.selected = selected
        self.content.bgcolor = CYAN if selected else "transparent"
        self.content.border = ft.Border.all(1, CYAN if selected else BORDER)
        self.text_control.color = BG if selected else TEXT
        self.update()


class SettingsHeader(ft.Row):
    def __init__(self, title: str, on_back=None):
        super().__init__(
            controls=[
                ft.IconButton(icon=ft.Icons.ARROW_BACK, icon_color=CYAN, on_click=on_back),
                ft.Text(title, size=24, weight=ft.FontWeight.W_700, color=TEXT),
            ],
            spacing=12,
        )


class HubSettingItem(ScaleButton):
    def __init__(self, icon: str, title: str, subtitle: str, on_tap=None):
        super().__init__(
            content=ft.Container(
                content=ft.Row([
                    ft.Icon(icon, color=CYAN, size=22),
                    ft.Column([
                        ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_700),
                        ft.Text(subtitle, color=DIM, size=12),
                    ], spacing=2, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=DIM, opacity=0.25, size=18),
                ], spacing=16),
                bgcolor="#0DFFFFFF",
                padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                border_radius=14,
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
            ft.Icons.KEYBOARD_ARROW_DOWN if initially_open else ft.Icons.CHEVRON_RIGHT,
            color=DIM, opacity=0.35, size=18
        )
        toggle_zone = ft.Container(
            content=ft.Row([
                ft.Icon(icon, color=CYAN, size=22),
                ft.Column([
                    ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_700),
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
            controls=[ft.Container(content=ft.Column([header, self.content_area], spacing=0), bgcolor="#0DFFFFFF", border_radius=14)],
            spacing=0,
        )

    def toggle(self, _e):
        self.is_open = not self.is_open
        self.content_area.visible = self.is_open
        self.chevron.icon = ft.Icons.KEYBOARD_ARROW_DOWN if self.is_open else ft.Icons.CHEVRON_RIGHT
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
