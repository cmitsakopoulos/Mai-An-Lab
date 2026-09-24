"""Metadata Workbench — the sorting board for artist provenance.

Auto-Play reads two fields per artist: `country` and `genres`. `_pool_foreign`
uses them to decide what belongs in a queue, and `genre_graph.node_label` uses
them to place the artist in the journey graph. This is the surface that fills
them in.

── Why it is a board and not a list of problems ──────────────────────────────
The previous design asked "which columns are empty?" and presented the answer as
four severity filters (critical / no tags / uncertain / all). Two things were
wrong with that:

  • it nagged about artists that already work. Measured on the real library it
    flagged 70, of which 32 had a usable tag sitting in their own files and 27
    more had a country — leaving 11 that genuinely needed a person;
  • fixing them was one-at-a-time, through an inline expander that reflowed the
    whole list under your finger.

So the unit of work here is not "an artist with a problem", it is "a pile of
artists that belong to one scene". You select several and drop them on a genre,
because that is what the user actually knows: not this artist's tag list, but
that these twenty are all Greek rap. The bins are the library's own vocabulary,
which matters for correctness and not just typing — the NPMI model in
`genre_similarity` is learned from co-occurrence across THIS library's tags, so
a freshly invented spelling has no learned relation to anything.

── Flet 0.86 mechanics baked in ──────────────────────────────────────────────
  • The ListView is a DIRECT child of an expanding Column, never wrapped in an
    expand Container, and the pane is mounted with `_show_sub_page_full` so it
    is not nested inside the settings hub's scrolling Column. Both were
    violated before, which is what made scrolling fight back.
  • `Draggable`/`DragTarget` carry the payload on `e.src.data`, so nothing has
    to go through `page.get_control`.
  • Drag is an accelerator, never the only path: a horizontal drag inside a
    settings sub-page already means "back" (docs/FLET_FACTS.md §4), and touch
    gesture arbitration is unkind to drag-inside-scroll. Select-then-tap does
    the same job and is the primary route.
  • Row edits mutate one row and call `row.update()`; only structural changes
    re-render the list. The old code rebuilt every row on every chip tap.
"""

from __future__ import annotations

import asyncio
import logging
import flet as ft

from ui.tokens import (
    TEXT, DIM, TEXT_TERTIARY, BORDER, BORDER_SUBTLE, SURFACE, SURFACE2,
    SURFACE_ELEVATED, CYAN, BG, RADIUS_CARD, RADIUS_PILL,
    ACCENT_GREEN, ACCENT_AMBER, ACCENT_RED, apply_opacity,
)
from ui.widgets import CupertinoSegmentedBar
from utils.metadata_enrich import (
    enrich_library,
    refresh_walk_models,
    search_musicbrainz_artists_candidates,
    musicbrainz_artist_details,
)

logger = logging.getLogger(__name__)

_COUNTRY_NAMES = {
    "GR": "Greece", "US": "United States", "GB": "United Kingdom",
    "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain",
    "SE": "Sweden", "NO": "Norway", "FI": "Finland", "DK": "Denmark",
    "NL": "Netherlands", "BE": "Belgium", "IE": "Ireland", "PT": "Portugal",
    "CA": "Canada", "AU": "Australia", "NZ": "New Zealand", "JP": "Japan",
    "KR": "South Korea", "CN": "China", "TW": "Taiwan", "IN": "India",
    "BR": "Brazil", "AR": "Argentina", "MX": "Mexico", "CL": "Chile",
    "CO": "Colombia", "JM": "Jamaica", "CU": "Cuba", "NG": "Nigeria",
    "ZA": "South Africa", "GH": "Ghana", "KE": "Kenya", "ET": "Ethiopia",
    "MA": "Morocco", "SN": "Senegal", "EG": "Egypt", "IL": "Israel",
    "TR": "Türkiye", "RU": "Russia", "UA": "Ukraine", "PL": "Poland",
    "CZ": "Czechia", "HU": "Hungary", "RO": "Romania", "RS": "Serbia",
    "HR": "Croatia", "BG": "Bulgaria", "CH": "Switzerland", "AT": "Austria",
    "IS": "Iceland", "CV": "Cape Verde", "CY": "Cyprus", "AL": "Albania",
    "HK": "Hong Kong", "SG": "Singapore", "TH": "Thailand", "PH": "Philippines",
    "ID": "Indonesia", "MY": "Malaysia", "VN": "Vietnam", "PR": "Puerto Rico",
}

# Where a field came from, and how to show it. Provenance is surfaced because
# the cascade now fuses sources: MusicBrainz for country, the artist's own files
# or Deezer for genres when MusicBrainz has none. Without this the user cannot
# tell an authority answer from a supplement, or see which side of a
# disagreement to distrust.
_SOURCE_STYLE = {
    "musicbrainz": ("MB", CYAN),
    "files":       ("FILES", ACCENT_GREEN),
    "qobuz":       ("QOBUZ", "#FF9F0A"),
    "manual":      ("YOURS", ACCENT_AMBER),
}

_DRAG_GROUP = "mw_artist"

# Fixed column widths, kept here so the header and the rows can never drift
# apart. Everything else is proportional, so the two text columns get the whole
# remaining width instead of the scraps four fixed columns used to leave them.
_CHECK_W = 34
_TRACKS_W = 26


def _flag(cc: str | None) -> str:
    if not cc or len(cc) != 2 or not cc.isalpha():
        return "🌐"
    cc = cc.upper()
    return chr(0x1F1E6 + ord(cc[0]) - 65) + chr(0x1F1E6 + ord(cc[1]) - 65)


def _country_label(cc: str | None) -> str:
    if not cc:
        return "—"
    return _COUNTRY_NAMES.get(cc.upper(), cc.upper())


def _genre_names(items) -> list[str]:
    return [
        (x.get("name") if isinstance(x, dict) else str(x))
        for x in (items or [])
        if (x.get("name") if isinstance(x, dict) else str(x))
    ]


