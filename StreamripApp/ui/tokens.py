from utils.streamrip_api import load_config

# ─── Design tokens (Apple HIG Dark Palette) ──────────────────────────────────
BG              = "#000000"  # Pure black canvas
SURFACE         = "#1C1C1E"  # Apple Secondary System Background (Grouped 1)
SURFACE2        = "#2C2C2E"  # Apple Tertiary System Background (Grouped 2 / Search field)
SURFACE_ELEVATED = "#3A3A3C" # Apple Quaternary Fill (Hover / Active Pill)

# Mapping legacy Flat UI / low-luminance hexes to vibrant Apple HIG Dark Mode accents
LEGACY_ACCENT_MAP = {
    "#FFD600": "#FFD60A",  # Apple System Yellow
    "#FFD700": "#FFD60A",  # Gold -> Apple Yellow
    "#00BFFF": "#64D2FF",  # Deep Sky Blue -> Apple System Teal / Cyan
    "#2979FF": "#0A84FF",  # Material Blue -> Apple System Blue
    "#9B59B6": "#BF5AF2",  # Flat Purple -> Apple System Purple
    "#B39DDB": "#D0BCFF",  # Lavender -> Luminous Lilac
    "#E91E63": "#FF375F",  # Flat Pink -> Apple System Pink
    "#E74C3C": "#FF453A",  # Flat Red -> Apple System Red
    "#DC143C": "#FF453A",  # Crimson -> Apple System Red
    "#E67E22": "#FF9F0A",  # Carrot Orange -> Apple System Orange
    "#2ECC71": "#30D158",  # Flat Green -> Apple System Green
    "#00FF7F": "#25E89B",  # Spring Green -> Electric Emerald
    "#69F0AE": "#63E6E2",  # Mint -> Apple System Mint
    "#78909C": "#5E5CE6",  # Slate -> Apple System Indigo
}

try:
    _cfg = load_config()
    _raw_accent = _cfg.get("appearance", {}).get("accent_color", "#FFD60A")
    _accent_hex = LEGACY_ACCENT_MAP.get(_raw_accent.upper(), _raw_accent)
except Exception:
    _accent_hex = "#FFD60A"

CYAN            = _accent_hex
AMBER           = "#FF9F0A"  # Apple System Orange/Amber
TEXT            = "#FFFFFF"  # Primary Label
DIM             = "#8E8E93"  # Apple Secondary System Gray
TEXT_TERTIARY   = "#636366"  # Apple Tertiary System Gray (Muted metadata / track numbers)
BORDER          = "#38383A"  # Apple Hairline Separator
BORDER_SUBTLE   = "#1AFFFFFF"# Translucent 10% white card edge
ACCENT_GREEN    = "#30D158"  # Apple System Green
ACCENT_AMBER    = AMBER
ACCENT_RED      = "#FF453A"  # Apple System Red

RADIUS_CARD     = 14
RADIUS_PILL     = 20
RADIUS_THUMB    = 8


SOURCE_COLORS = {
    "qobuz":      "#00E5FF",
    "tidal":      "#0088FF", # From earlier testing, ignore.
    "deezer":     "#CC00FF", # From earlier testing, ignore
    "soundcloud": "#FF5500", # From earlier testing, ignore
}

LIB_ARTIST_COLOR   = "#BF5AF2"  # Apple System Purple
LIB_ALBUM_COLOR    = "#0A84FF"  # Apple System Blue
LIB_TRACK_COLOR    = "#30D158"  # Apple System Green
LIB_PLAYLIST_COLOR = "#FF9F0A"  # Apple System Orange / Amber
LIB_PARTITION_COLOR = "#64D2FF" # Apple System Teal / Cyan

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

