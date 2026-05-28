import os
import sys
import platform
import logging
import asyncio
import flet as ft
from ui.tokens import (
    BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, apply_opacity
)
from ui.widgets import OnyxButton, HubSettingItem, pick_folder

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logger = logging.getLogger(__name__)

def get_app_dir() -> str:
    """Returns the primary writable directory for the app, prioritizing 'files'."""
    for env_var in ("APP_FILES_PATH", "FILES_DIR", "INTERNAL_STORAGE", "FLET_APP_STORAGE_DATA", "HOME"):
        val = os.getenv(env_var)
        if val and os.path.isdir(val):
            return val
    import tempfile
    return tempfile.gettempdir()


class SettingsView:
    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self.page = app.page
        self._picking_target = None # "download" or "library"

        # File Picker: Windows only. macOS/Linux use native subprocess picker.
        # Android uses _browse_android_paths(); no FilePicker (separate Flet extension).
        self._file_picker = None
        if platform.system() not in ["Darwin", "Linux"]:
            try:
                self._file_picker = ft.FilePicker()
                self._file_picker.on_result = self._on_file_picked
                self.page.overlay.append(self._file_picker)
            except Exception as exc:
                self._file_picker = None
                logger.warning(f"FilePicker fallback initialization failed: {exc}")

        # URL Launcher service
        self._url_launcher = ft.UrlLauncher()

        self._init_widgets() 

    def _init_widgets(self):
        """Initializes the functional controls (keeps original logic)."""
        self._dl_path_field = ft.TextField(
            label="Download Path",
            hint_text="e.g. C:\\Music\\Downloads",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._lib_path_field = ft.TextField(
            label="Library Path",
            hint_text="e.g. C:\\Music\\Library",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )

        self._selected_accent_color = CYAN

        # Qobuz Credentials
        self._qobuz_user_id_field = ft.TextField(
            label="Qobuz User ID",
            hint_text="e.g. 1234567",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._qobuz_token_field = ft.TextField(
            label="Auth Token / Password Hash",
            hint_text="Enter token or MD5 of password",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            password=True,
            can_reveal_password=True,
        )
        self._qobuz_use_token_switch = ft.Switch(
            value=True,
            active_color=CYAN
        )

        # Config Editor
        self._config_editor = ft.TextField(
            multiline=True,
            min_lines=15,
            max_lines=25,
            text_style=ft.TextStyle(color=TEXT, font_family="monospace", size=11),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            content_padding=12,
        )

        # Dropdowns for General Preferences
        common_style = dict(
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._startup_page_dropdown = ft.Dropdown(
            label="Startup Page",
            options=[
                ft.dropdown.Option("Search"),
                ft.dropdown.Option("Library"),
            ],
            on_select=lambda _e: self._save_general_settings(),
            **common_style
        )
        self._default_sort_dropdown = ft.Dropdown(
            label="Default Library Sort",
            options=[
                ft.dropdown.Option(key="date", text="Date Added"),
                ft.dropdown.Option(key="artist", text="Artist (A–Z)"),
                ft.dropdown.Option(key="album", text="Album (A–Z)"),
                ft.dropdown.Option(key="track", text="Track (A–Z)"),
            ],
            on_select=lambda _e: self._save_general_settings(),
            **common_style
        )

        # Landing Page Customization
        self._show_most_listened_switch = ft.Switch(value=True, active_color=CYAN)
        self._show_library_stats_switch  = ft.Switch(value=True, active_color=CYAN)

        # Play Similar temperature slider
        self._temp_slider = ft.Slider(
            min=0.01,
            max=0.20,
            divisions=19,
            label="{value}",
            value=0.05,
            active_color=CYAN,
            on_change=self._on_temp_change,
        )
        self._temp_value_text = ft.Text("0.05", color=TEXT, size=13, weight=ft.FontWeight.W_700)

        self._scroll_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=12,
            animate_opacity=300
        )
        self.main_content = ft.Container(
            content=self._scroll_column,
            expand=True, 
            padding=ft.Padding.symmetric(horizontal=28, vertical=20),
            animate=ft.Animation(300, ft.AnimationCurve.DECELERATE)
        )

    def build(self) -> ft.Control:
        self.refresh()
        if getattr(self, "initial_subpage", None) == "Storage":
            self.initial_subpage = None
            self._scroll_column.controls = [
                ft.Row([
                    ft.IconButton(ft.Icons.ARROW_BACK_IOS_NEW_ROUNDED, icon_color=CYAN, icon_size=16, 
                                  on_click=lambda _: self._show_hub()),
                    ft.Text("Storage", size=24, weight=ft.FontWeight.W_700, color=TEXT),
                ], spacing=10),
                ft.Container(height=20),
                self._build_storage_group()
            ]
        else:
            self._show_hub() # Start at the Hub
        return self.main_content

    def _show_hub(self):
        """Displays the main settings menu (the 'hub')."""
        self._scroll_column.controls = [
            ft.Text("Settings", size=32, weight=ft.FontWeight.W_900, color=TEXT),
            ft.Text("Configure your high-fidelity experience", color=DIM, size=14),
            ft.Container(height=24),
            
            # Thematic Tiles
            HubSettingItem(ft.Icons.LOCK_PERSON_ROUNDED, "Authentication", "Qobuz credentials & tokens", 
                           on_tap=lambda _: self._show_sub_page("Account", self._build_auth_group())),
            
            HubSettingItem(ft.Icons.STORAGE_ROUNDED, "Storage & Paths", "Library and download locations", 
                           on_tap=lambda _: self._show_sub_page("Storage", self._build_storage_group())),
            
            HubSettingItem(ft.Icons.PALETTE_ROUNDED, "Appearance", "Accent colors and UI behavior",
                           on_tap=lambda _: self._show_sub_page("Appearance", self._build_appearance_group())),

            HubSettingItem(ft.Icons.SHIELD_OUTLINED, "Permissions", "Notifications, audio, and file access",
                           on_tap=lambda _: self._show_sub_page("Permissions", self._build_permissions_group())),

            ft.Divider(color=BORDER, height=40),
            
            HubSettingItem(ft.Icons.TERMINAL_ROUNDED, "Advanced", "Edit TOML config and data maintenance", 
                           on_tap=lambda _: self._show_sub_page("Advanced", self._build_advanced_group())),
            
            HubSettingItem(ft.Icons.INFO_OUTLINE_ROUNDED, "About", "App version and developer info", 
                           on_tap=lambda _: self._show_sub_page("About", self._build_about_group())),
        ]
        self.app.safe_update(lambda: None)

    def _show_sub_page(self, title: str, content_control: ft.Control):
        """Swaps the hub for a specific settings group."""
        self._scroll_column.controls = [
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK_IOS_NEW_ROUNDED, icon_color=CYAN, icon_size=16, 
                              on_click=lambda _: self._show_hub()),
                ft.Text(title, size=24, weight=ft.FontWeight.W_700, color=TEXT),
            ], spacing=10),
            ft.Container(height=20),
            content_control
        ]
        self.app.safe_update(lambda: None)

    # --- Sub-Page Builders ---

    def _build_auth_group(self):
        return ft.Column([
            ft.Text("Enter your Qobuz credentials to enable search and preview.", color=DIM, size=12),
            self._qobuz_user_id_field,
            self._qobuz_token_field,
            ft.Row([self._qobuz_use_token_switch, ft.Text("Use Auth Token", color=TEXT, size=12)], spacing=10),
            OnyxButton("SAVE CREDENTIALS", ft.Icons.SAVE, on_tap=lambda _: self._save_qobuz_credentials())
        ], spacing=20)

    def _build_storage_group(self):
        return ft.Column([
            ft.Text("Define where your music is indexed and downloaded.", color=DIM, size=12),
            ft.Row([self._dl_path_field, ft.IconButton(ft.Icons.FOLDER_OPEN, icon_color=CYAN, on_click=self._browse_download_folder)]),
            ft.Row([self._lib_path_field, ft.IconButton(ft.Icons.FOLDER_OPEN, icon_color=CYAN, on_click=self._browse_library_folder)]),
            OnyxButton("SAVE PATHS", ft.Icons.SAVE, on_tap=lambda _: self._save_paths())
        ], spacing=20)

    def _build_appearance_group(self):
        return ft.Column([
            ft.Text("Customize how the app looks and behaves on startup.", color=DIM, size=12),
            self._startup_page_dropdown,
            self._default_sort_dropdown,
            ft.Divider(color=BORDER, height=20),
            ft.Text("Landing Page Sections", color=CYAN, size=12, weight=ft.FontWeight.BOLD),
            ft.Row([self._show_most_listened_switch, ft.Text("Show Most Listened Tracks", color=TEXT, size=12)], spacing=10),
            ft.Row([self._show_library_stats_switch, ft.Text("Show Library Stats", color=TEXT, size=12)], spacing=10),
            ft.Divider(color=BORDER, height=20),
            ft.Text("Accent Color", color=CYAN, size=12, weight=ft.FontWeight.BOLD),
            self._build_color_selector(mode="accent"),
            OnyxButton("APPLY VISUALS", ft.Icons.PALETTE, on_tap=lambda _: self._save_appearance_settings())
        ], spacing=20)


    # ── Permissions ──────────────────────────────────────────────────────────
    _PERMISSION_SPECS = [
        ("notification",            "Notifications",   "Required for media controls on the lock screen"),
        ("audio",                   "Audio Files",     "Read access to music files (Android 13+)"),
        ("storage",                 "Storage",         "Read/write external storage (Android ≤12)"),
        ("manage_external_storage", "All Files Access", "Required to delete or edit songs on Android 11+"),
        ("record_audio",            "Microphone",      "Required for Jarvis voice commands"),
    ]

    def _build_permissions_group(self):
        if "ANDROID_ROOT" not in os.environ and "ANDROID_DATA" not in os.environ:
            return ft.Column([
                ft.Text(
                    "Permissions are managed by the OS on this platform; this panel "
                    "only applies on Android.",
                    color=DIM, size=12,
                ),
            ], spacing=12)

        self._perm_rows: dict[str, dict] = {}
        rows: list[ft.Control] = [
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, color=CYAN, size=20),
                        ft.Text("How to Grant Permissions on Android", weight=ft.FontWeight.BOLD, color=TEXT, size=13),
                    ], spacing=6),
                    ft.Text(
                        "Android restricts sensitive permissions for security. To grant or manage access:\n"
                        "1. Tap 'OPEN SYSTEM SETTINGS' below to open the Android App Info page for Mai An Lab.\n"
                        "2. Tap 'Permissions'.\n"
                        "3. Select the required permission (e.g. Files and Media, Microphone) and set it to 'Allow'.",
                        color=DIM,
                        size=11,
                    ),
                ], spacing=6),
                padding=12,
                bgcolor=SURFACE2,
                border_radius=8,
                border=ft.Border.all(1, BORDER),
            ),
            ft.Container(height=4),
        ]

        for name, label, desc in self._PERMISSION_SPECS:
            status_text = ft.Text("checking…", color=DIM, size=11)
            grant_btn = ft.TextButton("GRANT", visible=False)
            row = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(label, color=TEXT, size=14, weight=ft.FontWeight.W_600, expand=True),
                        status_text,
                        grant_btn,
                    ]),
                    ft.Text(desc, color=DIM, size=11),
                ], spacing=4),
                padding=ft.Padding.all(12),
                border=ft.Border.all(1, BORDER),
                border_radius=10,
            )
            self._perm_rows[name] = {"status": status_text, "grant": grant_btn}
            rows.append(row)

        rows.append(ft.Container(height=8))
        rows.append(ft.Row([
            OnyxButton("REFRESH", ft.Icons.REFRESH, on_tap=lambda _: self._refresh_permissions()),
            OnyxButton("OPEN SYSTEM SETTINGS", ft.Icons.LAUNCH, on_tap=lambda _: self._open_app_settings()),
        ], spacing=10))

        self.page.run_task(self._refresh_permissions_async)
        return ft.Column(rows, spacing=12)

    def _refresh_permissions(self):
        self.page.run_task(self._refresh_permissions_async)

    async def _refresh_permissions_async(self):
        service = getattr(audio_engine, "audio_service", None)
        if service is None:
            return
        try:
            result = await service.query_permissions()
        except Exception as exc:
            logger.warning("query_permissions failed: %s", exc)
            return
        self._apply_perm_status(result)

    def _apply_perm_status(self, result: dict):
        for name, _label, _desc in self._PERMISSION_SPECS:
            row = self._perm_rows.get(name)
            if row is None:
                continue
            status = result.get(name, "unknown")
            granted = status == "granted"
            row["status"].value = status.upper() if status else "UNKNOWN"
            row["status"].color = CYAN if granted else "#FF8866"
            row["grant"].disabled = granted
            row["grant"].text = "GRANTED" if granted else "GRANT"
        self.app.safe_update(lambda: None)

    def _on_grant_permission(self, name: str):
        self.page.run_task(self._grant_permission_async, name)

    async def _grant_permission_async(self, name: str):
        service = getattr(audio_engine, "audio_service", None)
        if service is None:
            return
        try:
            await service.request_permission(name)
        except Exception as exc:
            logger.warning("request_permission(%s) failed: %s", name, exc)
            self.app.show_snackbar(f"Permission request failed: {exc}")
            return
        await self._refresh_permissions_async()

    def _open_app_settings(self):
        self.page.run_task(self._open_app_settings_async)

    async def _open_app_settings_async(self):
        service = getattr(audio_engine, "audio_service", None)
        if service is None:
            return
        try:
            await service.open_app_settings()
        except Exception as exc:
            logger.warning("open_app_settings failed: %s", exc)

    def _launch_github(self, e):
        self.page.run_task(self._launch_github_async)

    async def _launch_github_async(self):
        try:
            await self._url_launcher.launch_url("https://github.com/cmitsakopoulos/Mai-An-Lab")
        except Exception as exc:
            logger.warning("Failed to launch GitHub URL: %s", exc)
            self.app.show_snackbar("Could not open GitHub in browser.")

    def _build_advanced_group(self):
        return ft.Column([
            ft.Text("State Backup & Migration (Import / Export)", weight=ft.FontWeight.BOLD, color=CYAN),
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.LOCK_PERSON_ROUNDED, color=CYAN, size=20),
                        ft.Text(
                            "Android Release Build Sandbox Security",
                            weight=ft.FontWeight.BOLD,
                            color=TEXT,
                            size=13,
                        ),
                    ], spacing=6),
                    ft.Text(
                        "Because this application runs as a secure production release build, Android isolates all internal databases, config profiles, and search histories inside a restricted sandbox (/data/data/...). Direct file system access via developer tools like ADB is completely blocked.\n\n"
                        "This Import/Export state system is your exclusive gateway to back up, restore, or migrate your library and custom settings. "
                        "You can also use this system to offload heavy DSP feature computations: export the state ZIP here, run the parallelised desktop offloading script on your PC/Mac, and import the bundle back to instantly sync your track features.",
                        color=DIM,
                        size=12,
                    ),
                ], spacing=6),
                padding=12,
                bgcolor=SURFACE2,
                border_radius=8,
                border=ft.Border.all(1, BORDER),
            ),
            ft.Row([
                ft.TextButton(
                    "Export State",
                    icon=ft.Icons.IOS_SHARE_ROUNDED,
                    on_click=self._on_export_state_click,
                ),
                ft.TextButton(
                    "Import State",
                    icon=ft.Icons.FILE_DOWNLOAD_ROUNDED,
                    on_click=self._on_import_state_click,
                ),
            ]),
            ft.Divider(color=BORDER, height=40),
            ft.Text("Play Similar Recommendation Temperature", weight=ft.FontWeight.BOLD, color=CYAN),
            ft.Text("Controls the random softmax exploration of similarity walks. A lower value keeps transitions tight and genre-consistent, while a higher value adds variety.", color=DIM, size=12),
            ft.Row([
                ft.Container(content=self._temp_slider, expand=True),
                ft.Container(content=self._temp_value_text, margin=ft.Margin.only(right=10)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(color=BORDER, height=40),
            ft.Text("Maintenance", weight=ft.FontWeight.BOLD, color=DIM),
            ft.Row([
                ft.TextButton("Album Cache", icon=ft.Icons.IMAGE_ROUNDED, on_click=lambda _: self.app.clear_album_artwork_cache()),
                ft.TextButton("Preview Cache", icon=ft.Icons.MUSIC_NOTE_ROUNDED, on_click=lambda _: self.app.clear_preview_cache()),
                ft.TextButton("Library Index", icon=ft.Icons.FORMAT_LIST_BULLETED_ROUNDED, on_click=lambda _: self.app.open_maintenance_confirmation(
                     "Clear Library Index?", 
                     "This will clear all indexed music, playlists, and metadata from the local database, leaving configuration, history, and play counts intact.\n\nYour actual music files will NOT be touched.", 
                     "Clear Index", 
                     self.app.clear_library_index
                )),
                ft.TextButton("DSP Features", icon=ft.Icons.GRAPHIC_EQ_ROUNDED, on_click=lambda _: self.app.open_maintenance_confirmation(
                     "Clear DSP Features?", 
                     "This will purge all extracted acoustic DSP features and PCA space definitions, forcing a full recalculation/re-scan on your next sweep.\n\nYour library index will remain intact.", 
                     "Clear DSP", 
                     self.app.clear_dsp_features
                )),
                ft.TextButton("Taste Model", icon=ft.Icons.FAVORITE_ROUNDED, on_click=lambda _: self.app.open_maintenance_confirmation(
                     "Reset Taste Model?", 
                     "This will reset all user preference learning parameters and weights back to zero. The taste model will start cold.\n\nThis cannot be undone.", 
                     "Reset Model", 
                     self.app.clear_taste_model_weights
                )),
                ft.TextButton("Wipe DB", icon=ft.Icons.DELETE_FOREVER, icon_color="#FF4444", on_click=lambda _: self._on_wipe_db_click()),
            ], wrap=True, spacing=10),
            ft.Divider(color=BORDER, height=40),
            ft.Text("Raw Configuration (TOML)", weight=ft.FontWeight.BOLD, color=DIM),
            ft.Text("Directly edit the Streamrip TOML configuration file for advanced control.", color=DIM, size=12),
            self._config_editor,
            OnyxButton("SAVE CONFIG FILE", ft.Icons.TERMINAL, on_tap=lambda _: self._save_config()),
            ft.Container(height=10),
            ft.TextButton("Debug: Populate Play Counts", icon=ft.Icons.BUG_REPORT_ROUNDED, on_click=self._on_debug_populate_click),
        ], spacing=15)

    def _build_about_group(self):
        return ft.Column([
            ft.Container(
                content=ft.Column([
                    ft.Text("Mai An Lab", size=28, weight=ft.FontWeight.W_900, color=CYAN),
                    ft.Text("Version 1.1.0", color=DIM, size=14),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                alignment=ft.Alignment(0, 0),
                padding=ft.Padding.only(bottom=20),
            ),
            ft.Text("Summary", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text("A deployment friendly restructure of Streamrip (Qobuz only), packaged with Flet alongside custom Flutter (audio engine) extensions.", color=DIM, size=12),
            ft.Divider(color=BORDER, height=30),
            ft.Text("What's New in 1.1.0", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Column([
                ft.Text("• Unsupervised PCA engine with automatic double-pass SVD and Pearson correlation cleaving of redundant acoustic features", color=DIM, size=12),
                ft.Text("• Mood EQ sliders dynamically hide zero-weight features detected as redundant by the PCA engine", color=DIM, size=12),
                ft.Text("• On-device mathematical truth report: heatmap and biplot scatter PNGs written to your library folder after each PCA rebuild", color=DIM, size=12),
                ft.Text("• Play Similar now replaces the current song (consistent behaviour between Jarvis and the playback pane)", color=DIM, size=12),
                ft.Text("• PCA analysis script automatically locates the most recent analyzed state zip in tools/analyzed_states", color=DIM, size=12),
            ], spacing=4),
            ft.Divider(color=BORDER, height=30),
            ft.Text("Developer", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text("Christophoros Mitsakopoulos", color=DIM, size=13),
            ft.Divider(color=BORDER, height=30),
            ft.Text("Credits", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Column([
                ft.Text("• Streamrip by nathom and community", color=DIM, size=12),
                ft.Text("• Flet Framework and community", color=DIM, size=12),
            ], spacing=4),
            ft.Divider(color=BORDER, height=40),
            ft.Row([
                ft.TextButton("Project GitHub", icon=ft.Icons.CODE_ROUNDED, on_click=self._launch_github),
                ft.TextButton("Developer", icon=ft.Icons.PERSON_ROUNDED, on_click=lambda _: self.app.show_snackbar("Contact: mitsacopoulos@gmail.com")),
            ], alignment=ft.MainAxisAlignment.CENTER),
            ft.Container(height=20),
            ft.Text("2026 Mai An Lab", color=DIM, size=11, italic=True, text_align=ft.TextAlign.CENTER, width=float("inf")),
        ], spacing=15)

    def _on_wipe_db_click(self):
        self.app.open_wipe_confirmation()

    async def _on_debug_populate_click(self, _e):
        self.app.show_snackbar("Populating random play counts...", icon=ft.Icons.STORAGE_ROUNDED)
        await self.app.db_manager.debug_populate_play_counts()
        self.app.show_snackbar("Done! Check your Most Listened Tracks.", icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
        self.app.search_view.refresh_setup_state()

    # ── State bundle (export/import) ────────────────────────────────────────
    def _on_export_state_click(self, _e):
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_state_bundle(mode="export")
        else:
            path = pick_folder("Choose export folder") or os.path.join(
                os.path.expanduser("~"), "Downloads"
            )
            self.page.run_task(self._do_export_state, path)

    def _on_import_state_click(self, _e):
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_state_bundle(mode="import")
        else:
            self.app.show_snackbar(
                "Desktop import: drop a bundle into ~/Downloads and use Android.",
                color="#FF4444",
            )

    async def _do_export_state(self, out_dir: str):
        from utils import state_export
        from utils import track_graph as tg
        from utils.streamrip_api import get_config_path
        from utils.search_history import get_search_history_path

        self.app.show_snackbar("Exporting state...", icon=ft.Icons.IOS_SHARE_ROUNDED)
        try:
            out_path = await asyncio.to_thread(
                state_export.export_state,
                self.app.db_manager.db_path,
                get_config_path(),
                get_search_history_path(),
                out_dir,
                tg.CUSTOM_MOODS_PATH,
            )
        except Exception as ex:
            logger.exception("state export failed")
            self.app.show_snackbar(f"Export failed: {ex}", color="#FF4444")
            return
        self.app.show_snackbar(
            f"Exported to {out_path}", icon=ft.Icons.CHECK_CIRCLE, color=CYAN
        )

    async def _do_import_state(self, zip_path: str):
        from utils import state_export
        from utils import track_graph as tg
        from utils.streamrip_api import get_config_path
        from utils.search_history import get_search_history_path

        self.app.show_snackbar("Importing state...", icon=ft.Icons.FILE_DOWNLOAD_ROUNDED)

        try:
            close = getattr(self.app.db_manager, "close", None)
            if close is not None:
                await close()
        except Exception as ex:
            logger.warning(f"db_manager.close() raised before import: {ex}")

        try:
            result = await asyncio.to_thread(
                state_export.import_state,
                zip_path,
                self.app.db_manager.db_path,
                get_config_path(),
                get_search_history_path(),
                tg.CUSTOM_MOODS_PATH,
            )
        except Exception as ex:
            logger.exception("state import failed")
            self.app.show_snackbar(f"Import failed: {ex}", color="#FF4444")
            return

        replaced = ", ".join(result["replaced"].keys()) or "nothing"
        self.app.show_snackbar(
            f"Imported {replaced}. Force-close and relaunch the app.",
            icon=ft.Icons.CHECK_CIRCLE,
            color=CYAN,
        )

    def _browse_android_state_bundle(self, mode: str):
        is_import = (mode == "import")
        app_data = os.getenv("FLET_APP_STORAGE_DATA") or ""

        BOOKMARKS = [
            (os.path.abspath("/storage/emulated/0/Download"), "Downloads"),
            (os.path.abspath("/storage/emulated/0"),          "Internal Storage"),
            (os.path.abspath("/sdcard"),                       "SD Card"),
            (os.path.abspath("/storage/emulated/0/Music"),    "Music"),
        ]
        if app_data:
            BOOKMARKS.append((app_data, "App Storage"))

        bs_holder = [None]
        path_state = [None]

        title_text = ft.Text("", color=TEXT, weight=ft.FontWeight.W_700, size=14)
        path_text  = ft.Text("", color=DIM, size=10, italic=True)
        dir_list   = ft.Column(tight=True, spacing=0, scroll=ft.ScrollMode.AUTO)

        def _close():
            if bs_holder[0]:
                bs_holder[0].open = False
                bs_holder[0].update()
                self.page.update()

        def _confirm_dir(path):
            _close()
            self.page.run_task(self._do_export_state, path)

        def _confirm_file(path):
            _close()
            self.page.run_task(self._do_import_state, path)

        def _render(directory):
            path_state[0] = directory
            dir_list.controls.clear()

            if directory is None:
                title_text.value = "Import State Bundle" if is_import else "Export State Bundle"
                path_text.value  = "Pick a .zip bundle" if is_import else "Pick a destination folder"
                for bpath, bname in BOOKMARKS:
                    exists = os.path.isdir(bpath)
                    dir_list.controls.append(ft.ListTile(
                        leading=ft.Icon(
                            ft.Icons.FOLDER_ROUNDED,
                            color=CYAN if exists else DIM,
                            size=20,
                        ),
                        title=ft.Text(bname, color=TEXT if exists else DIM, size=13),
                        subtitle=ft.Text(bpath, color=DIM, size=10),
                        on_click=_nav_to(bpath),
                    ))
                return

            title_text.value = os.path.basename(directory) or directory
            path_text.value  = directory

            if not is_import:
                dir_list.controls.append(
                    ft.Container(
                        content=ft.Button(
                            f"Export here",
                            icon=ft.Icons.IOS_SHARE_ROUNDED,
                            on_click=lambda _: _confirm_dir(directory),
                            bgcolor=CYAN,
                            color=BG,
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                        ),
                        padding=ft.Padding.only(bottom=6),
                    )
                )

            try:
                entries = sorted(os.listdir(directory))
            except PermissionError:
                dir_list.controls.append(
                    ft.Text("Permission denied", color="#FF5555", size=12, italic=True)
                )
                if bs_holder[0] and bs_holder[0].open:
                    bs_holder[0].update()
                return

            sub_dirs = [e for e in entries if os.path.isdir(os.path.join(directory, e)) and not e.startswith(".")]
            zip_files = [e for e in entries if is_import and e.lower().endswith(".zip") and os.path.isfile(os.path.join(directory, e))]

            for entry in zip_files:
                full = os.path.join(directory, entry)
                dir_list.controls.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.ARCHIVE_ROUNDED, color=CYAN, size=18),
                    title=ft.Text(entry, color=TEXT, size=13),
                    subtitle=ft.Text(f"{os.path.getsize(full)/1024:.0f} KB", color=DIM, size=10),
                    on_click=lambda _e, p=full: _confirm_file(p),
                ))

            for entry in sub_dirs:
                full = os.path.join(directory, entry)
                dir_list.controls.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.FOLDER_OUTLINED, color=CYAN, size=18),
                    title=ft.Text(entry, color=TEXT, size=13),
                    on_click=_nav_to(full),
                ))

            if not sub_dirs and not zip_files:
                dir_list.controls.append(
                    ft.Text(
                        "(no .zip bundles or sub-folders)" if is_import else "(no sub-folders)",
                        color=DIM, size=12, italic=True,
                    )
                )

            if bs_holder[0] and bs_holder[0].open:
                bs_holder[0].update()

        def _nav_to(path):
            def _handler(_e):
                _render(path)
            return _handler

        def _go_up(_e):
            cur = path_state[0]
            if cur is None:
                return
            parent = os.path.dirname(cur)
            if parent == cur:
                _render(None)
            else:
                _render(parent)

        _render(None)

        bs = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.IconButton(
                                    ft.Icons.ARROW_BACK_ROUNDED,
                                    icon_color=CYAN,
                                    on_click=_go_up,
                                    tooltip="Up",
                                ),
                                ft.Column(
                                    [title_text, path_text],
                                    spacing=0,
                                    expand=True,
                                ),
                                ft.TextButton("Cancel", on_click=lambda _: _close()),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=4,
                        ),
                        ft.Divider(color=BORDER),
                        ft.Container(content=dir_list, height=320),
                    ],
                    tight=True,
                    spacing=6,
                ),
                bgcolor=SURFACE,
                padding=ft.Padding.only(left=16, right=16, top=16, bottom=40),
            ),
            use_safe_area=True,
            bgcolor=SURFACE,
        )
        bs_holder[0] = bs
        self.app.page.overlay.append(bs)
        bs.open = True
        self.app.page.update()

    def refresh(self):
        self._dl_path_field.value  = self.app.target_folder
        self._lib_path_field.value = self.app.library_folder
        try:
            from utils.streamrip_api import load_config, get_config_path
            cfg = load_config()
            with open(get_config_path(), "r", encoding="utf-8") as f:
                self._config_editor.value = f.read()
            
            gen = cfg.get("general", {})
            self._startup_page_dropdown.value = gen.get("startup_page", "Library")
            self._default_sort_dropdown.value = gen.get("library_sort", "date")
            self._temp_slider.value = float(gen.get("play_similar_temperature", 0.05))
            self._temp_value_text.value = f"{self._temp_slider.value:.2f}"

            qobuz = cfg.get("qobuz", {})
            self._qobuz_user_id_field.value = str(qobuz.get("email_or_userid", ""))
            self._qobuz_token_field.value   = str(qobuz.get("password_or_token", ""))
            self._qobuz_use_token_switch.value = bool(qobuz.get("use_auth_token", True))

            landing = cfg.get("landing", {})
            self._show_most_listened_switch.value = bool(landing.get("show_search_history", True))
            self._show_library_stats_switch.value  = bool(landing.get("show_library_stats", True))

            appearance = cfg.get("appearance", {})
            self._selected_accent_color = appearance.get("accent_color", "#00BFFF")
        except: pass
        if self.page: self.page.update()

    def _save_landing_settings(self):
        show_history = self._show_search_history_switch.value
        show_stats   = self._show_library_stats_switch.value
        
        from utils.streamrip_api import update_config_params
        update_config_params({
            "landing": {
                "show_search_history": show_history,
                "show_library_stats": show_stats
            }
        })
        self.app.show_snackbar("Landing page settings updated.")
        if hasattr(self.app, "search_view"):
            self.app.search_view.refresh_setup_state()

    def _build_color_selector(self, mode="accent"):
        colors = {
            "Cyan": "#00BFFF",
            "Deep Blue": "#2979FF",
            "Purple": "#9B59B6",
            "Lavender": "#B39DDB",
            "Pink": "#E91E63",
            "Red": "#E74C3C",
            "Crimson": "#DC143C",
            "Orange": "#E67E22",
            "Gold": "#FFD700",
            "Yellow": "#FFD600",
            "Green": "#2ECC71",
            "Emerald": "#00FF7F",
            "Mint": "#69F0AE",
            "Slate": "#78909C",
        }
        
        target_color_base = self._selected_accent_color

        circles = []
        for name, hex in colors.items():
            is_selected = (hex.lower() == target_color_base.lower())
            circle = ft.Container(
                width=32, height=32,
                bgcolor=hex,
                border_radius=16,
                border=ft.Border.all(2, TEXT if is_selected else "transparent"),
                on_click=lambda e, h=hex, m=mode: self._on_color_click(h, m),
                tooltip=name
            )
            circles.append(circle)
            
        return ft.Row(circles, spacing=12, alignment=ft.MainAxisAlignment.START, wrap=True)

    def _on_color_click(self, hex, mode):
        self._selected_accent_color = hex
        self.app.safe_update(lambda: None)
        self._show_sub_page("Appearance", self._build_appearance_group())

    def _save_appearance_settings(self):
        from utils.streamrip_api import update_config_params
        update_config_params({
            "appearance": {
                "accent_color": self._selected_accent_color
            },
            "landing": {
                "show_search_history": self._show_most_listened_switch.value,
                "show_library_stats": self._show_library_stats_switch.value
            }
        })
        self.app.show_snackbar("Appearance and interface settings saved.")
        if hasattr(self.app, "search_view"):
            self.app.search_view.refresh_setup_state()
        self.app.restart_ui(target_tab=2)

    def _save_paths(self):
        dl  = self._dl_path_field.value.strip()
        lib = self._lib_path_field.value.strip()
        if not dl or not lib:
            self.app.show_snackbar("Paths cannot be empty.")
            return
        
        self.app.target_folder = dl
        self.app.library_folder = lib
        from utils.streamrip_api import update_config_params
        update_config_params({"downloads": {"folder": dl}})
        self.app._save_pref("folder_path", dl)
        self.app._save_pref("library_path", lib)
        self.app.show_snackbar("Storage paths updated.")
        self.app.library_view.start_scan()

    def _save_general_settings(self):
        startup = self._startup_page_dropdown.value
        sort = self._default_sort_dropdown.value
        if not startup or not sort:
            return

        from utils.streamrip_api import update_config_params
        update_config_params({
            "general": {
                "startup_page": startup,
                "library_sort": sort,
            }
        })

        lib_view = getattr(self.app, "library_view", None)
        if lib_view is not None and getattr(lib_view, "sort_mode", None) != sort:
            lib_view.sort_mode = sort
            if hasattr(lib_view, "load_library"):
                self.page.run_task(lib_view.load_library)

    def _on_temp_change(self, e):
        val = round(self._temp_slider.value, 2)
        self._temp_value_text.value = f"{val:.2f}"
        self._temp_value_text.update()
        from utils.streamrip_api import update_config_params
        update_config_params({
            "general": {
                "play_similar_temperature": val
            }
        })

    def _save_config(self):
        try:
            from utils.streamrip_api import get_config_path
            with open(get_config_path(), "w", encoding="utf-8") as f:
                f.write(self._config_editor.value or "")
            self.app.show_snackbar("Configuration saved.")
            self.app.sync_config_to_ui()
        except Exception as exc:
            self.app.show_snackbar(f"Save failed: {exc}")

    def _save_qobuz_credentials(self):
        uid   = self._qobuz_user_id_field.value.strip()
        token = self._qobuz_token_field.value.strip()
        if not uid or not token:
            self.app.show_snackbar("Credentials cannot be empty.")
            return
        
        use_token = self._qobuz_use_token_switch.value
        from utils.streamrip_api import update_config_params
        success = update_config_params({
            "qobuz": {
                "use_auth_token": use_token,
                "email_or_userid": uid,
                "password_or_token": token
            }
        })
        if success:
            self.app.show_snackbar("Qobuz credentials updated.")
            self.app.sync_config_to_ui()
        else:
            self.app.show_snackbar("Failed to update credentials.")

    # ── native browsing ──────────────────────────────────────────────────────
    def _browse_android_paths(self, target: str):
        """Interactive directory navigator for Android."""
        app_data = os.getenv("FLET_APP_STORAGE_DATA") or ""
        label    = "Download Folder" if target == "download" else "Library Folder"

        BOOKMARKS = [
            (os.path.abspath("/storage/emulated/0"),          "Internal Storage"),
            (os.path.abspath("/sdcard"),                       "SD Card"),
            (os.path.abspath("/storage/emulated/0/Music"),    "Music"),
            (os.path.abspath("/storage/emulated/0/Download"), "Downloads"),
        ]
        if app_data:
            BOOKMARKS.append((app_data, "App Storage"))

        bs_holder  = [None]
        path_state = [None]

        title_text = ft.Text("", color=TEXT, weight=ft.FontWeight.W_700, size=14)
        path_text  = ft.Text("", color=DIM, size=10, italic=True)
        dir_list   = ft.Column(tight=True, spacing=0, scroll=ft.ScrollMode.AUTO)

        def _close():
            if bs_holder[0]:
                bs_holder[0].open = False
                bs_holder[0].update()
                self.page.update()

        def _confirm(path):
            _close()
            self._handle_folder_picked(path, target)

        def _render(directory):
            path_state[0] = directory
            dir_list.controls.clear()

            if directory is None:
                title_text.value = f"Select {label}"
                path_text.value  = "Choose a starting location"
                for bpath, bname in BOOKMARKS:
                    exists = os.path.isdir(bpath)
                    dir_list.controls.append(ft.ListTile(
                        leading=ft.Icon(
                            ft.Icons.FOLDER_ROUNDED,
                            color=CYAN if exists else DIM,
                            size=20,
                        ),
                        title=ft.Text(bname, color=TEXT if exists else DIM, size=13),
                        subtitle=ft.Text(bpath, color=DIM, size=10),
                        on_click=_nav_to(bpath),
                    ))
            else:
                title_text.value = os.path.basename(directory) or directory
                path_text.value  = directory

                dir_list.controls.append(
                    ft.Container(
                        content=ft.Button(
                            f"Use \"{os.path.basename(directory) or directory}\"",
                            icon=ft.Icons.CHECK_ROUNDED,
                            on_click=lambda _: _confirm(directory),
                            bgcolor=CYAN,
                            color=BG,
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                        ),
                        padding=ft.Padding.only(bottom=6),
                    )
                )

                try:
                    entries = sorted(
                        e for e in os.listdir(directory)
                        if os.path.isdir(os.path.join(directory, e))
                        and not e.startswith(".")
                    )
                except PermissionError:
                    entries = []
                    dir_list.controls.append(
                        ft.Text("Permission denied", color="#FF5555", size=12, italic=True)
                    )

                for entry in entries:
                    full = os.path.join(directory, entry)
                    dir_list.controls.append(ft.ListTile(
                        leading=ft.Icon(ft.Icons.FOLDER_OUTLINED, color=CYAN, size=18),
                        title=ft.Text(entry, color=TEXT, size=13),
                        on_click=_nav_to(full),
                    ))

                if not entries and not any(isinstance(c, ft.Text) for c in dir_list.controls):
                    dir_list.controls.append(
                        ft.Text("(no sub-folders)", color=DIM, size=12, italic=True)
                    )

            if bs_holder[0] and bs_holder[0].open:
                bs_holder[0].update()

        def _nav_to(path):
            def _handler(_e):
                _render(path)
            return _handler

        def _go_up(_e):
            cur = path_state[0]
            if cur is None:
                return
            parent = os.path.dirname(cur)
            if parent == cur:
                _render(None)
            else:
                _render(parent)

        _render(None)

        bs = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.IconButton(
                                    ft.Icons.ARROW_BACK_ROUNDED,
                                    icon_color=CYAN,
                                    on_click=_go_up,
                                    tooltip="Up",
                                ),
                                ft.Column(
                                    [title_text, path_text],
                                    spacing=0,
                                    expand=True,
                                ),
                                ft.TextButton("Cancel", on_click=lambda _: _close()),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=4,
                        ),
                        ft.Divider(color=BORDER),
                        ft.Container(content=dir_list, height=320),
                    ],
                    tight=True,
                    spacing=6,
                ),
                bgcolor=SURFACE,
                padding=ft.Padding.only(left=16, right=16, top=16, bottom=40),
            ),
            use_safe_area=True,
            bgcolor=SURFACE,
        )
        bs_holder[0] = bs
        self.app.page.overlay.append(bs)
        bs.open = True
        self.app.page.update()

    def _browse_download_folder(self, e):
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_paths("download")
            return
        if platform.system() in ["Darwin", "Linux"]:
            path = pick_folder("Select Download Folder")
            if path:
                self._handle_folder_picked(path, "download")
            return
        if self._file_picker:
            self._picking_target = "download"
            self._file_picker.get_directory_path()
        else:
            self.app.show_snackbar("Folder browsing not available")

    def _browse_library_folder(self, e):
        if hasattr(sys, 'getandroidapilevel'):
            self._browse_android_paths("library")
            return
        if platform.system() in ["Darwin", "Linux"]:
            path = pick_folder("Select Library Folder")
            if path:
                self._handle_folder_picked(path, "library")
            return
        if self._file_picker:
            self._picking_target = "library"
            self._file_picker.get_directory_path()
        else:
            self.app.show_snackbar("Folder browsing not available")

    def _on_file_picked(self, e) -> None:
        if hasattr(e, 'path') and e.path:
            self._handle_folder_picked(e.path, self._picking_target)
        self._picking_target = None

    def _handle_folder_picked(self, path: str, target: str):
        if not path:
            return

        if target == "download":
            self.app.target_folder = path
        elif target == "library":
            self.app.library_folder = path

        self._dl_path_field.value  = self.app.target_folder
        self._lib_path_field.value = self.app.library_folder

        from utils.streamrip_api import update_config_params
        if target == "download":
            update_config_params({"downloads": {"folder": path}})
            self.app._save_pref("folder_path", path)
        else:
            self.app._save_pref("library_path", path)

        self.refresh()

        label = "Download" if target == "download" else "Library"
        self.app.show_snackbar(f"{label} folder set: {path}")
        self.app.page.update()
