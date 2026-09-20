import logging

import flet as ft

from ui.tokens import TEXT, DIM, BORDER, SURFACE, CYAN
from ui.widgets import dialog_handoff

logger = logging.getLogger(__name__)

# (tier, title, subtitle, icon)
TIERS = [
    ("mp3",   "High",       "MP3 / AAC 320kbps", ft.Icons.MUSIC_NOTE),
    ("cd",    "CD Quality", "16-bit FLAC",       ft.Icons.ALBUM),
    ("hires", "Hi-Res",     "24-bit FLAC",       ft.Icons.HIGH_QUALITY),
]
TIER_KEYS = {t[0] for t in TIERS}


class QualitySelectorSheet:
    """Quality picker for one item or for a whole selection.

    The sheet asks one question and nothing else. A "remember this choice"
    control used to live here; it is a persistent preference, so it belongs in
    Settings (Storage -> Default Download Quality) where it can be seen and
    undone, not behind a checkbox on a transient sheet.
    """

    def __init__(self, app: "StreamripFletApp"):
        self.app          = app
        self.page         = app.page
        self._pending: list[dict] = []
        self._on_done     = None
        self._initialized = False
        self._sheet       = None

    # ── remembered default ──────────────────────────────────────────────────
    def _default_tier(self) -> str | None:
        """The tier to use without asking, or None to ask. Set in Settings."""
        try:
            from utils.streamrip_api import load_config
            tier = str((load_config().get("general", {}) or {}).get("default_quality", "")).lower()
            return tier if tier in TIER_KEYS else None
        except Exception:
            logger.exception("failed to read default quality")
            return None

    # ── construction ────────────────────────────────────────────────────────
    def _ensure_initialized(self):
        if self._initialized:
            return

        self._heading = ft.Text("Select Quality", color=TEXT, size=15, weight=ft.FontWeight.W_700)
        self._subheading = ft.Text("", color=DIM, size=12, visible=False)

        # Close-then-follow-up handoff. Closing a BottomSheet and raising a
        # snackbar in the same Flutter frame makes the outgoing sheet's bare
        # Navigator.pop() take the incoming toast's route instead — which blanks
        # the view. dialog_handoff defers the follow-up until Flutter confirms
        # the sheet's route is actually gone.
        self._on_dismiss, self._close_with = dialog_handoff(self.app, lambda: self._sheet)

        self._sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Column([self._heading, self._subheading], spacing=2),
                        ft.Divider(color=BORDER),
                        *[
                            ft.ListTile(
                                title=ft.Text(title, color=TEXT),
                                subtitle=ft.Text(sub, color=DIM),
                                leading=ft.Icon(icon, color=CYAN),
                                on_click=lambda e, t=tier: self._confirm(t),
                            )
                            for tier, title, sub, icon in TIERS
                        ],
                    ],
                    spacing=0,
                    tight=True,
                ),
                bgcolor=SURFACE,
                padding=20,
            ),
            bgcolor=SURFACE,
            on_dismiss=self._on_dismiss,
        )
        self._initialized = True

    def build(self) -> ft.BottomSheet:
        self._ensure_initialized()
        return self._sheet

    # ── entry points ────────────────────────────────────────────────────────
    def show(self, track_data: dict):
        """Single item. Honours the default from Settings and skips the sheet."""
        self._present([track_data], on_done=None)

    def show_batch(self, records: list[dict], on_done=None):
        """A whole selection at one quality, one decision."""
        if not records:
            return
        self._present(list(records), on_done=on_done)

    def _present(self, records: list[dict], on_done):
        self._pending = records
        self._on_done = on_done

        default = self._default_tier()
        if default:
            # The user set a default in Settings; don't ask.
            self._enqueue(default)
            return

        self._ensure_initialized()
        count = len(records)
        self._subheading.value = (
            f"{count} items" if count > 1 else records[0].get("ui_title", "")
        )
        self._subheading.visible = True
        # Presented through the Flet 0.86 dialog stack rather than the legacy
        # page.overlay + `.open = True` path, which never installs the dismiss
        # lifecycle: a scrim tap closed the sheet in Flutter while leaving
        # `.open` True in Python, making the next open a silent no-op.
        self.page.show_dialog(self._sheet)

    def _confirm(self, tier: str):
        records, on_done = self._pending, self._on_done
        self._pending, self._on_done = [], None
        # The enqueue runs only once Flutter reports the sheet gone.
        self._close_with(lambda: self._submit(records, on_done, tier))

    def _enqueue(self, tier: str):
        """Submit without a sheet — used when a default is configured."""
        records, on_done = self._pending, self._on_done
        self._pending, self._on_done = [], None
        self._submit(records, on_done, tier)

    def _submit(self, records: list[dict], on_done, tier: str):
        if not records:
            return

        if len(records) == 1:
            self.app.queue.enqueue(records[0], quality_tier=tier)
            label = records[0].get("ui_title") or records[0].get("name") or "item"
            message = f"Added “{label}” to downloads ({tier.upper()})."
        else:
            self.app.queue.enqueue_many(records, quality_tier=tier)
            message = f"Added {len(records)} items to downloads ({tier.upper()})."

        # The old controller only confirmed an enqueue while another job was
        # already running, so the first download landed with no feedback at all.
        self.app.show_snackbar(message, icon=ft.Icons.DOWNLOAD_ROUNDED)
        if on_done:
            on_done()
