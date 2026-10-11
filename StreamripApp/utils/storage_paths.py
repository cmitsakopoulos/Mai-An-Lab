"""Where music lives: one resolver for the library and download folders.

Every user-facing folder must be one the user can reach from a file manager.
On Android 11+ the app's sandbox (/data/user/0/<pkg>/…, and the app-specific
/storage/emulated/0/Android/data/<pkg>/…) is effectively sealed: no files app
can open it without root, so a track saved there is lost to the user.

The app used to land there by default. main.py points HOME at the sandbox, so
anything built from expanduser("~") or Path.home() (onboarding's suggested
"~/Music", the "~/StreamripDownloads" default, the scan's get_app_dir()
fallback) resolved inside it, and get_default_download_path() pointed at
FLET_APP_STORAGE_DATA directly.

The rules here:
  * An app-private path is never returned, whatever is stored.
  * Downloads follow the library: no download folder, or an unusable one,
    means "download into the library folder".
  * Fallbacks are resolved in memory and NEVER written back. The user's
    settings change only when they change them.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile

logger = logging.getLogger(__name__)

# Env vars that name the app's own sandbox (set by Flet / serious_python on
# Android, and by `flet run` on desktop).
_SANDBOX_ENV = (
    "APP_FILES_PATH", "FILES_DIR", "INTERNAL_STORAGE",
    "FLET_APP_STORAGE_DATA", "FLET_APP_STORAGE_TEMP", "APP_CACHE_PATH",
)

_ANDROID_MUSIC_DIRS = ("/storage/emulated/0/Music", "/sdcard/Music")


def is_android() -> bool:
    return hasattr(sys, "getandroidapilevel")


def real_home() -> str:
    """The user's actual home directory. main.py rewrites HOME and patches
    Path.home() to the app's data dir, so neither can be trusted here."""
    if is_android():
        return "/storage/emulated/0"
    if os.name == "nt":
        return os.environ.get("USERPROFILE") or os.path.expanduser("~")
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _private_roots() -> list[str]:
    roots = [os.environ.get(v) for v in _SANDBOX_ENV]
    roots.append(tempfile.gettempdir())
    try:
        from .config import APP_DIR
        roots.append(APP_DIR)
    except Exception:
        pass
    try:
        from .filepath_utils import get_app_dir
        roots.append(get_app_dir())
    except Exception:
        pass
    home = _norm(real_home())
    # On desktop get_app_dir() falls back to $HOME. That is the user's own
    # folder, not a sandbox, and must not make all of ~ count as private.
    return [r for r in {_norm(r) for r in roots if r} if not _under(home, r)]


def is_app_private(path: str | None) -> bool:
    """True when `path` is inside storage only this app can see."""
    if not path:
        return False
    p = _norm(path)
    if is_android():
        raw = os.path.abspath(path)
        if _under(raw, "/data") or _under(p, "/data"):
            return True
        # App-specific external storage: other apps (file managers included)
        # are locked out of these since Android 11.
        parts = p.split(os.sep)
        if "Android" in parts:
            i = parts.index("Android")
            if i + 1 < len(parts) and parts[i + 1] in ("data", "obb"):
                return True
    return any(_under(p, r) for r in _private_roots())


def usable(path: str | None) -> bool:
    """A stored folder we may use: set, absolute, and not app-private.
    Existence is not required; a missing folder is created at download time."""
    if not path or not str(path).strip():
        return False
    path = os.path.expanduser(str(path).strip())
    return os.path.isabs(path) and not is_app_private(path)


def default_music_dir() -> str:
    """The platform's public music folder."""
    if is_android():
        for d in _ANDROID_MUSIC_DIRS:
            if os.path.isdir(d):
                return d
        return _ANDROID_MUSIC_DIRS[0]
    return os.path.join(real_home(), "Music")


def resolve_library_dir(library_pref: str | None, download_cfg: str | None = None) -> str:
    """Library folder: the user's choice, else their download folder, else the
    public Music folder."""
    for cand in (library_pref, download_cfg):
        if usable(cand):
            return os.path.expanduser(str(cand).strip())
    return default_music_dir()


def resolve_download_dir(download_cfg: str | None, library_dir: str) -> str:
    """Download folder: the user's choice, else the library folder."""
    if usable(download_cfg):
        return os.path.expanduser(str(download_cfg).strip())
    return library_dir if usable(library_dir) else default_music_dir()


def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".mai_an_lab_probe_{os.getpid()}")
        with open(probe, "w") as fh:
            fh.write("")
        os.remove(probe)
        return True
    except OSError as exc:
        logger.warning("download folder %s not writable: %s", path, exc)
        return False


def pick_download_dir(download_dir: str | None, library_dir: str | None) -> str:
    """The folder a download actually goes to (blocking: touches the disk).

    Tries the download folder, then the library folder, then the public Music
    folder, skipping anything app-private or not writable. Raises rather than
    ever falling back into the sandbox."""
    tried = []
    for cand in (download_dir, library_dir, default_music_dir()):
        if not usable(cand):
            continue
        cand = os.path.expanduser(str(cand).strip())
        if cand in tried:
            continue
        tried.append(cand)
        if _writable(cand):
            if tried[0] != cand:
                logger.warning("downloading to %s instead of %s", cand, tried[0])
            return cand
    raise PermissionError(
        "No writable music folder. Grant storage access, or pick a download "
        "folder in Settings → Storage."
        + (f" Tried: {', '.join(tried)}" if tried else "")
    )


def load_saved_paths() -> tuple[str, str]:
    """(download folder, library folder) as stored, unresolved: config.toml's
    [downloads] folder and flet_prefs.json's library_path."""
    dl = lib = ""
    try:
        from .streamrip_api import load_config
        dl = str((load_config().get("downloads", {}) or {}).get("folder", "") or "")
    except Exception:
        logger.exception("could not read download folder from config")
    try:
        import json
        from .filepath_utils import get_app_dir
        with open(os.path.join(get_app_dir(), "flet_prefs.json")) as fh:
            lib = str(json.load(fh).get("library_path", "") or "")
    except Exception:
        pass
    return dl, lib


def resolve_saved_paths() -> tuple[str, str]:
    """(download folder, library folder) resolved from what is stored."""
    dl, lib = load_saved_paths()
    library = resolve_library_dir(lib, dl)
    return resolve_download_dir(dl, library), library
