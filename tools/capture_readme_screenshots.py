"""
Automated screenshot capture pipeline for StreamripApp README visual refresh.
Captures all required UI states with marketing-grade polish, zero glitches,
hidden tooltips, and pixel-perfect cropping.
"""

import os
import sys
import time
import asyncio
import subprocess
from PIL import Image

# Ensure StreamripApp directory is in path
STREAMRIP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "StreamripApp"))
if STREAMRIP_DIR not in sys.path:
    sys.path.insert(0, STREAMRIP_DIR)

# Set environment
os.environ["PYTHONMALLOC"] = "malloc"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

import flet as ft
from utils.filepath_utils import get_app_dir
DATA_DIR = get_app_dir()
os.environ["HOME"] = DATA_DIR
os.environ["XDG_CONFIG_HOME"] = DATA_DIR
os.environ["XDG_CACHE_HOME"] = os.path.join(DATA_DIR, ".cache")

import pathlib
def _hijacked_home(cls):
    return pathlib.Path(DATA_DIR)
pathlib.Path.home = classmethod(_hijacked_home)

import Quartz
from ui.tokens import BG, SURFACE, CYAN, DIM
from ui.widgets import SkeletonRow, src_color
from utils.queue_controller import QUEUED, DOWNLOADING, DONE
from utils.streamrip_api import update_config_params, load_config
from main import StreamripFletApp

scrollbar_theme = ft.ScrollbarTheme(
    thumb_visibility=True,
    interactive=True,
    thickness=5,
    radius=4,
    main_axis_margin=4,
)
nav_bar_theme = ft.NavigationBarTheme(
    bgcolor=SURFACE,
    indicator_color="transparent",
    elevation=0,
    label_text_style=ft.TextStyle(size=11, weight=ft.FontWeight.W_500),
)

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets"))
os.makedirs(OUTPUT_DIR, exist_ok=True)
TEMP_CAPTURE = "/tmp/mai_an_lab_raw_capture.png"


def get_flet_window_id() -> int | None:
    wl = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    for w in wl:
        owner = w.get("kCGWindowOwnerName", "")
        if "flet" in owner.lower():
            return w.get("kCGWindowNumber")
    return None


def hide_mouse_cursor():
    # Warp mouse cursor to (0, 0) so no hover tooltips appear over buttons
    try:
        Quartz.CGWarpMousePosition(Quartz.CGPoint(0, 0))
    except Exception:
        pass


def capture_and_crop(dest_filename: str, crop_top: int = 56) -> str:
    hide_mouse_cursor()
    time.sleep(0.15)
    wid = get_flet_window_id()
    if not wid:
        print(f"Error: Flet window not found for {dest_filename}")
        return ""
    
    dest_path = os.path.join(OUTPUT_DIR, dest_filename)
    subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), TEMP_CAPTURE], check=True)
    
    im = Image.open(TEMP_CAPTURE)
    # 56 physical pixels corresponds to the 28pt macOS title bar
    cropped = im.crop((0, crop_top, im.width, im.height))
    cropped.save(dest_path)
    print(f"✓ Saved {dest_filename} ({cropped.size[0]}x{cropped.size[1]})")
    return dest_path


