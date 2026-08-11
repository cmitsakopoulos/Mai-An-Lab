"""Metadata Enrichment Wizard for StreamripApp (Flet).

Minimalist, full-page wizard pane for artist metadata enrichment and manual overrides.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable
import flet as ft

from ui.tokens import (
    TEXT, DIM, BORDER, SURFACE, SURFACE2, CYAN, BG,
    ACCENT_GREEN, ACCENT_AMBER, ACCENT_RED, apply_opacity
)
from utils.metadata_enrich import enrich_library, search_musicbrainz_artists_candidates

logger = logging.getLogger(__name__)


class MetadataEnrichmentWizardPane(ft.Container):
    """Minimalist wizard pane for metadata enrichment & manual overrides."""

    def __init__(self, app: "StreamripFletApp", on_back: Callable | None = None):
        super().__init__(expand=True, bgcolor=BG, padding=12)
        self.app = app
        self.db = app.db_manager
        self.on_back = on_back

        # Wizard State
        self.step = 1  # 1: Scope, 2: Sync, 3: LowConfidence, 4: Gaps/Manual, 5: Complete
        self.include_failed = True
        self.is_syncing = False
        self.cancel_event: asyncio.Event | None = None

        # Metrics
        self.sync_total = 0
        self.sync_current = 0
        self.sync_ok_count = 0
        self.sync_low_count = 0
        self.sync_fail_count = 0
        self.current_artist_label = ""
        self.log_entries: list[str] = []

        # Datasets
        self.low_confidence_items: list[dict] = []
        self.gap_items: list[dict] = []
        self.filtered_gap_items: list[dict] = []
        self.gap_search_query = ""

        # Layout Container
        self.body_container = ft.Container(expand=True)
        self.content = self._build_layout()
        self._load_step1_data()

    def _build_layout(self) -> ft.Control:
        return ft.Column([
            self._build_step_indicator(),
            ft.Container(height=8),
            self.body_container,
            ft.Container(height=8),
            self._build_footer_actions(),
        ], expand=True, spacing=6)

    # ── Step Bar Indicator ──────────────────────────────────────────────────
    def _build_step_indicator(self) -> ft.Control:
        steps_info = [
            (1, "Scope"),
            (2, "Sync"),
            (3, "Matches"),
            (4, "Overrides"),
            (5, "Done"),
        ]
        step_chips = []
        for idx, name in steps_info:
            is_current = (idx == self.step)
            is_done = (idx < self.step)
            color = CYAN if is_current else (ACCENT_GREEN if is_done else DIM)
            bg = apply_opacity(0.18, CYAN) if is_current else (SURFACE2 if is_done else SURFACE)
            chip = ft.Container(
                content=ft.Row([
                    ft.Text(f"{idx}. {name}", color=TEXT if is_current else DIM, size=12, weight="bold" if is_current else "normal")
                ], spacing=4, tight=True),
                bgcolor=bg,
                padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                border_radius=12,
                border=ft.Border.all(1, color if is_current else BORDER)
            )
            step_chips.append(chip)
        return ft.Row(step_chips, spacing=8, wrap=True)

    # ── Minimal Footer Action Button ────────────────────────────────────────
    def _build_footer_actions(self) -> ft.Control:
        buttons = []
        if self.step == 1:
            buttons.append(
                ft.Button(
                    content=ft.Text("Start Sync Pass"),
                    style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                    on_click=lambda e: self._start_sync(),
                )
            )
        elif self.step == 2:
            if self.is_syncing:
                buttons.append(
                    ft.Button(
                        content=ft.Text("Stop Sync"),
                        style=ft.ButtonStyle(bgcolor=ACCENT_RED, color=TEXT),
                        on_click=lambda e: self._cancel_sync(),
                    )
                )
            else:
                buttons.append(
                    ft.Button(
                        content=ft.Text("Review Matches →"),
                        style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                        on_click=lambda e: self._go_to_step(3),
                    )
                )
        elif self.step == 3:
            buttons.append(
                ft.Button(
                    content=ft.Text("Manual Overrides →"),
                    style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                    on_click=lambda e: self._go_to_step(4),
                )
            )
        elif self.step == 4:
            buttons.append(
                ft.Button(
                    content=ft.Text("Finish →"),
                    style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                    on_click=lambda e: self._go_to_step(5),
                )
            )
        elif self.step == 5:
            if self.on_back:
                buttons.append(
                    ft.Button(
                        content=ft.Text("Done"),
                        style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                        on_click=lambda e: self.on_back(),
                    )
                )
        return ft.Row(buttons, alignment="end")

    def _go_to_step(self, target_step: int):
        self.step = target_step
        self._update_view()
        if target_step == 3:
            self._load_low_confidence_data()
        elif target_step == 4:
            self._load_gap_data()
        elif target_step == 5:
            self._load_step5_summary()

    def _update_view(self):
        self.content = self._build_layout()
        self.app.page.update()

    # ── Step 1: Minimal Scope ───────────────────────────────────────────────
    def _load_step1_data(self):
        async def _async_load():
            try:
                needing = await self.db.get_artists_needing_enrichment(include_failed=True)
                gaps = await self.db.get_metadata_gap_artists(limit=500)
                low_conf = await self.db.get_low_confidence_artists(limit=500)
                self.sync_total = len(needing)
                self._render_step1_view(len(needing), len(gaps), len(low_conf))
            except Exception as exc:
                logger.error("Error loading wizard pre-flight data: %s", exc)

        self.app.page.run_task(_async_load)

    def _render_step1_view(self, needing_count: int, gap_count: int, low_count: int):
        self.body_container.content = ft.Column([
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.AUTO_FIX_HIGH_ROUNDED, color=CYAN, size=15),
                        ft.Text("Artist Metadata & Auto-Play", color=CYAN, size=12, weight="bold"),
                    ], spacing=6),
                    ft.Text(
                        "Syncs MusicBrainz country provenance & genres to power precision Auto-Play recommendations. Hand-entered overrides are protected.",
                        color=TEXT, size=11
                    ),
                ], spacing=4),
                bgcolor=SURFACE2,
                padding=10,
                border_radius=8,
                border=ft.Border.all(1, apply_opacity(0.25, CYAN)),
            ),
            ft.Container(height=6),
            ft.Row([
                self._badge_card("Needing Sync", str(needing_count), CYAN),
                self._badge_card("Low Confidence", str(low_count), ACCENT_AMBER),
                self._badge_card("Metadata Gaps", str(gap_count), ACCENT_RED),
            ], spacing=10),
            ft.Container(height=6),
            ft.Checkbox(
                label="Retry incomplete & low-confidence artists",
                value=self.include_failed,
                on_change=self._on_include_failed_change,
                fill_color=CYAN,
            ),
        ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=6)
        self.app.page.update()

    def _on_include_failed_change(self, e):
        self.include_failed = e.control.value

    def _badge_card(self, title: str, val: str, color: str) -> ft.Control:
        return ft.Container(
            content=ft.Column([
                ft.Text(val, color=color, size=22, weight="bold"),
                ft.Text(title, color=DIM, size=11),
            ], horizontal_alignment="center", tight=True),
            bgcolor=SURFACE2,
            border_radius=8,
            padding=12,
            expand=True,
            alignment=ft.Alignment(0, 0),
        )

    # ── Step 2: Live Sync ───────────────────────────────────────────────────
    def _start_sync(self):
        self.step = 2
        self.is_syncing = True
        self.cancel_event = asyncio.Event()
        self.sync_current = 0
        self.sync_ok_count = 0
        self.sync_low_count = 0
        self.sync_fail_count = 0
        self.log_entries = []
        self._update_view()

        async def _do_sync():
            try:
                def _progress_cb(curr, total, name, res):
                    self.sync_current = curr
                    self.sync_total = total
                    self.current_artist_label = name
                    st = res.get("status")
                    if st == "ok":
                        self.sync_ok_count += 1
                        self.log_entries.append(f"✓ {name}")
                    elif st == "lowconfidence":
                        self.sync_low_count += 1
                        self.log_entries.append(f"⚠ {name} (score {res.get('score')}%)")
                    else:
                        self.sync_fail_count += 1
                        self.log_entries.append(f"✗ {name}")
                    if len(self.log_entries) > 150:
                        self.log_entries = self.log_entries[-150:]
                    self.app.page.run_task(self._update_sync_progress_ui)

                summary = await enrich_library(
                    self.db,
                    with_genres=True,
                    include_failed=self.include_failed,
                    progress=_progress_cb,
                    cancel_event=self.cancel_event,
                )
                logger.info("Wizard sync completed: %s", summary)
            except Exception as exc:
                logger.exception("Wizard sync error: %s", exc)
            finally:
                self.is_syncing = False
                self.app.page.run_task(self._update_sync_progress_ui)

        self.app.page.run_task(_do_sync)

    async def _update_sync_progress_ui(self):
        if self.step != 2:
            return
        pct = (self.sync_current / max(1, self.sync_total)) if self.sync_total else 1.0

        progress_bar = ft.ProgressBar(value=pct, color=CYAN, bgcolor=SURFACE2, height=6)
        progress_label = ft.Text(
            f"({self.sync_current}/{self.sync_total}) {self.current_artist_label}"
            if self.is_syncing else f"Sync Complete ({self.sync_current}/{self.sync_total})",
            color=TEXT, size=13, weight="bold"
        )

        ok_chip = ft.Text(f"OK: {self.sync_ok_count}", color=ACCENT_GREEN, size=12, weight="bold")
        low_chip = ft.Text(f"Low: {self.sync_low_count}", color=ACCENT_AMBER, size=12, weight="bold")
        fail_chip = ft.Text(f"Gap: {self.sync_fail_count}", color=ACCENT_RED, size=12, weight="bold")

        log_column = ft.Column(
            [ft.Text(log, color=DIM, size=11, font_family="monospace") for log in reversed(self.log_entries)],
            scroll=ft.ScrollMode.AUTO, expand=True
        )

        self.body_container.content = ft.Column([
            progress_label,
            progress_bar,
            ft.Row([ok_chip, low_chip, fail_chip], spacing=16),
            ft.Container(height=4),
            ft.Container(
                content=log_column,
                bgcolor=SURFACE2,
                padding=8,
                border_radius=6,
                expand=True,
            ),
        ], expand=True, spacing=8)
        self._update_view()

    def _cancel_sync(self):
        if self.cancel_event:
            self.cancel_event.set()
        self.is_syncing = False
        self._update_view()

    # ── Step 3: Review Matches ──────────────────────────────────────────────
    def _load_low_confidence_data(self):
        async def _load():
            items = await self.db.get_low_confidence_artists(limit=100)
            self.low_confidence_items = items
            self._render_step3_view()

        self.app.page.run_task(_load)

    def _render_step3_view(self):
        if not self.low_confidence_items:
            self.body_container.content = ft.Column([
                ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=CYAN, size=36),
                ft.Text("No low-confidence matches to review.", color=TEXT, size=14),
            ], horizontal_alignment="center", alignment=ft.MainAxisAlignment.CENTER, expand=True, spacing=8)
            self.app.page.update()
            return

        card_controls = []
        for item in self.low_confidence_items:
            name = item["artist_name"]
            c = item.get("country") or "Unknown"
            g_list = [g.get("name") if isinstance(g, dict) else str(g) for g in (item.get("genres") or [])]
            g_str = ", ".join(g for g in g_list if g) or "No tags"
            sc = item.get("score", 0)

            def _accept(e, artist_name=name, itm=item):
                async def _do_accept():
                    await self.db.confirm_artist_match(
                        artist_name,
                        mbid=itm.get("mbid"),
                        country=itm.get("country"),
                        area=itm.get("area"),
                        genres=itm.get("genres"),
                        status="ok",
                        score=100,
                    )
                    self._load_low_confidence_data()

                self.app.page.run_task(_do_accept)

            def _reject(e, artist_name=name):
                async def _do_reject():
                    await self.db.confirm_artist_match(artist_name, status="notfound", score=0)
                    self._load_low_confidence_data()

                self.app.page.run_task(_do_reject)

            def _search_custom(e, artist_name=name):
                self._open_custom_mb_search_dialog(artist_name)

            card = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(name, color=TEXT, size=14, weight="bold", overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
                        ft.Container(
                            content=ft.Text(f"{sc}% Match", color=BG, size=10, weight="bold"),
                            bgcolor=ACCENT_AMBER,
                            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                            border_radius=4,
                        ),
                    ], alignment="spaceBetween"),
                    ft.Container(
                        content=ft.Column([
                            ft.Row([
                                ft.Text("Country:", color=DIM, size=11, weight="bold"),
                                ft.Text(c, color=TEXT, size=11),
                            ], spacing=6),
                            ft.Row([
                                ft.Text("Genres:", color=DIM, size=11, weight="bold"),
                                ft.Text(g_str, color=TEXT, size=11, overflow=ft.TextOverflow.ELLIPSIS, max_lines=2, expand=True),
                            ], spacing=6),
                        ], spacing=2),
                        padding=ft.Padding.only(top=4, bottom=4),
                    ),
                    ft.Divider(color=BORDER, height=10),
                    ft.Row([
                        ft.Button(
                            content=ft.Text("Accept"),
                            style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                            icon=ft.Icons.CHECK,
                            on_click=_accept,
                        ),
                        ft.TextButton("Reject", icon=ft.Icons.CLOSE, on_click=_reject),
                        ft.TextButton("Search MB...", icon=ft.Icons.SEARCH, on_click=_search_custom),
                    ], alignment="end", spacing=8),
                ], tight=True, spacing=6),
                bgcolor=SURFACE2,
                border_radius=8,
                border=ft.Border.all(1, BORDER),
                padding=12,
            )
            card_controls.append(card)

        self.body_container.content = ft.Column(
            card_controls, scroll=ft.ScrollMode.AUTO, expand=True, spacing=10
        )
        self.app.page.update()

    def _open_custom_mb_search_dialog(self, artist_name: str):
        search_field = ft.TextField(
            value=artist_name, label="Search MusicBrainz",
            border_color=BORDER, focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE
        )
        results_col = ft.Column([], scroll=ft.ScrollMode.AUTO, height=260)

        sub_dlg = ft.AlertDialog(
            title=ft.Text(f"Candidates for '{artist_name}'", color=TEXT, size=14),
            bgcolor=SURFACE,
            content=ft.Column([search_field, results_col], tight=True, spacing=8),
        )

        def _do_search(e=None):
            async def _run():
                results_col.controls = [ft.ProgressRing(width=16, height=16, stroke_width=2)]
                self.app.page.update()
                candidates = await search_musicbrainz_artists_candidates(search_field.value)
                res_cards = []
                for cand in candidates:
                    c_name = cand["name"]
                    c_country = cand.get("country") or "Unknown"
                    c_genres = [g.get("name") if isinstance(g, dict) else str(g) for g in (cand.get("genres") or [])]
                    c_g_str = ", ".join(c_genres) or "No tags"

                    def _select_cand(ev, c_obj=cand):
                        async def _save_cand():
                            await self.db.confirm_artist_match(
                                artist_name,
                                mbid=c_obj.get("mbid"),
                                country=c_obj.get("country"),
                                area=c_obj.get("area"),
                                genres=c_obj.get("genres"),
                                status="ok",
                                score=c_obj.get("score", 100),
                            )
                            self.app.dismiss_dialog(sub_dlg)
                            self._load_low_confidence_data()

                        self.app.page.run_task(_save_cand)

                    res_cards.append(
                        ft.Container(
                            content=ft.Column([
                                ft.Row([
                                    ft.Text(c_name, color=TEXT, size=13, weight="bold", overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
                                    ft.Text(f"{cand.get('score', 0)}%", color=ACCENT_AMBER, size=11, weight="bold"),
                                ], alignment="spaceBetween"),
                                ft.Text(f"Country: {c_country} | Genres: {c_g_str}", color=DIM, size=11),
                                ft.Row([
                                    ft.Button("Select Match", style=ft.ButtonStyle(bgcolor=CYAN, color=BG), on_click=_select_cand),
                                ], alignment="end"),
                            ], tight=True, spacing=4),
                            bgcolor=SURFACE2,
                            padding=10,
                            border_radius=6,
                            border=ft.Border.all(1, BORDER),
                        )
                    )
                results_col.controls = res_cards if res_cards else [ft.Text("No candidates.", color=DIM, size=12)]
                self.app.page.update()

            self.app.page.run_task(_run)

        search_field.on_submit = _do_search
        sub_dlg.actions = [
            ft.TextButton("Cancel", on_click=lambda e: self.app.dismiss_dialog(sub_dlg)),
            ft.Button("Search", on_click=_do_search),
        ]
        if self.app.page:
            self.app.page.show_dialog(sub_dlg)
        _do_search()

    # ── Step 4: Manual Overrides ────────────────────────────────────────────
    def _load_gap_data(self):
        async def _load():
            gaps = await self.db.get_metadata_gap_artists(limit=150)
            self.gap_items = gaps
            self._apply_gap_filter()

        self.app.page.run_task(_load)

    def _apply_gap_filter(self):
        q = self.gap_search_query.strip().lower()
        if q:
            self.filtered_gap_items = [g for g in self.gap_items if q in g["artist_name"].lower()]
        else:
            self.filtered_gap_items = list(self.gap_items)
        self._render_step4_view()

    def _on_gap_search_change(self, e):
        self.gap_search_query = e.control.value or ""
        self._apply_gap_filter()

    def _render_step4_view(self):
        if not self.gap_items:
            self.body_container.content = ft.Column([
                ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=CYAN, size=36),
                ft.Text("All library artists have metadata coverage.", color=TEXT, size=14),
            ], horizontal_alignment="center", alignment=ft.MainAxisAlignment.CENTER, expand=True, spacing=8)
            self.app.page.update()
            return

        search_input = ft.TextField(
            hint_text="Search gap artists...",
            prefix_icon=ft.Icons.SEARCH,
            bgcolor=SURFACE2, border_color=BORDER, focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=12),
            on_change=self._on_gap_search_change,
            value=self.gap_search_query,
            dense=True,
        )

        rows = []
        for gap in self.filtered_gap_items:
            aname = gap["artist_name"]
            tc = gap.get("track_count", 0)
            c = gap.get("country") or "-"
            g_list = [g.get("name") if isinstance(g, dict) else str(g) for g in (gap.get("genres") or [])]
            g_str = ", ".join(g_list) if g_list else "-"

            def _open_edit(e, name=aname):
                from ui.player.dialogs import ArtistMetadataDialog
                dlg = ArtistMetadataDialog(self.app)
                dlg.open(name, on_saved=self._load_gap_data)

            item_row = ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(aname, color=TEXT, size=14, weight="bold", overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
                        ft.Container(
                            content=ft.Text(f"{tc} tracks", color=DIM, size=10),
                            bgcolor=SURFACE,
                            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                            border_radius=4,
                        ),
                    ], alignment="spaceBetween"),
                    ft.Row([
                        ft.Text("Country:", color=DIM, size=11, weight="bold"),
                        ft.Text(c, color=TEXT, size=11),
                        ft.Container(width=10),
                        ft.Text("Genres:", color=DIM, size=11, weight="bold"),
                        ft.Text(g_str, color=TEXT, size=11, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
                    ], spacing=4),
                    ft.Row([
                        ft.Button(
                            content=ft.Text("Edit Override"),
                            style=ft.ButtonStyle(bgcolor=CYAN, color=BG),
                            icon=ft.Icons.EDIT_ROUNDED,
                            on_click=_open_edit,
                        ),
                    ], alignment="end"),
                ], tight=True, spacing=6),
                bgcolor=SURFACE2,
                border_radius=8,
                border=ft.Border.all(1, BORDER),
                padding=12,
            )
            rows.append(item_row)

        self.body_container.content = ft.Column([
            search_input,
            ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True, spacing=8),
        ], expand=True, spacing=6)
        self.app.page.update()

    # ── Step 5: Complete ────────────────────────────────────────────────────
    def _load_step5_summary(self):
        async def _load():
            gaps = await self.db.get_metadata_gap_artists(limit=500)
            lows = await self.db.get_low_confidence_artists(limit=500)

            try:
                from utils.track_graph import build_genre_affinity
                await build_genre_affinity(self.db)
            except Exception as exc:
                logger.warning("Genre affinity refresh failed: %s", exc)

            self.body_container.content = ft.Column([
                ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=CYAN, size=48),
                ft.Text("Metadata Sync Complete", color=TEXT, size=16, weight="bold"),
                ft.Text(f"Residual Gaps: {len(gaps)}  |  Unresolved Low Confidence: {len(lows)}", color=DIM, size=12),
                ft.Text("NPMI genre affinity model updated live.", color=ACCENT_GREEN, size=12),
            ], horizontal_alignment="center", alignment=ft.MainAxisAlignment.CENTER, expand=True, spacing=10)
            self.app.page.update()

        self.app.page.run_task(_load)
