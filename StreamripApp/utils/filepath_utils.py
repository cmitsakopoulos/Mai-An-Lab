"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
from string import printable
import re

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