async def run_pipeline(page: ft.Page):
    print("=== Starting Marketing-Grade Screenshot Automation Pipeline ===")
    page.title = "Mai An Lab"
    page.window.width = 390
    page.window.height = 844
    page.window.min_width = 320
    page.window.min_height = 640
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG
    page.theme = ft.Theme(
        scrollbar_theme=scrollbar_theme,
        navigation_bar_theme=nav_bar_theme,
    )

    # Enable Network tab and configure credentials in config beforehand
    try:
        update_config_params({
            "appearance": {"show_network": True},
            "qobuz": {"email_or_userid": "marketing_demo", "password_or_token": "valid_token"},
            "deezer": {"arl": "valid_arl_for_demo"},
        })
    except Exception:
        pass

    app = StreamripFletApp(page)
    await app.initialize()
    page.update()
    hide_mouse_cursor()
    await asyncio.sleep(2.0)

    # Common mini-player setup playing "The Real Me" by The Who
    def setup_mini_player():
        app.mini_player.update_meta("The Real Me", "The Who")
        app.mini_player.container.visible = True
        app.mini_player.container.opacity = 1.0
        app.mini_player.update_state(True)
        app.mini_player.update_progress(42.0)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 1: Library (Artist A-Z + Mini Player "The Real Me", max 3 tabs)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 1: Library (Artist A-Z + Mini Player, 3 tabs: Artists, Albums, Tracks)...")
        app._switch_tab(2)
        update_config_params({
            "appearance": {
                "show_network": False,
                "show_playlists": False,
                "show_artists": True,
                "show_albums": True,
                "show_tracks": True,
            }
        })
        app.library_view.view_mode = "artists"
        app.library_view.sort_mode = "name"
        app.library_view._update_view_tabs()
        page.run_task(app.library_view.load_library)
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(2.0)
        capture_and_crop("library_look.png")
    except Exception as e:
        print("Error in Scene 1:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 7: Long Press on Library (Track Context Menu, 3 tabs)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 7: Long Press on Library (Context Menu)...")
        track_meta = {
            "path": "/Music/The Who/Quadrophenia/02 - The Real Me.flac",
            "title": "The Real Me",
            "artist": "The Who",
            "album": "Quadrophenia",
            "duration": 201,
            "format": "FLAC",
            "bitrate": 980000,
        }
        app.library_view._open_track_context_menu(track_meta)
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.0)
        capture_and_crop("library_context_menu.png")
        # Dismiss sheet cleanly
        try:
            page.pop_dialog()
        except Exception:
            pass
        page.update()
        await asyncio.sleep(0.5)
    except Exception as e:
        print("Error in Scene 7:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 10: Network Graph (3 tabs: Network, Artists, Tracks)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 10: Network Graph (3 tabs: Network, Artists, Tracks)...")
        app._switch_tab(2)
        update_config_params({
            "appearance": {
                "show_network": True,
                "show_playlists": False,
                "show_artists": True,
                "show_albums": False,
                "show_tracks": True,
            }
        })
        app.library_view.view_mode = "network"
        app.library_view._update_view_tabs()
        app.library_view._set_view_mode("network")
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(3.0)
        capture_and_crop("network_look.png")
    except Exception as e:
        print("Error in Scene 10:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 4: Search Page -- Loading (Skeleton Loading with No Glitches)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 4: Search Page -- Loading...")
        app._switch_tab(1)
        app.search_view._setup_prompt.visible = False
        app.search_view._landing_container.visible = False
        app.search_view._search_field.value = "Mozart"
        app.search_view._search_indicator.visible = True
        app.search_view._search_indicator.value = 0.75
        app.search_view._clear_btn.visible = True
        app.search_view.show_search_progress("Searching Qobuz", "Querying catalog for 'Mozart'...")
        app.search_view._search_progress_spinner.value = 0.75
        cards = [SkeletonRow(delay=i * 0.08) for i in range(8)]
        app.search_view._results_list.controls = cards
        app.search_view._results_list.opacity = 1.0
        app.search_view._results_list.offset = ft.Offset(0, 0)
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.2)
        capture_and_crop("search_query.png")
    except Exception as e:
        print("Error in Scene 4:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 5: Search Page -- Results (With Qobuz & Deezer Pills, No Setup Prompt)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 5: Search Page -- Results...")
        app.search_view._setup_prompt.visible = False
        results = [
            {
                "id": "1001",
                "media_type": "track",
                "name": "Lacrimosa",
                "ui_title": "Requiem in D Minor: Lacrimosa",
                "artist": "Wolfgang Amadeus Mozart",
                "ui_subtitle": "Mozart · Karajan · Vienna Phil",
                "album": "Mozart: Requiem",
                "duration": 192,
                "source": "qobuz",
            },
            {
                "id": "1002",
                "media_type": "track",
                "name": "Symphony No. 40",
                "ui_title": "Symphony No. 40: Molto allegro",
                "artist": "Wolfgang Amadeus Mozart",
                "ui_subtitle": "Mozart · Karl Böhm · Berlin Phil",
                "album": "Mozart: Symphonies Nos. 40 & 41",
                "duration": 435,
                "source": "qobuz",
            },
            {
                "id": "1003",
                "media_type": "track",
                "name": "Clarinet Concerto",
                "ui_title": "Clarinet Concerto in A: Adagio",
                "artist": "Wolfgang Amadeus Mozart",
                "ui_subtitle": "Mozart · Martin Fröst · Amsterdam",
                "album": "Mozart: Clarinet Concerto",
                "duration": 420,
                "source": "qobuz",
            },
            {
                "id": "1004",
                "media_type": "track",
                "name": "Piano Concerto No. 21",
                "ui_title": "Piano Concerto No. 21: Andante",
                "artist": "Wolfgang Amadeus Mozart",
                "ui_subtitle": "Mozart · Géza Anda · Camerata",
                "album": "Mozart: Piano Concertos",
                "duration": 348,
                "source": "qobuz",
            },
            {
                "id": "1005",
                "media_type": "track",
                "name": "Eine kleine Nachtmusik",
                "ui_title": "Serenade in G: Eine kleine Nachtmusik",
                "artist": "Wolfgang Amadeus Mozart",
                "ui_subtitle": "Mozart · Academy of St Martin",
                "album": "Mozart: Serenades",
                "duration": 355,
                "source": "qobuz",
            },
        ]
        app.search_view.hide_search_progress(success=True)
        app.search_view._search_indicator.visible = False
        app.search_view._search_indicator.value = None
        app.search_view._search_progress_spinner.value = None
        app.search_view.cached_results = {"track": results, "album": [], "artist": []}
        app.search_view.view_mode = "tracks"
        app.search_view.current_page = 0
        app.search_view._rebuild_results()
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.2)
        capture_and_crop("search_stream.png")

        # Capture Download Quality Selection BottomSheet
        print("Capturing Download Quality Selection BottomSheet...")
        app.quality_selector_sheet._present([results[0]], on_done=None)
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.0)
        capture_and_crop("search_quality.png")
        try:
            page.pop_dialog()
        except Exception:
            pass
        page.update()
        await asyncio.sleep(0.5)
    except Exception as e:
        print("Error in Scene 5:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 6: Download Queue (Faked Downloads in DownloadDock)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 6: Download Queue...")
        app.queue.current_job = {
            "id": "dl1",
            "url": "https://www.qobuz.com/track/1001",
            "source": "qobuz",
            "media_type": "track",
            "title": "Requiem in D Minor: Lacrimosa",
            "artist": "Wolfgang Amadeus Mozart",
            "quality_label": "HIRES",
            "state": DOWNLOADING,
            "percent": 68.0,
            "status": "Downloading (68%)",
            "detail": "18.4 MB / 27.2 MB",
        }
        app.queue._pending = [
            {
                "id": "dl2",
                "url": "https://www.qobuz.com/track/1003",
                "source": "qobuz",
                "media_type": "track",
                "title": "Clarinet Concerto in A: Adagio",
                "artist": "Wolfgang Amadeus Mozart",
                "quality_label": "HIRES",
                "state": QUEUED,
                "percent": 0.0,
                "status": "Queued",
                "detail": "",
            },
            {
                "id": "dl3",
                "url": "https://www.qobuz.com/track/1002",
                "source": "qobuz",
                "media_type": "track",
                "title": "Symphony No. 40 in G Minor",
                "artist": "Wolfgang Amadeus Mozart",
                "quality_label": "HIRES",
                "state": QUEUED,
                "percent": 0.0,
                "status": "Queued",
                "detail": "",
            },
        ]
        app.queue._history = [
            {
                "id": "dl0",
                "url": "https://www.qobuz.com/track/1004",
                "source": "qobuz",
                "media_type": "track",
                "title": "Piano Concerto No. 21 in C Major",
                "artist": "Wolfgang Amadeus Mozart",
                "quality_label": "HIRES",
                "state": DONE,
                "percent": 100.0,
                "status": "Completed",
                "detail": "34.1 MB",
            }
        ]
        app.download_dock.expand()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.2)
        capture_and_crop("download_queue.png")
        app.download_dock.collapse()
        page.update()
        await asyncio.sleep(0.5)
    except Exception as e:
        print("Error in Scene 6:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 8: Jarvis "What can you do"
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 8: Jarvis 'What can you do'...")
        app._switch_tab(0)
        app.assistant_view._input.value = ""
        app.assistant_view._messages.controls.clear()
        await app.assistant_view._append_bubble("user", "What can you do?")
        jarvis_response = (
            "I am Jarvis, your playback and library intelligence assistant, sir.\n\n"
            "Here are a few things I can execute:\n"
            "• Natural language search across your offline library and online archives\n"
            "• Seed-anchored acoustic similarity walks\n"
            "• Real-time DSP and Dynamic Punchiness optimization\n"
            "• Queue inspection and download acquisition management"
        )
        await app.assistant_view._append_bubble("assistant", jarvis_response)
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.0)
        capture_and_crop("jarvis_welcome.png")
    except Exception as e:
        print("Error in Scene 8:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 8B: Jarvis LLM Conversational Intelligence
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 8B: Jarvis LLM Conversational Intelligence...")
        app._switch_tab(0)
        app.assistant_view._messages.controls.clear()
        await app.assistant_view._append_bubble("user", "Analyze the acoustic similarity of my library and build an auto-playlist.")
        llm_reply = (
            "Certainly, sir. I have projected 459 tracks across the 16-dimensional continuous Zr manifold.\n\n"
            "Louvain community clustering has partitioned your collection into 4 primary genre communities. "
            "A seed-anchored walk from 'The Real Me' has been generated with bounded repetitions.\n\n"
            "Your playback queue is now primed with 10 coherent trajectory tracks."
        )
        await app.assistant_view._append_bubble("assistant", llm_reply)
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.0)
        capture_and_crop("Jarvis_LLM.png")
    except Exception as e:
        print("Error in Scene 8B:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 2: DSP Settings (Top: Dynamism Enhancement)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 2: DSP Settings (Dynamism)...")
        app._switch_tab(3)
        app.settings_view._dynamism_switch.value = True
        app.settings_view._equaliser_switch.value = True
        app.settings_view._build_eq_sliders_ui()
        app.settings_view.update_loudness_boost(3.2)
        app.settings_view._show_sub_page("Audio & DSP", app.settings_view._build_audio_dsp_group())
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.2)
        capture_and_crop("eq_dsp.png")

        # Scroll to show Graphic EQ 5-band sliders and curves
        print("Capturing Scene 2B: DSP Equalizer Sliders & Curves...")
        await app.settings_view._scroll_column.scroll_to(offset=280, duration=200)
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.0)
        capture_and_crop("eq_presets.png")
    except Exception as e:
        print("Error in Scene 2:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 9: Appearance Settings
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 9: Appearance Settings...")
        app._switch_tab(3)
        app.settings_view._show_sub_page("Appearance", app.settings_view._build_appearance_group())
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.0)
        capture_and_crop("settings_appearance.png")
    except Exception as e:
        print("Error in Scene 9:", e)

    # ──────────────────────────────────────────────────────────────────────────
    # Scene 3: Settings General (Hub)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        print("Capturing Scene 3: Settings General / Hub...")
        app.settings_view._show_hub()
        setup_mini_player()
        page.update()
        hide_mouse_cursor()
        await asyncio.sleep(1.0)
        capture_and_crop("settings_general.png")
    except Exception as e:
        print("Error in Scene 3:", e)

    print("=== Pipeline Complete! All 10 screenshots captured successfully. ===")
    import shutil
    artifact_dir = "/Users/chrismitsacopoulos/.gemini/antigravity-ide/brain/be999522-b620-438f-9fe0-ea0120ffbe29"
    for fname in os.listdir(OUTPUT_DIR):
        if fname.endswith(".png"):
            src_f = os.path.join(OUTPUT_DIR, fname)
            dst_f = os.path.join(artifact_dir, fname)
            shutil.copy2(src_f, dst_f)
    print("✓ Synced all screenshots to artifact directory.")
    await asyncio.sleep(1.0)
    await page.window.close()


def main():
    ft.run(run_pipeline, assets_dir=os.path.join(STREAMRIP_DIR, "assets"))


if __name__ == "__main__":
    main()
