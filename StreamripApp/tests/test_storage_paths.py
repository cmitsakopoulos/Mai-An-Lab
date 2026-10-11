"""User-facing folders never resolve into the app's sandbox.

On Android 11+ no files app can open /data/user/0/<pkg>/… or
/storage/emulated/0/Android/data/<pkg>/…, so music saved there is stranded.
Downloads follow the library folder whenever their own folder is unusable."""

import asyncio
import os

import pytest

from utils import storage_paths as sp

PKG_FILES = "/data/user/0/com.mitsacopoulos.maianlab/files"


@pytest.fixture
def android(monkeypatch):
    monkeypatch.setattr(sp, "is_android", lambda: True)
    monkeypatch.setenv("APP_FILES_PATH", PKG_FILES)
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", PKG_FILES + "/flet/storage/data")


@pytest.mark.parametrize("path", [
    PKG_FILES + "/Music",                                      # old onboarding default
    PKG_FILES + "/flet/storage/data/StreamripDownloads",       # old download default
    "/data/data/com.mitsacopoulos.maianlab/files",
    "/storage/emulated/0/Android/data/com.mitsacopoulos.maianlab/files/Music",
    "/storage/emulated/0/Android/obb/com.mitsacopoulos.maianlab",
])
def test_android_sandbox_paths_are_private(android, path):
    assert sp.is_app_private(path)
    assert not sp.usable(path)


@pytest.mark.parametrize("path", [
    "/storage/emulated/0/Music",
    "/storage/emulated/0/Download/Music",
    "/storage/emulated/0/Android/media/whatever",   # readable by files apps
    "/storage/1234-ABCD/Music",                     # SD card
])
def test_android_shared_storage_is_usable(android, path):
    assert sp.usable(path)


def test_android_defaults_to_public_music(android):
    assert sp.default_music_dir().startswith(("/storage/emulated/0", "/sdcard"))
    assert not sp.is_app_private(sp.default_music_dir())


def test_private_stored_paths_fall_back_to_public_music(android):
    lib = sp.resolve_library_dir(PKG_FILES + "/Music", PKG_FILES + "/Music")
    assert lib == sp.default_music_dir()
    assert sp.resolve_download_dir(PKG_FILES + "/flet/storage/data/StreamripDownloads", lib) == lib


def test_download_folder_follows_library(android):
    assert sp.resolve_download_dir("", "/storage/emulated/0/Music/Lib") == "/storage/emulated/0/Music/Lib"
    assert sp.resolve_download_dir("/storage/emulated/0/DL", "/storage/emulated/0/Music") == "/storage/emulated/0/DL"


def test_library_falls_back_to_download_folder(android):
    assert sp.resolve_library_dir("", "/storage/emulated/0/DL") == "/storage/emulated/0/DL"


def test_desktop_home_is_not_private_even_when_it_is_the_app_dir(monkeypatch, tmp_path):
    # Desktop without Flet env vars: get_app_dir() falls back to $HOME, which
    # must not make the user's whole home count as app storage.
    monkeypatch.setattr(sp, "is_android", lambda: False)
    monkeypatch.setattr(sp, "real_home", lambda: str(tmp_path))
    for v in sp._SANDBOX_ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("utils.filepath_utils.get_app_dir", lambda: str(tmp_path))
    assert not sp.is_app_private(str(tmp_path / "Music"))
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path / ".flet" / "storage"))
    assert sp.is_app_private(str(tmp_path / ".flet" / "storage" / "Music"))
    assert not sp.is_app_private(str(tmp_path / "Music"))


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """tmp_path lives under the system temp dir, which counts as private, so
    pin the private roots to a dedicated folder instead."""
    box = tmp_path / "sandbox"
    box.mkdir()
    monkeypatch.setattr(sp, "_private_roots", lambda: [str(box)])
    monkeypatch.setattr(sp, "is_android", lambda: False)
    monkeypatch.setattr(sp, "default_music_dir", lambda: str(tmp_path / "public_music"))
    return box


def test_pick_uses_download_folder_when_writable(sandbox, tmp_path):
    dl = tmp_path / "dl"
    assert sp.pick_download_dir(str(dl), str(tmp_path / "lib")) == str(dl)
    assert dl.is_dir() and not os.listdir(dl)   # probe file cleaned up


def test_pick_falls_back_to_library_when_download_unwritable(sandbox, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        lib = tmp_path / "lib"
        assert sp.pick_download_dir(str(ro / "dl"), str(lib)) == str(lib)
    finally:
        os.chmod(ro, 0o700)


def test_pick_never_returns_the_sandbox(sandbox, tmp_path):
    lib = tmp_path / "lib"
    assert sp.pick_download_dir(str(sandbox / "Music"), str(lib)) == str(lib)
    assert sp.pick_download_dir(str(sandbox / "a"), str(sandbox / "b")) == str(tmp_path / "public_music")


def test_pick_raises_rather_than_using_the_sandbox(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_writable", lambda p: False)
    with pytest.raises(PermissionError):
        sp.pick_download_dir(str(tmp_path / "dl"), str(tmp_path / "lib"))


def test_download_refuses_private_dir(sandbox):
    from utils import streamrip_api
    with pytest.raises(PermissionError):
        asyncio.run(streamrip_api.download("https://www.qobuz.com/track/1", str(sandbox / "Music")))
    assert not (sandbox / "Music").exists()
