"""
Audio Transcoding Service for Mai-An Lab Companion Sync.
Wraps ffmpeg with Apple AudioToolbox (aac_at) support for Bluetooth-optimized M4A/AAC.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sync.transcoder")


class TranscodeProfile(str, Enum):
    ORIGINAL = "original"
    AAC_VBR_MAX = "aac_vbr_max"      # AudioToolbox q=0 VBR or 320k
    AAC_320K = "aac_320k"            # CBR 320 kbps
    AAC_256K = "aac_256k"            # CBR 256 kbps (Bluetooth sweet spot)
    AAC_192K = "aac_192k"            # CBR 192 kbps


# A .app launched from Finder gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, which
# never contains Homebrew; only a terminal launch would find ffmpeg on PATH.
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")


def find_tool(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        cand = os.path.join(d, name)
        if os.access(cand, os.X_OK):
            return cand
    return None


def check_ffmpeg() -> tuple[bool, bool, bool]:
    """Verify ffmpeg is installed and detect supported encoders (ffmpeg_ok, has_aac_at, has_aac)."""
    ffmpeg_path = find_tool("ffmpeg")
    if not ffmpeg_path:
        return False, False, False
    try:
        result = subprocess.run(
            [ffmpeg_path, "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=5,
        )
        encoders = result.stdout
        return True, ("aac_at" in encoders), ("aac" in encoders)
    except Exception as ex:
        logger.warning("ffmpeg probe failed: %s", ex)
        return False, False, False


def get_sample_rate(input_path: str) -> int:
    """Query audio sample rate using ffprobe."""
    try:
        result = subprocess.run(
            [
                find_tool("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate",
                "-of", "default=noprint_wrappers=1:nokey=1", input_path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=5,
        )
        return int(result.stdout.strip())
    except Exception:
        return 44100


async def transcode_audio(
    input_path: str,
    profile: TranscodeProfile = TranscodeProfile.AAC_VBR_MAX,
    output_dir: Optional[str] = None,
) -> str:
    """Transcode input audio file according to the selected profile.
    If profile is ORIGINAL, returns input_path directly.
    Otherwise, produces an .m4a file in output_dir (or a temp directory) and returns its path.
    """
    if profile == TranscodeProfile.ORIGINAL or not input_path:
        return input_path

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input audio does not exist: {input_path}")

    ffmpeg_ok, has_aac_at, has_aac = await asyncio.to_thread(check_ffmpeg)
    if not ffmpeg_ok:
        logger.warning("ffmpeg not available on host — returning original audio as-is")
        return input_path

    encoder = "aac_at" if has_aac_at else "aac"
    sample_rate = await asyncio.to_thread(get_sample_rate, input_path)

    out_dir = output_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    
    # Keyed on the full path, not the stem: "01. Intro" exists on many albums,
    # and a stem-keyed cache served one album's intro for another's.
    digest = hashlib.sha1(input_path.encode("utf-8")).hexdigest()[:12]
    out_path = os.path.join(out_dir, f"{Path(input_path).stem[:60]}.{digest}.transcode_{profile.value}.m4a")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        try:
            if os.path.getmtime(out_path) >= os.path.getmtime(input_path):
                logger.debug("Reusing cached transcode: %s", out_path)
                return out_path
        except OSError:
            pass

    cmd = [
        find_tool("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-c:a", encoder,
    ]

    # Downsample if > 48000Hz for Bluetooth A2DP compatibility
    if sample_rate > 48000:
        cmd.extend(["-ar", "48000"])

    if encoder == "aac_at":
        if profile == TranscodeProfile.AAC_VBR_MAX:
            cmd.extend([
                "-aac_at_mode", "3",       # Variable Bitrate (VBR)
                "-q:a", "0",               # Highest quality level
                "-aac_at_quality", "0",    # Maximum encoder effort
            ])
        elif profile == TranscodeProfile.AAC_320K:
            cmd.extend(["-b:a", "320k"])
        elif profile == TranscodeProfile.AAC_256K:
            cmd.extend(["-b:a", "256k"])
        elif profile == TranscodeProfile.AAC_192K:
            cmd.extend(["-b:a", "192k"])
    else:  # native aac
        if profile in (TranscodeProfile.AAC_VBR_MAX, TranscodeProfile.AAC_320K):
            cmd.extend(["-b:a", "320k"])
        elif profile == TranscodeProfile.AAC_256K:
            cmd.extend(["-b:a", "256k"])
        elif profile == TranscodeProfile.AAC_192K:
            cmd.extend(["-b:a", "192k"])

    # Write to a temp name and rename: a concurrent request (HEAD then GET, or
    # two phones) must never be handed a half-written cache file.
    tmp_path = out_path + f".{os.getpid()}.{id(cmd)}.part.m4a"
    audio_only = cmd + ["-map", "0:a:0", "-map_metadata", "0", "-movflags", "+faststart", tmp_path]
    # Cover art rides along as an attached picture when the container allows.
    with_art = cmd + [
        "-map", "0:a:0", "-map", "0:v:0?", "-c:v", "copy",
        "-disposition:v:0", "attached_pic", "-map_metadata", "0",
        "-movflags", "+faststart", tmp_path,
    ]

    def _run():
        try:
            res = subprocess.run(with_art, capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                # Some art streams (PNG, odd sizes) won't mux into MP4; the
                # audio matters more than the picture.
                logger.debug("transcode with art failed (%s); retrying audio-only", res.stderr.strip())
                res = subprocess.run(audio_only, capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                raise RuntimeError(f"ffmpeg transcode failed: {res.stderr.strip()}")
            os.replace(tmp_path, out_path)
            return out_path
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return await asyncio.to_thread(_run)

