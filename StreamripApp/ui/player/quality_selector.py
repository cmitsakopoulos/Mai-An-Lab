import flet as ft

from ui.tokens import TEXT, DIM, BORDER, SURFACE, CYAN


class QualitySelectorSheet:
    def __init__(self, app: "StreamripFletApp"):
        self.app            = app
        self.page           = app.page
        self._current_track = None
        self._initialized   = False
        self._sheet         = None

    def _ensure_initialized(self):
        if self._initialized:
            return
        self._sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Select Quality", color=TEXT, size=15, weight=ft.FontWeight.W_700),
                        ft.Divider(color=BORDER),
                        ft.ListTile(
                            title=ft.Text("High", color=TEXT),
                            subtitle=ft.Text("MP3 / AAC 320kbps", color=DIM),
                            leading=ft.Icon(ft.Icons.MUSIC_NOTE, color=CYAN),
                            on_click=lambda e: self._enqueue("mp3"),
                        ),
                        ft.ListTile(
                            title=ft.Text("CD Quality", color=TEXT),
                            subtitle=ft.Text("16-bit FLAC", color=DIM),
                            leading=ft.Icon(ft.Icons.ALBUM, color=CYAN),
                            on_click=lambda e: self._enqueue("cd"),
                        ),
                        ft.ListTile(
                            title=ft.Text("Hi-Res", color=TEXT),
                            subtitle=ft.Text("24-bit FLAC", color=DIM),
                            leading=ft.Icon(ft.Icons.HIGH_QUALITY, color=CYAN),
                            on_click=lambda e: self._enqueue("hires"),
                        ),
                    ],
                    spacing=0,
                    tight=True,
                ),
                bgcolor=SURFACE,
                padding=20,
            ),
            bgcolor=SURFACE,
        )
        self._initialized = True

    def build(self) -> ft.BottomSheet:
        self._ensure_initialized()
        return self._sheet

    def show(self, track_data: dict):
        self._current_track = track_data
        self.page.bottom_sheet = self._sheet
        self._sheet.open = True
        self.page.update()

    def _close(self):
        self._sheet.open = False
        self.page.update()

    def _enqueue(self, selected_tier: str):
        if not self._current_track:
            return
        self.app.queue.enqueue(self._current_track, quality_tier=selected_tier)
        self._close()
