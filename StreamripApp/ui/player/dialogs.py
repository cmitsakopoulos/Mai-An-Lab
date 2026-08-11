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


class ArtistMetadataDialog:
    """Dialog to view and manually edit artist provenance and genres."""
    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self._dlg: ft.AlertDialog | None = None

    @property
    def page(self) -> ft.Page | None:
        return self.app.page

    def open(self, artist_name: str, on_saved: Callable | None = None):
        # Stale self._dlg after a scrim dismissal — see PlaylistEditorDialog.open.
        # (This used to reach into page.overlay, which dialogs no longer touch.)
        self.app.dismiss_dialog(self._dlg)
        self._dlg = None

        t_artist = ft.TextField(
            value=artist_name, label="Artist Name", read_only=True,
            border_color=BORDER, text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE
        )
        t_country = ft.TextField(
            value="", label="Country ISO (e.g. US, GB, GR)", hint_text="2-letter ISO code",
            border_color=BORDER, focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE
        )
        t_genres = ft.TextField(
            value="", label="Genres (comma-separated)", hint_text="e.g. hip hop, greek rap, trap",
            border_color=BORDER, focused_border_color=CYAN,
            multiline=True, min_lines=2, max_lines=4,
            text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE
        )
        lbl_status = ft.Text("Loading current metadata...", color=DIM, size=12)

        sug_files = ft.Row(wrap=True, spacing=6, run_spacing=6)
        sug_library = ft.Row(wrap=True, spacing=6, run_spacing=6)
        lbl_files = ft.Text("From this artist's files", color=DIM, size=11, weight="bold", visible=False)
        lbl_library = ft.Text("Used elsewhere in your library", color=DIM, size=11, weight="bold", visible=False)

        def _current_tokens() -> list[str]:
            return [g.strip() for g in (t_genres.value or "").split(",") if g and g.strip()]

        def _toggle(tok: str):
            toks = _current_tokens()
            low = [t.lower() for t in toks]
            if tok.lower() in low:
                toks = [t for t in toks if t.lower() != tok.lower()]
            else:
                toks.append(tok)
            t_genres.value = ", ".join(toks)
            _restyle()
            if self.page:
                self.page.update()

        def _chip(tok: str, subtitle: str | None = None) -> ft.Control:
            selected = tok.lower() in [t.lower() for t in _current_tokens()]
            label = tok if not subtitle else f"{tok}  {subtitle}"
            return ft.Container(
                content=ft.Text(
                    label, size=11,
                    color=(BG if selected else TEXT),
                    weight="bold" if selected else None,
                ),
                bgcolor=(CYAN if selected else SURFACE),
                border=ft.Border.all(1, CYAN if selected else BORDER),
                border_radius=12,
                padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                on_click=lambda e, t=tok: _toggle(t),
                tooltip="Tap to remove" if selected else "Tap to add",
            )

        self._sug_files: list[str] = []
        self._sug_library: list[tuple[str, int]] = []

        def _restyle():
            sug_files.controls = [_chip(t) for t in self._sug_files]
            sug_library.controls = [
                _chip(t, f"·{n}") for t, n in self._sug_library
            ]
            lbl_files.visible = bool(self._sug_files)
            lbl_library.visible = bool(self._sug_library)

        def _on_genres_change(_e):
            _restyle()
            if self.page:
                self.page.update()

        t_genres.on_change = _on_genres_change

        async def _load():
            try:
                data = await self.app.db_manager.get_artist_enrichment(artist_name)
                if data:
                    c = data.get("country") or ""
                    g_list = [g.get("name") if isinstance(g, dict) else str(g) for g in (data.get("genres") or [])]
                    t_country.value = c
                    t_genres.value = ", ".join(g_list)
                    src = data.get("source", "musicbrainz")
                    st = data.get("status", "ok")
                    lbl_status.value = f"Current status: {st} (source: {src})"
                else:
                    lbl_status.value = "No existing metadata row."
            except Exception as exc:
                lbl_status.value = f"Error loading info: {exc}"
            try:
                self._sug_files = await self.app.db_manager.get_artist_source_genres(artist_name)
            except Exception as exc:
                logger.debug("source-genre suggestions unavailable: %s", exc)
                self._sug_files = []
            try:
                vocab = await self.app.db_manager.get_genre_vocabulary(limit=40)
                on_files = {t.lower() for t in self._sug_files}
                self._sug_library = [
                    (v["name"], v.get("artists", 0)) for v in vocab
                    if v["name"].lower() not in on_files
                ]
            except Exception as exc:
                logger.debug("genre vocabulary unavailable: %s", exc)
                self._sug_library = []
            _restyle()
            if self.page:
                self.page.update()

        if self.page:
            self.page.run_task(_load)

        def save(e):
            async def _do_save():
                country_val = t_country.value.strip().upper() if t_country.value else None
                raw_g = [g.strip() for g in t_genres.value.split(",") if g and g.strip()]
                await self.app.db_manager.set_manual_artist_enrichment(
                    artist_name, country=country_val, genres=raw_g
                )
                try:
                    from utils.track_graph import build_genre_affinity
                    await build_genre_affinity(self.app.db_manager)
                except Exception:
                    pass
                self._close()
                self.app.show_snackbar(
                    f"Saved manual metadata for '{artist_name}'",
                    icon=ft.Icons.CHECK_CIRCLE, color=CYAN
                )
                if on_saved:
                    if asyncio.iscoroutinefunction(on_saved):
                        await on_saved()
                    else:
                        on_saved()

            if self.page:
                self.page.run_task(_do_save)

        self._dlg = ft.AlertDialog(
            title=ft.Text(f"Fix Artist Info: {artist_name}", color=TEXT, size=16, weight="bold"),
            bgcolor=SURFACE,
            content=ft.Column([
                lbl_status,
                t_artist,
                t_country,
                t_genres,
                lbl_files,
                sug_files,
                lbl_library,
                sug_library,
                ft.Text("Manual edits are preserved and will not be overwritten by automatic MusicBrainz sync.", color=DIM, size=11, italic=True),
            ], spacing=12, tight=True, scroll=ft.ScrollMode.AUTO),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: self._close()),
                ft.Button(
                    content=ft.Text("Save Override"),
                    style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                    on_click=save,
                ),
            ],
        )
        if self.page:
            self.page.show_dialog(self._dlg)

    def _close(self):
        self.app.dismiss_dialog(self._dlg)
        self._dlg = None

