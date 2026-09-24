from __future__ import annotations

import os
import sys
import hashlib
import asyncio
import logging
from typing import Callable
import flet as ft

from ui.tokens import TEXT, DIM, BORDER, SURFACE, SURFACE2, CYAN, BG, LIB_PLAYLIST_COLOR


from utils.filepath_utils import get_temp_artwork_dir

logger = logging.getLogger(__name__)


def get_asset_path(path: str) -> str:
    """Returns path as-is; desktop Flet loads images directly from disk."""
    return path or ""


class PlaylistEditorDialog:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self._dlg = None

    @property
    def page(self) -> ft.Page | None:
        return self.app.page

    def open(self, pl_id: int, name: str, current_color: str):
        # A scrim tap dismisses the dialog without ever reaching _close(), so
        # self._dlg routinely outlives what is on screen. pop_dialog() here
        # would then close an unrelated dialog — or a toast, which shares the
        # same stack. dismiss_dialog() no-ops on an already-closed dialog.
        self.app.dismiss_dialog(self._dlg)
        self._dlg = None

        t_name = ft.TextField(value=name, label="Playlist Name", border_color=BORDER, focused_border_color=LIB_PLAYLIST_COLOR, bgcolor=SURFACE)
        
        colors = ["#FF5555", "#55FF55", "#5555FF", "#FFFF55", "#FF55FF", "#55FFFF", "#FFFFFF", "#FF8C00", "#8A2BE2"]
        
        selected_color = [current_color]

        def set_color(c):
            selected_color[0] = c
            for i, circle in enumerate(color_row.controls):
                circle.border = ft.Border.all(2, TEXT if colors[i] == c else "transparent")
            if self.page:
                self.page.update()

        color_row = ft.Row(
            [
                ft.Container(
                    width=24, height=24, bgcolor=c, border_radius=12,
                    border=ft.Border.all(2, TEXT if c == current_color else "transparent"),
                    on_click=lambda e, color=c: set_color(color)
                ) for c in colors
            ],
            wrap=True, spacing=10
        )

        def save(e):
            async def _do():
                self._close()
                await self.app.db_manager.update_playlist(pl_id, name=t_name.value, color=selected_color[0])
                await self.app.library_view.load_library()
            if self.page:
                self.page.run_task(_do)

        self._dlg = ft.AlertDialog(
            title=ft.Text("Customize Playlist", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Column([
                t_name,
                ft.Text("Accent Color", color=DIM, size=12),
                color_row
            ], spacing=15, tight=True),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: self._close()),
                ft.Button("Save", bgcolor=LIB_PLAYLIST_COLOR, color=BG, on_click=save)
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        if self.page:
            self.page.show_dialog(self._dlg)

    def _close(self):
        self.app.dismiss_dialog(self._dlg)
        self._dlg = None
