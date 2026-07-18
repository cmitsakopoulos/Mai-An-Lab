import os
import sys
import platform
import logging
import asyncio
import flet as ft
import flet.canvas as cv
from ui.tokens import (
    BG, SURFACE, SURFACE2, CYAN, AMBER, TEXT, DIM, BORDER, apply_opacity
)
from ui.widgets import OnyxButton, HubSettingItem, pick_folder

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logger = logging.getLogger(__name__)


class EqualizerCurve(ft.Container):
    def __init__(self, bands, min_db=-15.0, max_db=15.0, on_band_change=None, eq_enabled_check=None):
        super().__init__()
        self.bands = bands  # list of {"center_frequency": float, "gain": float}
        self.min_db = min_db
        self.max_db = max_db
        self.on_band_change = on_band_change
        self.eq_enabled_check = eq_enabled_check
        
        self.canvas_width = 350.0  # Fallback initial width
        self.canvas_height = 180.0 # Fallback initial height
        
        self.active_band_idx = None
        
        self.canvas = cv.Canvas(
            expand=True,
            on_resize=self._handle_resize
        )
        
        self.content = ft.GestureDetector(
            content=self.canvas,
            on_pan_start=self._handle_pan_start,
            on_pan_update=self._handle_pan_update,
            on_pan_end=self._handle_pan_end,
            on_tap_down=self._handle_tap_down
        )
        
        self.bgcolor = ft.Colors.BLACK
        self.border_radius = 10
        self.border = ft.Border.all(1, ft.Colors.with_opacity(0.15, ft.Colors.WHITE))
        self.padding = 10
        self.height = 200  # Total height of the EQ panel
        
    def _handle_resize(self, e: cv.CanvasResizeEvent):
        self.canvas_width = e.width
        self.canvas_height = e.height
        self.redraw()
        
    def _get_band_x(self, idx, width):
        margin = 40.0
        n_bands = len(self.bands)
        if n_bands <= 1:
            return width / 2
        return margin + idx * (width - 2 * margin) / (n_bands - 1)
        
    def _gain_to_y(self, gain, height):
        margin = 25.0
        usable_h = height - 2 * margin
        db_range = self.max_db - self.min_db
        normalized = (gain - self.min_db) / db_range
        return height - margin - normalized * usable_h
        
    def _y_to_gain(self, y, height):
        margin = 25.0
        usable_h = height - 2 * margin
        db_range = self.max_db - self.min_db
        normalized = (height - margin - y) / usable_h
        gain = self.min_db + normalized * db_range
        return max(self.min_db, min(self.max_db, gain))
        
    def redraw(self):
        try:
            shapes = []
            width = self.canvas_width
            height = self.canvas_height
            
            # 1. Background Grid & Labels
            center_y = self._gain_to_y(0.0, height)
            top_grid_y = self._gain_to_y(10.0, height)
            bottom_grid_y = self._gain_to_y(-10.0, height)
            
            # Center line (0dB)
            shapes.append(cv.Line(
                0, center_y, width, center_y,
                paint=ft.Paint(color=ft.Colors.with_opacity(0.12, ft.Colors.WHITE), stroke_width=1)
            ))
            # Top grid line (+10dB)
            shapes.append(cv.Line(
                0, top_grid_y, width, top_grid_y,
                paint=ft.Paint(color=ft.Colors.with_opacity(0.06, ft.Colors.WHITE), stroke_width=1, stroke_dash_pattern=[4, 4])
            ))
            # Bottom grid line (-10dB)
            shapes.append(cv.Line(
                0, bottom_grid_y, width, bottom_grid_y,
                paint=ft.Paint(color=ft.Colors.with_opacity(0.06, ft.Colors.WHITE), stroke_width=1, stroke_dash_pattern=[4, 4])
            ))
            
            # Grid Y-axis labels
            shapes.append(cv.Text(5, top_grid_y - 12, "+10 dB", style=ft.TextStyle(color=ft.Colors.with_opacity(0.4, ft.Colors.WHITE), size=9)))
            shapes.append(cv.Text(5, center_y - 12, "0 dB", style=ft.TextStyle(color=ft.Colors.with_opacity(0.4, ft.Colors.WHITE), size=9)))
            shapes.append(cv.Text(5, bottom_grid_y - 12, "-10 dB", style=ft.TextStyle(color=ft.Colors.with_opacity(0.4, ft.Colors.WHITE), size=9)))
            
            points = []
            for i, band in enumerate(self.bands):
                x = self._get_band_x(i, width)
                y = self._gain_to_y(band["gain"], height)
                points.append((x, y))
                
            if points:
                is_enabled = self.eq_enabled_check() if self.eq_enabled_check else True
                accent_color = ft.Colors.CYAN if is_enabled else ft.Colors.with_opacity(0.3, ft.Colors.WHITE)
                fill_opacity = 0.18 if is_enabled else 0.05
                
                # Fill Area
                fill_path = cv.Path([cv.Path.MoveTo(points[0][0], center_y)])
                fill_path.elements.append(cv.Path.LineTo(points[0][0], points[0][1]))
                for i in range(len(points) - 1):
                    p0 = points[i]
                    p1 = points[i+1]
                    cx1 = p0[0] + (p1[0] - p0[0]) / 2.0
                    cy1 = p0[1]
                    cx2 = p0[0] + (p1[0] - p0[0]) / 2.0
                    cy2 = p1[1]
                    fill_path.elements.append(cv.Path.CubicTo(cx1, cy1, cx2, cy2, p1[0], p1[1]))
                fill_path.elements.append(cv.Path.LineTo(points[-1][0], center_y))
                fill_path.elements.append(cv.Path.Close())
                
                shapes.append(cv.Path(
                    elements=fill_path.elements,
                    paint=ft.Paint(
                        color=ft.Colors.with_opacity(fill_opacity, accent_color),
                        style=ft.PaintingStyle.FILL
                    )
                ))
                
                # Stroke Line
                stroke_path = cv.Path([cv.Path.MoveTo(points[0][0], points[0][1])])
                for i in range(len(points) - 1):
                    p0 = points[i]
                    p1 = points[i+1]
                    cx1 = p0[0] + (p1[0] - p0[0]) / 2.0
                    cy1 = p0[1]
                    cx2 = p0[0] + (p1[0] - p0[0]) / 2.0
                    cy2 = p1[1]
                    stroke_path.elements.append(cv.Path.CubicTo(cx1, cy1, cx2, cy2, p1[0], p1[1]))
                    
                shapes.append(cv.Path(
                    elements=stroke_path.elements,
                    paint=ft.Paint(
                        color=accent_color,
                        stroke_width=2.5,
                        style=ft.PaintingStyle.STROKE,
                        anti_alias=True
                    )
                ))
                
                # Nodes & Labels
                for i, (x, y) in enumerate(points):
                    band = self.bands[i]
                    freq = band["center_frequency"]
                    freq_str = f"{freq/1000:.1f}k" if freq >= 1000 else f"{int(freq)}"
                    
                    shapes.append(cv.Circle(
                        x, y, radius=5,
                        paint=ft.Paint(color=accent_color, style=ft.PaintingStyle.FILL)
                    ))
                    shapes.append(cv.Circle(
                        x, y, radius=1.5,
                        paint=ft.Paint(color=ft.Colors.BLACK, style=ft.PaintingStyle.FILL)
                    ))
                    
                    shapes.append(cv.Text(
                        x - 12, height - 18, freq_str,
                        style=ft.TextStyle(color=ft.Colors.with_opacity(0.5, ft.Colors.WHITE), size=9)
                    ))
                    
            self.canvas.shapes = shapes
            if self.canvas.page:
                self.canvas.update()
        except BaseException:
            pass
        
    def _handle_tap_down(self, e: ft.TapEvent):
        if self.eq_enabled_check and not self.eq_enabled_check():
            return
        self._find_and_update_closest_band(e.local_position.x, e.local_position.y)
        
    def _handle_pan_start(self, e: ft.DragStartEvent):
        if self.eq_enabled_check and not self.eq_enabled_check():
            return
        x, y = e.local_position.x, e.local_position.y
        self.active_band_idx = None
        min_dist = float('inf')
        for i in range(len(self.bands)):
            bx = self._get_band_x(i, self.canvas_width)
            by = self._gain_to_y(self.bands[i]["gain"], self.canvas_height)
            dist = ((x - bx)**2 + (y - by)**2)**0.5
            if dist < min_dist and dist < 35.0:  # 35px tolerance
                min_dist = dist
                self.active_band_idx = i
                
    def _handle_pan_update(self, e: ft.DragUpdateEvent):
        if self.eq_enabled_check and not self.eq_enabled_check():
            return
        if self.active_band_idx is not None:
            y = e.local_position.y
            gain = self._y_to_gain(y, self.canvas_height)
            self.bands[self.active_band_idx]["gain"] = gain
            self.redraw()
            if self.on_band_change:
                self.on_band_change(self.active_band_idx, gain)
                
    def _handle_pan_end(self, e):
        self.active_band_idx = None
        
    def _find_and_update_closest_band(self, x, y):
        closest_idx = None
        min_dist_x = float('inf')
        for i in range(len(self.bands)):
            bx = self._get_band_x(i, self.canvas_width)
            dist_x = abs(x - bx)
            if dist_x < min_dist_x:
                min_dist_x = dist_x
                closest_idx = i
                
        if closest_idx is not None and min_dist_x < 45.0:
            gain = self._y_to_gain(y, self.canvas_height)
            self.bands[closest_idx]["gain"] = gain
            self.redraw()
            if self.on_band_change:
                self.on_band_change(closest_idx, gain)


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
        self._qobuz_app_id_field = ft.TextField(
            label="Qobuz App ID",
            hint_text="Default: 312369995",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._qobuz_app_secret_field = ft.TextField(
            label="Qobuz App Secret",
            hint_text="Default: e79f8b9be485692b0e5f9dd895826368",
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
            on_select=lambda e: (self._save_general_settings(), self._on_appearance_change(e)),
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
            on_select=lambda e: (self._save_general_settings(), self._on_appearance_change(e)),
            **common_style
        )

        # Landing Page Customization
        self._show_most_listened_switch = ft.Switch(value=True, active_color=CYAN, on_change=self._on_appearance_change)
        self._show_library_stats_switch  = ft.Switch(value=True, active_color=CYAN, on_change=self._on_appearance_change)

        # Library Pane Customization & Jarvis
        self._show_jarvis_switch = ft.Switch(value=True, active_color=CYAN, on_change=self._on_appearance_change)
        self._show_network_switch = ft.Switch(value=False, active_color=CYAN, on_change=self._on_appearance_change)
        self._show_playlists_switch = ft.Switch(value=True, active_color=CYAN, on_change=self._on_appearance_change)
        self._show_artists_switch = ft.Switch(value=True, active_color=CYAN, on_change=self._on_appearance_change)
        self._show_albums_switch = ft.Switch(value=True, active_color=CYAN, on_change=self._on_appearance_change)
        self._show_tracks_switch = ft.Switch(value=True, active_color=CYAN, on_change=self._on_appearance_change)

        # Play Similar temperature slider. Default 0 = deterministic arg-max
        # (see streamrip_api.get_walk_params for why); the control stays so
        # variety is opt-in rather than imposed.
        self._temp_slider = ft.Slider(
            min=0.0,
            max=0.8,
            divisions=80,
            label="{value}",
            value=0.0,
            active_color=CYAN,
            on_change=self._on_temp_change,
        )
        self._temp_value_text = ft.Text("0.00", color=TEXT, size=13, weight=ft.FontWeight.W_700)

        # Play Similar mmr_lambda slider
        self._mmr_slider = ft.Slider(
            min=0.0,
            max=0.4,
            divisions=40,
            label="{value}",
            value=0.15,
            active_color=CYAN,
            on_change=self._on_mmr_change,
        )
        self._mmr_value_text = ft.Text("0.15", color=TEXT, size=13, weight=ft.FontWeight.W_700)

        # DSP Controls
        self._dynamism_switch = ft.Switch(value=False, active_color=CYAN, on_change=self._on_dynamism_change)
        self._dynamism_unavailable_text = ft.Text(
            "⚠ Requires DSP analysis — run the Jarvis Analyser first.",
            color=AMBER, size=11, visible=False,
        )
        self._dynamism_boost_card = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.FLASH_ON_ROUNDED, color=AMBER, size=16),
                ft.Column([
                    ft.Text("Active Track Boost", color=TEXT, size=12, weight=ft.FontWeight.BOLD),
                    ft.Text("Acoustic energy enhancement active", color=DIM, size=10),
                ], spacing=2, expand=True),
                ft.Text("+0.0 dB", color=AMBER, size=13, weight=ft.FontWeight.W_700),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            bgcolor=apply_opacity(0.04, TEXT),
            border_radius=8,
            border=ft.Border.all(1, BORDER),
            visible=False,
        )
        self._equaliser_switch = ft.Switch(value=False, active_color=CYAN, on_change=self._on_equaliser_change)
        
        # Haptics Controls
        self._haptic_feedback_switch = ft.Switch(value=True, active_color=CYAN, on_change=self._on_haptic_feedback_change)
        self._haptic_eq_drag_dropdown = ft.Dropdown(
            label="EQ node drag & sliders",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            on_select=self._on_haptic_intensity_change,
            options=[
                ft.dropdown.Option("none", "None"),
                ft.dropdown.Option("light", "Light"),
                ft.dropdown.Option("medium", "Medium"),
                ft.dropdown.Option("heavy", "Heavy"),
                ft.dropdown.Option("vibrate", "Vibrate (Long)"),
            ]
        )
        self._haptic_swipe_queue_dropdown = ft.Dropdown(
            label="Swipe track to Queue",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            on_select=self._on_haptic_intensity_change,
            options=[
                ft.dropdown.Option("none", "None"),
                ft.dropdown.Option("light", "Light"),
                ft.dropdown.Option("medium", "Medium"),
                ft.dropdown.Option("heavy", "Heavy"),
                ft.dropdown.Option("vibrate", "Vibrate (Long)"),
            ]
        )
        self._haptic_swipe_dismiss_dropdown = ft.Dropdown(
            label="Swipe to Remove from Queue",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            on_select=self._on_haptic_intensity_change,
            options=[
                ft.dropdown.Option("none", "None"),
                ft.dropdown.Option("light", "Light"),
                ft.dropdown.Option("medium", "Medium"),
                ft.dropdown.Option("heavy", "Heavy"),
                ft.dropdown.Option("vibrate", "Vibrate (Long)"),
            ]
        )
        self._haptic_long_press_dropdown = ft.Dropdown(
            label="Long press track",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            on_select=self._on_haptic_intensity_change,
            options=[
                ft.dropdown.Option("none", "None"),
                ft.dropdown.Option("light", "Light"),
                ft.dropdown.Option("medium", "Medium"),
                ft.dropdown.Option("heavy", "Heavy"),
                ft.dropdown.Option("vibrate", "Vibrate (Long)"),
            ]
        )
        self._haptic_network_tap_dropdown = ft.Dropdown(
            label="Network node tap & inspect",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            on_select=self._on_haptic_intensity_change,
            options=[
                ft.dropdown.Option("none", "None"),
                ft.dropdown.Option("selection", "Selection Click"),
                ft.dropdown.Option("light", "Light"),
                ft.dropdown.Option("medium", "Medium"),
                ft.dropdown.Option("heavy", "Heavy"),
            ]
        )
        self._haptic_network_reseed_dropdown = ft.Dropdown(
            label="Network reseed graph",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            on_select=self._on_haptic_intensity_change,
            options=[
                ft.dropdown.Option("none", "None"),
                ft.dropdown.Option("light", "Light"),
                ft.dropdown.Option("medium", "Medium"),
                ft.dropdown.Option("heavy", "Heavy"),
                ft.dropdown.Option("vibrate", "Vibrate (Long)"),
            ]
        )
        self._haptic_network_walk_dropdown = ft.Dropdown(
            label="Network walk step traversal",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            on_select=self._on_haptic_intensity_change,
            options=[
                ft.dropdown.Option("none", "None"),
                ft.dropdown.Option("selection", "Selection Click"),
                ft.dropdown.Option("light", "Light"),
                ft.dropdown.Option("medium", "Medium"),
                ft.dropdown.Option("heavy", "Heavy"),
            ]
        )
        self._eq_preset_type_radio = ft.RadioGroup(
            content=ft.Row([
                ft.Radio(value="system", label="System Presets", fill_color=CYAN, label_style=ft.TextStyle(color=TEXT, size=12)),
                ft.Radio(value="custom", label="Custom Presets", fill_color=CYAN, label_style=ft.TextStyle(color=TEXT, size=12)),
            ], spacing=20),
            value="system",
            on_change=self._on_preset_type_change,
        )
        self._eq_preset_dropdown = ft.Dropdown(
            label="Equalizer Preset",
            on_select=self._on_eq_preset_select,
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
        )
        self._eq_sliders_container = ft.Column(spacing=15)
        self._custom_preset_name_field = ft.TextField(
            label="Save Custom Preset",
            hint_text="Enter preset name",
            bgcolor=SURFACE2,
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=13),
            label_style=ft.TextStyle(color=CYAN, size=11),
            border_radius=10,
            expand=True,
        )
        self._eq_bands = [
            {"index": 0, "center_frequency": 60.0, "gain": 0.0},
            {"index": 1, "center_frequency": 230.0, "gain": 0.0},
            {"index": 2, "center_frequency": 910.0, "gain": 0.0},
            {"index": 3, "center_frequency": 4000.0, "gain": 0.0},
            {"index": 4, "center_frequency": 14000.0, "gain": 0.0},
        ]
        self._eq_min_db = -15.0
        self._eq_max_db = 15.0
        self._eq_curve = EqualizerCurve(
            bands=self._eq_bands,
            min_db=self._eq_min_db,
            max_db=self._eq_max_db,
            on_band_change=self._on_curve_band_change,
            eq_enabled_check=lambda: self._equaliser_switch.value
        )
        self._custom_presets_list_container = ft.Column(spacing=8)
        self._custom_preset_save_row = ft.Row([
            self._custom_preset_name_field,
            OnyxButton("SAVE", on_tap=self._on_save_custom_preset, width=90),
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        self._apply_visuals_btn = OnyxButton("APPLY VISUALS", ft.Icons.PALETTE, on_tap=lambda _: self._save_appearance_settings(), visible=False)
        self._apply_visuals_container = ft.Container(
            content=self._apply_visuals_btn,
            bottom=10,
            left=0,
            right=0,
        )

        self._scroll_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=12,
        )
        self.main_content = ft.Container(
            content=ft.Stack(
                [
                    self._scroll_column,
                    self._apply_visuals_container,
                ],
                expand=True,
            ),
            expand=True, 
            padding=ft.Padding.symmetric(horizontal=28, vertical=20),
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
        if getattr(self, "_enrichment_wizard_pane", None) and self._enrichment_wizard_pane.step == 5:
            self._enrichment_wizard_pane = None
        self._apply_visuals_btn.visible = False
        self._scroll_column.controls = [
            ft.Text("Settings", size=32, weight=ft.FontWeight.W_900, color=TEXT),
            ft.Container(height=16),
            
            # Set-up Section
            ft.Text("SET-UP", size=11, color=CYAN, weight=ft.FontWeight.W_800),
            HubSettingItem(ft.Icons.LOCK_PERSON_ROUNDED, "Authentication", "Qobuz credentials & tokens", 
                           on_tap=lambda _: self._show_sub_page("Account", self._build_auth_group())),
            HubSettingItem(ft.Icons.STORAGE_ROUNDED, "Storage & Paths", "Library and download locations", 
                           on_tap=lambda _: self._show_sub_page("Storage", self._build_storage_group())),
            
            ft.Divider(color=BORDER, height=30),
            
            # Customisation Section
            ft.Text("CUSTOMISATION", size=11, color=CYAN, weight=ft.FontWeight.W_800),
            HubSettingItem(ft.Icons.PALETTE_ROUNDED, "Appearance", "Accent colors and UI behavior",
                           on_tap=lambda _: self._show_sub_page("Appearance", self._build_appearance_group())),
            HubSettingItem(ft.Icons.GRAPHIC_EQ_ROUNDED, "Audio & DSP", "Equalizer, presets & dynamism enhancement",
                           on_tap=lambda _: self._show_sub_page("Audio & DSP", self._build_audio_dsp_group())),
            HubSettingItem(ft.Icons.VIBRATION_ROUNDED, "Haptic Feedback", "Vibration settings and intensity controls",
                           on_tap=lambda _: self._show_sub_page("Haptic Feedback", self._build_haptics_group())),
            
            ft.Divider(color=BORDER, height=30),
            
            # Developer Tools Section
            ft.Text("DEVELOPER TOOLS", size=11, color=CYAN, weight=ft.FontWeight.W_800),
            HubSettingItem(ft.Icons.SHIELD_OUTLINED, "Permissions", "Notifications, audio, and file access",
                           on_tap=lambda _: self._show_sub_page("Permissions", self._build_permissions_group())),
            HubSettingItem(ft.Icons.DNS_ROUNDED, "Database Management", "Wipe database, compute DSP & PCA",
                           on_tap=lambda _: self._show_sub_page("Database Management", self._build_database_management_group())),
            HubSettingItem(ft.Icons.TERMINAL_ROUNDED, "Advanced", "Edit TOML config and data maintenance", 
                           on_tap=lambda _: self._show_sub_page("Advanced", self._build_advanced_group())),
            
            ft.Divider(color=BORDER, height=30),
            
            # About Section
            HubSettingItem(ft.Icons.INFO_OUTLINE_ROUNDED, "About", "App version and developer info", 
                           on_tap=lambda _: self._show_sub_page("About", self._build_about_group())),
        ]
        self.app.safe_update(lambda: None)

    def _show_sub_page(self, title: str, content_control: ft.Control):
        """Swaps the hub for a specific settings group."""
        if title != "Appearance":
            self._apply_visuals_btn.visible = False
        self._scroll_column.controls = [
            ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK_IOS_NEW_ROUNDED, icon_color=CYAN, icon_size=16, 
                              on_click=lambda _: self._show_hub()),
                ft.Text(title, size=18, weight=ft.FontWeight.W_700, color=TEXT, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, expand=True),
            ], spacing=10),
            ft.Container(height=15),
            content_control
        ]
        self.app.safe_update(lambda: None)

    # --- Sub-Page Builders ---

    def _build_auth_group(self):
        return ft.Column([
            ft.Text("Enter your Qobuz credentials, App ID, and App Secret.", color=DIM, size=12),
            self._qobuz_user_id_field,
            self._qobuz_token_field,
            self._qobuz_app_id_field,
            self._qobuz_app_secret_field,
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
            ft.Text("Library Tabs Visibility", color=CYAN, size=12, weight=ft.FontWeight.BOLD),
            ft.Row([
                self._show_network_switch, 
                ft.Text("Acoustic Network", color=TEXT, size=12),
                ft.Container(
                    content=ft.Text("EXPERIMENTAL", color=BG, size=9, weight=ft.FontWeight.W_800),
                    bgcolor=AMBER,
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    border_radius=4,
                )
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self._show_playlists_switch, ft.Text("Playlists", color=TEXT, size=12)], spacing=10),
            ft.Row([self._show_artists_switch, ft.Text("Artists", color=TEXT, size=12)], spacing=10),
            ft.Row([self._show_albums_switch, ft.Text("Albums", color=TEXT, size=12)], spacing=10),
            ft.Row([self._show_tracks_switch, ft.Text("Tracks", color=TEXT, size=12)], spacing=10),
            ft.Divider(color=BORDER, height=20),
            ft.Text("AI Companion", color=CYAN, size=12, weight=ft.FontWeight.BOLD),
            ft.Row([self._show_jarvis_switch, ft.Text("Enable Jarvis Tab", color=TEXT, size=12)], spacing=10),
            ft.Divider(color=BORDER, height=20),
            ft.Text("Accent Color", color=CYAN, size=12, weight=ft.FontWeight.BOLD),
            self._build_color_selector(mode="accent"),
            ft.Container(height=70),
        ], spacing=20)

    def _build_database_management_group(self):
        return ft.Column([
            ft.Text("Database Maintenance", weight=ft.FontWeight.BOLD, color=DIM),
            ft.Text("Wipe or reset specific tables and indexes in the local application database. Your actual music files will NOT be touched.", color=DIM, size=12),
            ft.Row([
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
                ft.TextButton("Enrich Metadata", icon=ft.Icons.AUTO_FIX_HIGH_ROUNDED, on_click=self._on_launch_enrichment_wizard_click),
                ft.TextButton("Wipe DB", icon=ft.Icons.DELETE_FOREVER, icon_color="#FF4444", on_click=lambda _: self._on_wipe_db_click()),
            ], wrap=True, spacing=10),
            ft.Divider(color=BORDER, height=20),
            ft.Text("Compute Pipeline", weight=ft.FontWeight.BOLD, color=DIM),
            ft.Text(
                "Run the DSP / graph / PCA pipeline manually instead of going through Jarvis.",
                color=DIM, size=12,
            ),
            ft.Row([
                ft.TextButton(
                    "Commence/Continue DSP Compute",
                    icon=ft.Icons.PLAY_ARROW_ROUNDED,
                    on_click=self._on_compute_dsp_click,
                ),
                ft.TextButton(
                    "Recompute All DSP Features",
                    icon=ft.Icons.REPLAY_ROUNDED,
                    on_click=self._on_recompute_all_dsp_click,
                ),
                ft.TextButton(
                    "Recompute PCA",
                    icon=ft.Icons.SCATTER_PLOT_ROUNDED,
                    on_click=self._on_recompute_pca_click,
                ),
            ], wrap=True, spacing=10),
        ], spacing=15)


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
                        "Because this application runs as a secure production release build, Android isolates all internal databases, config profiles, and search histories inside a restricted sandbox (/data/data/...).\n\n"
                        "On every startup the app automatically writes a snapshot (mai_an_lab_state_latest.zip) to your library folder, keeping it in sync with your current state. "
                        "The desktop offload script picks this up automatically — no manual export required.\n\n"
                        "You can still manually Export/Import below for ad-hoc use or to restore from a specific point-in-time backup.",
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
            ft.Text("Play Similar / Discovery Settings", weight=ft.FontWeight.BOLD, color=CYAN),
            ft.Text("Variety (Temperature)", weight=ft.FontWeight.BOLD, color=TEXT, size=14),
            ft.Text("Controls the random softmax exploration of similarity walks. 0.0 is deterministic (always same transitions), while higher values add variety.", color=DIM, size=12),
            ft.Row([
                ft.Container(content=self._temp_slider, expand=True),
                ft.Container(content=self._temp_value_text, margin=ft.Margin.only(right=10)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(height=5),
            ft.Text("Avoid Near-Duplicates (MMR)", weight=ft.FontWeight.BOLD, color=TEXT, size=14),
            ft.Text("Applies a Maximal-Marginal-Relevance penalty to suppress remixes, alternate mixes, or duplicates of already played/queued tracks.", color=DIM, size=12),
            ft.Row([
                ft.Container(content=self._mmr_slider, expand=True),
                ft.Container(content=self._mmr_value_text, margin=ft.Margin.only(right=10)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(color=BORDER, height=40),
            ft.Text("Cache Maintenance", weight=ft.FontWeight.BOLD, color=DIM),
            ft.Row([
                ft.TextButton("Album Cache", icon=ft.Icons.IMAGE_ROUNDED, on_click=lambda _: self.app.clear_album_artwork_cache()),
                ft.TextButton("Preview Cache", icon=ft.Icons.MUSIC_NOTE_ROUNDED, on_click=lambda _: self.app.clear_preview_cache()),
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
                    ft.Text("Version 1.3.0", color=DIM, size=14),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                alignment=ft.Alignment(0, 0),
                padding=ft.Padding.only(bottom=20),
            ),
            ft.Text("Summary", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text("A deployment friendly restructure of Streamrip (Qobuz only), packaged with Flet alongside custom Flutter (audio engine) extensions.", color=DIM, size=12),
            ft.Divider(color=BORDER, height=30),
            ft.Text("What's New in 1.3.0", weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Column([
                ft.Text("• EQ coupled with DSP optimisation", color=DIM, size=12),
                ft.Text("• UI improvements / simplifications", color=DIM, size=12),
                ft.Text("• Qobuz connection improvements + UI", color=DIM, size=12),
                ft.Text("• Better search capabilities", color=DIM, size=12),
                ft.Text("• Improvements to track similarity search", color=DIM, size=12),
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
                ft.TextButton("Developer", icon=ft.Icons.PERSON_ROUNDED, on_click=lambda _: (
                    self.app.play_success_notification(),
                    self.app.show_snackbar("Contact: mitsacopoulos@gmail.com")
                )),
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

    # ── Manual DSP / PCA pipeline ───────────────────────────────────────────
    def _on_compute_dsp_click(self, _e):
        self.page.run_task(self._do_compute_dsp)

    async def _do_compute_dsp(self):
        from utils import track_graph as tg
        import sys
        if hasattr(sys, "getandroidapilevel") and audio_engine.audio_service is None:
            self.app.show_snackbar(
                "Audio service not ready — native analyser unavailable.",
                color="#FF4444",
            )
            return
        try:
            missing = await self.app.db_manager.get_tracks_missing_features(tg.FEATURES_VERSION)
        except Exception as exc:
            logger.exception("Advanced: missing-feature query failed: %s", exc)
            self.app.show_snackbar(f"DSP query failed: {exc}", color="#FF4444")
            return

        if missing:
            self.app.show_snackbar(
                f"Analysing {len(missing)} tracks — this can take a while.",
                icon=ft.Icons.GRAPHIC_EQ_ROUNDED,
            )
            try:
                await tg.bulk_analyze_library(
                    self.app.db_manager,
                    audio_engine.audio_service,
                )
            except Exception as exc:
                logger.exception("Advanced: bulk_analyze_library failed: %s", exc)
                self.app.show_snackbar(f"DSP analysis failed: {exc}", color="#FF4444")
                return
        else:
            self.app.show_snackbar(
                "All tracks already analysed — rebuilding graph and PCA.",
                icon=ft.Icons.GRAPHIC_EQ_ROUNDED,
            )

        try:
            await tg.build_metadata_edges(self.app.db_manager)
            await tg.build_acoustic_edges(self.app.db_manager)
        except Exception as exc:
            logger.exception("Advanced: graph/PCA rebuild failed: %s", exc)
            self.app.show_snackbar(f"Graph rebuild failed: {exc}", color="#FF4444")
            return

        if hasattr(self.app, "library_view") and self.app.library_view:
            self.app.library_view._cached_unanalysed = None

        self.app.show_snackbar(
            "DSP features, edges, and PCA space rebuilt.",
            icon=ft.Icons.CHECK_CIRCLE,
            color=CYAN,
        )

    def _on_recompute_all_dsp_click(self, _e):
        self.page.run_task(self._do_recompute_all_dsp)

    async def _do_recompute_all_dsp(self):
        from utils import track_graph as tg
        import sys
        if hasattr(sys, "getandroidapilevel") and audio_engine.audio_service is None:
            self.app.show_snackbar(
                "Audio service not ready — native analyser unavailable.",
                color="#FF4444",
            )
            return

        self.app.show_snackbar(
            "Purging acoustic features and starting full DSP re-analysis...",
            icon=ft.Icons.REPLAY_ROUNDED,
        )

        try:
            await self.app.clear_dsp_features()
        except Exception as exc:
            logger.exception("Advanced: clear_dsp_features failed: %s", exc)
            self.app.show_snackbar(f"DSP clear failed: {exc}", color="#FF4444")
            return

        try:
            missing = await self.app.db_manager.get_tracks_missing_features(tg.FEATURES_VERSION)
        except Exception as exc:
            logger.exception("Advanced: missing-feature query failed: %s", exc)
            self.app.show_snackbar(f"DSP query failed: {exc}", color="#FF4444")
            return

        if missing:
            self.app.show_snackbar(
                f"Recomputing features for {len(missing)} tracks — this can take a while.",
                icon=ft.Icons.GRAPHIC_EQ_ROUNDED,
            )
            try:
                await tg.bulk_analyze_library(
                    self.app.db_manager,
                    audio_engine.audio_service,
                )
            except Exception as exc:
                logger.exception("Advanced: bulk_analyze_library failed: %s", exc)
                self.app.show_snackbar(f"DSP analysis failed: {exc}", color="#FF4444")
                return

        try:
            await tg.build_metadata_edges(self.app.db_manager)
            await tg.build_acoustic_edges(self.app.db_manager)
        except Exception as exc:
            logger.exception("Advanced: graph/PCA rebuild failed: %s", exc)
            self.app.show_snackbar(f"Graph rebuild failed: {exc}", color="#FF4444")
            return

        if hasattr(self.app, "library_view") and self.app.library_view:
            self.app.library_view._cached_unanalysed = None

        self.app.show_snackbar(
            "DSP features, edges, and PCA space recomputed.",
            icon=ft.Icons.CHECK_CIRCLE,
            color=CYAN,
        )

    def _on_recompute_pca_click(self, _e):
        self.page.run_task(self._do_recompute_pca)

    async def _do_recompute_pca(self):
        from utils import track_graph as tg
        self.app.show_snackbar(
            "Recomputing PCA space…",
            icon=ft.Icons.SCATTER_PLOT_ROUNDED,
        )
        try:
            # The unified Zr geometry (projection + per-track coords + Louvain
            # communities) is rebuilt by the acoustic graph build.
            await tg.build_acoustic_edges(self.app.db_manager)
        except Exception as exc:
            logger.exception("Advanced: PCA/geometry rebuild failed: %s", exc)
            self.app.show_snackbar(f"PCA recompute failed: {exc}", color="#FF4444")
            return

        self.app.show_snackbar(
            "PCA space rebuilt.",
            icon=ft.Icons.CHECK_CIRCLE,
            color=CYAN,
        )

    def _on_launch_enrichment_wizard_click(self, _e=None):
        from ui.player.enrichment_wizard import MetadataEnrichmentWizardPane
        if not getattr(self, "_enrichment_wizard_pane", None):
            self._enrichment_wizard_pane = MetadataEnrichmentWizardPane(self.app, on_back=lambda: self._show_hub())
        self._show_sub_page("Enrich Metadata", self._enrichment_wizard_pane)

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
            bs = bs_holder[0]
            if bs:
                bs.open = False
                bs.update()
                # Remove the sheet from overlay to prevent orphaned controls
                # that cause a black screen on Android's Impeller renderer
                try:
                    self.app.page.overlay.remove(bs)
                except ValueError:
                    pass
                bs_holder[0] = None
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
        from utils.streamrip_api import get_default_download_path
        self._dl_path_field.value  = self.app.target_folder or get_default_download_path()
        self._lib_path_field.value = self.app.library_folder
        try:
            from utils.streamrip_api import load_config, get_config_path
            cfg = load_config()
            with open(get_config_path(), "r", encoding="utf-8") as f:
                self._config_editor.value = f.read()
            
            gen = cfg.get("general", {})
            self._startup_page_dropdown.value = gen.get("startup_page", "Library")
            self._default_sort_dropdown.value = gen.get("library_sort", "date")
            
            temp_val = gen.get("walk_temperature")
            if temp_val is None:
                temp_val = gen.get("play_similar_temperature", 0.0)
            self._temp_slider.value = float(temp_val)
            self._temp_value_text.value = f"{self._temp_slider.value:.2f}"

            self._mmr_slider.value = float(gen.get("walk_mmr_lambda", 0.15))
            self._mmr_value_text.value = f"{self._mmr_slider.value:.2f}"

            qobuz = cfg.get("qobuz", {})
            self._qobuz_user_id_field.value = str(qobuz.get("email_or_userid", ""))
            self._qobuz_token_field.value   = str(qobuz.get("password_or_token", ""))
            self._qobuz_app_id_field.value  = str(qobuz.get("app_id", "312369995"))
            secrets_list = qobuz.get("secrets", [])
            self._qobuz_app_secret_field.value = str(secrets_list[0]) if (secrets_list and isinstance(secrets_list, list) and len(secrets_list) > 0) else "e79f8b9be485692b0e5f9dd895826368"
            self._qobuz_use_token_switch.value = bool(qobuz.get("use_auth_token", True))

            landing = cfg.get("landing", {})
            self._show_most_listened_switch.value = bool(landing.get("show_search_history", True))
            self._show_library_stats_switch.value  = bool(landing.get("show_library_stats", True))

            appearance = cfg.get("appearance", {})
            self._selected_accent_color = appearance.get("accent_color", "#00BFFF")
            self._show_jarvis_switch.value = bool(appearance.get("show_jarvis", True))
            self._show_network_switch.value = bool(appearance.get("show_network", False))
            self._show_playlists_switch.value = bool(appearance.get("show_playlists", True))
            self._show_artists_switch.value = bool(appearance.get("show_artists", True))
            self._show_albums_switch.value = bool(appearance.get("show_albums", True))
            self._show_tracks_switch.value = bool(appearance.get("show_tracks", True))

            # Load DSP Settings
            dsp = cfg.get("dsp", {})
            self._dynamism_switch.value = bool(dsp.get("dynamism_enabled", False))
            self._equaliser_switch.value = bool(dsp.get("equalizer_enabled", False))
            # Kick off an async check to gray the dynamism switch if features are absent
            self.page.run_task(self._check_dynamism_availability)
            # Load Haptics Settings
            haptics = cfg.get("haptics", {})
            self._haptic_feedback_switch.value = bool(haptics.get("haptic_feedback_enabled", True))
            self._haptic_eq_drag_dropdown.value = haptics.get("eq_drag_intensity", "light")
            self._haptic_swipe_queue_dropdown.value = haptics.get("swipe_queue_intensity", "medium")
            self._haptic_swipe_dismiss_dropdown.value = haptics.get("swipe_dismiss_intensity", "medium")
            self._haptic_long_press_dropdown.value = haptics.get("long_press_intensity", "heavy")
            self._haptic_network_tap_dropdown.value = haptics.get("network_tap_intensity", "selection")
            self._haptic_network_reseed_dropdown.value = haptics.get("network_reseed_intensity", "medium")
            self._haptic_network_walk_dropdown.value = haptics.get("network_walk_intensity", "light")
            active_p = dsp.get("active_preset", "Flat")
            self._refresh_eq_presets_dropdown(active_value=active_p)
            self._update_eq_sliders_from_active_preset()
            
            boost = getattr(audio_engine, "loudness_boost_db", 0.0)
            self.update_loudness_boost(boost)
        except Exception as e:
            logger.error(f"Failed to load dsp config: {e}")
        self._apply_visuals_btn.visible = False
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
        self._apply_visuals_btn.visible = True
        self.app.safe_update(lambda: None)
        self._show_sub_page("Appearance", self._build_appearance_group())

    def _on_appearance_change(self, e=None):
        if not self._apply_visuals_btn.visible:
            def _mutate():
                self._apply_visuals_btn.visible = True
            self.app.safe_update(_mutate)

    def _save_appearance_settings(self):
        from utils.streamrip_api import update_config_params
        update_config_params({
            "appearance": {
                "accent_color": self._selected_accent_color,
                "show_jarvis": self._show_jarvis_switch.value,
                "show_network": self._show_network_switch.value,
                "show_playlists": self._show_playlists_switch.value,
                "show_artists": self._show_artists_switch.value,
                "show_albums": self._show_albums_switch.value,
                "show_tracks": self._show_tracks_switch.value,
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
                "walk_temperature": val
            }
        })

    def _on_mmr_change(self, e):
        val = round(self._mmr_slider.value, 2)
        self._mmr_value_text.value = f"{val:.2f}"
        self._mmr_value_text.update()
        from utils.streamrip_api import update_config_params
        update_config_params({
            "general": {
                "walk_mmr_lambda": val
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
        uid        = self._qobuz_user_id_field.value.strip()
        token      = self._qobuz_token_field.value.strip()
        app_id     = self._qobuz_app_id_field.value.strip() or "312369995"
        app_secret = self._qobuz_app_secret_field.value.strip() or "e79f8b9be485692b0e5f9dd895826368"
        if not uid or not token:
            self.app.show_snackbar("Credentials cannot be empty.")
            return
        
        use_token = self._qobuz_use_token_switch.value
        from utils.streamrip_api import update_config_params
        success = update_config_params({
            "qobuz": {
                "use_auth_token": use_token,
                "email_or_userid": uid,
                "password_or_token": token,
                "app_id": app_id,
                "secrets": [app_secret]
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

        def _close(do_update=True):
            bs = bs_holder[0]
            if bs:
                bs.open = False
                bs.update()
                # Remove the sheet from overlay to prevent orphaned controls
                # that cause a black screen on Android's Impeller renderer
                try:
                    self.app.page.overlay.remove(bs)
                except ValueError:
                    pass
                bs_holder[0] = None
                if do_update and self.page:
                    self.page.update()

        def _confirm(path):
            _close(do_update=False)
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

    def _build_audio_dsp_group(self):
        # Trigger background load of equalizer parameters
        self.page.run_task(self._load_equalizer_bands)
        
        return ft.Column([
            ft.Text("Real-time digital signal processing adjustments.", color=DIM, size=12),
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.GRAPHIC_EQ_ROUNDED, color=CYAN, size=20),
                        ft.Text("Dynamic Punchiness", weight=ft.FontWeight.BOLD, color=TEXT, size=14),
                    ], spacing=10),
                    ft.Text("Automatically enhance output gain based on track energy and beat strength to increase dynamic punchiness of rhythmic tracks.", color=DIM, size=12),
                    ft.Row([
                        self._dynamism_switch,
                        ft.Text("Enable Dynamism Enhancement", color=TEXT, size=12)
                    ], spacing=10),
                    self._dynamism_unavailable_text,
                    self._dynamism_boost_card,
                ], spacing=10),
                padding=16,
                bgcolor=SURFACE2,
                border_radius=10,
                border=ft.Border.all(1, BORDER),
            ),
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.TUNE_ROUNDED, color=CYAN, size=20),
                        ft.Text("Equalizer", weight=ft.FontWeight.BOLD, color=TEXT, size=14),
                    ], spacing=10),
                    ft.Text("Boost or cut specific frequency bands to match your headphones or genre preferences.", color=DIM, size=12),
                    ft.Row([
                        self._equaliser_switch,
                        ft.Text("Enable 5-Band Equalizer", color=TEXT, size=12)
                    ], spacing=10),
                    ft.Container(height=5),
                    self._eq_preset_type_radio,
                    ft.Container(height=5),
                    self._eq_preset_dropdown,
                    ft.Container(height=5),
                    self._eq_curve,
                    ft.Container(height=5),
                    self._eq_sliders_container,
                    ft.Container(height=5),
                    self._custom_preset_save_row,
                    self._custom_presets_list_container,
                ], spacing=10),
                padding=16,
                bgcolor=SURFACE2,
                border_radius=10,
                border=ft.Border.all(1, BORDER),
            ),
        ], spacing=20)

    async def _load_equalizer_bands(self):
        try:
            res = await audio_engine.get_equalizer_bands()
            if res.get("ok"):
                self._eq_bands = res["bands"]
                self._eq_min_db = res.get("min_db", -15.0)
                self._eq_max_db = res.get("max_db", 15.0)
                self._eq_curve.bands = self._eq_bands
                self._eq_curve.min_db = self._eq_min_db
                self._eq_curve.max_db = self._eq_max_db
                self._update_eq_sliders_from_active_preset()
                self.app.safe_update(lambda: None)
        except Exception as e:
            logger.error(f"Failed to load equalizer bands: {e}")

    def _on_curve_band_change(self, idx, gain):
        gain = round(gain, 1)
        self.app.trigger_haptic("light")
        audio_engine.set_eq_band_gain(idx, gain)
        
        # Sync to text labels and slider elements
        if hasattr(self, "_sliders") and idx < len(self._sliders):
            try:
                self._sliders[idx].value = gain
                self._sliders[idx].update()
            except:
                pass
        if hasattr(self, "_gain_texts") and idx < len(self._gain_texts):
            try:
                self._gain_texts[idx].value = f"{gain:+.1f} dB"
                self._gain_texts[idx].update()
            except:
                pass
                
        if self._eq_preset_type_radio.value != "custom" or self._eq_preset_dropdown.value != "Custom":
            self._refresh_eq_presets_dropdown(active_value="Custom", set_radio=True)
            self._eq_preset_dropdown.update()
            from utils.streamrip_api import update_config_params
            update_config_params({
                "dsp": {
                    "active_preset": "Custom"
                }
            })
            
        self._save_current_slider_gains_to_preset("Custom")

    def _update_eq_sliders_from_active_preset(self):
        try:
            from utils.streamrip_api import load_config
            cfg = load_config()
            dsp = cfg.get("dsp", {})
            active_preset = dsp.get("active_preset", "Flat")
            
            PRESETS = {
                "Flat": [0.0, 0.0, 0.0, 0.0, 0.0],
                "Rock": [4.0, 2.0, -2.0, 2.0, 4.0],
                "Pop": [2.0, 3.0, 1.0, -1.0, 2.0],
                "Jazz": [3.0, 2.0, 1.0, 2.0, 3.0],
                "Classical": [3.0, 2.0, -1.0, 2.0, 3.0],
                "Electronic": [4.0, 2.0, 0.0, 2.0, 3.0],
                "Bass Booster": [5.0, 3.0, 0.0, 0.0, 0.0],
                "Vocal Booster": [-2.0, -1.0, 3.0, 2.0, -1.0],
            }
            
            gains = None
            if active_preset in PRESETS:
                gains = PRESETS[active_preset]
            else:
                custom_presets = dsp.get("custom_presets", {})
                if active_preset in custom_presets:
                    gains = custom_presets[active_preset]
            
            if not gains:
                gains = [0.0] * len(self._eq_bands)
            
            for i in range(len(self._eq_bands)):
                if i < len(gains):
                    self._eq_bands[i]["gain"] = gains[i]
            
            self._build_eq_sliders_ui()
        except Exception as e:
            logger.error(f"Failed to update EQ sliders: {e}")

    def _build_eq_sliders_ui(self):
        self._eq_sliders_container.controls.clear()
        self._sliders = []
        self._gain_texts = []
        is_eq_enabled = self._equaliser_switch.value
        
        try:
            self._eq_curve.redraw()
        except BaseException as ex:
            logger.warning(f"Failed to redraw EQ curve during sliders UI build: {ex}")
        
        for idx, band in enumerate(self._eq_bands):
            freq = band["center_frequency"]
            gain = band["gain"]
            
            if freq >= 1000:
                freq_str = f"{freq/1000:.1f} kHz" if freq % 1000 != 0 else f"{int(freq/1000)} kHz"
            else:
                freq_str = f"{int(freq)} Hz"
                
            gain_field = ft.TextField(
                value=f"{gain:+.1f} dB",
                text_style=ft.TextStyle(color=TEXT, size=11, weight=ft.FontWeight.W_700),
                text_align=ft.TextAlign.RIGHT,
                width=75,
                height=30,
                content_padding=ft.Padding.symmetric(vertical=4, horizontal=6),
                border=ft.InputBorder.NONE,
                bgcolor=SURFACE2,
                border_radius=5,
                disabled=not is_eq_enabled,
            )
            gain_field.on_submit = lambda e, idx=idx, tf=gain_field: self._on_gain_text_field_submit(idx, e.control.value, tf)
            gain_field.on_blur = lambda e, idx=idx, tf=gain_field: self._on_gain_text_field_submit(idx, e.control.value, tf)
            self._gain_texts.append(gain_field)
            
            slider = ft.Slider(
                min=self._eq_min_db,
                max=self._eq_max_db,
                divisions=int(self._eq_max_db - self._eq_min_db) * 2,
                value=gain,
                active_color=CYAN,
                disabled=not is_eq_enabled,
                on_change=lambda e, idx=idx, gt=gain_field: self._on_slider_change(idx, e.control.value, gt),
            )
            self._sliders.append(slider)
            
            row = ft.Row([
                ft.Text(freq_str, color=DIM, size=12, width=60),
                ft.Container(content=slider, expand=True),
                gain_field,
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)
            
            self._eq_sliders_container.controls.append(row)

    def _on_slider_change(self, idx, val, text_control):
        val = round(val, 1)
        text_control.value = f"{val:+.1f} dB"
        text_control.update()
        
        if idx < len(self._eq_bands) and self._eq_bands[idx]["gain"] != val:
            self.app.trigger_haptic("light")
            self._eq_bands[idx]["gain"] = val
            audio_engine.set_eq_band_gain(idx, val)
            self._eq_curve.redraw()
            
        if self._eq_preset_type_radio.value != "custom" or self._eq_preset_dropdown.value != "Custom":
            self._refresh_eq_presets_dropdown(active_value="Custom", set_radio=True)
            self._eq_preset_dropdown.update()
            from utils.streamrip_api import update_config_params
            update_config_params({
                "dsp": {
                    "active_preset": "Custom"
                }
            })
            
        self._save_current_slider_gains_to_preset("Custom")

    def _on_gain_text_field_submit(self, idx, text_value, text_control):
        s = text_value.strip()
        if s.lower().endswith("db"):
            s = s[:-2].strip()
        
        try:
            val = float(s)
            val = round(val, 1)
            val = max(self._eq_min_db, min(self._eq_max_db, val))
        except ValueError:
            val = self._eq_bands[idx]["gain"]
            
        text_control.value = f"{val:+.1f} dB"
        text_control.update()
        
        if idx < len(self._eq_bands) and self._eq_bands[idx]["gain"] != val:
            self.app.trigger_haptic("light")
            self._eq_bands[idx]["gain"] = val
            audio_engine.set_eq_band_gain(idx, val)
            
            if hasattr(self, "_sliders") and idx < len(self._sliders):
                try:
                    self._sliders[idx].value = val
                    self._sliders[idx].update()
                except:
                    pass
            
            self._eq_curve.redraw()
            
            if self._eq_preset_type_radio.value != "custom" or self._eq_preset_dropdown.value != "Custom":
                self._refresh_eq_presets_dropdown(active_value="Custom", set_radio=True)
                self._eq_preset_dropdown.update()
                from utils.streamrip_api import update_config_params
                update_config_params({
                    "dsp": {
                        "active_preset": "Custom"
                    }
                })
            else:
                from utils.streamrip_api import update_config_params
                update_config_params({
                    "dsp": {
                        "active_preset": "Custom"
                    }
                })
                
            self._save_current_slider_gains_to_preset("Custom")

    def _save_current_slider_gains_to_preset(self, preset_name):
        try:
            from utils.streamrip_api import load_config, update_config_params
            cfg = load_config()
            dsp = cfg.get("dsp", {})
            custom_presets = dsp.get("custom_presets", {})
            
            gains = [band["gain"] for band in self._eq_bands]
            custom_presets[preset_name] = gains
            
            update_config_params({
                "dsp": {
                    "custom_presets": custom_presets
                }
            })
        except Exception as e:
            logger.error(f"Failed to save preset {preset_name}: {e}")

    def _on_eq_preset_select(self, e):
        preset_name = e.control.value
        if not preset_name:
            return
            
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "active_preset": preset_name
            }
        })
        
        self._update_eq_sliders_from_active_preset()
        
        if self._equaliser_switch.value:
            for idx, band in enumerate(self._eq_bands):
                audio_engine.set_eq_band_gain(idx, band["gain"])
                
        self.app.safe_update(lambda: None)

    async def _check_dynamism_availability(self):
        """Background check: disable the Dynamism switch when no tracks have been analyzed."""
        try:
            from utils import track_graph as tg
            db = getattr(self.app, "db_manager", None)
            if db is None:
                return
            # Get total track count and how many have features
            conn = await db.get_connection()
            async with conn.execute("SELECT COUNT(*) FROM tracks") as cur:
                row = await cur.fetchone()
                total = row[0] if row else 0
            if total == 0:
                # Library is empty — hide warning, switch disabled (no tracks at all)
                self._dynamism_switch.disabled = True
                self._dynamism_switch.opacity = 0.4
                self._dynamism_unavailable_text.value = "⚠ Library is empty — add tracks first."
                self._dynamism_unavailable_text.visible = True
            else:
                missing = await db.get_tracks_missing_features(tg.FEATURES_VERSION)
                all_missing = len(missing) >= total
                self._dynamism_switch.disabled = all_missing
                self._dynamism_switch.opacity = 0.4 if all_missing else 1.0
                self._dynamism_unavailable_text.visible = all_missing
                if all_missing:
                    self._dynamism_unavailable_text.value = (
                        f"⚠ {len(missing)} track{'s' if len(missing) != 1 else ''} lack DSP features — run Jarvis Analyser first."
                    )
        except Exception as exc:
            logger.debug("_check_dynamism_availability error (non-fatal): %s", exc)
        finally:
            try:
                if self.page:
                    self._dynamism_switch.update()
                    self._dynamism_unavailable_text.update()
            except Exception:
                pass

    def _on_dynamism_change(self, e):
        val = e.control.value
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "dynamism_enabled": val
            }
        })
        
        audio_engine.reapply_dsp()
        self.app.safe_update(lambda: None)

    def update_loudness_boost(self, val: float):
        is_active = (val > 0.0)
        self._dynamism_boost_card.visible = is_active
        if is_active:
            try:
                self._dynamism_boost_card.content.controls[2].value = f"+{val:.1f} dB"
            except Exception as e:
                logger.debug("Failed to set settings dynamism card text: %s", e)
        try:
            self._dynamism_boost_card.update()
        except Exception:
            pass

    def _on_equaliser_change(self, e):
        val = e.control.value
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "equalizer_enabled": val
            }
        })
        
        self._build_eq_sliders_ui()
        
        if val:
            for idx, band in enumerate(self._eq_bands):
                audio_engine.set_eq_band_gain(idx, band["gain"])
        else:
            for idx in range(5):
                audio_engine.set_eq_band_gain(idx, 0.0)
                
        self.app.safe_update(lambda: None)

    def _on_haptic_feedback_change(self, e):
        val = e.control.value
        from utils.streamrip_api import update_config_params
        update_config_params({
            "haptics": {
                "haptic_feedback_enabled": val
            }
        })
        self.app.safe_update(lambda: None)

    def _on_haptic_intensity_change(self, e):
        from utils.streamrip_api import update_config_params
        update_config_params({
            "haptics": {
                "eq_drag_intensity": self._haptic_eq_drag_dropdown.value,
                "swipe_queue_intensity": self._haptic_swipe_queue_dropdown.value,
                "swipe_dismiss_intensity": self._haptic_swipe_dismiss_dropdown.value,
                "long_press_intensity": self._haptic_long_press_dropdown.value,
                "network_tap_intensity": self._haptic_network_tap_dropdown.value,
                "network_reseed_intensity": self._haptic_network_reseed_dropdown.value,
                "network_walk_intensity": self._haptic_network_walk_dropdown.value,
            }
        })
        if e.control.value and e.control.value != "none":
            self.app.trigger_haptic(e.control.value)
        self.app.safe_update(lambda: None)

    def _build_haptics_group(self):
        return ft.Column([
            ft.Text("Customize tactile vibration effects for user gestures and actions (Android only).", color=DIM, size=12),
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.VIBRATION_ROUNDED, color=CYAN, size=20),
                        ft.Text("Global Control", weight=ft.FontWeight.BOLD, color=TEXT, size=14),
                    ], spacing=10),
                    ft.Text("Enable or completely disable tactile haptic feedback vibrations throughout the application.", color=DIM, size=12),
                    ft.Row([
                        self._haptic_feedback_switch,
                        ft.Text("Enable Haptic Feedback", color=TEXT, size=12)
                    ], spacing=10),
                ], spacing=10),
                padding=16,
                bgcolor=SURFACE2,
                border_radius=10,
                border=ft.Border.all(1, BORDER),
            ),
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.TOUCH_APP_ROUNDED, color=CYAN, size=20),
                        ft.Text("Effect Intensities", weight=ft.FontWeight.BOLD, color=TEXT, size=14),
                    ], spacing=10),
                    ft.Text("Configure the vibration intensity for each tactile action. Select 'None' to disable haptics for that action.", color=DIM, size=12),
                    ft.Container(height=5),
                    self._haptic_eq_drag_dropdown,
                    ft.Container(height=5),
                    self._haptic_swipe_queue_dropdown,
                    ft.Container(height=5),
                    self._haptic_swipe_dismiss_dropdown,
                    ft.Container(height=5),
                    self._haptic_long_press_dropdown,
                    ft.Container(height=5),
                    self._haptic_network_tap_dropdown,
                    ft.Container(height=5),
                    self._haptic_network_reseed_dropdown,
                    ft.Container(height=5),
                    self._haptic_network_walk_dropdown,
                ], spacing=10),
                padding=16,
                bgcolor=SURFACE2,
                border_radius=10,
                border=ft.Border.all(1, BORDER),
            ),
            ft.Container(height=50)
        ], spacing=20)

    def _on_save_custom_preset(self, e):
        name = self._custom_preset_name_field.value.strip()
        if not name:
            self.app.show_snackbar("Preset name cannot be empty.")
            return
        if name in ["Flat", "Rock", "Pop", "Jazz", "Classical", "Electronic", "Bass Booster", "Vocal Booster", "Custom"]:
            self.app.show_snackbar("Cannot overwrite default presets.")
            return
            
        self._save_current_slider_gains_to_preset(name)
        
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "active_preset": name
            }
        })
        
        self._refresh_eq_presets_dropdown(active_value=name)
        self._custom_preset_name_field.value = ""
        self.app.show_snackbar(f"Preset '{name}' saved successfully.")
        self.app.safe_update(lambda: None)

    def _on_preset_type_change(self, e):
        preset_type = e.control.value
        
        try:
            from utils.streamrip_api import load_config
            cfg = load_config()
            active_preset = cfg.get("dsp", {}).get("active_preset", "Flat")
        except:
            active_preset = "Flat"
            
        SYSTEM_PRESETS = ["Flat", "Rock", "Pop", "Jazz", "Classical", "Electronic", "Bass Booster", "Vocal Booster"]
        
        if preset_type == "system":
            if active_preset in SYSTEM_PRESETS:
                target_val = active_preset
            else:
                target_val = "Flat"
        else:
            try:
                custom_presets = cfg.get("dsp", {}).get("custom_presets", {})
            except:
                custom_presets = {}
            if active_preset == "Custom" or active_preset in custom_presets:
                target_val = active_preset
            else:
                target_val = "Custom"
                
        self._refresh_eq_presets_dropdown(active_value=target_val, set_radio=False)
        
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "active_preset": target_val
            }
        })
        
        self._eq_preset_dropdown.update()
        
        self._update_eq_sliders_from_active_preset()
        if self._equaliser_switch.value:
            for idx, band in enumerate(self._eq_bands):
                audio_engine.set_eq_band_gain(idx, band["gain"])
        self.app.safe_update(lambda: None)

    def _refresh_eq_presets_dropdown(self, active_value="Flat", set_radio=True):
        SYSTEM_PRESETS = ["Flat", "Rock", "Pop", "Jazz", "Classical", "Electronic", "Bass Booster", "Vocal Booster"]
        
        if set_radio and hasattr(self, "_eq_preset_type_radio"):
            if active_value in SYSTEM_PRESETS:
                self._eq_preset_type_radio.value = "system"
            else:
                self._eq_preset_type_radio.value = "custom"
            try:
                self._eq_preset_type_radio.update()
            except:
                pass

        preset_type = "system"
        if hasattr(self, "_eq_preset_type_radio") and self._eq_preset_type_radio.value:
            preset_type = self._eq_preset_type_radio.value
            
        options = []
        if preset_type == "system":
            options = [
                ft.dropdown.Option("Flat"),
                ft.dropdown.Option("Rock"),
                ft.dropdown.Option("Pop"),
                ft.dropdown.Option("Jazz"),
                ft.dropdown.Option("Classical"),
                ft.dropdown.Option("Electronic"),
                ft.dropdown.Option("Bass Booster"),
                ft.dropdown.Option("Vocal Booster"),
            ]
        else:
            options = [
                ft.dropdown.Option("Custom"),
            ]
            try:
                from utils.streamrip_api import load_config
                cfg = load_config()
                custom_presets = cfg.get("dsp", {}).get("custom_presets", {})
                for name in custom_presets.keys():
                    if name != "Custom":
                        options.append(ft.dropdown.Option(name))
            except:
                pass
                
        self._eq_preset_dropdown.options = options
        
        option_keys = [opt.key for opt in options]
        if active_value not in option_keys:
            if preset_type == "system":
                active_value = "Flat"
            else:
                active_value = "Custom"
                
        self._eq_preset_dropdown.value = active_value
        self._refresh_custom_presets_list()

    def _refresh_custom_presets_list(self):
        is_custom = False
        if hasattr(self, "_eq_preset_type_radio") and self._eq_preset_type_radio.value == "custom":
            is_custom = True
            
        if hasattr(self, "_custom_preset_save_row"):
            self._custom_preset_save_row.visible = is_custom
        self._custom_presets_list_container.visible = is_custom
        self._custom_presets_list_container.controls.clear()
        
        if is_custom:
            custom_presets = {}
            try:
                from utils.streamrip_api import load_config
                cfg = load_config()
                custom_presets = cfg.get("dsp", {}).get("custom_presets", {})
            except Exception as e:
                logger.error(f"Failed to load custom presets: {e}")
                
            custom_names = [name for name in custom_presets.keys() if name != "Custom"]
            
            if custom_names:
                self._custom_presets_list_container.controls.append(
                    ft.Container(
                        content=ft.Text("Custom Presets", size=12, weight=ft.FontWeight.W_700, color=CYAN),
                        margin=ft.Margin.only(top=10, bottom=5)
                    )
                )
                for name in sorted(custom_names):
                    gains = custom_presets[name]
                    gains_str = ", ".join(f"{float(g):+.1f}" for g in gains)
                    
                    preset_card = ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.EQUALIZER_ROUNDED, color=CYAN, size=16),
                            ft.Column([
                                ft.Text(name, color=TEXT, size=13, weight=ft.FontWeight.BOLD),
                                ft.Text(f"Gains: [{gains_str}] dB", color=DIM, size=10),
                            ], spacing=2, expand=True),
                            ft.IconButton(
                                icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                icon_color=CYAN,
                                icon_size=18,
                                tooltip="Apply Preset",
                                on_click=lambda e, n=name: self._apply_custom_preset(n)
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                                icon_color="#FF4444",
                                icon_size=18,
                                tooltip="Delete Preset",
                                on_click=lambda e, n=name: self._delete_custom_preset(n)
                            ),
                        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                        bgcolor=apply_opacity(0.04, TEXT),
                        border_radius=8,
                        border=ft.Border.all(1, BORDER),
                    )
                    self._custom_presets_list_container.controls.append(preset_card)
        
        self.app.safe_update(lambda: None)

    def _apply_custom_preset(self, name):
        from utils.streamrip_api import update_config_params
        update_config_params({
            "dsp": {
                "active_preset": name
            }
        })
        self._refresh_eq_presets_dropdown(active_value=name)
        self._update_eq_sliders_from_active_preset()
        
        if self._equaliser_switch.value:
            for idx, band in enumerate(self._eq_bands):
                audio_engine.set_eq_band_gain(idx, band["gain"])
                
        self.app.show_snackbar(f"Applied custom preset '{name}'.")
        self.app.safe_update(lambda: None)

    def _delete_custom_preset(self, name):
        try:
            from utils.streamrip_api import load_config, update_config_params
            cfg = load_config()
            dsp = cfg.get("dsp", {})
            custom_presets = dsp.get("custom_presets", {})
            
            if name in custom_presets:
                del custom_presets[name]
                
            active_preset = dsp.get("active_preset", "Flat")
            new_active = active_preset
            if active_preset == name:
                new_active = "Flat"
                
            update_config_params({
                "dsp": {
                    "custom_presets": custom_presets,
                    "active_preset": new_active
                }
            })
            
            self._refresh_eq_presets_dropdown(active_value=new_active)
            self._update_eq_sliders_from_active_preset()
            
            if self._equaliser_switch.value:
                for idx, band in enumerate(self._eq_bands):
                    audio_engine.set_eq_band_gain(idx, band["gain"])
                    
            self.app.show_snackbar(f"Deleted preset '{name}'.")
        except Exception as e:
            logger.error(f"Failed to delete preset {name}: {e}")
            self.app.show_snackbar(f"Failed to delete preset: {e}")
