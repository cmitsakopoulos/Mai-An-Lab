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
artists that belong to one scene". You select several and tap a genre, because that is what the user actually knows: not this artist's tag list, but
that these twenty are all Greek rap. The bins are the library's own vocabulary,
which matters for correctness and not just typing — the NPMI model in
`genre_similarity` is learned from co-occurrence across THIS library's tags, so
a freshly invented spelling has no learned relation to anything.

── Flet 0.86 mechanics baked in ──────────────────────────────────────────────
  • The ListView is a DIRECT child of an expanding Column, never wrapped in an
    expand Container, and the pane is mounted with `_show_sub_page_full` so it
    is not nested inside the settings hub's scrolling Column. Both were
    violated before, which is what made scrolling fight back.
  • Select-then-tap is the ONLY tagging route. Drag-and-drop was removed: a
    horizontal drag inside a settings sub-page already means "back"
    (docs/FLET_FACTS.md §4), and touch gesture arbitration is unkind to
    drag-inside-scroll.
  • Row edits mutate one row and call `row.update()`; only structural changes
    re-render the list. The old code rebuilt every row on every chip tap.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
import flet as ft

from ui.tokens import (
    TEXT, DIM, TEXT_TERTIARY, BORDER, BORDER_SUBTLE, SURFACE, SURFACE2,
    SURFACE_ELEVATED, CYAN, BG, RADIUS_CARD, RADIUS_PILL,
    ACCENT_GREEN, ACCENT_AMBER, ACCENT_RED, apply_opacity,
)
from ui.widgets import CupertinoSegmentedBar, dialog_handoff
from utils.metadata_enrich import (
    enrich_library,
    refresh_walk_models,
    search_musicbrainz_artists_candidates,
    musicbrainz_artist_details,
)
from utils import metadata_ai

logger = logging.getLogger(__name__)


def _input_border(color=BORDER, focused_color=CYAN, radius=10):
    return {
        ft.ControlState.FOCUSED: ft.OutlineInputBorder(border_radius=radius, side=ft.BorderSide(1, focused_color)),
        ft.ControlState.DEFAULT: ft.OutlineInputBorder(border_radius=radius, side=ft.BorderSide(1, color)),
    }

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
    "ai":          ("AI", "#5E5CE6"),
}

# AI research accent: distinct from country purple and the provider colours.
_AI = "#5E5CE6"
_PROVIDERS = {
    "musicbrainz": ("MusicBrainz", ft.Icons.PUBLIC_ROUNDED, CYAN),
    "qobuz": ("Qobuz", ft.Icons.STOREFRONT_ROUNDED, "#FF9F0A"),
}

# Fixed column widths, kept here so the header and the rows can never drift
# apart. Everything else is proportional, so the two text columns get the whole
# remaining width instead of the scraps four fixed columns used to leave them.
_CHECK_W = 34
_TRACKS_W = 26
_ACCEPT_W = 34


