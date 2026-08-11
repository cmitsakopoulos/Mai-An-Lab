"""RETIRED — see deprecated_feature/README.md.

The "Edit Metadata" dialog, removed from ui/player/dialogs.py. Track
deletion — previously reachable only through this dialog — now lives in
the library long-press context menu.
"""
class MetadataEditorDialog:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self._dlg: ft.AlertDialog | None = None

    @property
    def page(self) -> ft.Page | None:
        return self.app.page

    def open(self, edit_type: str, meta: dict):
        if self._dlg and self.page:
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            self._dlg = None

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
            self._close()
            if self.page:
                self.page.run_task(self.app.apply_metadata_edit,
                    edit_type, meta,
                    t_title.value, t_artist.value, t_album.value,
                )

        def delete(e):
            self._close()
            self.app.confirm_delete_track(path, title_val)

        cancel_btn = ft.TextButton("Cancel", on_click=lambda e: self._close())
        save_btn = ft.Button(
            content=ft.Text("Save"),
            style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
            on_click=save,
        )

        if edit_type == "track" and path:
            delete_btn = ft.TextButton(
                content=ft.Text("DELETE TRACK", color="#FF4444", size=11, weight="bold"),
                on_click=delete,
            )
            actions = [delete_btn, cancel_btn, save_btn]
            actions_align = ft.MainAxisAlignment.SPACE_BETWEEN
        else:
            actions = [cancel_btn, save_btn]
            actions_align = ft.MainAxisAlignment.END

        self._dlg = ft.AlertDialog(
            title=ft.Text("Edit Metadata", color=TEXT),
            bgcolor=SURFACE,
            content=ft.Column(content_cols, spacing=12, tight=True),
            actions=actions,
            actions_alignment=actions_align,
        )
        if self.page:
            self.page.show_dialog(self._dlg)

    def _close(self):
        if self.page:
            try:
                self.page.pop_dialog()
            except Exception:
                pass
        self._dlg = None