class MetadataWorkbenchPane(ft.Column):
    """Standing surface for artist-metadata sync, review, and bulk curation.

    A non-scrolling expanding Column: hero, filter, the list (which owns the
    only scroll), and the pinned bin tray."""

    def __init__(self, app, on_back=None, on_open_sync=None):
        super().__init__(expand=True, spacing=0)
        self.app = app
        self.db = app.db_manager
        self.on_back = on_back
        self.on_open_sync = on_open_sync  # signature compatibility; sync is inline

        # ── data ──
        self.coverage: dict = {}
        self.all_gaps: list[dict] = []
        self.low_conf: list[dict] = []
        self.vocab: list[dict] = []
        self.countries: list[dict] = []

        # ── view state ──
        self.filter = "needs"      # needs | suggested | all | uncertain
        self.search = ""
        self.selected: set[str] = set()
        self._pending_focus: str | None = None
        self._row_refs: dict[str, ft.Control] = {}

        # ── sync state ──
        self.syncing = False
        self.cancel_event: asyncio.Event | None = None
        self.s_cur = self.s_total = 0
        self.s_ok = self.s_sup = self.s_blank = self.s_err = 0
        self.s_name = ""

        # ── chrome ──
        self._h_hero = ft.Container()
        self._h_filter = ft.Container()
        self._h_bins = ft.Container()
        # The ONE scrollable. Direct child of this expanding Column: wrapping it
        # in an expand Container leaves it unbounded (blank subtree, no scroll),
        # and `scroll` must be set explicitly or Android shows no scrollbar.
        self._list = ft.ListView(
            expand=True, spacing=0, padding=ft.Padding.only(bottom=8),
            build_controls_on_demand=True, scroll=ft.ScrollMode.ALWAYS,
        )
        self._search_field = ft.TextField(
            hint_text="Find an artist…", prefix_icon=ft.Icons.SEARCH_ROUNDED,
            bgcolor=SURFACE2, border_color=BORDER, focused_border_color=CYAN,
            border_radius=10, text_style=ft.TextStyle(color=TEXT, size=13),
            dense=True, content_padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            on_change=self._on_search,
        )

        self.controls = [
            self._h_hero,
            self._h_filter,
            self._list,
            self._h_bins,
        ]
        self._reload()

    # ── public entry point (Library's per-artist edit button) ────────────────
    def focus_artist(self, name: str):
        """Jump to one artist and open its editor. The Library's edit button
        routes here instead of opening a second editor of its own."""
        self._pending_focus = name
        self.filter = "all"
        self.search = name
        self._search_field.value = name
        self._reload()

    # ── data ─────────────────────────────────────────────────────────────────
    async def _reload_async(self):
        try:
            # One gap scan, shared with coverage. Previously coverage ran its
            # own full-library scan and the pane ran a second.
            self.all_gaps = await self.db.get_metadata_gap_artists(limit=2000)
            self.coverage = await self.db.get_metadata_coverage(gaps=self.all_gaps)
            self.low_conf = await self.db.get_low_confidence_artists(limit=500)
            self.vocab = await self.db.get_genre_vocabulary(limit=40)
            self.countries = await self.db.get_library_countries()
        except Exception as exc:
            logger.exception("Metadata workbench load failed: %s", exc)
        self._render()
        if self._pending_focus:
            name, self._pending_focus = self._pending_focus, None
            row = next((g for g in self.all_gaps if g["artist_name"] == name), None)
            if row:
                self._open_editor(row)

    def _reload(self):
        self.app.page.run_task(self._reload_async)

    def _safe_update(self):
        try:
            self.app.page.update()
        except Exception:
            pass

    def _render(self):
        self._render_hero()
        self._render_filter()
        self._render_list()
        self._render_bins()
        self._safe_update()

    # ── HERO ─────────────────────────────────────────────────────────────────
    def _render_hero(self):
        cov = self.coverage
        artists = cov.get("artists", 0)
        placeable = cov.get("placeable", 0)
        blocking = cov.get("blocking", 0)

        if self.syncing:
            body = self._sync_banner()
        else:
            # An outcome, not a percentage: "can Auto-Play position this artist
            # at all" is the fact the user can act on, and it doesn't nag about
            # the artists that already work.
            if artists == 0:
                headline, sub, tone = "No artists yet", "Index your library to begin.", DIM
            elif blocking == 0:
                headline = "Every artist is placeable."
                sub = f"All {artists:,} have a genre or a country."
                tone = ACCENT_GREEN
            else:
                headline = f"{placeable:,} of {artists:,} artists placeable"
                sub = f"{blocking} still need a genre or a country."
                tone = ACCENT_AMBER
            # The button sits on its OWN row. Inline, it stole ~120pt from the
            # headline, which then wrapped to two lines and pushed the subtitle
            # to three — a 230pt card for two short sentences.
            body = ft.Column([
                ft.Row([
                    ft.Container(width=3, height=34, bgcolor=tone, border_radius=2),
                    ft.Column([
                        ft.Text(headline, size=14.5, weight=ft.FontWeight.W_700,
                                color=TEXT, max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(sub, size=11.5, color=DIM, max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=2, expand=True, tight=True),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([
                    ft.Container(expand=True),
                    self._filled_btn("Sync", lambda _e: self._start_sync(),
                                     icon=ft.Icons.CLOUD_SYNC_ROUNDED,
                                     disabled=(artists == 0)),
                ]),
            ], spacing=6, tight=True)

        self._h_hero.content = ft.Column([
            ft.Container(
                content=body,
                bgcolor=SURFACE, border_radius=RADIUS_CARD, padding=14,
                border=ft.Border.all(1, BORDER_SUBTLE),
            ),
            ft.Container(height=10),
            self._search_field,
            ft.Container(height=10),
        ], spacing=0)

    def _sync_banner(self) -> ft.Control:
        pct = (self.s_cur / self.s_total) if self.s_total else None
        self._sync_bar = ft.ProgressBar(value=pct, color=CYAN, bgcolor=SURFACE2, height=5)
        self._sync_label = ft.Text(
            f"Looking up {self.s_cur} of {self.s_total} · {self.s_name}"[:64],
            size=12.5, color=TEXT, weight=ft.FontWeight.W_600,
            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
        )
        # Four counters, not three. 'No tags found' and 'Connection failed' are
        # completely different situations that the old UI collapsed into one ✗,
        # so an outage looked exactly like a library full of untagged artists.
        self._sync_counts = ft.Row([
            self._counter("matched", self.s_ok, ACCENT_GREEN),
            self._counter("from other sources", self.s_sup, "#BF5AF2"),
            self._counter("no tags found", self.s_blank, DIM),
            self._counter("connection failed", self.s_err, ACCENT_RED),
        ], spacing=14, wrap=True, run_spacing=4)
        return ft.Column([
            self._sync_label,
            self._sync_bar,
            ft.Row([
                self._sync_counts,
                ft.Container(expand=True),
                self._ghost_btn("Stop", lambda _e: self._cancel_sync(), fg=ACCENT_RED),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=9)

    def _counter(self, label: str, n: int, color) -> ft.Control:
        return ft.Row([
            ft.Text(str(n), size=12.5, color=color, weight=ft.FontWeight.W_800,
                    font_family="monospace"),
            ft.Text(label, size=10.5, color=DIM),
        ], spacing=4, tight=True)

    def _update_sync_banner(self):
        if not self.syncing or getattr(self, "_sync_bar", None) is None:
            return
        self._sync_bar.value = (self.s_cur / self.s_total) if self.s_total else None
        self._sync_label.value = (
            f"Looking up {self.s_cur} of {self.s_total} · {self.s_name}"[:64]
        )
        for ctrl, n in zip(
            self._sync_counts.controls,
            (self.s_ok, self.s_sup, self.s_blank, self.s_err),
        ):
            ctrl.controls[0].value = str(n)
        self._safe_update()

    # ── SYNC ─────────────────────────────────────────────────────────────────
    def _start_sync(self):
        if self.syncing:
            return
        self.syncing = True
        self.cancel_event = asyncio.Event()
        self.s_cur = self.s_total = 0
        self.s_ok = self.s_sup = self.s_blank = self.s_err = 0
        self.s_name = "Starting…"
        self._render_hero()
        self._safe_update()
        self.app.page.run_task(self._do_sync)

    async def _do_sync(self):
        def cb(i, total, name, res):
            self.s_cur, self.s_total, self.s_name = i, total, name
            st, reason = res.get("status"), res.get("reason")
            if st == "error":
                self.s_err += 1
            elif not res.get("genres"):
                self.s_blank += 1
            elif reason in ("files", "qobuz"):
                self.s_sup += 1
            else:
                self.s_ok += 1
            self._update_sync_banner()

        summary: dict = {}
        try:
            summary = await enrich_library(
                self.db, with_genres=True, include_failed=True,
                retry_incomplete=True, progress=cb,
                cancel_event=self.cancel_event,
            )
        except Exception as exc:
            logger.exception("Workbench sync failed: %s", exc)
            summary = {"status": f"error: {exc}"}
        finally:
            self.syncing = False
            await self._reload_async()
            self._report_sync(summary)

    def _report_sync(self, summary: dict):
        """Say what actually happened.

        The summary used to be discarded entirely, so an aborted pass, a
        cancelled one and a missing aiohttp all reported "Sync done · 0 matched"
        — indistinguishable from a library that genuinely has no metadata."""
        status = (summary or {}).get("status")
        if status == "no_aiohttp":
            self.app.show_snackbar(
                "Can't sync — networking is unavailable in this build.",
                color=ACCENT_RED)
            return
        if isinstance(status, str) and status.startswith("error:"):
            self.app.show_snackbar(f"Sync failed — {status[6:].strip()}", color=ACCENT_RED)
            return
        if status == "aborted":
            self.app.show_snackbar(
                f"Stopped after {self.s_cur} of {self.s_total} — no connection. "
                f"Nothing was lost; run Sync again when you're back online.",
                color=ACCENT_RED)
            return
        if status == "cancelled":
            self.app.show_snackbar(
                f"Stopped at {self.s_cur} of {self.s_total}. "
                f"{summary.get('enriched', 0)} artists saved.", color=ACCENT_AMBER)
            return
        if status == "uptodate":
            self.app.show_snackbar("Everything is already up to date.",
                                   icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
            return

        parts = [f"{self.s_ok} matched"]
        srcs = [("your files", summary.get("from_files", 0) or 0),
                ("Qobuz", summary.get("from_qobuz", 0) or 0)]
        filled = [f"{n} from {label}" for label, n in srcs if n]
        if filled:
            parts.append(", ".join(filled))
        if self.s_blank:
            parts.append(f"{self.s_blank} still blank")
        self.app.show_snackbar("Sync done · " + ", ".join(parts),
                               icon=ft.Icons.CHECK_CIRCLE, color=CYAN)

    def _cancel_sync(self):
        if self.cancel_event:
            self.cancel_event.set()

    # ── FILTER ───────────────────────────────────────────────────────────────
    def _render_filter(self):
        # Counts do NOT go in the labels. `CupertinoSegmentedBar` sets
        # `no_wrap=True` on segment text, so at four segments on a 390pt screen
        # 'Needs you  11' rendered as 'Needs you 1' and 'Suggested  10' as
        # 'Suggested 1('. The count belongs in the list caption below.
        segs = [
            ("needs", "Needs you", None, ACCENT_RED),
            ("suggested", "Suggested", None, ACCENT_GREEN),
            ("uncertain", "Review", None, ACCENT_AMBER),
            ("all", "All", None, CYAN),
        ]
        self._seg = CupertinoSegmentedBar(segs, self.filter, self._set_filter, height=36)
        self._h_filter.content = ft.Column([
            self._seg,
            ft.Container(height=8),
        ], spacing=0)

    def _set_filter(self, key: str):
        self.filter = key
        self.selected.clear()
        # The bar restyles itself when the USER taps it, but not when we set the
        # filter in code (focus_artist does), so sync it explicitly.
        seg = getattr(self, "_seg", None)
        if seg is not None and seg.selected_key != key:
            seg.set_selected(key)
        self._render_list()
        self._render_bins()
        self._safe_update()

    def _on_search(self, e):
        self.search = e.control.value or ""
        self._render_list()
        self._render_bins()
        self._safe_update()

    # ── LIST ─────────────────────────────────────────────────────────────────
    def _visible(self) -> list[dict]:
        q = self.search.strip().lower()
        if self.filter == "uncertain":
            pool = self.low_conf
        else:
            pool = self.all_gaps
            if self.filter == "needs":
                pool = [g for g in pool if g.get("gap_severity") == 0]
            elif self.filter == "suggested":
                pool = [g for g in pool if g.get("gap_severity") == 1]
        if q:
            pool = [g for g in pool if q in g["artist_name"].lower()]
        return pool

    def _render_list(self):
        rows = self._visible()
        self._row_refs.clear()
        if not rows:
            self._list.controls = [self._empty_state()]
            return
        controls: list[ft.Control] = [self._column_header()]
        for i, g in enumerate(rows):
            ctrl = self._row(g, last=(i == len(rows) - 1))
            self._row_refs[g["artist_name"]] = ctrl
            controls.append(ctrl)
        n = len(rows)
        plural = "s" if n != 1 else ""
        caption = {
            "needs": f"{n} artist{plural} Auto-Play can't place yet",
            "suggested": f"{n} artist{plural} your own files can already tag",
            "uncertain": f"{n} match{'es' if n != 1 else ''} MusicBrainz wasn't sure about",
            "all": f"{n} artist{plural} with incomplete metadata",
        }[self.filter]
        # One inset group with hairline rules between rows, Apple-style, rather
        # than a stack of separate cards.
        self._list.controls = [
            ft.Container(
                content=ft.Text(caption, size=10.5,
                                color=TEXT_TERTIARY, max_lines=2),
                padding=ft.Padding.only(left=4, bottom=6),
            ),
            ft.Container(
                content=ft.Column(controls, spacing=0),
                bgcolor=SURFACE, border_radius=RADIUS_CARD,
                border=ft.Border.all(1, BORDER_SUBTLE),
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
            ),
        ]

    def _column_header(self) -> ft.Control:
        """Three columns, not four.

        At 390pt the four-column version left ARTIST and GENRE about 57pt each
        once the checkbox, a FROM column and a TRACKS column had taken their
        fixed widths — narrow enough that every value ellipsised to nothing.
        Provenance moved into the genre cell as a coloured suffix, which is
        where it reads better anyway, and TRACKS lost its wide header word."""
        def h(t, **kw):
            return ft.Text(t, size=9, weight=ft.FontWeight.W_800,
                           color=TEXT_TERTIARY, **kw)
        return ft.Container(
            content=ft.Row([
                ft.Container(width=_CHECK_W),
                ft.Container(content=h("ARTIST"), expand=5),
                ft.Container(content=h("GENRE"), expand=4),
                ft.Container(content=h("♪", text_align=ft.TextAlign.RIGHT),
                             width=_TRACKS_W),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.only(left=8, right=10, top=9, bottom=5),
        )

    def _row(self, g: dict, *, last: bool = False) -> ft.Control:
        """One table row. Draggable on desktop, checkbox-selectable everywhere."""
        name = g["artist_name"]
        sev = g.get("gap_severity", 2)
        genres = _genre_names(g.get("genres"))
        files = g.get("source_genres") or []
        country = (g.get("country") or "").upper() or None
        prov = g.get("provenance") or {}
        selected = name in self.selected

        # Genre cell: what we have, else what a source proposes.
        if genres:
            gtext, gcolor = ", ".join(genres[:3]), TEXT
            src_key = prov.get("genres")
        elif files:
            gtext, gcolor = files[0], ACCENT_GREEN
            src_key = "files"
        else:
            gtext, gcolor = "—", TEXT_TERTIARY
            src_key = None

        badge_text, badge_color = _SOURCE_STYLE.get(src_key or "", ("", DIM))

        # `fill_color` as a bare colour fills the box in EVERY state, so every
        # unselected row rendered as a solid accent square that read as already
        # ticked. It has to be keyed on ControlState.
        check = ft.Checkbox(
            value=selected, check_color=BG, splash_radius=0,
            fill_color={
                ft.ControlState.SELECTED: CYAN,
                ft.ControlState.DEFAULT: "transparent",
            },
            border_side={
                ft.ControlState.DEFAULT: ft.BorderSide(1.5, DIM),
                ft.ControlState.SELECTED: ft.BorderSide(0, "transparent"),
            },
            on_change=lambda e, n=name: self._toggle_select(n, bool(e.control.value)),
        )
        # Severity reads as a coloured left edge on the row rather than a
        # separate dot column — one less fixed width competing for the name.
        edge = ACCENT_RED if sev == 0 else (ACCENT_GREEN if sev == 1 else "transparent")

        # Names and genres WRAP to a second line instead of ellipsising away.
        # 'Vasilis Karras, Pantelis Pantelidis' has no useful one-line form at
        # this width, and a truncated name is not something you can act on.
        name_cell = ft.Row([
            ft.Text(_flag(country), size=11) if country else ft.Container(width=0),
            ft.Text(name, size=13, weight=ft.FontWeight.W_600, color=TEXT,
                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
        ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.START, tight=True)

        genre_cell = ft.Column([
            ft.Text(gtext, size=11.5, color=gcolor,
                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
            (ft.Text(badge_text, size=8, weight=ft.FontWeight.W_800, color=badge_color)
             if badge_text else ft.Container(height=0)),
        ], spacing=1, tight=True)

        inner = ft.Row([
            ft.Container(content=check, width=_CHECK_W, alignment=ft.Alignment(0, 0)),
            ft.Container(content=name_cell, expand=5),
            ft.Container(content=genre_cell, expand=4),
            ft.Container(
                content=ft.Text(str(g.get("track_count", 0)), size=11, color=DIM,
                                font_family="monospace",
                                text_align=ft.TextAlign.RIGHT),
                width=_TRACKS_W,
            ),
        ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        row = ft.Container(
            content=inner,
            bgcolor=apply_opacity(0.10, CYAN) if selected else "transparent",
            padding=ft.Padding.only(left=8, right=10, top=8, bottom=8),
            border_radius=0,
            border=ft.Border(
                left=ft.BorderSide(3, edge),
                bottom=(ft.BorderSide(0, "transparent") if last
                        else ft.BorderSide(1, BORDER_SUBTLE)),
            ),
            on_click=lambda _e, gg=g: self._open_editor(gg),
            ink=True, data=name,
        )
        # Drag is the desktop accelerator. `content_feedback` shows what is
        # actually travelling, which for a multi-selection is the whole pile.
        drag = ft.Draggable(
            group=_DRAG_GROUP, content=row, data=name,
            content_feedback=self._drag_feedback(name),
            on_drag_start=lambda _e, n=name: self._on_drag_start(n),
        )
        # Keep the checkbox reachable: selection can be changed in code (a drag
        # start replaces the selection, "Clear" empties it), and the tick has to
        # follow — a native click updates the widget itself, nothing else does.
        drag._checkbox = check
        return drag

    def _drag_feedback(self, name: str) -> ft.Control:
        n = len(self.selected) if name in self.selected and self.selected else 1
        label = f"{n} artists" if n > 1 else name
        return ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.DRAG_INDICATOR_ROUNDED, size=15, color=BG),
                ft.Text(label, size=12.5, weight=ft.FontWeight.W_700, color=BG),
            ], spacing=6, tight=True),
            bgcolor=CYAN, border_radius=RADIUS_PILL,
            padding=ft.Padding.symmetric(horizontal=14, vertical=9),
            opacity=0.95,
        )

    def _on_drag_start(self, name: str):
        # Dragging an unselected row acts on that row alone — it must not
        # silently carry a selection the user made somewhere else.
        if name not in self.selected:
            for other in list(self.selected):
                self._toggle_select(other, False)
            self._toggle_select(name, True)

    def _toggle_select(self, name: str, on: bool):
        """Selection changes ONE row's appearance. The old code re-rendered the
        entire list (up to a thousand rows) on every tick."""
        if on:
            self.selected.add(name)
        else:
            self.selected.discard(name)
        drag = self._row_refs.get(name)
        if drag is not None:
            row = drag.content
            row.bgcolor = apply_opacity(0.10, CYAN) if on else "transparent"
            drag.content_feedback = self._drag_feedback(name)
            cb = getattr(drag, "_checkbox", None)
            if cb is not None and cb.value != on:
                cb.value = on
            try:
                row.update()
            except Exception:
                pass
        self._render_bins()
        self._safe_update()

    def _empty_state(self) -> ft.Control:
        if self.coverage.get("artists", 0) == 0:
            icon, title, sub = (ft.Icons.LIBRARY_MUSIC_ROUNDED, "Your library is empty.",
                                "Index your music to start curating metadata.")
        elif self.search.strip():
            icon, title, sub = (ft.Icons.SEARCH_OFF_ROUNDED, "No artist by that name.",
                                "Try a different spelling, or clear the search.")
        elif self.filter == "needs":
            icon, title, sub = (ft.Icons.CHECK_CIRCLE_ROUNDED,
                                "Every artist can be placed.",
                                "Auto-Play has a genre or a country for all of them.")
        elif self.filter == "uncertain":
            icon, title, sub = (ft.Icons.CHECK_CIRCLE_ROUNDED, "Nothing to review.",
                                "Every match MusicBrainz returned is resolved.")
        else:
            icon, title, sub = (ft.Icons.CHECK_CIRCLE_ROUNDED, "Nothing to fix here.",
                                "This view is clear.")
        return ft.Container(
            content=ft.Column([
                ft.Icon(icon, color=ACCENT_GREEN if "CHECK" in str(icon) else DIM, size=38),
                ft.Text(title, color=TEXT, size=15, weight=ft.FontWeight.W_700),
                ft.Text(sub, color=DIM, size=11.5, text_align=ft.TextAlign.CENTER),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER, spacing=8),
            padding=ft.Padding.symmetric(horizontal=20, vertical=48),
            alignment=ft.Alignment(0, 0),
        )

    # ── BINS ─────────────────────────────────────────────────────────────────
    def _render_bins(self):
        """The pinned tray: drop targets for the library's own vocabulary.

        Visible only when something is selected or draggable — an empty tray on
        an empty list is just chrome."""
        if self.filter == "uncertain" or not self.all_gaps:
            self._h_bins.content = None
            self._h_bins.visible = False
            return
        self._h_bins.visible = True
        n = len(self.selected)

        bins: list[ft.Control] = []
        for v in self.vocab[:10]:
            bins.append(self._bin(v["name"], kind="genre", count=v.get("artists", 0)))
        for c in self.countries[:4]:
            bins.append(self._bin(c["code"], kind="country", count=c.get("artists", 0)))
        bins.append(self._bin("New…", kind="new", count=0))

        hint = (f"{n} selected — tap a tag to apply"
                if n else "Select artists, or drag one onto a tag")
        # ONE fixed-height row that scrolls sideways, NOT a wrapping row.
        # Wrapping put twelve pills on five lines (~600pt) in a tray that does
        # not expand, so the ListView — which does — was left roughly 110pt and
        # showed a single clipped artist. A tray must cost a fixed, small slice
        # of the screen no matter how much vocabulary the library has.
        self._h_bins.content = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text(hint, size=10.5,
                            color=CYAN if n else TEXT_TERTIARY,
                            weight=ft.FontWeight.W_700 if n else ft.FontWeight.W_600,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                    (ft.TextButton("Clear", on_click=lambda _e: self._clear_selection(),
                                   style=ft.ButtonStyle(color=DIM)) if n else ft.Container()),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Container(
                    content=ft.Row(bins, spacing=7, scroll=ft.ScrollMode.AUTO,
                                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    # Pill (~31) + the horizontal scrollbar Flet draws UNDER the
                    # row; at 40 the pills were clipped along their bottom edge.
                    height=54,
                ),
            ], spacing=4, tight=True),
            padding=ft.Padding.only(left=2, right=2, top=8, bottom=2),
            border=ft.Border.only(top=ft.BorderSide(1, BORDER_SUBTLE)),
        )

    def _bin(self, label: str, *, kind: str, count: int) -> ft.Control:
        accent = {"genre": CYAN, "country": "#BF5AF2", "new": DIM}[kind]
        armed = bool(self.selected)
        text = _flag(label) + " " + label if kind == "country" else label

        inner = ft.Container(
            content=ft.Row([
                ft.Text(text, size=12, weight=ft.FontWeight.W_600,
                        color=accent if armed else DIM),
                (ft.Text(str(count), size=9.5, color=TEXT_TERTIARY,
                         font_family="monospace") if count else ft.Container()),
            ], spacing=6, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=apply_opacity(0.12, accent) if armed else SURFACE2,
            border=ft.Border.all(1, apply_opacity(0.45 if armed else 0.25, accent)),
            border_radius=RADIUS_PILL,
            padding=ft.Padding.symmetric(horizontal=12, vertical=7),
            on_click=lambda _e: self._apply_bin(label, kind),
            ink=True,
            alignment=ft.Alignment(0, 0),
            animate=ft.Animation(120, ft.AnimationCurve.EASE_OUT),
        )

        def _will_accept(e):
            inner.bgcolor = apply_opacity(0.30, accent)
            inner.border = ft.Border.all(2, accent)
            try:
                inner.update()
            except Exception:
                pass

        def _leave(_e):
            inner.bgcolor = apply_opacity(0.12, accent) if self.selected else SURFACE2
            inner.border = ft.Border.all(
                1, apply_opacity(0.45 if self.selected else 0.25, accent))
            try:
                inner.update()
            except Exception:
                pass

        def _accept(e):
            _leave(e)
            # e.src is the Draggable itself, so the payload arrives without a
            # page.get_control round trip.
            dragged = getattr(getattr(e, "src", None), "data", None)
            if dragged and dragged not in self.selected:
                self.selected = {dragged}
            self._apply_bin(label, kind)

        return ft.DragTarget(
            group=_DRAG_GROUP, content=inner,
            on_will_accept=_will_accept, on_leave=_leave, on_accept=_accept,
        )

    def _apply_bin(self, label: str, kind: str):
        names = sorted(self.selected)
        if not names:
            self.app.show_snackbar("Select one or more artists first.", color=ACCENT_AMBER)
            return
        if kind == "new":
            self._prompt_new_genre(names)
            return
        genre = label if kind == "genre" else None
        country = label if kind == "country" else None
        self._commit_tag(names, genre=genre, country=country)

    def _commit_tag(self, names: list[str], *, genre=None, country=None):
        async def _do():
            try:
                # Merge, never overwrite: dropping artists on a genre must not
                # erase a country MusicBrainz already found for some of them.
                n = await self.db.bulk_tag_artists(
                    names, genre=genre, country=country, refresh_model=False,
                )
            except Exception as exc:
                logger.exception("bulk tag failed: %s", exc)
                self.app.show_snackbar(f"Couldn't save: {exc}", color=ACCENT_RED)
                return
            what = genre or _country_label(country)
            self.app.show_snackbar(
                f"{n} artist{'s' if n != 1 else ''} → {what}",
                icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
            self.selected.clear()
            await self._reload_async()
            self._schedule_walk_refresh()
        self.app.page.run_task(_do)

    def _schedule_walk_refresh(self):
        """Rebuild the models Auto-Play reads, once per burst.

        Coalesced, so tagging six artists in a row costs one rebuild rather than
        six full-library passes — and confirmed only when it has actually
        landed, since that is the moment the queue would really change."""
        refresh_walk_models(
            self.db,
            on_done=lambda _s: self.app.show_snackbar(
                "Auto-Play updated.", icon=ft.Icons.AUTO_AWESOME, color=ACCENT_GREEN),
        )

    def _clear_selection(self):
        for name in list(self.selected):
            self._toggle_select(name, False)
        self.selected.clear()
        self._render_bins()
        self._safe_update()

    def _prompt_new_genre(self, names: list[str]):
        field = ft.TextField(
            label="Genre", autofocus=True, bgcolor=SURFACE2, border_color=BORDER,
            focused_border_color=CYAN, border_radius=10,
            text_style=ft.TextStyle(color=TEXT, size=14),
        )

        def _ok(_e=None):
            val = (field.value or "").strip().lower()
            self.app.dismiss_dialog(dlg)
            if val:
                self._commit_tag(names, genre=val)

        field.on_submit = _ok
        dlg = ft.AlertDialog(
            bgcolor=SURFACE,
            title=ft.Text(f"Tag {len(names)} artist{'s' if len(names) != 1 else ''}",
                          color=TEXT, size=16, weight=ft.FontWeight.W_700),
            content=ft.Column([
                ft.Text("A tag your library already uses will be understood better "
                        "than a new one.", size=11.5, color=DIM),
                field,
            ], tight=True, spacing=12, width=320),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _e: self.app.dismiss_dialog(dlg)),
                self._filled_btn("Apply", _ok),
            ],
        )
        self.app.page.show_dialog(dlg)

    # ── EDITOR (bottom sheet) ────────────────────────────────────────────────
    def _open_editor(self, item: dict):
        """Per-artist editor as a bottom sheet.

        It used to be an inline expander that pushed the list around under the
        user's finger and forced a full re-render on every chip tap."""
        name = item["artist_name"]
        genres = {g.lower() for g in _genre_names(item.get("genres"))}
        files = item.get("source_genres") or []
        country = {"v": (item.get("country") or "").upper()}

        chips_row = ft.Row(wrap=True, spacing=6, run_spacing=6)
        country_row = ft.Row(wrap=True, spacing=6, run_spacing=6)
        custom = ft.TextField(
            hint_text="Add a genre…", dense=True, width=160, bgcolor=SURFACE2,
            border_color=BORDER, focused_border_color=CYAN, border_radius=8,
            text_style=ft.TextStyle(color=TEXT, size=12),
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        )

        def _chip(label, *, on, accent=CYAN, prefix=""):
            return ft.Container(
                content=ft.Text(f"{prefix}{label}" + ("  ✕" if on else ""),
                                size=12, color=BG if on else accent,
                                weight=ft.FontWeight.W_600 if on else None),
                bgcolor=accent if on else "transparent",
                border=ft.Border.all(1, accent if on else apply_opacity(0.4, accent)),
                border_radius=RADIUS_PILL,
                padding=ft.Padding.symmetric(horizontal=11, vertical=6),
                ink=True,
            )

        def _rebuild():
            sel = []
            for t in sorted(genres):
                c = _chip(t, on=True)
                c.on_click = lambda _e, t=t: (genres.discard(t), _rebuild())
                sel.append(c)
            sug = []
            for t in files:
                if t.lower() in genres:
                    continue
                c = _chip(t, on=False, accent=ACCENT_GREEN, prefix="+ ")
                c.on_click = lambda _e, t=t: (genres.add(t.lower()), _rebuild())
                sug.append(c)
            voc = []
            for v in self.vocab[:12]:
                t = v["name"]
                if t.lower() in genres or t.lower() in {f.lower() for f in files}:
                    continue
                c = _chip(t, on=False, accent=DIM, prefix="+ ")
                c.on_click = lambda _e, t=t: (genres.add(t.lower()), _rebuild())
                voc.append(c)
            # Selected first, then what the sources suggest, then the library's
            # vocabulary, and the free-text field LAST — it led the row before,
            # so the first thing you saw was an empty box rather than your tags.
            chips_row.controls = sel + sug + voc + [custom]

            country_row.controls = []
            # Tap a country, don't type an ISO code. The library already knows
            # which countries it contains, and the answer is nearly always one.
            for c in self.countries[:8]:
                code = c["code"]
                on = country["v"] == code
                ch = _chip(f"{_flag(code)} {code}", on=on, accent="#BF5AF2")
                ch.on_click = lambda _e, code=code: (
                    country.__setitem__("v", "" if country["v"] == code else code),
                    _rebuild(),
                )
                country_row.controls.append(ch)
            self._safe_update()

        def _add_custom(e):
            v = (e.control.value or "").strip().lower()
            if v:
                genres.add(v)
            e.control.value = ""
            _rebuild()
        custom.on_submit = _add_custom
        _rebuild()

        def _save(_e):
            self.app.dismiss_dialog(sheet)

            async def _do():
                try:
                    await self.db.set_manual_artist_enrichment(
                        name, country=(country["v"] or None), genres=sorted(genres))
                except Exception as exc:
                    logger.exception("save failed for %s: %s", name, exc)
                    self.app.show_snackbar(f"Couldn't save: {exc}", color=ACCENT_RED)
                    return
                self.app.show_snackbar(f"Saved {name}", icon=ft.Icons.CHECK_CIRCLE,
                                       color=CYAN)
                self.selected.discard(name)
                await self._reload_async()
                self._schedule_walk_refresh()
            self.app.page.run_task(_do)

        body = [
            self._sheet_section("GENRES", ft.Icons.LABEL_ROUNDED, CYAN, chips_row),
            self._sheet_section("COUNTRY", ft.Icons.PUBLIC_ROUNDED, "#BF5AF2", country_row),
            ft.Row([
                self._ghost_btn("Find on MusicBrainz",
                                lambda _e: (self.app.dismiss_dialog(sheet),
                                            self._open_mb_dialog(name)),
                                fg=CYAN, icon=ft.Icons.TRAVEL_EXPLORE_ROUNDED),
            ]),
        ]
        if self.filter == "uncertain":
            body.append(ft.Row([
                self._ghost_btn("Reject this match",
                                lambda _e, n=name: (self.app.dismiss_dialog(sheet),
                                                    self._review_reject(n)),
                                fg=ACCENT_RED, icon=ft.Icons.CLOSE_ROUNDED),
            ]))

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column([
                    ft.Container(
                        content=ft.Container(width=36, height=5, bgcolor=SURFACE_ELEVATED,
                                             border_radius=3),
                        alignment=ft.Alignment(0, 0),
                        padding=ft.Padding.only(top=10, bottom=6),
                    ),
                    ft.Container(
                        content=ft.Row([
                            ft.Text(name, size=17, weight=ft.FontWeight.W_600, color=TEXT,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
                                    expand=True),
                            self._filled_btn("Save", _save),
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                        padding=ft.Padding.symmetric(horizontal=16, vertical=4),
                    ),
                    ft.Divider(color=BORDER_SUBTLE, height=1),
                    ft.Container(
                        content=ft.Column(body, spacing=14, scroll=ft.ScrollMode.AUTO),
                        padding=16, expand=True,
                    ),
                ], spacing=0, expand=True),
                bgcolor=SURFACE,
                border_radius=ft.BorderRadius.only(top_left=20, top_right=20),
                expand=True,
            ),
            bgcolor=SURFACE, draggable=True, use_safe_area=True,
            show_drag_handle=False, scrollable=False,
        )
        self.app.page.show_dialog(sheet)

    def _sheet_section(self, label, icon, accent, content) -> ft.Control:
        return ft.Column([
            ft.Row([
                ft.Icon(icon, size=13, color=accent),
                ft.Text(label, size=10, color=accent, weight=ft.FontWeight.W_800),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            content,
        ], spacing=8)

    # ── REVIEW actions ───────────────────────────────────────────────────────
    def _review_reject(self, name: str):
        async def _do():
            try:
                await self.db.confirm_artist_match(name, status="notfound", score=0)
            except Exception as exc:
                self.app.show_snackbar(f"Couldn't reject: {exc}", color=ACCENT_RED)
                return
            self.app.show_snackbar(f"Rejected {name}", color=ACCENT_AMBER)
            await self._reload_async()
            self._schedule_walk_refresh()
        self.app.page.run_task(_do)

    # ── MusicBrainz identity picker ──────────────────────────────────────────
    def _open_mb_dialog(self, artist: str):
        pw = self.app.page.width or 400
        ph = self.app.page.height or 720
        dlg_w = max(280, min(420, pw - 40))
        res_h = max(200, min(380, ph - 280))

        query_field = ft.TextField(
            value=artist, label="Artist name", dense=True, bgcolor=SURFACE2,
            border_color=BORDER, focused_border_color=CYAN, border_radius=10,
            text_style=ft.TextStyle(color=TEXT, size=13),
        )
        results = ft.Column([], scroll=ft.ScrollMode.AUTO, height=res_h, spacing=8)

        def _search(_e=None):
            q = (query_field.value or artist).strip() or artist

            async def _run():
                results.controls = [ft.Row([
                    ft.ProgressRing(width=15, height=15, stroke_width=2),
                    ft.Text("Searching MusicBrainz…", size=12, color=DIM)], spacing=8)]
                self._safe_update()
                try:
                    cands = await search_musicbrainz_artists_candidates(q)
                except Exception as exc:
                    logger.warning("MB search failed for %s: %s", q, exc)
                    results.controls = [ft.Text("Lookup failed — are you offline?",
                                                size=12, color=ACCENT_AMBER)]
                    self._safe_update()
                    return
                cands = sorted(
                    cands, key=lambda c: (bool(c.get("is_junk")), -(c.get("score") or 0))
                )[:8]
                results.controls = (
                    [self._mb_card(artist, c, dlg) for c in cands] if cands
                    else [ft.Text("No candidates. Try a different spelling.",
                                  size=12, color=DIM)]
                )
                self._safe_update()

            self.app.page.run_task(_run)

        query_field.on_submit = _search
        dlg = ft.AlertDialog(
            bgcolor=SURFACE,
            title=ft.Text(f"MusicBrainz · {artist}"[:40], color=TEXT, size=15,
                          weight=ft.FontWeight.W_700),
            content=ft.Column([
                ft.Text("Pick the right act — its country and genres are fetched and "
                        "saved as a confirmed match.", color=DIM, size=11),
                query_field, results,
            ], tight=True, spacing=10, width=dlg_w),
            actions=[
                ft.TextButton("Close", on_click=lambda _e: self.app.dismiss_dialog(dlg)),
                self._filled_btn("Search", lambda _e: _search()),
            ],
        )
        self.app.page.show_dialog(dlg)
        _search()

    def _mb_card(self, artist: str, cand: dict, dlg) -> ft.Control:
        cname = cand.get("name") or artist
        disamb = (cand.get("disambiguation") or "").strip()
        cc = cand.get("country")
        gstr = ", ".join(_genre_names(cand.get("genres")))
        score = cand.get("score") or 0
        sc = ACCENT_GREEN if score >= 80 else (ACCENT_AMBER if score >= 50 else ACCENT_RED)

        head = []
        if cc:
            head.append(ft.Text(_flag(cc), size=15))
        head.append(ft.Text(cname, size=13.5, color=TEXT, weight=ft.FontWeight.W_600,
                            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True))
        head.append(ft.Container(
            content=ft.Text(f"{score}%", size=10, color=sc, weight=ft.FontWeight.W_800),
            bgcolor=apply_opacity(0.14, sc), border_radius=6,
            padding=ft.Padding.symmetric(horizontal=7, vertical=3),
        ))
        lines: list[ft.Control] = [ft.Row(head, spacing=6,
                                          vertical_alignment=ft.CrossAxisAlignment.CENTER)]
        if disamb:
            lines.append(ft.Text(disamb, size=11, color=TEXT, max_lines=2,
                                 overflow=ft.TextOverflow.ELLIPSIS))
        lines.append(ft.Text(gstr or "No genres listed — fetched when you select",
                             size=10.5, color=DIM if gstr else ACCENT_AMBER,
                             max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        lines.append(ft.Row([
            ft.Container(expand=True),
            self._filled_btn("Use this match",
                             lambda _e, c=cand: self._use_mb_candidate(artist, c, dlg),
                             icon=ft.Icons.CHECK_ROUNDED),
        ]))
        return ft.Container(
            content=ft.Column(lines, spacing=6, tight=True),
            bgcolor=SURFACE2, border_radius=10, padding=12,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

    def _use_mb_candidate(self, artist: str, cand: dict, dlg=None):
        async def _do():
            mbid = cand.get("mbid")
            country, area, genres = cand.get("country"), cand.get("area"), cand.get("genres")
            if mbid:
                # The search payload usually omits genres, so committing the
                # shallow hit would store a blank and look like a no-op.
                details = await musicbrainz_artist_details(mbid)
                if details.get("genres"):
                    genres = details["genres"]
                country = details.get("country") or country
                area = details.get("area") or area
            try:
                await self.db.confirm_artist_match(
                    artist, mbid=mbid, country=country, area=area,
                    genres=genres, status="ok", score=cand.get("score") or 100)
            except Exception as exc:
                logger.exception("use_mb_candidate failed: %s", exc)
                self.app.show_snackbar(f"Couldn't save: {exc}", color=ACCENT_RED)
                return
            self.app.dismiss_dialog(dlg)
            got = _genre_names(genres)
            self.app.show_snackbar(
                f"Matched {artist} · {', '.join(got[:3])}" if got else
                f"Matched {artist}, but MusicBrainz lists no genres — add one by hand.",
                icon=ft.Icons.CHECK_CIRCLE, color=CYAN if got else ACCENT_AMBER)
            self.selected.discard(artist)
            await self._reload_async()
            self._schedule_walk_refresh()
        self.app.page.run_task(_do)

    # ── shared button styles ─────────────────────────────────────────────────
    def _filled_btn(self, text, on_click, *, icon=None, disabled=False):
        row = []
        if icon:
            row.append(ft.Icon(icon, size=16, color=BG))
        row.append(ft.Text(text, weight=ft.FontWeight.W_700, size=13, color=BG))
        return ft.Container(
            content=ft.Row(row, spacing=7, tight=True,
                           alignment=ft.MainAxisAlignment.CENTER),
            bgcolor=apply_opacity(0.4, CYAN) if disabled else CYAN,
            border_radius=RADIUS_PILL,
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            on_click=None if disabled else on_click, ink=not disabled,
            alignment=ft.Alignment(0, 0),
        )

    def _ghost_btn(self, text, on_click, *, fg=DIM, icon=None):
        row = []
        if icon:
            row.append(ft.Icon(icon, size=15, color=fg))
        row.append(ft.Text(text, weight=ft.FontWeight.W_600, size=12.5, color=fg))
        return ft.Container(
            content=ft.Row(row, spacing=7, tight=True,
                           alignment=ft.MainAxisAlignment.CENTER),
            bgcolor="transparent", border_radius=10,
            border=ft.Border.all(1, BORDER),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            on_click=on_click, ink=True, alignment=ft.Alignment(0, 0),
        )
