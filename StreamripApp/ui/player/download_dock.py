"""App-level download surface: a collapsed pill plus an expandable sheet.

The download queue used to render exclusively inside SearchView, so switching
to Library mid-download made the transfer invisible. Both presentations here
read the same QueueController state and are driven by one listener, so the pill
and the sheet can never disagree.
"""
import flet as ft

from ui.tokens import (
    SURFACE, SURFACE2, SURFACE_ELEVATED, CYAN, TEXT, DIM, BORDER_SUBTLE,
    ACCENT_GREEN, ACCENT_RED, RADIUS_CARD, RADIUS_THUMB, apply_opacity,
)
from ui.widgets import src_color
from utils.queue_controller import (
    QUEUED, DOWNLOADING, DONE, FAILED, CANCELLED, TERMINAL_STATES,
)

# One table drives every per-state visual. A new state is a row here, not a
# branch in three render functions.
STATE_VISUALS = {
    QUEUED:      (ft.Icons.SCHEDULE_ROUNDED,           DIM),
    DOWNLOADING: (ft.Icons.ARROW_CIRCLE_DOWN_ROUNDED,  CYAN),
    DONE:        (ft.Icons.CHECK_CIRCLE_ROUNDED,       ACCENT_GREEN),
    FAILED:      (ft.Icons.ERROR_ROUNDED,              ACCENT_RED),
    CANCELLED:   (ft.Icons.REMOVE_CIRCLE_ROUNDED,      DIM),
}


