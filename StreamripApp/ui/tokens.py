from utils.streamrip_api import load_config

# ─── Design tokens (mirrors style.kv) ─────────────────────────────────────────
BG       = "#08080A"
SURFACE  = "#0D0D12"
SURFACE2 = "#111116"

try:
    _cfg = load_config()
    _accent_hex = _cfg.get("appearance", {}).get("accent_color", "#FFD600")
except Exception:
    _accent_hex = "#FFD600"

CYAN     = _accent_hex
AMBER    = "#FFBF00"
TEXT     = "#FFFFFF"
DIM      = "#A0A0A0"
BORDER   = "#262626"
ACCENT_GREEN = "#00FF88"
ACCENT_AMBER = AMBER
ACCENT_RED   = "#FF4444"

SOURCE_COLORS = {
    "qobuz":      "#00E5FF",
    "tidal":      "#0088FF", # From earlier testing, ignore.
    "deezer":     "#CC00FF", # From earlier testing, ignore
    "soundcloud": "#FF5500", # From earlier testing, ignore
}

LIB_ARTIST_COLOR   = "#CC00FF"
LIB_ALBUM_COLOR    = "#00E3FF"
LIB_TRACK_COLOR    = "#35fc03"
LIB_PLAYLIST_COLOR = "#FFBF00"
LIB_PARTITION_COLOR = "#00FF88"

def lerp_hex(c0: str, c1: str, ratio: float) -> str:
    """Linear-interpolate between two #RRGGBB colours. ratio is clamped to
    [0,1]. Used to give the walk-parameter sliders a semantic fill that tracks
    their value."""
    ratio = min(1.0, max(0.0, ratio))
    a, b = c0.lstrip("#"), c1.lstrip("#")
    r = int(int(a[0:2], 16) + (int(b[0:2], 16) - int(a[0:2], 16)) * ratio)
    g = int(int(a[2:4], 16) + (int(b[2:4], 16) - int(a[2:4], 16)) * ratio)
    bl = int(int(a[4:6], 16) + (int(b[4:6], 16) - int(a[4:6], 16)) * ratio)
    return f"#{r:02x}{g:02x}{bl:02x}"

# Semantic ramps for the two walk-parameter sliders. MMR is the *safe* variety
# lever (spreads the queue without wandering off-genre), so it warms from teal
# to green — "more of a good thing". Temperature trades queue quality for
# randomness, so it warms from amber to red — "more adventurous / higher risk".
MMR_RAMP  = ("#2DD4BF", "#00FF88")   # teal → green
TEMP_RAMP = ("#FFBF00", "#FF4444")   # amber → red


def apply_opacity(opacity: float, hex_color: str) -> str:
    if hex_color == "white": hex_color = "#FFFFFF"
    if hex_color == "black": hex_color = "#000000"
    if hex_color.startswith("#"):
        # Convert hex to ARGB (Flet format) or RGBA-like string
        # Actually, if we use #RRGGBB, opacity can be prepended as #AARRGGBB
        alpha = int(opacity * 255)
        return f"#{alpha:02X}{hex_color[1:]}"
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    if len(hex_color) != 6:
        return "#FFFFFF"
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"#{int(opacity*255):02X}{r:02X}{g:02X}{b:02X}"