def _flag(cc: str | None) -> str:
    if not cc or len(cc) != 2 or not cc.isalpha():
        return ""
    return cc.upper()


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
        # AI research: pending suggestions by artist, names already researched
        # (any outcome), and whether a Gemini key is configured at all.
        self.ai_pending: dict[str, dict] = {}
        self.researched: set[str] = set()
        self.ai_ready = False

        # ── view state ──
        self.filter = "needs"      # needs | suggested | all | uncertain
        self.search = ""
        self.selected: set[str] = set()
        self._pending_focus: str | None = None
        self._row_refs: dict[str, ft.Control] = {}

        # ── sync state ──
        prefs = getattr(self.app, "_prefs", None) or {}
        self.sync_provider = prefs.get("sync_provider", "musicbrainz")
        self.syncing = False
        self.cancel_event: asyncio.Event | None = None
        self.s_cur = self.s_total = 0
        self.s_ok = self.s_sup = self.s_blank = self.s_err = 0
        self.s_name = ""
        self.researching = False
        self.r_cur = self.r_total = self.r_found = self.r_unknown = 0
        self.r_name = ""
        self._url_launcher = ft.UrlLauncher()

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
            bgcolor=SURFACE2, border=_input_border(BORDER, CYAN, 10),
            text_style=ft.TextStyle(color=TEXT, size=13),
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

    def filter_genre(self, genre_name: str):
        """Filter workbench by genre tag or name."""
        self._pending_focus = None
        self.filter = "all"
        self.search = genre_name
        self._search_field.value = genre_name
        self._reload()

    async def _reload_async(self):
        try:
            # One gap scan, shared with coverage. Previously coverage ran its
            # own full-library scan and the pane ran a second.
            self.all_gaps = await self.db.get_metadata_gap_artists(limit=2000)
            self.coverage = await self.db.get_metadata_coverage(gaps=self.all_gaps)
            self.low_conf = await self.db.get_low_confidence_artists(limit=500)
            self.vocab = await self.db.get_genre_vocabulary(limit=40)
            self.countries = await self.db.get_library_countries()
            self.ai_pending = await self.db.get_pending_ai_research()
            self.researched = await self.db.get_researched_artist_names()
            self.ai_ready = metadata_ai.research_available()
        except Exception as exc:
            logger.exception("Metadata workbench load failed: %s", exc)
        if self.search:
            try:
                if not any(g["artist_name"].lower() == self.search.lower() for g in self.all_gaps):
                    item = await self.db.get_artist_workbench_item(self.search)
                    if item:
                        self.all_gaps.append(item)
                genre_artists = await self.db.get_artists_by_genre(self.search)
                existing_names = {g["artist_name"] for g in self.all_gaps}
                for ga in genre_artists[:30]:
                    ganame = ga["name"]
                    if ganame not in existing_names:
                        item = await self.db.get_artist_workbench_item(ganame)
                        if item:
                            self.all_gaps.append(item)
                            existing_names.add(ganame)
            except Exception as e:
                logger.debug("Failed populating additional workbench items for search: %s", e)
        self._render()
        if self._pending_focus:
            name, self._pending_focus = self._pending_focus, None
            row = next((g for g in self.all_gaps if g["artist_name"] == name), None)
            if not row:
                row = await self.db.get_artist_workbench_item(name)
                if row:
                    self.all_gaps.append(row)
                    self._render()
            if row:
                self._open_editor(row)

    def _reload(self):
        self.app.page.run_task(self._reload_async)

    def _safe_update(self):
        try:
            if hasattr(self.app, "safe_update"):
                self.app.safe_update(lambda: None)
            elif self.app.page:
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
    def _research_targets(self) -> list[str]:
        """Artists nothing can place that haven't been researched yet."""
        return [g["artist_name"] for g in self.all_gaps
                if g.get("gap_severity") == 0
                and g["artist_name"].lower() not in self.researched]

    def _render_hero(self):
        """One sentence of status and at most two buttons.

        The provider pair used to be two pills plus a description line above
        the Sync button — three controls for a choice most people make once.
        It is now a menu on the Sync button itself."""
        cov = self.coverage
        artists = cov.get("artists", 0)
        placeable = cov.get("placeable", 0)
        blocking = cov.get("blocking", 0)
        n_sugg = len(self._suggestion_rows())

        if self.syncing:
            body = self._sync_banner()
        elif self.researching:
            body = self._research_banner()
        else:
            targets = self._research_targets()
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
            if n_sugg:
                sub += f" {n_sugg} suggestion{'s' if n_sugg != 1 else ''} ready to review."

            actions: list[ft.Control] = [self._sync_button(disabled=(artists == 0))]
            if targets and self.ai_ready:
                actions.append(self._filled_btn(
                    f"Research {len(targets)}", lambda _e: self._start_research(targets),
                    icon=ft.Icons.AUTO_AWESOME_ROUNDED, bg=_AI))
            elif n_sugg:
                actions.append(self._filled_btn(
                    "Review", lambda _e: self._set_filter("suggested"),
                    icon=ft.Icons.CHECKLIST_ROUNDED))

            lines: list[ft.Control] = [
                ft.Row([
                    ft.Container(width=3, height=34, bgcolor=tone, border_radius=2),
                    ft.Column([
                        ft.Text(headline, size=14.5, weight=ft.FontWeight.W_700,
                                color=TEXT, max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(sub, size=11.5, color=DIM, max_lines=3,
                                overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=2, expand=True, tight=True),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row(actions, spacing=8, wrap=True, run_spacing=8,
                       alignment=ft.MainAxisAlignment.END),
            ]
            if targets and not self.ai_ready:
                lines.append(ft.Text(
                    "Tip: add a Gemini API key in Settings → AI Assistant and the "
                    "app can research these artists on the web for you.",
                    size=10.5, color=DIM))
            body = ft.Column(lines, spacing=10, tight=True)

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

    def _sync_button(self, *, disabled: bool) -> ft.Control:
        """'Sync' plus a menu to pick the source it syncs from."""
        label, icon, accent = _PROVIDERS.get(self.sync_provider, _PROVIDERS["musicbrainz"])

        def _select_provider(p: str):
            if self.sync_provider == p:
                return
            self.sync_provider = p
            if hasattr(self.app, "set_pref"):
                self.app.set_pref("sync_provider", p)
            elif hasattr(self.app, "_prefs") and isinstance(self.app._prefs, dict):
                self.app._prefs["sync_provider"] = p
            self._render_hero()
            self._safe_update()

        items = []
        for key, (plabel, picon, pacc) in _PROVIDERS.items():
            on = key == self.sync_provider
            items.append(ft.PopupMenuItem(
                content=ft.Row([
                    ft.Icon(ft.Icons.CHECK_ROUNDED if on else picon, size=15,
                            color=pacc if on else DIM),
                    ft.Text(f"Sync from {plabel}", size=12.5,
                            color=TEXT if on else DIM,
                            weight=ft.FontWeight.W_700 if on else ft.FontWeight.W_400),
                ], spacing=8, tight=True),
                on_click=lambda _e, k=key: _select_provider(k),
            ))
        menu = ft.PopupMenuButton(
            items=items, tooltip="Choose the sync source",
            content=ft.Container(
                content=ft.Icon(ft.Icons.ARROW_DROP_DOWN_ROUNDED, size=20, color=accent),
                padding=ft.Padding.symmetric(horizontal=6, vertical=8),
            ),
        )
        btn = ft.Container(
            content=ft.Row([
                ft.Icon(icon, size=15, color=accent),
                ft.Text(f"Sync · {label}", size=12.5, weight=ft.FontWeight.W_600,
                        color=TEXT),
            ], spacing=7, tight=True),
            padding=ft.Padding.only(left=14, top=10, bottom=10),
            on_click=None if disabled else (lambda _e: self._start_sync()),
            ink=not disabled,
        )
        return ft.Container(
            content=ft.Row([btn, menu], spacing=0, tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            border=ft.Border.all(1, BORDER), border_radius=RADIUS_PILL,
            opacity=0.5 if disabled else 1.0,
        )

    def _sync_banner(self) -> ft.Control:
        pct = (self.s_cur / self.s_total) if self.s_total else None
        self._sync_bar = ft.ProgressBar(value=pct, color=CYAN, bgcolor=SURFACE2, height=5)
        provider_title = self.sync_provider.capitalize()
        self._sync_label = ft.Text(
            f"{provider_title}: {self.s_cur} of {self.s_total} · {self.s_name}"[:64],
            size=12.5, color=TEXT, weight=ft.FontWeight.W_600,
            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
        )
        self._sync_counts = ft.Row([
            self._counter("matched", self.s_ok, ACCENT_GREEN),
            self._counter("no tags found", self.s_blank, DIM),
            self._counter("failed", self.s_err, ACCENT_RED),
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
        provider_title = self.sync_provider.capitalize()
        self._sync_label.value = (
            f"{provider_title}: {self.s_cur} of {self.s_total} · {self.s_name}"[:64]
        )
        for ctrl, n in zip(
            self._sync_counts.controls,
            (self.s_ok, self.s_blank, self.s_err),
        ):
            ctrl.controls[0].value = str(n)
        self._safe_update()

    # ── SYNC ─────────────────────────────────────────────────────────────────
    def _start_sync(self):
        if self.syncing or self.researching:
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
            st = res.get("status")
            reason = res.get("reason")
            if st == "error" or (reason and str(reason).startswith("error:")):
                self.s_err += 1
            elif not res.get("genres"):
                self.s_blank += 1
            else:
                self.s_ok += 1
            self._update_sync_banner()

        from utils.progress_notify import (
            ProgressNotifier, metadata_progress_cb, metadata_retry_action,
            metadata_summary_text,
        )
        notifier = ProgressNotifier("metadata", "Syncing artist metadata", on_cancel=self.cancel_event.set)
        summary: dict = {}
        try:
            summary = await enrich_library(
                self.db, provider=self.sync_provider,
                with_genres=True, include_failed=True,
                retry_incomplete=True, progress=metadata_progress_cb(notifier, cb),
                cancel_event=self.cancel_event,
            )
        except Exception as exc:
            logger.exception("Workbench sync failed: %s", exc)
            summary = {"status": f"error: {exc}"}
        finally:
            notifier.finish(metadata_summary_text(summary),
                            action=metadata_retry_action(summary))
            self.syncing = False
            await self._reload_async()
            self._report_sync(summary)

    def _report_sync(self, summary: dict):
        """Say what actually happened loudly."""
        status = (summary or {}).get("status")
        provider_name = (summary or {}).get("provider", self.sync_provider).capitalize()
        if status == "no_aiohttp":
            self.app.show_snackbar(
                "Can't sync — networking is unavailable in this build.",
                color=ACCENT_RED)
            return
        if isinstance(status, str) and status.startswith("error:"):
            # Loud failure reporting
            self.app.show_snackbar(f"{provider_name} sync failed: {status[6:].strip()}", color=ACCENT_RED)
            return
        if status == "aborted":
            self.app.show_snackbar(
                f"Stopped after {self.s_cur} of {self.s_total} — connection failed. "
                f"Run Sync again when you're back online.",
                color=ACCENT_RED)
            return
        if status == "cancelled":
            self.app.show_snackbar(
                f"Stopped at {self.s_cur} of {self.s_total}. "
                f"{summary.get('enriched', 0)} artists saved.", color=ACCENT_AMBER)
            return
        if status == "uptodate":
            self.app.show_snackbar(f"All artists are already up to date on {provider_name}.",
                                   icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
            return

        parts = [f"{self.s_ok} matched"]
        if self.s_blank:
            parts.append(f"{self.s_blank} blank/unmatched")
        if self.s_err:
            parts.append(f"{self.s_err} failed")
        self.app.show_snackbar(f"{provider_name} Sync done · " + ", ".join(parts),
                               icon=ft.Icons.CHECK_CIRCLE, color=CYAN)

    def _cancel_sync(self):
        if self.cancel_event:
            self.cancel_event.set()


    # ── AI RESEARCH ──────────────────────────────────────────────────────────
    def _research_banner(self) -> ft.Control:
        pct = (self.r_cur / self.r_total) if self.r_total else None
        self._r_bar = ft.ProgressBar(value=pct, color=_AI, bgcolor=SURFACE2, height=5)
        self._r_label = ft.Text(
            f"Researching {self.r_cur} of {self.r_total} · {self.r_name}"[:64],
            size=12.5, color=TEXT, weight=ft.FontWeight.W_600,
            overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
        )
        self._r_counts = ft.Row([
            self._counter("suggested", self.r_found, _AI),
            self._counter("not found", self.r_unknown, DIM),
        ], spacing=14, wrap=True, run_spacing=4)
        return ft.Column([
            self._r_label,
            self._r_bar,
            ft.Row([
                self._r_counts,
                ft.Container(expand=True),
                self._ghost_btn("Stop", lambda _e: self._cancel_sync(), fg=ACCENT_RED),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=9)

    def _start_research(self, names: list[str]):
        """Research artists on the web, one grounded Gemini call each. Answers
        land as suggestions under the Suggestions filter; nothing is saved to
        an artist until the user accepts it."""
        if self.syncing or self.researching or not names:
            return
        self.researching = True
        self.cancel_event = asyncio.Event()
        self.r_cur, self.r_total = 0, len(names)
        self.r_found = self.r_unknown = 0
        self.r_name = "Starting…"
        self._render_hero()
        self._safe_update()
        self.app.page.run_task(self._do_research, list(names))

    async def _do_research(self, names: list[str]):
        from utils.progress_notify import ProgressNotifier
        notifier = ProgressNotifier("metadata", "Researching artists",
                                    on_cancel=self.cancel_event.set)

        def cb(i, total, name, res):
            self.r_cur, self.r_total, self.r_name = i, total, name
            st = res.get("status")
            if st == "ok":
                self.r_found += 1
            elif st != "skipped":
                self.r_unknown += 1
            notifier.update(i, total, f"{i} / {total} artists · {name}")
            if getattr(self, "_r_bar", None) is not None:
                self._r_bar.value = i / total if total else None
                self._r_label.value = f"Researching {i} of {total} · {name}"[:64]
                for ctrl, n in zip(self._r_counts.controls, (self.r_found, self.r_unknown)):
                    ctrl.controls[0].value = str(n)
                self._safe_update()

        summary: dict = {}
        try:
            summary = await metadata_ai.research_artists(
                self.db, names, progress=cb, cancel_event=self.cancel_event)
        except Exception as exc:
            logger.exception("AI research run failed: %s", exc)
            summary = {"status": f"error: {exc}"}
        finally:
            status = summary.get("status") or ""
            found = summary.get("found", 0)
            notifier.finish(
                f"Research failed: {status[6:].strip()}" if status.startswith("error")
                else f"{found} suggestion{'s' if found != 1 else ''} to review"
                if found else "No new suggestions found",
                action=("Review", "workbench:suggestions") if found else None,
            )
            self.researching = False
            await self._reload_async()
            self._report_research(summary)

    def _report_research(self, summary: dict):
        status = (summary or {}).get("status") or ""
        found = summary.get("found", 0)
        unknown = summary.get("unknown", 0) + summary.get("failed", 0)
        if status.startswith("error"):
            self.app.show_snackbar(status[6:].strip(), color=ACCENT_RED)
            return
        if found:
            self._set_filter("suggested")
        msg = (f"{found} suggestion{'s' if found != 1 else ''} ready to review"
               if found else "The web had nothing reliable on these artists")
        if unknown and found:
            msg += f" · {unknown} not found"
        if status == "cancelled":
            msg = "Stopped. " + msg
        self.app.show_snackbar(msg, icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                               color=_AI if found else ACCENT_AMBER)

    # ── SUGGESTIONS ──────────────────────────────────────────────────────────
    def _suggestion_rows(self) -> list[dict]:
        """Rows with something to accept: an AI answer, or a tag the artist's
        own files already carry."""
        return [g for g in self.all_gaps
                if g["artist_name"] in self.ai_pending
                or g.get("gap_severity") == 1]

    def _suggestion_of(self, g: dict) -> tuple[list[str], str, str]:
        """(genres, country, origin) this row would get on Accept."""
        ai = self.ai_pending.get(g["artist_name"])
        if ai:
            return list(ai.get("genres") or []), ai.get("country") or "", "ai"
        files = [f.lower() for f in (g.get("source_genres") or [])[:3]]
        return files, "", "files"

    def _accept(self, names: list[str]):
        """Accept the suggestion on each row. AI answers and file tags both
        MERGE into what the artist already has."""
        async def _do():
            by_name = {g["artist_name"]: g for g in self.all_gaps}
            n = 0
            for name in names:
                g = by_name.get(name)
                if not g:
                    continue
                try:
                    if name in self.ai_pending:
                        await metadata_ai.accept_suggestion(self.db, name)
                    else:
                        genres, _cc, _o = self._suggestion_of(g)
                        if not genres:
                            continue
                        await self.db.bulk_tag_artists(
                            [name], genre=",".join(genres), refresh_model=False)
                    n += 1
                except Exception as exc:
                    logger.exception("accept failed for %s: %s", name, exc)
            for name in names:
                self.selected.discard(name)
            await self._reload_async()
            if n:
                self.app.show_snackbar(
                    f"Accepted {n} suggestion{'s' if n != 1 else ''}",
                    icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
                self._schedule_walk_refresh()
        self.app.page.run_task(_do)

    # ── FILTER ───────────────────────────────────────────────────────────────
    def _render_filter(self):
        # Counts do NOT go in the labels. `CupertinoSegmentedBar` sets
        # `no_wrap=True` on segment text, so at four segments on a 390pt screen
        # 'Needs you  11' rendered as 'Needs you 1' and 'Suggested  10' as
        # 'Suggested 1('. The count belongs in the list caption below.
        segs = [
            ("needs", "Needs you", None, ACCENT_RED),
            ("suggested", "Suggestions", None, ACCENT_GREEN),
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
                # An artist with an answer waiting is under Suggestions, not here.
                pool = [g for g in pool if g.get("gap_severity") == 0
                        and g["artist_name"] not in self.ai_pending]
            elif self.filter == "suggested":
                pool = self._suggestion_rows()
        if q:
            def _matches_q(g):
                if q in (g.get("artist_name") or "").lower():
                    return True
                for gn in _genre_names(g.get("genres")):
                    if q in gn.lower():
                        return True
                for gn in (g.get("source_genres") or []):
                    if q in str(gn).lower():
                        return True
                return False
            pool = [g for g in pool if _matches_q(g)]
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
            "suggested": f"{n} suggestion{plural} — tick ✓ to accept, or tap to adjust",
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
            ] + ([ft.Container(width=_ACCEPT_W)] if self.filter == "suggested" else []),
                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.only(left=8, right=10, top=9, bottom=5),
        )

    def _row(self, g: dict, *, last: bool = False) -> ft.Control:
        """One table row: tap opens the editor, the checkbox selects."""
        name = g["artist_name"]
        sev = g.get("gap_severity", 2)
        genres = _genre_names(g.get("genres"))
        files = g.get("source_genres") or []
        country = (g.get("country") or "").upper() or None
        prov = g.get("provenance") or {}
        selected = name in self.selected

        # Genre cell: an AI answer waiting, else what we have, else what the
        # files propose.
        ai = self.ai_pending.get(name)
        if ai:
            bits = list(ai.get("genres") or [])[:3]
            if ai.get("country"):
                bits.append(ai["country"])
            gtext, gcolor = ", ".join(bits) or "—", _AI
            src_key = "ai"
        elif genres:
            gtext, gcolor = ", ".join(genres[:3]), TEXT
            src_key = prov.get("genres")
        elif files:
            gtext, gcolor = files[0], ACCENT_GREEN
            src_key = "files"
        else:
            gtext, gcolor = "—", TEXT_TERTIARY
            src_key = None

        badge_text, badge_color = _SOURCE_STYLE.get(src_key or "", ("", DIM))
        if ai and ai.get("confidence"):
            badge_text = f"AI · {ai['confidence'].upper()}"

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
            ft.Container(
                content=ft.Text(country.upper(), size=9.5, weight=ft.FontWeight.W_700, color="#BF5AF2"),
                bgcolor=apply_opacity(0.14, "#BF5AF2"),
                border_radius=4,
                padding=ft.Padding.symmetric(horizontal=4, vertical=1),
            ) if country else ft.Container(width=0),
            ft.Text(name, size=13, weight=ft.FontWeight.W_600, color=TEXT,
                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
        ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True)

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
        ] + ([ft.Container(
            content=ft.IconButton(
                icon=ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED, icon_size=20,
                icon_color=_AI if ai else ACCENT_GREEN, tooltip="Accept",
                on_click=lambda _e, n=name: self._accept([n]),
                style=ft.ButtonStyle(padding=0),
            ),
            width=_ACCEPT_W, alignment=ft.Alignment(0, 0),
        )] if self.filter == "suggested" else []),
            spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)

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
        # Keep the checkbox reachable: selection can be changed in code ("Clear"
        # empties it), and the tick has to follow — a native click updates the
        # widget itself, nothing else does.
        row._checkbox = check
        return row

    def _toggle_select(self, name: str, on: bool):
        """Selection changes ONE row's appearance. The old code re-rendered the
        entire list (up to a thousand rows) on every tick."""
        if on:
            self.selected.add(name)
        else:
            self.selected.discard(name)
        row = self._row_refs.get(name)
        if row is not None:
            row.bgcolor = apply_opacity(0.10, CYAN) if on else "transparent"
            cb = getattr(row, "_checkbox", None)
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
        elif self.filter == "suggested":
            icon, title, sub = (ft.Icons.CHECK_CIRCLE_ROUNDED, "No suggestions waiting.",
                                "Research artists under Needs you, or Sync.")
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
        """The pinned tray: tap targets for the library's own vocabulary.

        Hidden on the Uncertain filter and on an empty list — a tray with
        nothing to apply it to is just chrome."""
        if self.filter == "uncertain" or not self.all_gaps:
            self._h_bins.content = None
            self._h_bins.visible = False
            return
        self._h_bins.visible = True
        n = len(self.selected)
        if self.filter == "suggested":
            self._render_accept_tray(n)
            return

        bins: list[ft.Control] = []
        if self.ai_ready:
            bins.append(self._bin("Research", kind="research", count=0))
        for v in self.vocab[:10]:
            bins.append(self._bin(v["name"], kind="genre", count=v.get("artists", 0)))
        for c in self.countries[:4]:
            bins.append(self._bin(c["code"], kind="country", count=c.get("artists", 0)))
        bins.append(self._bin("New…", kind="new", count=0))

        hint = (f"{n} selected — tap a tag to apply"
                if n else "Select artists, then tap a tag")
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

    def _render_accept_tray(self, n: int):
        rows = self._visible()
        target = sorted(self.selected) if n else [g["artist_name"] for g in rows]
        label = (f"Accept {n} selected" if n else f"Accept all {len(rows)}")
        self._h_bins.content = ft.Container(
            content=ft.Row([
                ft.Text("Accepting saves the suggestion as your own answer. "
                        "AI answers keep an AI label.",
                        size=10.5, color=TEXT_TERTIARY, expand=True, max_lines=2),
                (ft.TextButton("Clear", on_click=lambda _e: self._clear_selection(),
                               style=ft.ButtonStyle(color=DIM)) if n else ft.Container()),
                self._filled_btn(label, lambda _e: self._accept(target),
                                 icon=ft.Icons.DONE_ALL_ROUNDED,
                                 disabled=not target),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.only(left=2, right=2, top=8, bottom=6),
            border=ft.Border.only(top=ft.BorderSide(1, BORDER_SUBTLE)),
        )

    def _bin(self, label: str, *, kind: str, count: int) -> ft.Control:
        accent = {"genre": CYAN, "country": "#BF5AF2", "new": DIM, "research": _AI}[kind]
        armed = bool(self.selected)
        text = label

        inner = ft.Container(
            content=ft.Row([
                (ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, size=13,
                         color=accent if armed else DIM)
                 if kind == "research" else ft.Container(width=0)),
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
        return inner

    def _apply_bin(self, label: str, kind: str):
        names = sorted(self.selected)
        if not names:
            self.app.show_snackbar("Select one or more artists first.", color=ACCENT_AMBER)
            return
        if kind == "new":
            self._prompt_new_genre(names)
            return
        if kind == "research":
            self.selected.clear()
            self._start_research(names)
            return
        genre = label if kind == "genre" else None
        country = label if kind == "country" else None
        self._commit_tag(names, genre=genre, country=country)

    def _commit_tag(self, names: list[str], *, genre=None, country=None):
        async def _do():
            try:
                # Merge, never overwrite: tagging artists with a genre must not
                # erase a country MusicBrainz already found for some of them.
                n = await self.db.bulk_tag_artists(
                    names, genre=genre, country=country, refresh_model=False,
                )
            except Exception as exc:
                logger.exception("bulk tag failed: %s", exc)
                self.app.show_snackbar(f"Couldn't save: {exc}", color=ACCENT_RED)
                return
            what = genre or _country_label(country)
            self.selected.clear()
            await self._reload_async()
            self.app.show_snackbar(
                f"{n} artist{'s' if n != 1 else ''} → {what}",
                icon=ft.Icons.CHECK_CIRCLE, color=CYAN)
            self._schedule_walk_refresh()
        self.app.page.run_task(_do)

    def _schedule_walk_refresh(self):
        """Rebuild the models Auto-Play reads, once per burst.

        Coalesced, so tagging six artists in a row costs one rebuild rather than
        six full-library passes — and confirmed only when it has actually
        landed, since that is the moment the queue would really change."""
        if hasattr(self.app, "library_view") and self.app.library_view:
            self.app.library_view._needs_reload = True
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
        dlg = None
        _on_dismiss, _close = dialog_handoff(self.app, lambda: dlg)

        field = ft.TextField(
            label="Genre", autofocus=True, bgcolor=SURFACE2,
            border=_input_border(BORDER, CYAN, 10),
            text_style=ft.TextStyle(color=TEXT, size=14),
        )

        def _ok(_e=None):
            val = (field.value or "").strip().lower()
            if val:
                _close(lambda: self._commit_tag(names, genre=val))
            else:
                _close()

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
                ft.TextButton("Cancel", on_click=lambda _e: _close()),
                self._filled_btn("Apply", _ok),
            ],
            on_dismiss=_on_dismiss,
        )
        self.app.page.show_dialog(dlg)

    # ── EDITOR (bottom sheet) ────────────────────────────────────────────────
    def _open_editor(self, item: dict):
        """Per-artist editor as a bottom sheet.

        It used to be an inline expander that pushed the list around under the
        user's finger and forced a full re-render on every chip tap."""
        sheet = None
        _on_sheet_dismissed, _close = dialog_handoff(self.app, lambda: sheet)

        name = item["artist_name"]
        genres = {g.lower() for g in _genre_names(item.get("genres"))}
        files = item.get("source_genres") or []
        country = {"v": (item.get("country") or "").upper()}

        chips_row = ft.Row(wrap=True, spacing=6, run_spacing=6)
        country_row = ft.Row(wrap=True, spacing=6, run_spacing=6)
        custom = ft.TextField(
            hint_text="Add a genre…", dense=True, width=160, bgcolor=SURFACE2,
            border=_input_border(BORDER, CYAN, 8),
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
            codes = [c["code"] for c in self.countries[:8]]
            if country["v"] and country["v"] not in codes:
                codes.insert(0, country["v"])  # e.g. set from an AI answer
            for code in codes:
                on = country["v"] == code
                ch = _chip(code, on=on, accent="#BF5AF2")
                ch.on_click = lambda _e, code=code: (
                    country.__setitem__("v", "" if country["v"] == code else code),
                    _rebuild(),
                )
                country_row.controls.append(ch)
            try:
                chips_row.update()
                country_row.update()
            except Exception:
                self._safe_update()

        def _add_custom(e):
            v = (e.control.value or "").strip().lower()
            if v:
                for part in v.split(","):
                    p = part.strip()
                    if p:
                        genres.add(p)
            e.control.value = ""
            _rebuild()
        custom.on_submit = _add_custom
        _rebuild()

        # ── AI suggestion section ──
        ai_box = ft.Column(spacing=8, tight=True)
        ai_state = {"payload": self.ai_pending.get(name), "busy": False, "loaded": False}

        def _use(gs=(), cc="", toggle=False):
            for g in gs:
                if toggle and g.lower() in genres:
                    genres.discard(g.lower())
                else:
                    genres.add(g.lower())
            if cc:
                country["v"] = "" if (toggle and country["v"] == cc) else cc
            _rebuild()
            _render_ai()

        def _dismiss_ai():
            async def _do():
                await metadata_ai.dismiss_suggestion(self.db, name)
                self.ai_pending.pop(name, None)
                ai_state["payload"] = None
                ai_state["dismissed"] = True
                _render_ai()
                self._render_list()
                self._render_hero()
                self._safe_update()
            self.app.page.run_task(_do)

        def _research(force: bool):
            async def _do():
                ai_state["busy"] = True
                _render_ai()
                try:
                    res = await metadata_ai.research_artist(self.db, name, force=force)
                except Exception as exc:
                    ai_state["busy"] = False
                    _render_ai()
                    self.app.show_snackbar(str(exc), color=ACCENT_RED)
                    return
                ai_state.update(busy=False, payload=res, dismissed=False)
                self.researched.add(name.lower())
                if res.get("status") == "ok":
                    self.ai_pending[name] = res
                else:
                    self.ai_pending.pop(name, None)
                _render_ai()
                self._render_list()
                self._render_hero()
                self._safe_update()
            self.app.page.run_task(_do)

        def _render_ai():
            p = ai_state["payload"]
            ctrls: list[ft.Control] = []
            if ai_state["busy"]:
                ctrls.append(ft.Row([
                    ft.ProgressRing(width=15, height=15, stroke_width=2, color=_AI),
                    ft.Text("Searching the web…", size=12, color=DIM),
                ], spacing=8))
            elif p and p.get("status") == "ok" and not ai_state.get("dismissed"):
                conf = p.get("confidence") or "low"
                if p.get("summary"):
                    ctrls.append(ft.Text(p["summary"], size=12.5, color=TEXT))
                if p.get("matched_as") and p["matched_as"].lower() != name.lower():
                    ctrls.append(ft.Text(f"Matched as: {p['matched_as']}", size=11, color=DIM))
                sug = []
                new = set(p.get("new_genres") or [])
                for g in p.get("genres") or []:
                    c = _chip(g + ("  (new)" if g in new else ""),
                              on=g in genres, accent=_AI, prefix="" if g in genres else "+ ")
                    c.on_click = lambda _e, g=g: _use([g], toggle=True)
                    sug.append(c)
                cc = p.get("country") or ""
                if cc:
                    c = _chip(f"{cc} · {_country_label(cc)}", on=country["v"] == cc,
                              accent="#BF5AF2", prefix="" if country["v"] == cc else "+ ")
                    c.on_click = lambda _e, cc=cc: _use((), cc, toggle=True)
                    sug.append(c)
                if sug:
                    ctrls.append(ft.Row(sug, wrap=True, spacing=6, run_spacing=6))
                if not p.get("grounded"):
                    ctrls.append(ft.Text(
                        "No web sources came back, so this is from the model's memory "
                        "and no country is offered.", size=10.5, color=ACCENT_AMBER))
                links = [self._link(src.get("title") or "source", src["uri"])
                         for src in (p.get("sources") or [])[:5] if src.get("uri")]
                if links:
                    ctrls.append(ft.Row([ft.Text("Sources", size=10, color=TEXT_TERTIARY)]
                                        + links, wrap=True, spacing=4, run_spacing=2))
                searches = [self._link(q, "https://www.google.com/search?q="
                                       + urllib.parse.quote_plus(q), icon=ft.Icons.SEARCH_ROUNDED)
                            for q in (p.get("queries") or [])[:3]]
                if searches:
                    ctrls.append(ft.Row([ft.Text("Google", size=10, color=TEXT_TERTIARY)]
                                        + searches, wrap=True, spacing=4, run_spacing=2))
                ctrls.append(ft.Row([
                    self._ghost_btn("Use all", lambda _e: _use(p.get("genres") or [], cc),
                                    fg=_AI, icon=ft.Icons.DONE_ALL_ROUNDED),
                    self._ghost_btn("Dismiss", lambda _e: _dismiss_ai(), fg=DIM),
                    self._ghost_btn("Again", lambda _e: _research(True), fg=DIM,
                                    icon=ft.Icons.REFRESH_ROUNDED),
                ], spacing=8, wrap=True))
                head_note = f"{conf} confidence"
                ai_box.controls = [self._sheet_section(
                    f"AI SUGGESTION · {head_note.upper()}", ft.Icons.AUTO_AWESOME_ROUNDED,
                    _AI, ft.Column(ctrls, spacing=8, tight=True))]
                _refresh_ai()
                return
            elif p:
                ctrls.append(ft.Text(
                    f"The web had nothing reliable on {name}." if p.get("status") != "ok"
                    else "Suggestion dismissed.", size=12, color=DIM))
                ctrls.append(ft.Row([self._ghost_btn(
                    "Search again", lambda _e: _research(True), fg=_AI,
                    icon=ft.Icons.REFRESH_ROUNDED)]))
            else:
                ctrls.append(ft.Text(
                    "Search the web for this artist's genre and country. Nothing is "
                    "saved until you accept it.", size=11.5, color=DIM))
                ctrls.append(ft.Row([self._filled_btn(
                    "Research on the web", lambda _e: _research(False),
                    icon=ft.Icons.AUTO_AWESOME_ROUNDED, bg=_AI)]))
            ai_box.controls = [self._sheet_section(
                "AI RESEARCH", ft.Icons.AUTO_AWESOME_ROUNDED, _AI,
                ft.Column(ctrls, spacing=8, tight=True))]
            _refresh_ai()

        def _refresh_ai():
            try:
                ai_box.update()
            except Exception:
                self._safe_update()

        async def _load_cached_ai():
            if ai_state["payload"] is None:
                try:
                    cached = await self.db.get_ai_research(name)
                except Exception:
                    cached = None
                if cached:
                    ai_state["payload"] = cached["payload"]
                    ai_state["dismissed"] = cached["state"] == "dismissed" and \
                        cached["payload"].get("status") == "ok"
            _render_ai()

        if self.ai_ready or ai_state["payload"]:
            _render_ai()
            self.app.page.run_task(_load_cached_ai)

        def _save(_e):
            # Capture any unsubmitted genre left in the custom input field
            unsubmitted = (custom.value or "").strip().lower()
            if unsubmitted:
                for part in unsubmitted.split(","):
                    p = part.strip()
                    if p:
                        genres.add(p)

            async def _do():
                try:
                    await self.db.set_manual_artist_enrichment(
                        name, country=(country["v"] or None), genres=sorted(genres))
                except Exception as exc:
                    logger.exception("save failed for %s: %s", name, exc)
                    self.app.show_snackbar(f"Couldn't save: {exc}", color=ACCENT_RED)
                    return
                self.selected.discard(name)
                await self._reload_async()
                self.app.show_snackbar(f"Saved {name}", icon=ft.Icons.CHECK_CIRCLE,
                                       color=CYAN)
                self._schedule_walk_refresh()

            _close(lambda: self.app.page.run_task(_do))

        async def _fetch_qobuz_action():
            self.app.show_snackbar(f"Querying Qobuz for '{name}'…", color=CYAN)
            try:
                from utils.metadata_qobuz import fetch_qobuz_artist_metadata
                res = await fetch_qobuz_artist_metadata(name)
            except Exception as exc:
                logger.error("Qobuz lookup failed for %s: %s", name, exc)
                self.app.show_snackbar(f"Qobuz error: {exc}", color=ACCENT_RED)
                return

            qgenres = res.get("genres") or []
            reason = res.get("reason")
            if qgenres:
                count_added = 0
                for g in qgenres:
                    gn = g.get("name") if isinstance(g, dict) else str(g)
                    if gn and gn.strip():
                        tag = gn.strip().lower()
                        if tag not in genres:
                            genres.add(tag)
                            count_added += 1
                _rebuild()
                self.app.show_snackbar(
                    f"Qobuz: found {len(qgenres)} genres ({count_added} added)",
                    icon=ft.Icons.CHECK_CIRCLE, color=CYAN
                )
            else:
                msg = f"No genres found on Qobuz for '{name}'"
                if reason:
                    msg += f" ({reason})"
                self.app.show_snackbar(msg, color=ACCENT_AMBER)

        # The AI section only exists when research can run or an answer is on
        # file; an empty column would still cost a gap at the top of the sheet.
        body = ([ai_box] if (self.ai_ready or ai_state["payload"]) else []) + [
            self._sheet_section("GENRES", ft.Icons.LABEL_ROUNDED, CYAN, chips_row),
            self._sheet_section("COUNTRY", ft.Icons.PUBLIC_ROUNDED, "#BF5AF2", country_row),
            ft.Text("OTHER SOURCES", size=10, color=TEXT_TERTIARY, weight=ft.FontWeight.W_800),
            ft.Row([
                self._ghost_btn("Find on MusicBrainz",
                                lambda _e: _close(lambda: self._open_mb_dialog(name)),
                                fg=CYAN, icon=ft.Icons.TRAVEL_EXPLORE_ROUNDED),
                self._ghost_btn("Fetch from Qobuz",
                                lambda _e: self.app.page.run_task(_fetch_qobuz_action),
                                fg="#FF9F0A", icon=ft.Icons.CLOUD_DOWNLOAD_ROUNDED),
            ], spacing=8, wrap=True),
        ]
        if self.filter == "uncertain":
            body.append(ft.Row([
                self._ghost_btn("Reject this match",
                                lambda _e, n=name: _close(lambda: self._review_reject(n)),
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
                            ft.IconButton(
                                icon=ft.Icons.CLOSE_ROUNDED,
                                icon_color=DIM,
                                icon_size=20,
                                tooltip="Close",
                                on_click=lambda _e: _close(),
                            ),
                            ft.Text(name, size=17, weight=ft.FontWeight.W_600, color=TEXT,
                                    overflow=ft.TextOverflow.ELLIPSIS, max_lines=1,
                                    expand=True),
                            self._filled_btn("Save", _save),
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
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
            on_dismiss=_on_sheet_dismissed,
        )
        self.app.page.show_dialog(sheet)

    def _link(self, label: str, url: str, *, icon=ft.Icons.OPEN_IN_NEW_ROUNDED) -> ft.Control:
        return ft.Container(
            content=ft.Row([
                ft.Icon(icon, size=11, color=_AI),
                ft.Text(label[:40], size=11, color=_AI),
            ], spacing=3, tight=True),
            padding=ft.Padding.symmetric(horizontal=6, vertical=3),
            border_radius=6, ink=True,
            on_click=lambda _e, u=url: self.app.page.run_task(self._url_launcher.launch_url, u),
        )

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
            self.selected.discard(name)
            await self._reload_async()
            self.app.show_snackbar(f"Rejected {name}", color=ACCENT_AMBER)
            self._schedule_walk_refresh()
        self.app.page.run_task(_do)

    # ── MusicBrainz identity picker ──────────────────────────────────────────
    def _open_mb_dialog(self, artist: str):
        pw = self.app.page.width or 400
        ph = self.app.page.height or 720
        dlg_w = max(280, min(420, pw - 40))
        res_h = max(200, min(380, ph - 280))

        dlg = None
        _on_dlg_dismissed, _close_dlg = dialog_handoff(self.app, lambda: dlg)

        query_field = ft.TextField(
            value=artist, label="Artist name", dense=True, bgcolor=SURFACE2,
            border=_input_border(BORDER, CYAN, 10),
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
                    [self._mb_card(artist, c, _close_dlg) for c in cands] if cands
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
                ft.TextButton("Close", on_click=lambda _e: _close_dlg()),
                self._filled_btn("Search", lambda _e: _search()),
            ],
            on_dismiss=_on_dlg_dismissed,
        )
        self.app.page.show_dialog(dlg)
        _search()

    def _mb_card(self, artist: str, cand: dict, close_fn=None) -> ft.Control:
        cname = cand.get("name") or artist
        disamb = (cand.get("disambiguation") or "").strip()
        cc = cand.get("country")
        gstr = ", ".join(_genre_names(cand.get("genres")))
        score = cand.get("score") or 0
        sc = ACCENT_GREEN if score >= 80 else (ACCENT_AMBER if score >= 50 else ACCENT_RED)

        head = []
        if cc:
            head.append(ft.Container(
                content=ft.Text(cc.upper(), size=10, weight=ft.FontWeight.W_700, color="#BF5AF2"),
                bgcolor=apply_opacity(0.14, "#BF5AF2"),
                border_radius=4,
                padding=ft.Padding.symmetric(horizontal=5, vertical=2),
            ))
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
                             lambda _e, c=cand: self._use_mb_candidate(artist, c, close_fn),
                             icon=ft.Icons.CHECK_ROUNDED),
        ]))
        return ft.Container(
            content=ft.Column(lines, spacing=6, tight=True),
            bgcolor=SURFACE2, border_radius=10, padding=12,
            border=ft.Border.all(1, BORDER_SUBTLE),
        )

    def _use_mb_candidate(self, artist: str, cand: dict, close_fn=None):
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
            got = _genre_names(genres)
            self.selected.discard(artist)
            await self._reload_async()
            self.app.show_snackbar(
                f"Matched {artist} · {', '.join(got[:3])}" if got else
                f"Matched {artist}, but MusicBrainz lists no genres — add one by hand.",
                icon=ft.Icons.CHECK_CIRCLE, color=CYAN if got else ACCENT_AMBER)
            self._schedule_walk_refresh()

        if close_fn:
            close_fn(lambda: self.app.page.run_task(_do))
        else:
            self.app.page.run_task(_do)

    # ── shared button styles ─────────────────────────────────────────────────
    def _filled_btn(self, text, on_click, *, icon=None, disabled=False, bg=CYAN):
        row = []
        if icon:
            row.append(ft.Icon(icon, size=16, color=BG))
        row.append(ft.Text(text, weight=ft.FontWeight.W_700, size=13, color=BG))
        return ft.Container(
            content=ft.Row(row, spacing=7, tight=True,
                           alignment=ft.MainAxisAlignment.CENTER),
            bgcolor=apply_opacity(0.4, bg) if disabled else bg,
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