class DownloadDock:
    def __init__(self, app: "StreamripFletApp"):
        self.app  = app
        self.page = app.page
        self._sheet_initialized = False
        self._sheet = None
        self._build_pill()

    # ── collapsed pill ──────────────────────────────────────────────────────
    def _build_pill(self):
        self._pill_ring = ft.ProgressRing(
            width=18, height=18, stroke_width=2.5, color=CYAN, value=0,
        )
        self._pill_headline = ft.Text(
            "", color=TEXT, size=13, weight=ft.FontWeight.W_600,
            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._pill_detail = ft.Text(
            "", color=DIM, size=11,
            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._pill_bar = ft.ProgressBar(value=0, color=CYAN, bgcolor=SURFACE2, height=2)

        self.pill = ft.Container(
            content=ft.Stack([
                ft.Container(
                    content=ft.Row(
                        [
                            self._pill_ring,
                            ft.Column(
                                [self._pill_headline, self._pill_detail],
                                spacing=1, expand=True,
                            ),
                            ft.Icon(ft.Icons.KEYBOARD_ARROW_UP_ROUNDED, color=DIM, size=20),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.only(left=14, right=10, top=9, bottom=8),
                ),
                ft.Container(content=self._pill_bar, bottom=0, left=0, right=0),
            ]),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER_SUBTLE),
            border_radius=14,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            margin=ft.Margin.only(left=10, right=10, bottom=6),
            on_click=lambda e: self.expand(),
            visible=False,
            opacity=0,
            animate_opacity=ft.Animation(180, ft.AnimationCurve.EASE_OUT),
            animate_offset=ft.Animation(220, ft.AnimationCurve.EASE_OUT_CUBIC),
            offset=ft.Offset(0, 0.3),
        )

    def build(self) -> ft.Control:
        return self.pill

    # ── expandable sheet ────────────────────────────────────────────────────
    def _ensure_sheet(self):
        if self._sheet_initialized:
            return

        self._count_text = ft.Text("", color=DIM, size=12)
        self._job_list = ft.ListView(
            expand=True, spacing=8,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            # Android renders no scrollbar unless `scroll` is set.
            scroll=ft.ScrollMode.ALWAYS,
        )
        self._sheet_empty = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.DOWNLOAD_DONE_ROUNDED, color=DIM, size=48),
                    ft.Text("Nothing downloading", color=DIM, size=14),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8,
            ),
            alignment=ft.Alignment(0, 0), expand=True, visible=False,
        )
        self._sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Container(
                            content=ft.Row(
                                [ft.Container(width=36, height=5, bgcolor=SURFACE_ELEVATED,
                                              border_radius=3)],
                                alignment=ft.MainAxisAlignment.CENTER,
                            ),
                            padding=ft.Padding.only(top=10, bottom=6),
                        ),
                        ft.Container(
                            content=ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("Downloads", color=TEXT, size=17,
                                                    weight=ft.FontWeight.W_600),
                                            self._count_text,
                                        ],
                                        spacing=2,
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=ft.Padding.only(left=20, right=20, top=2, bottom=6),
                        ),
                        ft.Divider(color=BORDER_SUBTLE, height=1),
                        self._sheet_empty,
                        self._job_list,
                    ],
                    spacing=0, expand=True,
                ),
                bgcolor=SURFACE,
                border_radius=ft.BorderRadius.only(top_left=20, top_right=20),
                expand=True,
            ),
            fullscreen=True,
            scrollable=False,
            show_drag_handle=False,
            draggable=True,
            use_safe_area=True,
            bgcolor=SURFACE,
        )
        self._sheet_initialized = True

    def build_sheet(self) -> ft.Control:
        self._ensure_sheet()
        return self._sheet

    def expand(self):
        def _mutate():
            self._ensure_sheet()
            self._render_sheet()
            self._sheet.open = True
        self.app.safe_update(_mutate)

    def collapse(self):
        def _mutate():
            self._ensure_sheet()
            self._sheet.open = False
        self.app.safe_update(_mutate)

    def _on_clear_finished(self):
        self.app.queue.clear_finished()
        if self.app.queue.is_idle:
            self.collapse()

    # ── rendering ───────────────────────────────────────────────────────────
    def refresh(self):
        """The single QueueController listener. Both presentations refresh from
        the same call, so the pill can never disagree with the sheet.

        Synced narrowly: this fires roughly four times a second for the whole
        length of a download, and a full page.update() per tick re-syncs every
        control in every cached tab — which is what made the entire UI churn
        while downloading. Only the pill (and the sheet, when it is actually
        open) need to move.
        """
        self.app.safe_update(self._render, target=self.pill)

    def _render(self):
        self._render_pill()
        if self._sheet_initialized and getattr(self._sheet, "open", False):
            self._render_sheet()
            try:
                self._job_list.update()
                self._count_text.update()
            except Exception:
                pass

    def _render_pill(self):
        queue   = self.app.queue
        active  = queue.active_count
        current = queue.current_job

        if active == 0:
            # Keep it mounted but collapsed: toggling `visible` alone would make
            # the layout jump, so fade and slide out first.
            self.pill.opacity = 0
            self.pill.offset  = ft.Offset(0, 0.3)
            self.pill.visible = False
            return

        self.pill.visible = True
        self.pill.opacity = 1
        self.pill.offset  = ft.Offset(0, 0)

        if current:
            pct = current.get("percent")
            self._pill_headline.value = (
                f"{active} downloading" if active > 1 else current.get("title", "Downloading")
            )
            status = current.get("status") or "Downloading"
            self._pill_detail.value = (
                f"{status} · {current.get('title', '')}" if active > 1
                else f"{status}{' · ' + current['detail'] if current.get('detail') else ''}"
            )
            fraction = (pct / 100) if isinstance(pct, (int, float)) and pct >= 0 else None
            self._pill_ring.value = fraction
            self._pill_bar.value  = fraction
        else:
            self._pill_headline.value = f"{active} queued"
            self._pill_detail.value   = "Waiting to start…"
            self._pill_ring.value = None
            self._pill_bar.value  = None

    def _render_sheet(self):
        queue = self.app.queue
        jobs  = queue.jobs
        active = queue.active_count
        finished = len(jobs) - active

        self._count_text.value = (
            f"{active} in progress"
            + (f" · {finished} finished" if finished else "")
        ) if active else (f"{finished} finished" if finished else "Nothing queued")

        self._sheet_empty.visible        = not jobs

        self._job_list.controls = [self._job_row(job) for job in jobs]

    def _job_row(self, job: dict) -> ft.Control:
        state = job.get("state", QUEUED)
        icon, tint = STATE_VISUALS.get(state, STATE_VISUALS[QUEUED])
        is_active = state not in TERMINAL_STATES
        accent = src_color(job.get("source", ""))

        art = job.get("image")
        if art:
            thumb = ft.Container(
                content=ft.Image(src=art, width=44, height=44, fit="cover",
                                 border_radius=ft.BorderRadius.all(RADIUS_THUMB)),
                width=44, height=44, border_radius=RADIUS_THUMB,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                border=ft.Border.all(1, BORDER_SUBTLE),
            )
        else:
            thumb = ft.Container(
                content=ft.Icon(icon, color=tint, size=22),
                width=44, height=44, bgcolor=SURFACE2, border_radius=RADIUS_THUMB,
                alignment=ft.Alignment(0, 0), border=ft.Border.all(1, BORDER_SUBTLE),
            )

        pct = job.get("percent")
        bar = ft.ProgressBar(
            value=(pct / 100) if isinstance(pct, (int, float)) and pct >= 0 else None,
            color=tint, bgcolor=SURFACE2, height=3,
        ) if state == DOWNLOADING else ft.Container(height=3)

        # Source identity is carried as a coloured badge, never as prose in the
        # status line — that is what keeps status strings backend-agnostic.
        badges = ft.Row(
            [
                ft.Container(
                    content=ft.Text((job.get("source") or "").upper(), color=accent, size=9,
                                    weight=ft.FontWeight.W_700),
                    bgcolor=apply_opacity(0.15, accent),
                    border_radius=4,
                    padding=ft.Padding.symmetric(horizontal=5, vertical=1),
                ),
                ft.Container(
                    content=ft.Text(job.get("quality_label", ""), color=DIM, size=9,
                                    weight=ft.FontWeight.W_700),
                    bgcolor=apply_opacity(0.10, DIM),
                    border_radius=4,
                    padding=ft.Padding.symmetric(horizontal=5, vertical=1),
                ),
            ],
            spacing=5, tight=True,
        )

        status_line = job.get("status", "")
        if job.get("detail"):
            status_line = f"{status_line} · {job['detail']}"

        trailing = []
        if state in (FAILED, CANCELLED):
            trailing.append(ft.IconButton(
                icon=ft.Icons.REFRESH_ROUNDED, icon_color=CYAN, icon_size=18,
                tooltip="Retry",
                on_click=lambda e, jid=job["id"]: self._retry(jid),
            ))
        trailing.append(ft.IconButton(
            icon=ft.Icons.CLOSE_ROUNDED, icon_color=DIM, icon_size=18,
            tooltip="Cancel" if is_active else "Dismiss",
            on_click=lambda e, jid=job["id"]: self._remove(jid),
        ))

        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            thumb,
                            ft.Column(
                                [
                                    ft.Text(job.get("title", "Unknown"), color=TEXT, size=14,
                                            weight=ft.FontWeight.W_600, max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS),
                                    ft.Row(
                                        [
                                            ft.Text(job.get("artist", ""), color=DIM, size=12,
                                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                                                    expand=True),
                                            badges,
                                        ],
                                        spacing=6,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    ft.Text(status_line, color=tint, size=10, max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS),
                                ],
                                spacing=2, expand=True,
                            ),
                            ft.Row(trailing, spacing=0, tight=True),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bar,
                ],
                spacing=6,
            ),
            bgcolor=apply_opacity(0.10, tint) if state == DOWNLOADING else SURFACE2,
            border=ft.Border.all(1, apply_opacity(0.3, tint) if state == DOWNLOADING else BORDER_SUBTLE),
            border_radius=RADIUS_CARD,
            padding=ft.Padding.symmetric(horizontal=10, vertical=9),
        )

    def _retry(self, job_id: str):
        if self.app.queue.retry(job_id):
            self.app.show_snackbar("Retrying download…", icon=ft.Icons.REFRESH_ROUNDED)

    def _remove(self, job_id: str):
        self.app.queue.remove(job_id)
