from __future__ import annotations

import os
import sys
import hashlib
import asyncio
from typing import Callable
import flet as ft

from ui.tokens import TEXT, DIM, BORDER, SURFACE, SURFACE2, CYAN, BG, LIB_PLAYLIST_COLOR


from utils.filepath_utils import get_temp_artwork_dir


def get_asset_path(path: str) -> str:
    """Returns path as-is; desktop Flet loads images directly from disk."""
    return path or ""


class PlaylistEditorDialog:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._dlg = None

    def open(self, pl_id: int, name: str, current_color: str):
        t_name = ft.TextField(value=name, label="Playlist Name", border_color=BORDER, focused_border_color=LIB_PLAYLIST_COLOR, bgcolor=SURFACE)
        
        colors = ["#FF5555", "#55FF55", "#5555FF", "#FFFF55", "#FF55FF", "#55FFFF", "#FFFFFF", "#FF8C00", "#8A2BE2"]
        
        selected_color = [current_color]

        def set_color(c):
            selected_color[0] = c
            for i, circle in enumerate(color_row.controls):
                circle.border = ft.Border.all(2, TEXT if colors[i] == c else "transparent")
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
                self._dlg.open = False
                self.page.update()
                await self.app.db_manager.update_playlist(pl_id, name=t_name.value, color=selected_color[0])
                await self.app.library_view.load_library()
                self.app.show_snackbar(f"Playlist '{t_name.value}' updated.")
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
            ]
        )
        self.page.overlay.append(self._dlg)
        self._dlg.open = True
        self.page.update()

    def _close(self):
        if self._dlg:
            self._dlg.open = False
            self.page.update()


class MetadataEditorDialog:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._dlg: ft.AlertDialog | None = None

    def open(self, edit_type: str, meta: dict):
        path        = meta.get("path", "")
        title_val   = meta.get("track_title", "")
        artist_val  = meta.get("artist_name", "")
        album_val   = meta.get("album_title", "")

        t_title  = ft.TextField(value=title_val,  hint_text="Track Title",
                                border_color=BORDER, focused_border_color=CYAN,
                                text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE)
        t_artist = ft.TextField(value=artist_val, hint_text="Artist",
                                border_color=BORDER, focused_border_color=CYAN,
                                text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE)
        t_album  = ft.TextField(value=album_val,  hint_text="Album",
                                border_color=BORDER, focused_border_color=CYAN,
                                text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE)

        # artwork preview (async extraction)
        art_image = ft.Image(src="", width=100, height=100, fit="cover",
                              border_radius=ft.BorderRadius.all(8), visible=False)
        art_box   = ft.Container(
            content=art_image,
            width=100, height=100,
            bgcolor=SURFACE2,
            border_radius=8,
            alignment=ft.Alignment(0, 0),
            padding=ft.Padding.all(0),
        )

        def load_art():
            if path:
                try:
                    from utils.metadata_editor import extract_artwork
                    raw = extract_artwork(path)
                    if raw:
                        ext      = "png" if raw.startswith(b"\x89PNG") else "jpg"
                        ph       = hashlib.md5(path.encode()).hexdigest()
                        tmp_path = os.path.join(get_temp_artwork_dir(), f"meta_art_{ph}.{ext}")
                        with open(tmp_path, "wb") as fh:
                            fh.write(raw)
                        art_image.src     = get_asset_path(tmp_path)
                        def _show_art():
                            art_image.visible = True
                        self.app.safe_update(_show_art)
                except Exception:
                    pass

        asyncio.create_task(asyncio.to_thread(load_art))

        content_cols = [art_box]
        if edit_type == "track":
            content_cols.append(t_title)
        content_cols += [t_artist, t_album]

        def save(e):
            def _mutate():
                self._dlg.open = False
            self.app.safe_update(_mutate)
            self.app.page.run_task(self.app.apply_metadata_edit,
                edit_type, meta,
                t_title.value, t_artist.value, t_album.value,
            )

        def delete(e):
            self._close()
            self.app.confirm_delete_track(path, title_val)

        actions = [
            ft.TextButton("Cancel", on_click=lambda e: self._close()),
            ft.Button(
                content=ft.Text("Save"),
                style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                on_click=save,
            ),
        ]
        if edit_type == "track" and path:
            actions.insert(0, ft.TextButton(
                content=ft.Text("DELETE TRACK", color="#FF4444", size=11, weight="bold"),
                on_click=delete
            ))

        self._dlg = ft.AlertDialog(
            title=ft.Text("Edit Metadata", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Column(content_cols, spacing=12, tight=True),
            actions=actions,
        )
        self.page.overlay.append(self._dlg)
        self._dlg.open = True
        self.page.update()

    def _close(self):
        if self._dlg:
            self._dlg.open = False
            self.page.update()


class ArtistMetadataDialog:
    """Dialog to view and manually edit artist provenance and genres."""
    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self.page = app.page
        self._dlg: ft.AlertDialog | None = None

    def open(self, artist_name: str, on_saved: Callable | None = None):
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
            self.page.update()

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

            self.page.run_task(_do_save)

        self._dlg = ft.AlertDialog(
            title=ft.Text(f"Fix Artist Info: {artist_name}", color=TEXT, size=16, weight="bold"),
            bgcolor=SURFACE,
            content=ft.Column([
                lbl_status,
                t_artist,
                t_country,
                t_genres,
                ft.Text("Manual edits are preserved and will not be overwritten by automatic MusicBrainz sync.", color=DIM, size=11, italic=True),
            ], spacing=12, tight=True),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: self._close()),
                ft.Button(
                    content=ft.Text("Save Override"),
                    style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                    on_click=save,
                ),
            ],
        )
        self.page.overlay.append(self._dlg)
        self._dlg.open = True
        self.page.update()

    def _close(self):
        if self._dlg:
            self._dlg.open = False
            self.page.update()

