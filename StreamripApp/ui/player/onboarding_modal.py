"""
Interactive multi-step Onboarding Wizard for Mai-An Lab.
Provides a modern, frictionless introduction to the app's core innovations,
guides initial music source configuration, and personalizes theme accents.
"""
import os
import flet as ft
from ui.tokens import (
    BG, SURFACE, SURFACE2, SURFACE_ELEVATED, TEXT, DIM, TEXT_TERTIARY,
    BORDER, BORDER_SUBTLE, CYAN, AMBER, ACCENT_GREEN, RADIUS_CARD, apply_opacity
)
from utils.streamrip_api import update_config_params, load_config
from utils.filepath_utils import get_app_dir

ACCENT_OPTIONS = [
    ("#FFD60A", "Gold"),
    ("#64D2FF", "Cyan"),
    ("#BF5AF2", "Violet"),
    ("#FF375F", "Rose"),
    ("#30D158", "Mint"),
]

STARTUP_OPTIONS = [
    ("Library", "Library", ft.Icons.LIBRARY_MUSIC_ROUNDED),
    ("Search", "Search", ft.Icons.SEARCH_ROUNDED),
    ("Jarvis", "Jarvis", ft.Icons.AUTO_AWESOME_ROUNDED),
]


class OnboardingWizardModal:
    """Glassmorphic 3-slide modal for first-run orientation and configuration."""

    def __init__(self, app):
        self.app = app
        self.page = app.page
        self.current_slide = 0
        self.selected_accent = "#FFD60A"
        self.selected_startup = "Library"
        self._dialog = None

        # Resolve local music directory
        home = os.path.expanduser("~")
        candidate_dir = os.path.join(home, "Music", "Streamrip")
        if not os.path.exists(candidate_dir):
            candidate_dir = os.path.join(home, "Music")
        self.detected_music_dir = candidate_dir

        # Interactive sources state
        self.streaming_tab = "qobuz"  # "qobuz", "deezer"
        self.qobuz_user = ""
        self.qobuz_token = ""
        self.deezer_arl = ""
        self._folder_field = None

        self._build_controls()

    def _build_controls(self):
        self._slide_container = ft.AnimatedSwitcher(
            content=self._build_slide_0(),
            transition=ft.AnimatedSwitcherTransition.FADE,
            duration=220,
            reverse_duration=180,
            expand=True,
        )

        self._dots_row = ft.Row(
            controls=self._build_dots(),
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        )

        self._dialog_content = ft.Container(
            content=ft.Column(
                [
                    self._slide_container,
                    ft.Container(height=12),
                    self._dots_row,
                ],
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=420,
            height=530,
            padding=ft.Padding.only(top=18, left=18, right=18, bottom=14),
            border_radius=22,
            bgcolor=SURFACE,
            border=ft.Border.all(1, apply_opacity(0.18, TEXT)),
            shadow=ft.BoxShadow(
                spread_radius=0,
                blur_radius=28,
                color=apply_opacity(0.18, self.selected_accent),
                offset=ft.Offset(0, 8),
            ),
        )

        self._dialog = ft.AlertDialog(
            modal=True,
            bgcolor="transparent",
            content_padding=0,
            content=self._dialog_content,
        )

    def _build_step_badge(self, step_num: int) -> ft.Control:
        return ft.Container(
            content=ft.Text(
                f"STEP {step_num} OF 3",
                size=9,
                weight=ft.FontWeight.W_800,
                color=self.selected_accent,
            ),
            bgcolor=apply_opacity(0.12, self.selected_accent),
            border=ft.Border.all(1, apply_opacity(0.24, self.selected_accent)),
            border_radius=8,
            padding=ft.Padding.symmetric(vertical=3, horizontal=8),
        )

    def _build_dots(self) -> list[ft.Control]:
        dots = []
        for i in range(3):
            is_active = (i == self.current_slide)
            dots.append(
                ft.Container(
                    width=is_active and 22 or 7,
                    height=7,
                    border_radius=4,
                    bgcolor=is_active and self.selected_accent or apply_opacity(0.25, TEXT),
                    animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
                )
            )
        return dots

    def _update_view(self):
        builders = [self._build_slide_0, self._build_slide_1, self._build_slide_2]
        self._slide_container.content = builders[self.current_slide]()
        self._dots_row.controls = self._build_dots()
        self._dialog_content.shadow = ft.BoxShadow(
            spread_radius=0,
            blur_radius=28,
            color=apply_opacity(0.18, self.selected_accent),
            offset=ft.Offset(0, 8),
        )
        try:
            self._dialog_content.update()
        except Exception:
            self.app.safe_update(lambda: None)

    # ── Slide 0: Discovery & Innovations ─────────────────────────────────────
    def _build_slide_0(self) -> ft.Control:
        features = [
            (
                ft.Icons.DOWNLOAD_ROUNDED,
                "#30D158",
                "Streamrip Downloader",
                "Download and stream lossless FLAC & MP3 from Qobuz or Deezer directly into your library.",
            ),
            (
                ft.Icons.AUTO_AWESOME_ROUNDED,
                "#64D2FF",
                "Customizable AI Assistant",
                "Voice and chat control powered by cloud LLMs (Gemini, Ollama with your API keys) or offline tokenizer.",
            ),
            (
                ft.Icons.TUNE_ROUNDED,
                "#FF9F0A",
                "Studio DSP & Dynamism",
                "5-band graphic equalizer with custom presets, real-time beat punch enhancement, and dynamic theme.",
            ),
            (
                ft.Icons.POLYLINE_ROUNDED,
                "#BF5AF2",
                "Interactive Network & Auto-Play",
                "Explore your music as an interactive 2D acoustic map with endless similarity-based auto-play.",
            ),
            (
                ft.Icons.AUTO_FIX_HIGH_ROUNDED,
                "#FF375F",
                "Playlists & Metadata Wizard",
                "Curate custom playlists and run MusicBrainz tag enrichment to boost auto-play accuracy.",
            ),
        ]

        feature_rows = []
        for icon, color, title, desc in features:
            feature_rows.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(icon, color=color, size=16),
                                width=32,
                                height=32,
                                border_radius=9,
                                bgcolor=apply_opacity(0.12, color),
                                alignment=ft.Alignment(0, 0),
                            ),
                            ft.Column(
                                [
                                    ft.Text(title, size=11.5, weight=ft.FontWeight.BOLD, color=TEXT),
                                    ft.Text(desc, size=10, color=DIM, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.symmetric(vertical=3, horizontal=4),
                )
            )

        return ft.Column(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, color=self.selected_accent, size=22),
                                width=40,
                                height=40,
                                border_radius=12,
                                bgcolor=apply_opacity(0.14, self.selected_accent),
                                alignment=ft.Alignment(0, 0),
                            ),
                            ft.Column(
                                [
                                    ft.Text("Welcome to Mai An Lab", size=17, weight=ft.FontWeight.W_800, color=TEXT),
                                    ft.Text("Dual-Platform Studio Music Hub", size=11, color=DIM),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            self._build_step_badge(1),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.only(bottom=8),
                ),
                ft.Divider(color=apply_opacity(0.12, TEXT), height=1),
                ft.Container(height=4),
                ft.Column(feature_rows, spacing=2, expand=True, scroll=ft.ScrollMode.ADAPTIVE),
                ft.Container(height=6),
                ft.Row(
                    [
                        ft.Button(
                            content=ft.Row(
                                [
                                    ft.Text("GET STARTED", weight=ft.FontWeight.W_800, color=BG, size=12),
                                    ft.Icon(ft.Icons.ARROW_FORWARD_ROUNDED, color=BG, size=15),
                                ],
                                alignment=ft.MainAxisAlignment.CENTER,
                                spacing=8,
                            ),
                            style=ft.ButtonStyle(
                                bgcolor=self.selected_accent,
                                shape=ft.RoundedRectangleBorder(radius=12),
                                padding=ft.Padding.symmetric(horizontal=16, vertical=0),
                            ),
                            height=42,
                            expand=True,
                            on_click=lambda _: self._go_to_slide(1),
                        ),
                    ],
                ),
            ],
            spacing=0,
            expand=True,
        )

    # ── Slide 1: Music Sources Setup ─────────────────────────────────────────
    def _build_slide_1(self) -> ft.Control:
        header_container = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(ft.Icons.FOLDER_SPECIAL_ROUNDED, color=ACCENT_GREEN, size=24),
                        width=44,
                        height=44,
                        border_radius=14,
                        bgcolor=apply_opacity(0.14, ACCENT_GREEN),
                        alignment=ft.Alignment(0, 0),
                    ),
                    ft.Column(
                        [
                            ft.Text("Music Sources", size=18, weight=ft.FontWeight.W_800, color=TEXT),
                            ft.Text("Choose where to index and save music", size=11, color=DIM),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    self._build_step_badge(2),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.only(bottom=10),
        )

        # Ensure _folder_field persists user input
        if self._folder_field is None:
            self._folder_field = ft.TextField(
                value=self.detected_music_dir,
                text_size=11,
                text_style=ft.TextStyle(font_family="Courier New", color=CYAN),
                dense=True,
                content_padding=ft.Padding.symmetric(vertical=8, horizontal=10),
                border_radius=8,
                bgcolor=apply_opacity(0.06, TEXT),
                border_color=apply_opacity(0.15, TEXT),
                focused_border_color=self.selected_accent,
                expand=True,
                on_change=self._on_folder_change,
            )
        else:
            self._folder_field.value = self.detected_music_dir
            self._folder_field.focused_border_color = self.selected_accent

        folder_card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=ACCENT_GREEN, size=16),
                            ft.Text("Local Library Folder", weight=ft.FontWeight.BOLD, size=12, color=TEXT),
                        ],
                        spacing=6,
                    ),
                    ft.Row(
                        [
                            self._folder_field,
                            ft.IconButton(
                                icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                                icon_color=self.selected_accent,
                                tooltip="Browse folders",
                                style=ft.ButtonStyle(
                                    shape=ft.RoundedRectangleBorder(radius=8),
                                    bgcolor=apply_opacity(0.1, self.selected_accent),
                                ),
                                on_click=self._browse_folder,
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "Auto-indexed on startup. You can change or add folders anytime in Settings.",
                        size=10,
                        color=DIM,
                    ),
                ],
                spacing=4,
            ),
            bgcolor=SURFACE2,
            border_radius=14,
            padding=12,
            border=ft.Border.all(1, apply_opacity(0.1, TEXT)),
        )

        # Streaming tab selector chips (Qobuz & Deezer)
        def _build_tab_chip(tab_key: str, label: str, icon):
            is_active = (self.streaming_tab == tab_key)
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(icon, size=14, color=is_active and BG or (self.selected_accent if is_active else TEXT)),
                        ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=is_active and BG or TEXT),
                    ],
                    spacing=6,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                bgcolor=is_active and self.selected_accent or apply_opacity(0.06, TEXT),
                border=ft.Border.all(1, is_active and self.selected_accent or apply_opacity(0.12, TEXT)),
                border_radius=10,
                padding=ft.Padding.symmetric(vertical=8, horizontal=10),
                animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
                expand=True,
                on_click=lambda _, k=tab_key: self._set_streaming_tab(k),
            )

        tab_chips = ft.Row(
            [
                _build_tab_chip("qobuz", "Qobuz", ft.Icons.MUSIC_NOTE_ROUNDED),
                _build_tab_chip("deezer", "Deezer", ft.Icons.GRAPHIC_EQ_ROUNDED),
            ],
            spacing=8,
        )

        if self.streaming_tab == "deezer":
            tab_body = ft.Column(
                [
                    ft.Text("Deezer Authentication (FLAC & MP3)", size=11, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.TextField(
                        value=self.deezer_arl,
                        label="Your Deezer ARL Cookie",
                        hint_text="Paste your 192-char personal ARL cookie",
                        dense=True,
                        text_size=11,
                        password=True,
                        can_reveal_password=True,
                        label_style=ft.TextStyle(size=10, color=DIM),
                        content_padding=ft.Padding.symmetric(vertical=10, horizontal=10),
                        border_radius=8,
                        bgcolor=apply_opacity(0.06, TEXT),
                        border_color=apply_opacity(0.15, TEXT),
                        focused_border_color=self.selected_accent,
                        on_change=lambda e: self._set_deezer_arl(e.control.value),
                    ),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=13, color=DIM),
                            ft.Text("Requires your own personal Deezer account. Retrieve ARL from browser cookies. Leave blank to skip.", size=10, color=DIM, expand=True),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                ],
                spacing=8,
            )
        else:
            tab_body = ft.Column(
                [
                    ft.Text("Qobuz Authentication (Hi-Res FLAC)", size=11, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.TextField(
                        value=self.qobuz_user,
                        label="Your Qobuz Email or User ID",
                        hint_text="e.g. user@example.com or numeric ID",
                        dense=True,
                        text_size=11,
                        label_style=ft.TextStyle(size=10, color=DIM),
                        content_padding=ft.Padding.symmetric(vertical=10, horizontal=10),
                        border_radius=8,
                        bgcolor=apply_opacity(0.06, TEXT),
                        border_color=apply_opacity(0.15, TEXT),
                        focused_border_color=self.selected_accent,
                        on_change=lambda e: self._set_qobuz_user(e.control.value),
                    ),
                    ft.TextField(
                        value=self.qobuz_token,
                        label="Your Password or Session Token",
                        hint_text="Your personal account password or token",
                        dense=True,
                        text_size=11,
                        password=True,
                        can_reveal_password=True,
                        label_style=ft.TextStyle(size=10, color=DIM),
                        content_padding=ft.Padding.symmetric(vertical=10, horizontal=10),
                        border_radius=8,
                        bgcolor=apply_opacity(0.06, TEXT),
                        border_color=apply_opacity(0.15, TEXT),
                        focused_border_color=self.selected_accent,
                        on_change=lambda e: self._set_qobuz_token(e.control.value),
                    ),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=13, color=DIM),
                            ft.Text("Requires your own personal Qobuz subscription. Leave blank to skip.", size=10, color=DIM, expand=True),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                ],
                spacing=8,
            )

        streaming_card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.STREAM_ROUNDED, color=AMBER, size=16),
                            ft.Text("Streaming Accounts (Optional)", weight=ft.FontWeight.BOLD, size=12, color=TEXT),
                        ],
                        spacing=6,
                    ),
                    tab_chips,
                    ft.Container(height=2),
                    tab_body,
                ],
                spacing=8,
            ),
            bgcolor=SURFACE2,
            border_radius=14,
            padding=12,
            border=ft.Border.all(1, apply_opacity(0.1, TEXT)),
        )

        skip_notice = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=13, color=DIM),
                    ft.Text(
                        "Streaming accounts are optional. Users must supply their own subscriptions, which can be configured anytime in Settings.",
                        size=10,
                        color=DIM,
                        expand=True,
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=4),
        )

        nav_row = ft.Row(
            [
                ft.OutlinedButton(
                    "BACK",
                    style=ft.ButtonStyle(
                        color=TEXT,
                        side=ft.BorderSide(1, apply_opacity(0.2, TEXT)),
                        shape=ft.RoundedRectangleBorder(radius=12),
                        padding=ft.Padding.symmetric(horizontal=8, vertical=0),
                    ),
                    height=44,
                    width=74,
                    on_click=lambda _: self._go_to_slide(0),
                ),
                ft.OutlinedButton(
                    "SKIP",
                    style=ft.ButtonStyle(
                        color=DIM,
                        side=ft.BorderSide(1, apply_opacity(0.15, TEXT)),
                        shape=ft.RoundedRectangleBorder(radius=12),
                        padding=ft.Padding.symmetric(horizontal=8, vertical=0),
                    ),
                    height=44,
                    width=74,
                    on_click=lambda _: self._skip_sources(),
                ),
                ft.Button(
                    content=ft.Row(
                        [
                            ft.Text("PERSONALIZE", weight=ft.FontWeight.W_800, color=BG, size=12),
                            ft.Icon(ft.Icons.ARROW_FORWARD_ROUNDED, color=BG, size=15),
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=8,
                    ),
                    style=ft.ButtonStyle(
                        bgcolor=self.selected_accent,
                        shape=ft.RoundedRectangleBorder(radius=12),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=0),
                    ),
                    height=44,
                    expand=True,
                    on_click=lambda _: self._save_sources_and_next(),
                ),
            ],
            spacing=8,
        )

        content_column = ft.Column(
            [
                folder_card,
                ft.Container(height=10),
                streaming_card,
                ft.Container(height=8),
                skip_notice,
            ],
            spacing=0,
            scroll=ft.ScrollMode.ADAPTIVE,
            expand=True,
        )

        return ft.Column(
            [
                header_container,
                ft.Divider(color=apply_opacity(0.12, TEXT), height=1),
                ft.Container(height=10),
                content_column,
                ft.Container(height=10),
                nav_row,
            ],
            spacing=0,
            expand=True,
        )

    # ── Slide 2: Personalization & Launch ────────────────────────────────────
    def _build_slide_2(self) -> ft.Control:
        # Accent chips
        accent_chips = []
        for hex_code, label in ACCENT_OPTIONS:
            is_sel = (hex_code == self.selected_accent)
            accent_chips.append(
                ft.GestureDetector(
                    content=ft.Container(
                        width=38,
                        height=38,
                        border_radius=19,
                        bgcolor=hex_code,
                        border=ft.Border.all(is_sel and 3 or 1, is_sel and "#FFFFFF" or apply_opacity(0.2, TEXT)),
                        alignment=ft.Alignment(0, 0),
                        content=is_sel and ft.Icon(ft.Icons.CHECK_ROUNDED, color=BG, size=18) or None,
                        animate=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
                    ),
                    on_tap=lambda _, c=hex_code: self._set_accent(c),
                )
            )

        # Startup page chips
        startup_chips = []
        for key, label, icon in STARTUP_OPTIONS:
            is_sel = (key == self.selected_startup)
            startup_chips.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(icon, size=14, color=is_sel and BG or self.selected_accent),
                            ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=is_sel and BG or TEXT),
                        ],
                        spacing=6,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    bgcolor=is_sel and self.selected_accent or apply_opacity(0.08, TEXT),
                    border=ft.Border.all(1, is_sel and self.selected_accent or apply_opacity(0.12, TEXT)),
                    border_radius=10,
                    padding=ft.Padding.symmetric(vertical=8, horizontal=8),
                    animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
                    expand=True,
                    on_click=lambda _, k=key: self._set_startup(k),
                )
            )

        return ft.Column(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(ft.Icons.PALETTE_ROUNDED, color=self.selected_accent, size=24),
                                width=44,
                                height=44,
                                border_radius=14,
                                bgcolor=apply_opacity(0.14, self.selected_accent),
                                alignment=ft.Alignment(0, 0),
                            ),
                            ft.Column(
                                [
                                    ft.Text("Make It Yours", size=18, weight=ft.FontWeight.W_800, color=TEXT),
                                    ft.Text("Select your accent and default start view", size=11, color=DIM),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            self._build_step_badge(3),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.only(bottom=10),
                ),
                ft.Divider(color=apply_opacity(0.12, TEXT), height=1),
                ft.Container(height=12),
                ft.Text("THEME ACCENT", size=10, weight=ft.FontWeight.W_700, color=DIM),
                ft.Container(height=6),
                ft.Row(accent_chips, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Container(height=18),
                ft.Text("STARTUP SCREEN", size=10, weight=ft.FontWeight.W_700, color=DIM),
                ft.Container(height=6),
                ft.Row(startup_chips, spacing=8),
                ft.Container(height=18),
                # Pro tip highlight
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.LIGHTBULB_ROUNDED, color=self.selected_accent, size=15),
                            ft.Text(
                                "Pro Tip: Long-press any track for technical audio specs and instant acoustic similarity walks.",
                                size=10,
                                color=DIM,
                                expand=True,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    bgcolor=SURFACE2,
                    border_radius=10,
                    padding=8,
                    border=ft.Border.all(1, apply_opacity(0.08, TEXT)),
                ),
                ft.Container(height=14),
                ft.Row(
                    [
                        ft.OutlinedButton(
                            "BACK",
                            style=ft.ButtonStyle(
                                color=TEXT,
                                side=ft.BorderSide(1, apply_opacity(0.2, TEXT)),
                                shape=ft.RoundedRectangleBorder(radius=12),
                                padding=ft.Padding.symmetric(horizontal=8, vertical=0),
                            ),
                            height=44,
                            width=82,
                            on_click=lambda _: self._go_to_slide(1),
                        ),
                        ft.Button(
                            content=ft.Row(
                                [
                                    ft.Text("ENTER MAI-AN LAB", weight=ft.FontWeight.W_900, color=BG, size=12),
                                    ft.Icon(ft.Icons.CHECK_ROUNDED, color=BG, size=15),
                                ],
                                alignment=ft.MainAxisAlignment.CENTER,
                                spacing=8,
                            ),
                            style=ft.ButtonStyle(
                                bgcolor=self.selected_accent,
                                shape=ft.RoundedRectangleBorder(radius=12),
                                padding=ft.Padding.symmetric(horizontal=12, vertical=0),
                            ),
                            height=44,
                            expand=True,
                            on_click=self._finish_onboarding,
                        ),
                    ],
                    spacing=10,
                ),
            ],
            spacing=0,
            expand=True,
        )

    def _go_to_slide(self, index: int):
        self.current_slide = index
        self._update_view()

    def _on_folder_change(self, e):
        if self._folder_field and self._folder_field.value:
            self.detected_music_dir = self._folder_field.value.strip()

    async def _browse_folder(self, e):
        from ui.widgets import pick_folder
        import asyncio
        picked = await asyncio.to_thread(pick_folder, "Select Music Library Folder")
        if picked:
            self.detected_music_dir = picked
            if hasattr(self, "_folder_field") and self._folder_field:
                self._folder_field.value = picked
                try:
                    self._folder_field.update()
                except Exception:
                    pass
            self._update_view()

    def _set_streaming_tab(self, tab_key: str):
        self.streaming_tab = tab_key
        self._update_view()

    def _set_qobuz_user(self, val: str):
        self.qobuz_user = val

    def _set_qobuz_token(self, val: str):
        self.qobuz_token = val

    def _set_deezer_arl(self, val: str):
        self.deezer_arl = val

    def _skip_sources(self):
        self._go_to_slide(2)

    def _save_sources_and_next(self):
        if hasattr(self, "_folder_field") and self._folder_field and self._folder_field.value:
            self.detected_music_dir = self._folder_field.value.strip()
        self._go_to_slide(2)

    def _set_accent(self, hex_code: str):
        self.selected_accent = hex_code
        self._update_view()

    def _set_startup(self, key: str):
        self.selected_startup = key
        self._update_view()

    def _finish_onboarding(self, e):
        # 1. Dismiss modal
        if self._dialog and hasattr(self.app, "dismiss_dialog"):
            self.app.dismiss_dialog(self._dialog)
        elif self._dialog and hasattr(self.page, "close"):
            try:
                self.page.close(self._dialog)
            except Exception:
                pass

        # 2. Write .onboarded marker
        try:
            data_dir = get_app_dir()
            os.makedirs(data_dir, exist_ok=True)
            marker_path = os.path.join(data_dir, ".onboarded")
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write("1")
        except Exception:
            pass
        try:
            from utils.streamrip_api import get_config_path
            app_dir = os.path.dirname(get_config_path())
            os.makedirs(app_dir, exist_ok=True)
            marker_path2 = os.path.join(app_dir, ".onboarded")
            with open(marker_path2, "w", encoding="utf-8") as f:
                f.write("1")
        except Exception:
            pass
        try:
            flet_storage = os.environ.get("FLET_APP_STORAGE_DATA", "")
            if flet_storage and os.path.isdir(flet_storage):
                with open(os.path.join(flet_storage, ".onboarded"), "w", encoding="utf-8") as f:
                    f.write("1")
        except Exception:
            pass

        # 3. Save chosen preferences
        update_dict = {
            "appearance": {
                "accent_color": self.selected_accent,
                "show_jarvis": True,
                "show_network": False,
                "show_playlists": True,
                "show_artists": True,
                "show_albums": False,
                "show_tracks": True,
            },
            "general": {
                "startup_page": self.selected_startup,
            },
            "downloads": {
                "folder": self.detected_music_dir,
            }
        }

        # 4. Save streaming credentials if entered
        q_user = self.qobuz_user.strip()
        q_token = self.qobuz_token.strip()
        if q_user and q_token:
            update_dict["qobuz"] = {
                "use_auth_token": True,
                "email_or_userid": q_user,
                "password_or_token": q_token,
                "app_id": "312369995",
                "secrets": ["e79f8b9be485692b0e5f9dd895826368"],
            }

        d_arl = self.deezer_arl.strip()
        if d_arl:
            update_dict["deezer"] = {
                "arl": d_arl
            }

        update_config_params(update_dict)

        if hasattr(self.app, "_save_pref"):
            self.app._save_pref("folder_path", self.detected_music_dir)
            self.app._save_pref("library_path", self.detected_music_dir)
        if hasattr(self.app, "target_folder"):
            self.app.target_folder = self.detected_music_dir
        if hasattr(self.app, "library_folder"):
            self.app.library_folder = self.detected_music_dir

        # 5. Map startup tab index
        tab_map = {"Jarvis": 0, "Search": 1, "Library": 2}
        target_tab = tab_map.get(self.selected_startup, 2)
        welcome_msg = "Welcome to Mai-An Lab! Enjoy studio-grade listening."

        # 6. Soft-restart UI so the newly chosen theme and layout apply app-wide
        if hasattr(self.app, "restart_ui"):
            self.app.restart_ui(target_tab=target_tab, post_message=welcome_msg)
        else:
            if hasattr(self.app, "_apply_accent"):
                self.app._apply_accent(self.selected_accent)
            if hasattr(self.app, "_switch_tab"):
                self.app._switch_tab(target_tab)
            if hasattr(self.app, "show_snackbar"):
                self.app.show_snackbar(welcome_msg)

    @classmethod
    def show(cls, app):
        wizard = cls(app)
        if app.page:
            app.page.show_dialog(wizard._dialog)
        return wizard
