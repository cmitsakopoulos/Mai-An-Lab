"""Metadata Workbench — the single surface for artist-provenance curation.

This absorbs what used to be a separate 5-step wizard. There is now ONE place to
do everything metadata:

  • SYNC   — pull country + genres from MusicBrainz for un-enriched artists,
             shown as an inline progress banner (no separate page, so there is
             nowhere to get lost coming back from);
  • REVIEW — low-confidence matches surface as the "Uncertain" filter, accepted
             or rejected inline;
  • FIX    — the artists MusicBrainz simply has no tags for (on this library, the
             Greek scene) are tagged by hand or in a scene-wide batch.

Design notes (see the redesign appraisal):
  • summary before detail — a coverage figure + severity bar, on the same field
    the walk reads; severity is a colour (red = an artist Auto-Play cannot fence
    at all — no genre AND no country);
  • fix a scene, not a row — gaps group by country and a whole shelf tags at once;
  • tap, don't type — editing is inline, seeded by the artist's own file tags and
    the library's existing genre vocabulary so a hand entry lands inside the NPMI
    model that has to read it.

Flet 0.86 gotchas baked in here: `SegmentedButton.selected` is `list[str]` and
its msgpack encoder cannot serialize a `set` — so the filter control is a custom
scrollable pill row (four filters don't fit a native segmented button in
portrait anyway), and every control property stays a list/str/num.
"""

from __future__ import annotations

import asyncio
import logging
import flet as ft

from ui.tokens import (
    TEXT, DIM, BORDER, SURFACE, SURFACE2, CYAN, BG,
    ACCENT_GREEN, ACCENT_AMBER, ACCENT_RED, apply_opacity,
)
from utils.metadata_enrich import (
    enrich_library,
    search_musicbrainz_artists_candidates,
    musicbrainz_artist_details,
)

logger = logging.getLogger(__name__)

_COUNTRY_NAMES = {
    "GR": "Greece", "US": "United States", "GB": "United Kingdom", "FR": "France",
    "DE": "Germany", "IT": "Italy", "ES": "Spain", "NL": "Netherlands",
    "SE": "Sweden", "CA": "Canada", "AU": "Australia", "JP": "Japan",
    "KR": "South Korea", "BR": "Brazil", "RU": "Russia", "IE": "Ireland",
}


def _flag(cc: str | None) -> str:
    if not cc or len(cc) != 2 or not cc.isalpha():
        return "🌐"
    cc = cc.upper()
    return chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)


