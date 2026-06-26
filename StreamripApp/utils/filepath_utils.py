"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
import os
import tempfile
from string import printable
import re


def get_app_dir() -> str:
    """Returns the primary writable directory for the app, prioritizing 'files'."""
    for env_var in ("APP_FILES_PATH", "FILES_DIR", "INTERNAL_STORAGE", "FLET_APP_STORAGE_DATA", "HOME"):
        val = os.getenv(env_var)
        if val and os.path.isdir(val):
            return val
    return tempfile.gettempdir()


def get_temp_artwork_dir() -> str:
    """Returns the dedicated directory for temporary artwork, creating it and a .nomedia file if missing."""
    dir_path = os.path.join(get_app_dir(), "temp")
    try:
        os.makedirs(dir_path, exist_ok=True)
        nomedia_file = os.path.join(dir_path, ".nomedia")
        if not os.path.exists(nomedia_file):
            with open(nomedia_file, "w") as f:
                pass
    except Exception:
        return get_app_dir()
    return dir_path

# We don't depend on pathvalidate here to keep it simple for now, 
# but we implement a robust manual version of sanitize_filename
def sanitize_filename(name: str) -> str:
    # Remove illegal characters for both Windows and Unix
    return re.sub(r'[\\/*?:"<>|]', "", name)

ALLOWED_CHARS = set(printable)

def truncate_str(text: str) -> str:
    str_bytes = text.encode()
    str_bytes = str_bytes[:255]
    return str_bytes.decode(errors="ignore")

def clean_filename(fn: str, restrict: bool = False) -> str:
    path = truncate_str(str(sanitize_filename(fn)))
    if restrict:
        path = "".join(c for c in path if c in ALLOWED_CHARS)
    return path.strip()

def clean_filepath(fn: str, restrict: bool = False) -> str:
    # Basic cleanup for paths
    path = str(fn).replace("..", "").replace(":", "")
    if restrict:
        path = "".join(c for c in path if c in ALLOWED_CHARS)
    return path.strip()
