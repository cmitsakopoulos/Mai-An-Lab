import asyncio
import functools
import logging
import flet as ft
from ui.tokens import BG, TEXT, DIM

logger = logging.getLogger(__name__)

class JobCancelledException(Exception):
    pass

class ErrorBoundary:
    def __init__(self, page, on_restart=None):
        self.page = page
        self.on_restart = on_restart
        self._error_view = ft.Container(
            content=ft.Column([
                ft.Icon(ft.Icons.ERROR_OUTLINE, color="#FF4444", size=64),
                ft.Text("Something went wrong", size=20, weight=ft.FontWeight.W_700, color=TEXT),
                ft.Text("Tap to restart", size=14, color=DIM),
            ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment(0, 0),
            visible=False,
            bgcolor=BG,
            expand=True,
            on_click=lambda _: self.restart()
        )
        
    def capture(self, fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                if asyncio.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                else:
                    return fn(*args, **kwargs)
            except Exception as e:
                logger.exception("Captured error")
                self._show_error(e)
        return wrapper
        
    def _show_error(self, e=None):
        if e:
            # Add selectable error detail if provided
            detail = ft.Text(f"Error: {e}", color="#FF4444", size=11, selectable=True, text_align=ft.TextAlign.CENTER)
            self._error_view.content.controls.insert(2, detail)
            
        self._error_view.visible = True
        try:
            self.page.update()
        except:
            pass

    def restart(self):
        self._error_view.visible = False
        try:
            self.page.update()
        except:
            pass
        if self.on_restart:
            if asyncio.iscoroutinefunction(self.on_restart):
                asyncio.create_task(self.on_restart())
            else:
                self.on_restart()