class MetadataWorkbenchPane(ft.Container):
    """Standing surface for artist-metadata sync, review, and curation."""

    def __init__(self, app, on_back=None, on_open_sync=None):
        super().__init__(expand=True, bgcolor=BG, padding=ft.Padding.symmetric(horizontal=14, vertical=8))
        self.app = app
        self.db = app.db_manager
        self.on_back = on_back
        self.on_open_sync = on_open_sync  # kept for signature compatibility; unused (sync is inline)

        # ── data ──
        self.coverage: dict = {}
        self.all_gaps: list[dict] = []
        self.low_conf: list[dict] = []
        self.vocab: list[dict] = []
        self.source_cache: dict[str, list[str]] = {}

        # ── view state ──
        self.filter = "critical"          # critical | no_tags | uncertain | all
        self.search = ""
        self.selected: set[str] = set()
        self.expanded: str | None = None
        self.edit_country = ""
        self.edit_genres: set[str] = set()
        # Inline custom-genre buffer — persisted to state so a full _render_body
        # (fired on every chip tap) doesn't wipe half-typed text, the same way
        # edit_country already does via on_change.
        self.edit_custom = ""

        # ── sync state ──
        self.syncing = False
        self.cancel_event: asyncio.Event | None = None
        self.s_cur = self.s_total = self.s_ok = self.s_low = self.s_gap = 0
        self.s_name = ""

        # ── holders ──
        self._h_overview = ft.Container()
        self._h_controls = ft.Container()
        self._h_batch = ft.Container()
        self._h_body = ft.Container(expand=True)
        self._search_field = ft.TextField(
            hint_text="Search artists…", prefix_icon=ft.Icons.SEARCH,
            bgcolor=SURFACE2, border_color=BORDER, focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13), dense=True,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            on_change=self._on_search,
        )

        self.content = ft.Column(
            [self._h_overview, self._h_controls, self._h_batch, self._h_body],
            expand=True, spacing=14,
        )
        self._reload()

    # ── shared button styling — pronounced, filled, generous hit area ─────────
    def _filled_btn(self, text, on_click, *, bg=CYAN, fg=BG, icon=None, expand=False, disabled=False):
        row = []
        if icon:
            row.append(ft.Icon(icon, size=17, color=fg))
        row.append(ft.Text(text, weight="bold", size=13, color=fg))
        return ft.Container(
            content=ft.Row(row, spacing=8, tight=True, alignment="center"),
            bgcolor=apply_opacity(0.4, bg) if disabled else bg,
            border_radius=10, padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            on_click=None if disabled else on_click, ink=not disabled,
            alignment=ft.Alignment(0, 0), expand=expand,
        )

    def _ghost_btn(self, text, on_click, *, fg=DIM, icon=None):
        row = []
        if icon:
            row.append(ft.Icon(icon, size=16, color=fg))
        row.append(ft.Text(text, weight="bold", size=12.5, color=fg))
        return ft.Container(
            content=ft.Row(row, spacing=7, tight=True, alignment="center"),
            bgcolor="transparent", border_radius=10, border=ft.Border.all(1, BORDER),
            padding=ft.Padding.symmetric(horizontal=14, vertical=11),
            on_click=on_click, ink=True, alignment=ft.Alignment(0, 0),
        )

    def _section_label(self, text: str, trailing: ft.Control | None = None) -> ft.Control:
        left = ft.Text(text, size=11, weight=ft.FontWeight.W_800, color=CYAN)
        if trailing is None:
            return ft.Container(content=left, padding=ft.Padding.only(bottom=2))
        return ft.Row([left, ft.Container(expand=True), trailing], vertical_alignment="center")

    # ── data load ─────────────────────────────────────────────────────────────
    async def _reload_async(self):
        try:
            self.coverage = await self.db.get_metadata_coverage()
            self.all_gaps = await self.db.get_metadata_gap_artists(limit=1000)
            self.low_conf = await self.db.get_low_confidence_artists(limit=500)
            self.vocab = await self.db.get_genre_vocabulary(limit=48)
        except Exception as exc:
            logger.exception("Metadata workbench load failed: %s", exc)
        self._render()

    def _reload(self):
        self.app.page.run_task(self._reload_async)

    async def _source_tags(self, artist: str) -> list[str]:
        if artist not in self.source_cache:
            try:
                self.source_cache[artist] = await self.db.get_artist_source_genres(artist)
            except Exception:
                self.source_cache[artist] = []
        return self.source_cache[artist]

    # ── render ────────────────────────────────────────────────────────────────
    def _render(self):
        self._render_overview()
        self._render_controls()
        self._render_batch()
        self._render_body()
        self.app.page.update()

    # ── OVERVIEW: coverage + sync ─────────────────────────────────────────────
    def _render_overview(self):
        cov = self.coverage
        pct = int(round(100 * cov.get("genre_pct", 0.0)))
        tracks = cov.get("tracks", 0)
        with_g = cov.get("tracks_with_genres", 0)
        crit = cov.get("critical", 0)
        gaps = cov.get("gap_artists", 0)

        badge_text = "NO DATA" if tracks == 0 else ("WALK READY" if pct >= 80 else "NEEDS TAGS")
        badge_bg = DIM if tracks == 0 else (CYAN if pct >= 80 else ACCENT_AMBER)

        # Track-level three-way partition (green + amber + red == tracks), so the
        # bar is honest about proportions.
        g_green = cov.get("tracks_with_genres", 0)
        g_red = cov.get("tracks_critical", 0)
        g_amber = cov.get("tracks_partial", max(0, tracks - g_green - g_red))
        bar = ft.Container(
            content=ft.Row([
                ft.Container(expand=max(1, g_green), height=10, bgcolor=ACCENT_GREEN),
                ft.Container(expand=max(1, g_amber), height=10, bgcolor=ACCENT_AMBER),
                ft.Container(expand=max(1, g_red), height=10, bgcolor=ACCENT_RED),
            ], spacing=2) if tracks > 0 else ft.Container(expand=True, height=10, bgcolor=SURFACE2),
            border_radius=6, clip_behavior=ft.ClipBehavior.HARD_EDGE,
            margin=ft.Margin.only(top=8, bottom=8),
        )

        coverage_card = ft.Container(
            content=ft.Row([
                ft.Column([
                    ft.Text(f"{pct}%", size=32, weight=ft.FontWeight.W_800, color=CYAN if tracks > 0 else DIM),
                    ft.Container(
                        content=ft.Text(badge_text, size=9, weight="bold", color=BG),
                        bgcolor=badge_bg, border_radius=4,
                        padding=ft.Padding.symmetric(horizontal=5, vertical=1),
                    ),
                ], spacing=2, horizontal_alignment="center", tight=True),
                ft.Container(width=14),
                ft.Column([
                    ft.Text(f"{with_g:,} of {tracks:,} tracks have usable tags" if tracks > 0 else "No tracks indexed in library",
                            size=13, color=TEXT, weight="bold"),
                    bar,
                    ft.Text(f"{gaps} artists need attention · {crit} critical" if tracks > 0 else "Index your music library to begin metadata curation",
                            size=11.5, color=DIM),
                ], spacing=0, expand=True),
            ], vertical_alignment="center"),
            bgcolor=apply_opacity(0.12, SURFACE2), border_radius=14, padding=14,
            border=ft.Border.all(1, apply_opacity(0.4, CYAN if tracks > 0 else BORDER)),
        )

        if self.syncing:
            action = self._sync_banner()
        else:
            action = ft.Row([
                self._filled_btn("Sync from MusicBrainz", lambda _e: self._start_sync(),
                                 icon=ft.Icons.CLOUD_SYNC_ROUNDED, expand=True, disabled=(tracks == 0)),
            ])

        refresh_btn = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.REFRESH_ROUNDED, size=13, color=CYAN),
                ft.Text("Reload", size=11, color=CYAN, weight="bold"),
            ], spacing=4, tight=True, vertical_alignment="center"),
            bgcolor=apply_opacity(0.12, CYAN),
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            border=ft.Border.all(1, apply_opacity(0.4, CYAN)),
            on_click=lambda _e: self._reload(),
            ink=True,
            tooltip="Reload metadata from database",
        )

        self._h_overview.content = ft.Column([
            self._section_label("OVERVIEW", trailing=refresh_btn),
            coverage_card,
            action,
        ], spacing=10)

    def _sync_banner(self) -> ft.Control:
        pct = (self.s_cur / self.s_total) if self.s_total else None
        self._sync_bar = ft.ProgressBar(value=pct, color=CYAN, bgcolor=SURFACE2, height=6)
        self._sync_label = ft.Text(
            f"Syncing {self.s_cur}/{self.s_total} · {self.s_name}"[:60],
            size=12, color=TEXT, weight="bold", overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
        )
        self._sync_counts = ft.Row([
            ft.Text(f"✓ {self.s_ok}", size=12, color=ACCENT_GREEN, weight="bold"),
            ft.Text(f"⚠ {self.s_low}", size=12, color=ACCENT_AMBER, weight="bold"),
            ft.Text(f"✗ {self.s_gap}", size=12, color=ACCENT_RED, weight="bold"),
        ], spacing=16)
        return ft.Container(
            content=ft.Column([
                self._sync_label,
                self._sync_bar,
                ft.Row([
                    self._sync_counts,
                    ft.Container(expand=True),
                    self._ghost_btn("Stop", lambda _e: self._cancel_sync(), fg=ACCENT_RED),
                ], vertical_alignment="center"),
            ], spacing=8),
            bgcolor=SURFACE2, border_radius=12, padding=12,
            border=ft.Border.all(1, apply_opacity(0.4, CYAN)),
        )

    # ── SYNC ──────────────────────────────────────────────────────────────────
    def _start_sync(self):
        if self.syncing:
            return
        self.syncing = True
        self.cancel_event = asyncio.Event()
        self.s_cur = self.s_total = self.s_ok = self.s_low = self.s_gap = 0
        self.s_name = "Starting…"
        self._render_overview()
        self.app.page.update()
        self.app.page.run_task(self._do_sync)

    async def _do_sync(self):
        def cb(i, total, name, res):
            self.s_cur, self.s_total, self.s_name = i, total, name
            st = res.get("status")
            if st == "ok":
                self.s_ok += 1
            elif st == "lowconfidence":
                self.s_low += 1
            else:
                self.s_gap += 1
            self._update_sync_banner()

        try:
            await enrich_library(
                self.db, with_genres=True, include_failed=True,
                progress=cb, cancel_event=self.cancel_event,
            )
        except Exception as exc:
            logger.exception("Workbench sync failed: %s", exc)
            self.app.show_snackbar(f"Sync failed: {exc}", color=ACCENT_RED)
        finally:
            self.syncing = False
            await self._reload_async()
            self.app.show_snackbar(
                f"Sync done · {self.s_ok} matched, {self.s_low} uncertain, {self.s_gap} still blank",
                icon=ft.Icons.CHECK_CIRCLE, color=CYAN,
            )

    def _update_sync_banner(self):
        if not self.syncing:
            return
        bar = getattr(self, "_sync_bar", None)
        if bar is None:
            return
        bar.value = (self.s_cur / self.s_total) if self.s_total else None
        self._sync_label.value = f"Syncing {self.s_cur}/{self.s_total} · {self.s_name}"[:60]
        self._sync_counts.controls[0].value = f"✓ {self.s_ok}"
        self._sync_counts.controls[1].value = f"⚠ {self.s_low}"
        self._sync_counts.controls[2].value = f"✗ {self.s_gap}"
        try:
            self.app.page.update()
        except Exception:
            pass

    def _cancel_sync(self):
        if self.cancel_event:
            self.cancel_event.set()

    # ── CONTROLS: filters + search ────────────────────────────────────────────
    def _render_controls(self):
        counts = {
            "critical": sum(1 for g in self.all_gaps if g.get("gap_severity") == 0),
            "no_tags": sum(1 for g in self.all_gaps if g.get("gap_severity") in (0, 1)),
            "uncertain": len(self.low_conf),
            "all": len(self.all_gaps),
        }
        # Wrap rather than horizontal-scroll: Flet renders a horizontal
        # scrollbar UNDER the row (and can't place it on top), which crowded the
        # search field. Wrapping keeps every category visible with no scrollbar.
        pills = ft.Row([
            self._filter_pill("critical", "Critical", counts["critical"]),
            self._filter_pill("no_tags", "No tags", counts["no_tags"]),
            self._filter_pill("uncertain", "Uncertain", counts["uncertain"]),
            self._filter_pill("all", "All", counts["all"]),
        ], spacing=8, run_spacing=8, wrap=True)

        self._h_controls.content = ft.Column([
            self._section_label("ARTISTS"),
            pills,
            self._search_field,
        ], spacing=10)

    def _filter_pill(self, key: str, label: str, count: int) -> ft.Control:
        active = self.filter == key
        bg_col = CYAN if active else "transparent"
        fg_col = BG if active else DIM
        badge_bg = apply_opacity(0.25, BG) if active else apply_opacity(0.12, TEXT)
        badge_fg = BG if active else DIM

        return ft.Container(
            content=ft.Row([
                ft.Text(label, size=12.5, color=fg_col, weight="bold" if active else None),
                ft.Container(
                    content=ft.Text(str(count), size=11, color=badge_fg, font_family="monospace", weight="bold"),
                    bgcolor=badge_bg,
                    border_radius=6, padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                ),
            ], spacing=7, tight=True, vertical_alignment="center"),
            bgcolor=bg_col,
            border=ft.Border.all(1, CYAN if active else BORDER),
            border_radius=10, padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            on_click=lambda _e, k=key: self._set_filter(k), ink=True,
        )

    # ── BATCH bar ──────────────────────────────────────────────────────────────
    def _render_batch(self):
        if not self.selected or self.filter == "uncertain":
            self._h_batch.content = None
            self._h_batch.visible = False
            return
        self._h_batch.visible = True
        self._h_batch.content = ft.Container(
            content=ft.Row([
                ft.Text(f"{len(self.selected)} selected", size=12.5, color=CYAN, weight="bold"),
                ft.TextButton("Clear", on_click=lambda _e: self._clear_selection(),
                              style=ft.ButtonStyle(color=DIM)),
                ft.Container(expand=True),
                self._filled_btn("Tag All →", lambda _e: self._open_batch_editor()),
            ], vertical_alignment="center"),
            bgcolor=apply_opacity(0.10, CYAN), border_radius=12,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border=ft.Border.all(1, apply_opacity(0.4, CYAN)),
        )

    # ── BODY ────────────────────────────────────────────────────────────────────
    def _render_body(self):
        if self.filter == "uncertain":
            self._render_review_body()
            return

        gaps = self._visible_gaps()
        if not gaps:
            if self.coverage.get("tracks", 0) == 0:
                self._h_body.content = self._empty_state(
                    "Your library is empty.",
                    "Index your music files or download tracks to populate artists and metadata tags.",
                    icon=ft.Icons.LIBRARY_MUSIC_ROUNDED,
                )
            else:
                self._h_body.content = self._empty_state(
                    "Nothing to fix in this view.",
                    "Every artist here has the tags Auto-Play needs.",
                    icon=ft.Icons.CHECK_CIRCLE_ROUNDED,
                )
            return

        groups: dict[str, list[dict]] = {}
        for g in gaps:
            groups.setdefault(g.get("country") or "", []).append(g)
        ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "", -len(kv[1]), kv[0]))

        controls: list[ft.Control] = []
        for country, items in ordered:
            controls.append(self._scene_header(country, len(items)))
            for g in items:
                controls.append(self._gap_row(g))
                if self.expanded == g["artist_name"]:
                    controls.append(self._tag_editor(g, is_review=False))
        self._h_body.content = ft.ListView(controls=controls, expand=True, spacing=0,
                                            build_controls_on_demand=True,
                                            # Android needs explicit scroll for a
                                            # visible scrollbar (see library.py).
                                            scroll=ft.ScrollMode.ALWAYS)

    def _render_review_body(self):
        q = self.search.strip().lower()
        items = [x for x in self.low_conf if not q or q in x["artist_name"].lower()]
        if not items:
            if self.coverage.get("tracks", 0) == 0:
                self._h_body.content = self._empty_state(
                    "Your library is empty.",
                    "Index your music files or download tracks to populate artists and metadata tags.",
                    icon=ft.Icons.LIBRARY_MUSIC_ROUNDED,
                )
            else:
                self._h_body.content = self._empty_state(
                    "No uncertain matches.",
                    "Run a sync, or everything MusicBrainz returned is already resolved.",
                    icon=ft.Icons.CHECK_CIRCLE_ROUNDED,
                )
            return
        controls: list[ft.Control] = [self._section_hint(
            "Matches MusicBrainz wasn't sure about. Accept to keep, reject to blank, "
            "or search for the right act.")]
        for it in items:
            controls.append(self._review_row(it))
            if self.expanded == it["artist_name"]:
                controls.append(self._tag_editor(it, is_review=True))
        self._h_body.content = ft.ListView(controls=controls, expand=True, spacing=0,
                                            build_controls_on_demand=True,
                                            # Android needs explicit scroll for a
                                            # visible scrollbar (see library.py).
                                            scroll=ft.ScrollMode.ALWAYS)

    def _empty_state(self, title: str, sub: str, icon=ft.Icons.CHECK_CIRCLE_ROUNDED) -> ft.Control:
        icon_color = ACCENT_GREEN if icon == ft.Icons.CHECK_CIRCLE_ROUNDED else DIM
        return ft.Column([
            ft.Icon(icon, color=icon_color, size=40),
            ft.Text(title, color=TEXT, size=15, weight="bold"),
            ft.Text(sub, color=DIM, size=11.5, text_align=ft.TextAlign.CENTER),
        ], horizontal_alignment="center", alignment=ft.MainAxisAlignment.CENTER,
            expand=True, spacing=8)

    def _section_hint(self, text: str) -> ft.Control:
        return ft.Container(
            content=ft.Text(text, size=11.5, color=DIM),
            padding=ft.Padding.only(bottom=8, top=2),
        )

    def _scene_header(self, country: str, n: int) -> ft.Control:
        name = _COUNTRY_NAMES.get(country, country) if country else "Unknown country"
        badge = ft.Text(_flag(country), size=13) if country else ft.Text("?", size=12, color=DIM, weight="bold")
        return ft.Container(
            content=ft.Row([
                badge,
                ft.Text(name.upper(), size=11, weight=ft.FontWeight.W_800, color=DIM),
                ft.Container(height=1, bgcolor=BORDER, expand=True),
                ft.Text(str(n), size=11, color=DIM, font_family="monospace"),
            ], vertical_alignment="center", spacing=8),
            padding=ft.Padding.only(top=14, bottom=6),
        )

    def _sev_badge(self, sev: int) -> ft.Control:
        if sev == 0:
            label, bg, fg = "CRITICAL", apply_opacity(0.18, ACCENT_RED), ACCENT_RED
        elif sev == 1:
            label, bg, fg = "NO TAGS", apply_opacity(0.18, ACCENT_AMBER), ACCENT_AMBER
        else:
            label, bg, fg = "PARTIAL", apply_opacity(0.14, ACCENT_AMBER), DIM
        return ft.Container(
            content=ft.Text(label, size=9.5, weight="bold", color=fg),
            bgcolor=bg, border_radius=6,
            padding=ft.Padding.symmetric(horizontal=7, vertical=3),
            border=ft.Border.all(1, apply_opacity(0.4, fg)),
            tooltip="No Tags · No Country" if sev == 0 else "Incomplete",
        )

    def _sev_dot(self, sev: int) -> ft.Control:
        return self._sev_badge(sev)

    # ── gap row + editor ──────────────────────────────────────────────────────
    def _visible_gaps(self) -> list[dict]:
        q = self.search.strip().lower()
        out = []
        for g in self.all_gaps:
            sev = g.get("gap_severity", 2)
            if self.filter == "critical" and sev != 0:
                continue
            if self.filter == "no_tags" and sev not in (0, 1):
                continue
            if q and q not in g["artist_name"].lower():
                continue
            out.append(g)
        return out

    def _gap_row(self, g: dict) -> ft.Control:
        name = g["artist_name"]
        sev = g.get("gap_severity", 2)
        tc = g.get("track_count", 0)
        genres = [x.get("name") if isinstance(x, dict) else str(x) for x in (g.get("genres") or [])]
        country = g.get("country")

        if not genres and not country:
            summary = ft.Text("No Tags · No Country", size=11, color=ACCENT_RED)
        elif not genres:
            summary = ft.Text("No Tags", size=11, color=ACCENT_RED)
        elif not country:
            summary = ft.Text(f"{', '.join(genres[:3])} · No Country", size=11, color=DIM,
                              overflow=ft.TextOverflow.ELLIPSIS, max_lines=1)
        else:
            summary = ft.Text(", ".join(genres[:3]), size=11, color=DIM,
                              overflow=ft.TextOverflow.ELLIPSIS, max_lines=1)

        is_expanded = self.expanded == name
        checkbox = ft.Checkbox(value=name in self.selected, fill_color=CYAN,
                               on_change=lambda e, n=name: self._toggle_select(n, e.control.value))
        return ft.Container(
            content=ft.Row([
                checkbox,
                ft.Column([
                    ft.Row([
                        ft.Text(_flag(country), size=14) if country else ft.Container(),
                        ft.Text(name, size=14, weight="bold",
                                color=CYAN if is_expanded else TEXT,
                                overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
                    ], spacing=6, vertical_alignment="center"),
                    summary,
                ], spacing=2, expand=True, tight=True),
                ft.Container(
                    content=ft.Text(f"{tc} Track{'s' if tc != 1 else ''}", size=10.5, color=DIM, font_family="monospace", weight="bold"),
                    bgcolor=SURFACE, border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                    border=ft.Border.all(1, BORDER),
                ),
            ], vertical_alignment="center", spacing=11),
            padding=ft.Padding.symmetric(horizontal=10, vertical=10),
            margin=ft.Margin.only(bottom=4),
            bgcolor=apply_opacity(0.65, SURFACE2) if is_expanded else SURFACE2,
            border_radius=10,
            border=ft.Border.all(1, apply_opacity(0.65, CYAN) if is_expanded else apply_opacity(0.5, BORDER)),
            on_click=lambda _e, n=name: self._toggle_expand(n), ink=True,
        )

    def _chip(self, label: str, *, kind: str, on_click) -> ft.Control:
        if kind == "on":                    # currently selected (tap to remove)
            bg, fg, border, txt = CYAN, BG, CYAN, f"{label}  ✕"
        elif kind == "sug":                 # from the artist's own files
            bg, fg, border, txt = "transparent", ACCENT_GREEN, apply_opacity(0.45, ACCENT_GREEN), f"+ {label}"
        elif kind == "mb":                  # from a MusicBrainz lookup
            bg, fg, border, txt = "transparent", CYAN, apply_opacity(0.5, CYAN), f"+ {label}"
        else:                               # kind == "ghost" — library vocabulary
            bg, fg, border, txt = "transparent", DIM, BORDER, label
        return ft.Container(
            content=ft.Text(txt, size=12, color=fg, weight="bold" if kind == "on" else None),
            bgcolor=bg, border_radius=14, border=ft.Border.all(1, border),
            padding=ft.Padding.symmetric(horizontal=11, vertical=6),
            on_click=on_click, ink=True,
        )

    def _on_country_change(self, val: str, flag_widget: ft.Text):
        self.edit_country = (val or "").strip().upper()
        if flag_widget:
            flag_widget.value = _flag(self.edit_country)
        self.app.page.update()

    def _tag_editor(self, item: dict, *, is_review: bool) -> ft.Control:
        """Inline hand-tagging editor for both gap artists and low-confidence
        review. Tag chips come from three inline sources — current selection, the
        artist's own file tags, and the library vocabulary."""
        name = item["artist_name"]
        source = self.source_cache.get(name, [])
        src_lc = {s.lower() for s in source}

        selected_chips = [self._chip(t, kind="on", on_click=lambda _e, t=t: self._edit_toggle_genre(t))
                          for t in sorted(self.edit_genres)]
        src_chips = [self._chip(t, kind="sug", on_click=lambda _e, t=t: self._edit_add_genre(t))
                     for t in source if t.lower() not in self.edit_genres]
        vocab_chips = [self._chip(v["name"], kind="ghost", on_click=lambda _e, t=v["name"]: self._edit_add_genre(t))
                       for v in self.vocab
                       if v["name"].lower() not in self.edit_genres
                       and v["name"].lower() not in src_lc][:10]

        custom = ft.TextField(value=self.edit_custom, hint_text="Add a genre…", dense=True, width=150, bgcolor=SURFACE2,
                              border_color=BORDER, focused_border_color=CYAN,
                              text_style=ft.TextStyle(color=TEXT, size=12),
                              content_padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                              on_change=lambda e: setattr(self, "edit_custom", e.control.value or ""),
                              on_submit=self._edit_add_custom)

        country_flag_preview = ft.Text(_flag(self.edit_country), size=18)
        country_field = ft.TextField(
            value=self.edit_country, width=100, dense=True, hint_text="ISO",
            bgcolor=SURFACE2, border_color=BORDER, focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13, weight="bold"),
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            on_change=lambda e: self._on_country_change(e.control.value, country_flag_preview),
        )

        blocks: list[ft.Control] = []
        if src_chips:
            blocks.append(ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.FOLDER_ROUNDED, size=14, color=ACCENT_GREEN),
                        ft.Text("FROM THIS ARTIST'S FILES", size=10, color=ACCENT_GREEN, weight="bold"),
                        ft.Container(expand=True),
                        ft.Text("Tap to add", size=10, color=DIM),
                    ], spacing=6, vertical_alignment="center"),
                    ft.Row(src_chips, wrap=True, spacing=6, run_spacing=6),
                ], spacing=8),
                bgcolor=SURFACE2, border_radius=10, padding=10,
                border=ft.Border.all(1, apply_opacity(0.35, ACCENT_GREEN)),
            ))

        blocks.append(ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.LABEL_ROUNDED, size=14, color=CYAN),
                    ft.Text("SELECTED GENRES", size=10, color=CYAN, weight="bold"),
                ], spacing=6, vertical_alignment="center"),
                ft.Row((selected_chips + [custom]) if selected_chips else [custom],
                       wrap=True, spacing=6, run_spacing=6),
            ], spacing=8),
            bgcolor=SURFACE2, border_radius=10, padding=10,
            border=ft.Border.all(1, apply_opacity(0.35, CYAN)),
        ))

        if vocab_chips:
            blocks.append(ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.BOOKMARKS_ROUNDED, size=14, color=DIM),
                        ft.Text("USED ELSEWHERE IN YOUR LIBRARY", size=10, color=DIM, weight="bold"),
                    ], spacing=6, vertical_alignment="center"),
                    ft.Row(vocab_chips, wrap=True, spacing=6, run_spacing=6),
                ], spacing=8),
                bgcolor=SURFACE2, border_radius=10, padding=10,
                border=ft.Border.all(1, BORDER),
            ))

        blocks.append(ft.Container(
            content=ft.Row([
                ft.Row([
                    ft.Icon(ft.Icons.PUBLIC_ROUNDED, size=14, color=CYAN),
                    ft.Text("COUNTRY", size=10, color=CYAN, weight="bold"),
                ], spacing=6, vertical_alignment="center"),
                ft.Container(expand=True),
                country_flag_preview,
                country_field,
            ], spacing=10, vertical_alignment="center"),
            bgcolor=SURFACE2, border_radius=10, padding=10,
            border=ft.Border.all(1, BORDER),
        ))

        blocks.append(self._ghost_btn(
            "Search MusicBrainz", lambda _e, n=name: self._open_mb_dialog(n),
            fg=CYAN, icon=ft.Icons.TRAVEL_EXPLORE_ROUNDED,
        ))

        actions: list[ft.Control] = []
        if is_review:
            actions.append(self._ghost_btn("Reject", lambda _e, n=name: self._review_reject(n),
                                           fg=ACCENT_RED, icon=ft.Icons.CLOSE_ROUNDED))
        actions += [
            ft.Container(expand=True),
            self._ghost_btn("Cancel", lambda _e: self._collapse()),
            self._filled_btn("Save", lambda _e, n=name: self._save_one(n), icon=ft.Icons.CHECK_ROUNDED),
        ]
        blocks += [
            ft.Row(actions, vertical_alignment="center", spacing=10),
            ft.Text("Saved as your override — kept safe from future syncs.", size=10, color=DIM, italic=True),
        ]
        return ft.Container(
            content=ft.Column(blocks, spacing=11, tight=True),
            bgcolor=SURFACE, border_radius=12, padding=14,
            margin=ft.Margin.only(top=4, bottom=10),
            border=ft.Border.all(1, apply_opacity(0.3, CYAN)),
        )

    # ── MusicBrainz identity dialog ────────────────────────────────────────────
    def _open_mb_dialog(self, artist: str):
        """Roomy MusicBrainz identity picker — a dialog, not the cramped inline
        row. Lists candidate entities with the detail you actually choose by
        (disambiguation, country, genres, match score), and on selection does the
        per-MBID detail fetch the shallow search omits — so a picked match lands
        with real genres instead of the blanks that made a pick look like a no-op."""
        # Size to the viewport so the dialog itself can't overflow a narrow
        # portrait screen — the constraint that pushed MB out of the inline row.
        pw = self.app.page.width or 400
        ph = self.app.page.height or 720
        dlg_w = max(280, min(420, pw - 40))
        res_h = max(200, min(380, ph - 280))

        query_field = ft.TextField(
            value=artist, label="Artist Name", dense=True,
            border_color=BORDER, focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13), bgcolor=SURFACE2,
        )
        results = ft.Column([], scroll=ft.ScrollMode.AUTO, height=res_h, spacing=8)

        def _search(_e=None):
            q = (query_field.value or artist).strip() or artist

            async def _run():
                results.controls = [ft.Row(
                    [ft.ProgressRing(width=16, height=16, stroke_width=2),
                     ft.Text("Searching MusicBrainz…", size=12, color=DIM)], spacing=8)]
                self.app.page.update()
                try:
                    cands = await search_musicbrainz_artists_candidates(q)
                except Exception as exc:
                    logger.warning("MB search failed for %s: %s", q, exc)
                    results.controls = [ft.Text("Lookup failed — offline?", size=12, color=ACCENT_AMBER)]
                    self.app.page.update()
                    return
                cands = sorted(cands, key=lambda c: (bool(c.get("is_junk")), -(c.get("score") or 0)))[:8]
                if not cands:
                    results.controls = [ft.Text("No candidates found. Try a different spelling.",
                                                size=12, color=DIM)]
                else:
                    results.controls = [self._mb_dialog_card(artist, c, dlg) for c in cands]
                self.app.page.update()

            self.app.page.run_task(_run)

        query_field.on_submit = _search
        dlg = ft.AlertDialog(
            bgcolor=SURFACE,
            title=ft.Text(f"MusicBrainz · {artist}"[:40], color=TEXT, size=15, weight="bold"),
            content=ft.Column([
                ft.Text("Pick the correct artist — its country and genres are fetched "
                        "and saved as a confirmed match.", color=DIM, size=11),
                query_field,
                results,
            ], tight=True, spacing=10, width=dlg_w),
            actions=[
                ft.TextButton("Close", on_click=lambda _e: self.app.page.pop_dialog() if self.app.page else None),
                self._filled_btn("Search", lambda _e: _search()),
            ],
        )
        if self.app.page:
            self.app.page.show_dialog(dlg)
        _search()

    def _mb_dialog_card(self, artist: str, cand: dict, dlg) -> ft.Control:
        """One candidate row in the MB dialog — full detail, room to breathe."""
        cname = cand.get("name") or artist
        disamb = (cand.get("disambiguation") or "").strip()
        cc = cand.get("country")
        genres = [g.get("name") if isinstance(g, dict) else str(g) for g in (cand.get("genres") or [])]
        gstr = ", ".join(g for g in genres if g)
        score = cand.get("score") or 0

        score_color = ACCENT_GREEN if score >= 80 else (ACCENT_AMBER if score >= 50 else ACCENT_RED)
        head = []
        if cc:
            head.append(ft.Text(_flag(cc), size=16))
        head.append(ft.Text(cname, size=13.5, color=TEXT, weight="bold",
                            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True))
        head.append(ft.Container(
            content=ft.Text(f"{score}% match", size=10, color=score_color, weight="bold"),
            bgcolor=apply_opacity(0.14, score_color), border_radius=6,
            padding=ft.Padding.symmetric(horizontal=7, vertical=3),
            border=ft.Border.all(1, apply_opacity(0.35, score_color)),
        ))
        lines: list[ft.Control] = [ft.Row(head, spacing=6, vertical_alignment="center")]
        if disamb:
            lines.append(ft.Text(f"• {disamb}", size=11, color=TEXT, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        lines.append(ft.Text(gstr or "No genres listed — fetched when you select",
                             size=10.5, color=DIM if gstr else ACCENT_AMBER,
                             max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        lines.append(ft.Row([
            ft.Container(expand=True),
            self._filled_btn("Use This Match", lambda _e, c=cand: self._use_mb_candidate(artist, c, dlg),
                             icon=ft.Icons.CHECK_ROUNDED),
        ]))
        return ft.Container(
            content=ft.Column(lines, spacing=6, tight=True),
            bgcolor=SURFACE2, border_radius=10, padding=12,
            border=ft.Border.all(1, apply_opacity(0.5, BORDER)),
        )

    def _use_mb_candidate(self, artist: str, cand: dict, dlg=None):
        """Confirm a chosen MusicBrainz candidate. Fetches full genres/country by
        MBID first (the search payload omits them) so the artist actually
        resolves, then writes it as a confirmed source='musicbrainz' match."""
        async def _do():
            mbid = cand.get("mbid")
            country, area, genres = cand.get("country"), cand.get("area"), cand.get("genres")
            if mbid:
                details = await musicbrainz_artist_details(mbid)
                if details.get("genres"):
                    genres = details["genres"]
                country = details.get("country") or country
                area = details.get("area") or area
            try:
                await self.db.confirm_artist_match(
                    artist, mbid=mbid, country=country, area=area,
                    genres=genres, status="ok", score=cand.get("score") or 100,
                )
                try:
                    await self.db.fix_and_normalize_track_genres()
                    from utils.track_graph import build_genre_affinity
                    await build_genre_affinity(self.db)
                except Exception:
                    pass
                got = [g.get("name") if isinstance(g, dict) else str(g) for g in (genres or [])]
                if got:
                    self.app.show_snackbar(f"Matched {artist} · {', '.join(got[:3])}",
                                           icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
                else:
                    self.app.show_snackbar(
                        f"Matched {artist}, but MusicBrainz listed no genres — add some by hand.",
                        color=ACCENT_AMBER)
            except Exception as exc:
                logger.exception("use_mb_candidate failed: %s", exc)
                self.app.show_snackbar(f"Match failed: {exc}", color=ACCENT_RED)
            if dlg is not None and self.app.page:
                try:
                    self.app.page.pop_dialog()
                except Exception:
                    pass
            self.expanded = None
            self.selected.discard(artist)
            self.source_cache.pop(artist, None)
            await self._reload_async()
        self.app.page.run_task(_do)

    # ── review row + editor (low-confidence) ──────────────────────────────────
    def _review_row(self, it: dict) -> ft.Control:
        name = it["artist_name"]
        tc = it.get("track_count", 0)
        score = it.get("score", 0)
        country = it.get("country")
        genres = [x.get("name") if isinstance(x, dict) else str(x) for x in (it.get("genres") or [])]
        gstr = ", ".join(genres[:3]) or "No Tags"
        is_expanded = self.expanded == name

        score_color = ACCENT_GREEN if score >= 80 else (ACCENT_AMBER if score >= 50 else ACCENT_RED)
        return ft.Container(
            content=ft.Row([
                ft.Column([
                    ft.Row([
                        ft.Text(_flag(country), size=14) if country else ft.Container(),
                        ft.Text(name, size=14, weight="bold", color=CYAN if is_expanded else TEXT,
                                overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
                    ], spacing=6, vertical_alignment="center"),
                    ft.Text(f"{_COUNTRY_NAMES.get(country, country) if country else 'No Country'} · {gstr}", size=11, color=DIM,
                            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                ], spacing=2, expand=True, tight=True),
                ft.Container(
                    content=ft.Text(f"{score}%", size=10.5, color=score_color, weight="bold"),
                    bgcolor=apply_opacity(0.14, score_color), border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=7, vertical=3),
                    border=ft.Border.all(1, apply_opacity(0.3, score_color)),
                ),
                ft.Container(
                    content=ft.Text(f"{tc} Track{'s' if tc != 1 else ''}", size=10.5, color=DIM, font_family="monospace", weight="bold"),
                    bgcolor=SURFACE, border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                    border=ft.Border.all(1, BORDER),
                ),
                # One-tap accept of the match as-is — the wizard's primary review
                # action. Keeps mbid/country/genres as source='musicbrainz' via
                # confirm_artist_match (expand only to reject, rebind, or edit).
                ft.Container(
                    content=ft.Icon(ft.Icons.CHECK_ROUNDED, size=18, color=BG),
                    bgcolor=ACCENT_GREEN, border_radius=8,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                    on_click=lambda _e, n=name: self._review_accept(n), ink=True,
                    tooltip="Accept this match",
                ),
            ], vertical_alignment="center", spacing=11),
            padding=ft.Padding.symmetric(horizontal=10, vertical=10),
            margin=ft.Margin.only(bottom=4),
            bgcolor=apply_opacity(0.65, SURFACE2) if is_expanded else SURFACE2,
            border_radius=10,
            border=ft.Border.all(1, apply_opacity(0.65, CYAN) if is_expanded else apply_opacity(0.5, BORDER)),
            on_click=lambda _e, n=name: self._toggle_expand(n), ink=True,
        )

    def _review_accept(self, name: str):
        """Confirm a low-confidence MusicBrainz match, keeping its
        source='musicbrainz' provenance. If the stored row has thin genres (the
        weak-fallback path stores an mbid but no tags), fetch the full entity by
        MBID first so accepting actually resolves the artist rather than
        confirming a blank."""
        it = next((x for x in self.low_conf if x["artist_name"] == name), None)
        if not it:
            return

        async def _do():
            mbid = it.get("mbid")
            country, area = it.get("country"), it.get("area")
            genres = it.get("genres") or []
            if mbid and not genres:
                details = await musicbrainz_artist_details(mbid)
                if details.get("genres"):
                    genres = details["genres"]
                country = country or details.get("country")
                area = area or details.get("area")
            try:
                await self.db.confirm_artist_match(
                    name, mbid=mbid, country=country, area=area,
                    genres=genres, status="ok", score=100,
                )
                try:
                    await self.db.fix_and_normalize_track_genres()
                    from utils.track_graph import build_genre_affinity
                    await build_genre_affinity(self.db)
                except Exception:
                    pass
                self.app.show_snackbar(f"Accepted {name}", icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
            except Exception as exc:
                logger.exception("review_accept failed: %s", exc)
                self.app.show_snackbar(f"Accept failed: {exc}", color=ACCENT_RED)
            self.expanded = None
            await self._reload_async()
        self.app.page.run_task(_do)

    def _review_reject(self, name: str):
        async def _do():
            await self.db.confirm_artist_match(name, status="notfound", score=0)
            self.app.show_snackbar(f"Rejected {name}", color=ACCENT_AMBER)
            self.expanded = None
            await self._reload_async()
        self.app.page.run_task(_do)

    # ── event handlers ────────────────────────────────────────────────────────
    def _on_search(self, e):
        self.search = e.control.value or ""
        self._render_body()
        self.app.page.update()

    def _set_filter(self, key: str):
        self.filter = key
        self.expanded = None
        self._render_controls()
        self._render_batch()
        self._render_body()
        self.app.page.update()

    def _toggle_select(self, name: str, on: bool):
        if on:
            self.selected.add(name)
        else:
            self.selected.discard(name)
        self._render_batch()
        self.app.page.update()

    def _clear_selection(self):
        self.selected.clear()
        self._render_batch()
        self._render_body()
        self.app.page.update()

    def _toggle_expand(self, name: str):
        if self.expanded == name:
            self._collapse()
            return
        self.expanded = name
        pool = self.low_conf if self.filter == "uncertain" else self.all_gaps
        g = next((x for x in pool if x["artist_name"] == name), None)
        genres = [x.get("name") if isinstance(x, dict) else str(x) for x in (g.get("genres") or [])] if g else []
        self.edit_genres = {t.lower() for t in genres if t}
        self.edit_country = (g.get("country") or "") if g else ""
        self.edit_custom = ""

        async def _prime():
            # Source-file tags are useful for review artists too (they're in the
            # library), so load them regardless of which filter we're under.
            await self._source_tags(name)
            self._render_body()
            self.app.page.update()
        self.app.page.run_task(_prime)

    def _collapse(self):
        self.expanded = None
        self._render_body()
        self.app.page.update()

    def _edit_toggle_genre(self, tok: str):
        low = tok.lower()
        self.edit_genres.discard(low) if low in self.edit_genres else self.edit_genres.add(low)
        self._render_body()
        self.app.page.update()

    def _edit_add_genre(self, tok: str):
        self.edit_genres.add(tok.lower())
        self._render_body()
        self.app.page.update()

    def _edit_add_custom(self, e):
        val = (e.control.value or "").strip().lower()
        if val:
            self.edit_genres.add(val)
        self.edit_custom = ""   # clear the buffer so it doesn't re-seed on render
        self._render_body()
        self.app.page.update()

    def _save_one(self, name: str):
        country = (self.edit_country or "").strip().upper() or None
        genres = sorted(self.edit_genres)

        async def _do():
            try:
                await self.db.set_manual_artist_enrichment(name, country=country, genres=genres)
                try:
                    await self.db.fix_and_normalize_track_genres()
                    from utils.track_graph import build_genre_affinity
                    await build_genre_affinity(self.db)
                except Exception:
                    pass
                self.app.show_snackbar(f"Saved tags for {name}", icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
            except Exception as exc:
                logger.exception("save_one failed: %s", exc)
                self.app.show_snackbar(f"Save failed: {exc}", color=ACCENT_RED)
            self.expanded = None
            self.selected.discard(name)
            self.source_cache.pop(name, None)
            await self._reload_async()

        self.app.page.run_task(_do)

    # ── batch editor ──────────────────────────────────────────────────────────
    def _open_batch_editor(self):
        names = sorted(self.selected)
        batch_genres: set[str] = set()
        country_field = ft.TextField(label="Country ISO", hint_text="e.g. GR", width=150, dense=True,
                                     border_color=BORDER, focused_border_color=CYAN,
                                     text_style=ft.TextStyle(color=TEXT), bgcolor=SURFACE2)
        chips_row = ft.Row(wrap=True, spacing=6, run_spacing=6)
        custom = ft.TextField(hint_text="Add a genre…", dense=True, width=170,
                              border_color=BORDER, focused_border_color=CYAN,
                              text_style=ft.TextStyle(color=TEXT, size=12), bgcolor=SURFACE2)

        def _rebuild():
            sel = [self._chip(t, kind="on", on_click=lambda _e, t=t: _toggle(t)) for t in sorted(batch_genres)]
            vocab = [self._chip(v["name"], kind="ghost", on_click=lambda _e, t=v["name"]: _add(t))
                     for v in self.vocab if v["name"].lower() not in batch_genres][:12]
            chips_row.controls = sel + [custom] + vocab
            self.app.page.update()

        def _toggle(t): batch_genres.discard(t.lower()); _rebuild()
        def _add(t): batch_genres.add(t.lower()); _rebuild()
        def _add_custom(e):
            v = (e.control.value or "").strip().lower()
            if v:
                batch_genres.add(v)
            e.control.value = ""
            _rebuild()
        custom.on_submit = _add_custom
        _rebuild()

        def _apply(_e):
            country = (country_field.value or "").strip().upper() or None
            genres = sorted(batch_genres)
            if not country and not genres:
                self.app.show_snackbar("Add a country or at least one genre first.", color=ACCENT_AMBER)
                return

            async def _do():
                n = await self.db.set_manual_artist_enrichment_bulk(names, country=country, genres=genres)
                try:
                    await self.db.fix_and_normalize_track_genres()
                except Exception:
                    pass
                if self.app.page:
                    self.app.page.pop_dialog()
                self.app.show_snackbar(f"Tagged {n} artists", icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
                self.selected.clear()
                await self._reload_async()
            self.app.page.run_task(_do)

        dlg = ft.AlertDialog(
            bgcolor=SURFACE,
            title=ft.Text(f"Tag {len(names)} Artists", color=TEXT, size=16, weight="bold"),
            content=ft.Column([
                ft.Text("Applies the same country and genres to every selected artist.", color=DIM, size=11),
                ft.Text(", ".join(names[:6]) + ("…" if len(names) > 6 else ""), color=DIM, size=11, italic=True),
                ft.Divider(color=BORDER, height=14),
                country_field,
                ft.Text("GENRES", size=10, color=DIM, weight="bold"),
                chips_row,
            ], tight=True, spacing=10, scroll=ft.ScrollMode.AUTO, width=360),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.app.page.pop_dialog() if self.app.page else None),
                self._filled_btn("Apply to All", _apply),
            ],
        )
        if self.app.page:
            self.app.page.show_dialog(dlg)
