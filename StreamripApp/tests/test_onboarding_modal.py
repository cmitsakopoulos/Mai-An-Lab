import os
import sys
from unittest.mock import MagicMock, patch
import pytest
import flet as ft

from ui.player.onboarding_modal import OnboardingWizardModal, ACCENT_OPTIONS, STARTUP_OPTIONS


class MockApp:
    def __init__(self):
        self.page = MagicMock()
        self.page.show_dialog = MagicMock()
        self.dismiss_dialog = MagicMock()
        self.restart_ui = MagicMock()
        self._apply_accent = MagicMock()
        self._switch_tab = MagicMock()
        self.show_snackbar = MagicMock()
        self.notify = MagicMock()
        self.safe_update = lambda fn: fn()


def find_text(control, target_text: str) -> bool:
    """Recursively searches for target_text in control hierarchy."""
    if isinstance(control, ft.Text) and control.value == target_text:
        return True
    if getattr(control, "text", None) == target_text:
        return True
    if getattr(control, "content", None) == target_text:
        return True
    # Check children/content
    for attr in ("content", "controls"):
        child = getattr(control, attr, None)
        if child is not None:
            if isinstance(child, str):
                if child == target_text:
                    return True
            elif isinstance(child, list):
                if any(find_text(c, target_text) for c in child):
                    return True
            else:
                if find_text(child, target_text):
                    return True
    return False


def test_onboarding_modal_dimensions_and_glow():
    app = MockApp()
    wizard = OnboardingWizardModal(app)

    # Test dialog dimensions
    assert wizard._dialog_content.width == 420
    assert wizard._dialog_content.height == 530
    assert wizard._dialog_content.padding.left == 18
    assert wizard._dialog_content.padding.right == 18

    # Initial shadow glow
    assert wizard._dialog_content.shadow is not None
    assert wizard._dialog_content.shadow.blur_radius == 28

    # Changing accent updates shadow glow
    wizard._set_accent("#64D2FF")
    assert wizard.selected_accent == "#64D2FF"
    assert wizard._dialog_content.shadow is not None


def test_onboarding_slide_0_step_badge_and_button():
    app = MockApp()
    wizard = OnboardingWizardModal(app)

    slide_0 = wizard._build_slide_0()
    assert find_text(slide_0, "STEP 1 OF 3")
    assert find_text(slide_0, "GET STARTED")
    assert find_text(slide_0, "Welcome to Mai An Lab")

    # Verify all 5 core features are present plainly
    assert find_text(slide_0, "Streamrip Downloader")
    assert find_text(slide_0, "Customizable AI Assistant")
    assert find_text(slide_0, "Studio DSP & Dynamism")
    assert find_text(slide_0, "Interactive Network & Auto-Play")
    assert find_text(slide_0, "Playlists & Metadata Wizard")

    # Verify academic jargon has been purged
    assert not find_text(slide_0, "Louvain")
    assert not find_text(slide_0, "Force-directed")


def test_onboarding_slide_1_step_badge_and_buttons():
    app = MockApp()
    wizard = OnboardingWizardModal(app)
    wizard._go_to_slide(1)

    slide_1 = wizard._build_slide_1()
    assert find_text(slide_1, "STEP 2 OF 3")
    assert find_text(slide_1, "Music Sources")
    assert find_text(slide_1, "Local Library Folder")
    assert find_text(slide_1, "Streaming Accounts (Optional)")
    assert find_text(slide_1, "BACK")
    assert find_text(slide_1, "SKIP")
    assert find_text(slide_1, "PERSONALIZE")
    assert find_text(slide_1, "Streaming accounts are optional. Users must supply their own subscriptions, which can be configured anytime in Settings.")

    # Find navigation buttons in slide_1
    row_nav = slide_1.controls[-1]
    back_btn = row_nav.controls[0]
    skip_btn = row_nav.controls[1]
    next_btn = row_nav.controls[2]
    assert back_btn.width == 74
    assert skip_btn.width == 74
    assert next_btn.expand is True


