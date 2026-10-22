"""Deezer downloads fall back to a quality the account's plan can serve.

deezer-py raises WrongLicense for FLAC on a plan without lossless (and for
MP3_320 on a free plan). Settings promised a fallback that never existed, so
every track failed and the reason was lost in a log."""

import asyncio
import types

import pytest

deezer = pytest.importorskip("deezer")

from utils import deezer as dz
from utils.exceptions import NonStreamableError

SIZES = {"FILESIZE_MP3_128": 1, "FILESIZE_MP3_320": 2, "FILESIZE_FLAC": 3}


def _client(allowed: set[str], lower=True, sizes=SIZES):
    c = object.__new__(dz.DeezerClient)
    c.session = None
    c.config = types.SimpleNamespace(lower_quality_if_not_available=lower)
    asked = []

    def get_track_url(token, fmt):
        asked.append(fmt)
        if fmt not in allowed:
            raise deezer.WrongLicense(fmt)
        return f"https://cdn/{fmt}"

    c.client = types.SimpleNamespace(
        gw=types.SimpleNamespace(get_track=lambda i: {"TRACK_TOKEN": "t", **sizes}),
        get_track_url=get_track_url,
    )
    return c, asked


def _get(c, quality=2):
    return asyncio.run(c.get_downloadable("1", quality))


def test_flac_on_a_lossy_plan_falls_back_to_mp3_320():
    c, asked = _client({"MP3_320", "MP3_128"})
    d = _get(c)
    assert asked == ["FLAC", "MP3_320"]
    assert d.url == "https://cdn/MP3_320"
    assert (d.quality, d.extension) == (1, "mp3")   # not saved as .flac


def test_free_plan_falls_all_the_way_to_128():
    c, asked = _client({"MP3_128"})
    d = _get(c)
    assert asked == ["FLAC", "MP3_320", "MP3_128"]
    assert d.quality == 0


def test_licensed_quality_is_used_directly():
    c, asked = _client({"FLAC", "MP3_320", "MP3_128"})
    d = _get(c)
    assert asked == ["FLAC"]
    assert (d.quality, d.extension) == (2, "flac")


def test_formats_the_track_lacks_are_skipped():
    c, asked = _client({"FLAC", "MP3_320", "MP3_128"},
                       sizes={**SIZES, "FILESIZE_FLAC": 0})
    assert _get(c).quality == 1
    assert asked == ["MP3_320"]


def test_no_fallback_when_disabled():
    c, asked = _client({"MP3_320"}, lower=False)
    with pytest.raises(NonStreamableError, match="lower_quality_if_not_available"):
        _get(c)
    assert asked == ["FLAC"]


def test_no_licensed_quality_at_all_names_the_plan():
    c, _ = _client(set())
    with pytest.raises(NonStreamableError, match="Deezer plan"):
        _get(c)


def test_hires_label_is_honest_about_deezer():
    from ui.player.quality_selector import hires_subtitle
    assert hires_subtitle([{"source": "qobuz"}]) == "24-bit FLAC"
    assert "16-bit" in hires_subtitle([{"source": "deezer"}])
    assert "Deezer items: 16-bit" in hires_subtitle([{"source": "deezer"}, {}])


@pytest.mark.parametrize("source, streams", [("deezer", False), ("qobuz", True)])
def test_deezer_preview_skips_the_doomed_stream_attempt(source, streams, monkeypatch, tmp_path):
    """Deezer's CDN audio is encrypted, so a stream attempt can only fail after
    ~7 s; Deezer previews go straight to the decrypting download."""
    from unittest.mock import AsyncMock, MagicMock
    import ui.views.search as search_mod
    import utils.streamrip_api as api

    async def _fast_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(search_mod, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(search_mod.asyncio, "sleep", _fast_sleep)
    download = AsyncMock(return_value=[])
    monkeypatch.setattr(api, "download", download)

    v = search_mod.SearchView.__new__(search_mod.SearchView)
    v.app = MagicMock()
    v.selected_source = source
    v._active_preview_task = None
    v._active_preview_stop_event = None
    v.searcher = MagicMock()
    v.searcher.get_track_stream_url = AsyncMock(side_effect=RuntimeError("no stream"))
    v.show_preview_progress = MagicMock()
    v.update_preview_progress = MagicMock()
    v._find_audio = AsyncMock(side_effect=[None, str(tmp_path / "x.flac")])

    async def run():
        v._start_preview(0, {"id": "1", "source": source, "ui_title": "T",
                             "url": f"https://www.{source}.com/track/1"},
                         MagicMock(), MagicMock())
        await v._active_preview_task

    asyncio.run(run())
    assert v.searcher.get_track_stream_url.called is streams
    download.assert_awaited_once()
