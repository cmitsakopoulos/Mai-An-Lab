"""Settings → Auto-play: what the listener has taught auto-play.

  • Songs removed from its recommendations: auto-play never suggests them again
    (utils/autoplay.py hides them on removal); here they can be restored.
    Nothing is ever hidden from the library itself.
  • Genre hops it has stopped taking, after enough of the songs right after a
    hop were removed or skipped; here they can be reset.
"""
import os
import time

import flet as ft

from ui.tokens import BORDER, CYAN, DIM, SURFACE2, TEXT
from utils.genre_graph import node_display


class HiddenTracksPane:
    """Built per visit; the list loads asynchronously from the DB."""

    def __init__(self, app):
        self.app = app
        self._list = ft.Column(spacing=0, tight=True)
        self._count = ft.Text("", color=DIM, size=12)
        self._restore_all = ft.TextButton(
            content=ft.Text("Restore all", color=CYAN, size=13, weight=ft.FontWeight.W_600),
            on_click=lambda _e: self.app.page.run_task(self._restore, None),
            visible=False,
        )
        self._hops = ft.Column(spacing=0, tight=True)
        self._hop_count = ft.Text("", color=DIM, size=12)
        self._reset_all = ft.TextButton(
            content=ft.Text("Reset all", color=CYAN, size=13, weight=ft.FontWeight.W_600),
            on_click=lambda _e: self.app.page.run_task(self._unblock, None),
            visible=False,
        )

    def build(self) -> ft.Control:
        self.app.page.run_task(self._load)
        return ft.Column(
            [
                ft.Text(
                    "What auto-play has learned from you. Songs you removed from its "
                    "suggestions aren't recommended again (they stay in your library), "
                    "and genre jumps you kept turning down aren't made again.",
                    color=DIM, size=12,
                ),
                self._card(
                    ft.Icons.VISIBILITY_OFF_ROUNDED, "Hidden from Auto-play", self._restore_all,
                    [self._count, self._list],
                ),
                self._card(
                    ft.Icons.CALL_SPLIT_ROUNDED, "Blocked genre hops", self._reset_all,
                    [self._hop_count, self._hops],
                ),
            ],
            spacing=20,
        )

    @staticmethod
    def _card(icon, title, action, body) -> ft.Control:
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(icon, color=CYAN, size=20),
                            ft.Text(title, weight=ft.FontWeight.BOLD, color=TEXT, size=14,
                                    expand=True),
                            action,
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    *body,
                ],
                spacing=8,
                tight=True,
            ),
            padding=16,
            bgcolor=SURFACE2,
            border_radius=10,
            border=ft.Border.all(1, BORDER),
        )

    async def _load(self):
        try:
            rows = await self.app.db_manager.get_autoplay_hidden()
        except Exception:
            rows = []
        hops = self.app.autoplay.blocked_hops()
        self.app.safe_update(lambda: (self._render(rows), self._render_hops(hops)))

    def _render_hops(self, hops: list[dict]):
        n = len(hops)
        self._hop_count.value = (
            f"Auto-play no longer moves between these genres. {n} blocked." if n
            else "None. When you remove or skip several songs right after auto-play "
                 "changes genre, it stops making that jump and it shows up here."
        )
        self._reset_all.visible = n > 1
        self._hops.controls = [self._hop_row(h) for h in hops]

    def _hop_row(self, h: dict) -> ft.Control:
        pair = (h["src"], h["dst"])
        return ft.Container(
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(f"{node_display(pair[0])} → {node_display(pair[1])}",
                                    color=TEXT, size=13, weight=ft.FontWeight.W_500,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(f"Blocked after you turned down {h['rejects']} "
                                    f"songs by {h['artists']} artists",
                                    color=DIM, size=11, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=2, expand=True, tight=True,
                    ),
                    ft.TextButton(
                        content=ft.Text("Reset", color=CYAN, size=13, weight=ft.FontWeight.W_600),
                        on_click=lambda _e, p=pair: self.app.page.run_task(self._unblock, [p]),
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(vertical=6),
            border=ft.Border.only(top=ft.BorderSide(1, BORDER)),
        )

    async def _unblock(self, pairs):
        await self.app.autoplay.unblock_hops(pairs)
        await self._load()

    def _render(self, rows: list[dict]):
        n = len(rows)
        self._count.value = (
            f"{n} song{'s' if n != 1 else ''} hidden" if n
            else "Nothing hidden. Remove a suggestion from the queue and it shows up here."
        )
        self._restore_all.visible = n > 1
        self._list.controls = [self._row(r) for r in rows]

    def _row(self, r: dict) -> ft.Control:
        path = r.get("path") or ""
        title = r.get("title") or os.path.splitext(os.path.basename(path))[0]
        detail = " · ".join(x for x in (r.get("artist"), r.get("album")) if x)
        if not r.get("title"):
            detail = "No longer in your library"
        when = time.strftime("%d %b %Y", time.localtime(float(r.get("hidden_at") or 0)))
        return ft.Container(
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(title, color=TEXT, size=13, weight=ft.FontWeight.W_500,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(f"{detail} · hidden {when}" if detail else f"Hidden {when}",
                                    color=DIM, size=11, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=2, expand=True, tight=True,
                    ),
                    ft.TextButton(
                        content=ft.Text("Restore", color=CYAN, size=13, weight=ft.FontWeight.W_600),
                        on_click=lambda _e, p=path: self.app.page.run_task(self._restore, [p]),
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(vertical=6),
            border=ft.Border.only(top=ft.BorderSide(1, BORDER)),
        )

    async def _restore(self, paths: list[str] | None):
        await self.app.autoplay.unhide(paths)
        await self._load()