def test_onboarding_slide_1_tabs_interactive():
    app = MockApp()
    wizard = OnboardingWizardModal(app)
    wizard._go_to_slide(1)

    # Default tab is Qobuz
    slide_1 = wizard._build_slide_1()
    assert find_text(slide_1, "Qobuz Authentication (Hi-Res FLAC)")
    assert find_text(slide_1, "Requires your own personal Qobuz subscription. Leave blank to skip.")

    # Switch to Deezer
    wizard._set_streaming_tab("deezer")
    slide_1_deezer = wizard._build_slide_1()
    assert find_text(slide_1_deezer, "Deezer Authentication (FLAC & MP3)")
    assert find_text(slide_1_deezer, "Requires your own personal Deezer account. Retrieve ARL from browser cookies. Leave blank to skip.")

    # Test skip advances to slide 2
    wizard.current_slide = 1
    wizard._skip_sources()
    assert wizard.current_slide == 2

    # Test save and next advances to slide 2
    wizard.current_slide = 1
    wizard._save_sources_and_next()
    assert wizard.current_slide == 2


def test_onboarding_slide_2_step_badge_and_buttons():
    app = MockApp()
    wizard = OnboardingWizardModal(app)
    wizard._go_to_slide(2)

    slide_2 = wizard._build_slide_2()
    assert find_text(slide_2, "STEP 3 OF 3")
    assert find_text(slide_2, "Make It Yours")
    assert find_text(slide_2, "THEME ACCENT")
    assert find_text(slide_2, "STARTUP SCREEN")
    assert find_text(slide_2, "BACK")
    assert find_text(slide_2, "ENTER MAI-AN LAB")

    row_nav = slide_2.controls[-1]
    back_btn = row_nav.controls[0]
    enter_btn = row_nav.controls[1]
    assert back_btn.width == 82
    assert enter_btn.expand is True


def test_onboarding_finish_flow_with_credentials(tmp_path):
    import ui.player.onboarding_modal as ob_mod
    app = MockApp()
    wizard = ob_mod.OnboardingWizardModal(app)
    wizard.selected_accent = "#BF5AF2"
    wizard.selected_startup = "Search"
    wizard.detected_music_dir = "/Custom/Music/Path"
    wizard.qobuz_user = "testuser@example.com"
    wizard.qobuz_token = "secrettoken123"
    wizard.deezer_arl = "deezer_arl_cookie_test"

    with patch.object(ob_mod, "get_app_dir", return_value=str(tmp_path)), \
         patch.object(ob_mod, "update_config_params") as mock_update_config:

        wizard._finish_onboarding(None)

        # Dialog dismissed
        app.dismiss_dialog.assert_called_once_with(wizard._dialog)

        # Onboarding marker created in app dir
        marker = tmp_path / ".onboarded"
        assert marker.exists()

        # Config updated with choices and credentials
        mock_update_config.assert_called_once()
        saved_params = mock_update_config.call_args[0][0]
        assert saved_params["appearance"]["accent_color"] == "#BF5AF2"
        assert saved_params["general"]["startup_page"] == "Search"
        assert saved_params["downloads"]["folder"] == "/Custom/Music/Path"

        # Qobuz saved
        assert saved_params["qobuz"]["email_or_userid"] == "testuser@example.com"
        assert saved_params["qobuz"]["password_or_token"] == "secrettoken123"

        # Deezer saved
        assert saved_params["deezer"]["arl"] == "deezer_arl_cookie_test"

        # UI restarted with target_tab=1 (Search) and welcome post_message
        app.restart_ui.assert_called_once_with(
            target_tab=1,
            post_message="Welcome to Mai-An Lab! Enjoy studio-grade listening."
        )


@pytest.mark.asyncio
async def test_onboarding_browse_folder():
    app = MockApp()
    wizard = OnboardingWizardModal(app)
    wizard._go_to_slide(1)

    with patch("ui.widgets.pick_folder", return_value="/Users/test/Music"):
        await wizard._browse_folder(None)
        assert wizard.detected_music_dir == "/Users/test/Music"
        assert wizard._folder_field.value == "/Users/test/Music"
