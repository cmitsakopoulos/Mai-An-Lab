#!/usr/bin/env python3
"""Export Phase 6d DSP goldens from StreamripApp/utils/dsp.py.

Writes `test/goldens/dsp.json`:
- the extractor's fixed tables (mel bin points, DCT rows, contrast edges,
  Hann window and rfft frequencies, chroma pitch classes)
- `extract_features_from_pcm` on synthetic signals built from integer-only
  generators (an LCG, square/triangle waves with integer periods), so the
  Dart test regenerates bit-identical PCM without committing audio.

With `--real N` (or explicit audio paths after `--real-paths`), also decodes
N tracks from the Flet library with dsp.py's own ffmpeg command into
`tool/.dsp_cache/` (gitignored) and writes their features to
`tool/.dsp_cache/manifest.json`; test/audio_features_real_test.dart
compares against those when present.

Run from mai_an_flutter/:  ~/miniconda3/bin/python tool/export_dsp_goldens.py [--real 5]
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PY_APP = ROOT.parent / "StreamripApp"
sys.path.insert(0, str(PY_APP))

from utils import dsp  # noqa: E402

SR = dsp.TARGET_SAMPLE_RATE
CACHE = ROOT / "tool" / ".dsp_cache"


# ── Integer-only signal generators (mirrored in test/audio_features_test.dart)

class Lcg:
    def __init__(self, seed: int):
        self.s = seed & 0x7FFFFFFF

    def next(self) -> int:
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return self.s

    def sym(self, amp: int) -> int:
        """Uniform integer in [-amp, amp]."""
        return (self.next() % (2 * amp + 1)) - amp


def triangle(t: int, period: int, amp: int) -> int:
    """Integer triangle wave in [-amp, amp]."""
    ph = t % period
    half = period // 2
    if ph < half:
        return -amp + (2 * amp * ph) // half
    return amp - (2 * amp * (ph - half)) // (period - half)


def clip16(v: int) -> int:
    return max(-32768, min(32767, v))


def gen(kind: str, n: int, seed: int = 1) -> list[int]:
    rng = Lcg(seed)
    out = []
    if kind == "silence":
        return [0] * n
    if kind == "noise":
        return [rng.sym(8000) for _ in range(n)]
    if kind == "chord_am":
        # A minor (A3, C4, E4) as integer-period triangles.
        return [clip16(triangle(t, 100, 6000) + triangle(t, 84, 5000)
                       + triangle(t, 67, 5000)) for t in range(n)]
    if kind == "chord_g":
        # G major (G3, B3, D4).
        return [clip16(triangle(t, 112, 6000) + triangle(t, 89, 5000)
                       + triangle(t, 75, 5000)) for t in range(n)]
    if kind.startswith("beat_"):
        bpm = int(kind.split("_")[1])
        beat = SR * 60 // bpm
        half = beat // 2
        for t in range(n):
            v = 0
            k = t % beat
            if k < 2000:  # kick: decaying 49 Hz square
                g = 20000 * (2000 - k) // 2000
                v += g if (k // 225) % 2 == 0 else -g
            h = t % half
            if h < 400:  # hat: decaying noise burst every half beat
                v += rng.sym(5000) * (400 - h) // 400
            else:
                rng.next()
            v += triangle(t, 100, 1500) + triangle(t, 67, 1200)
            out.append(clip16(v))
        return out
    raise ValueError(kind)


SIGNALS = [
    # (name, kind, seconds or sample count)
    ("beat_120", "beat_120", 20 * SR),
    ("beat_95", "beat_95", 20 * SR),
    ("beat_174", "beat_174", 12 * SR),
    ("chord_am", "chord_am", 8 * SR),
    ("chord_g", "chord_g", 8 * SR),
    ("noise", "noise", 6 * SR),
    ("silence", "silence", 2 * SR),
    ("short", "beat_120", 1000),        # < one frame: zero-padded
    ("tiny", "noise", 2048 + 512 * 5),  # 6 frames: reflect-padded medians
]


def features_json(f: dsp.Features) -> dict:
    return {
        "bpm": float(f.bpm),
        "energy": float(f.energy),
        "brightness": float(f.brightness),
        "rolloff": float(f.rolloff),
        "beat_strength": float(f.beat_strength),
        "spectral_flatness": float(f.spectral_flatness),
        "spectral_contrast": float(f.spectral_contrast),
        "key_index": int(f.key_index),
        "mfcc_mean": [float(v) for v in f.mfcc_mean],
        "mfcc_std": [float(v) for v in f.mfcc_std],
        "chroma": [float(v) for v in f.chroma],
        "rhythm": [float(v) for v in f.rhythm],
        "timbre_len": len(f.timbre_blob()),
    }


def extract_samples(samples: list[int]) -> dsp.Features:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "_synthetic.pcm"
    np.asarray(samples, dtype="<i2").tofile(p)
    try:
        return dsp.extract_features_from_pcm(str(p), SR)
    finally:
        p.unlink(missing_ok=True)


def tables() -> dict:
    fb = dsp._mel_filterbank(SR, dsp.N_FFT, dsp.N_MELS)
    mel_pts = np.linspace(dsp._hz_to_mel(0.0), dsp._hz_to_mel(SR / 2.0), dsp.N_MELS + 2)
    bin_pts = np.clip(np.floor((dsp.N_FFT + 1) * dsp._mel_to_hz(mel_pts) / SR).astype(int),
                      0, dsp.N_FFT // 2)
    dct = dsp._dct_matrix(dsp.N_MELS, dsp.N_MFCC + 1)
    freqs = np.fft.rfftfreq(dsp.N_FFT, d=1.0 / SR).astype(np.float32)
    valid = (freqs >= 80.0) & (freqs <= 5000.0)
    pc = (np.round(12.0 * np.log2(freqs[valid] / 440.0)).astype(int)) % 12
    return {
        "constants": {
            "features_version": dsp.FEATURES_VERSION,
            "n_fft": dsp.N_FFT, "hop": dsp.HOP, "n_mels": dsp.N_MELS,
            "n_mfcc": dsp.N_MFCC, "n_chroma": dsp.N_CHROMA,
            "rhythm_dims": dsp.RHYTHM_DIMS, "embed_dims": dsp.EMBED_DIMS,
            "max_seconds": dsp.MAX_SECONDS, "sample_rate": SR,
        },
        "mel_bin_points": [int(v) for v in bin_pts],
        "mel_row_sums": [float(v) for v in fb.sum(axis=1)],
        "dct_rows": {str(k): [float(v) for v in dct[k]] for k in (0, 1, 7, 20)},
        "contrast_edges": [int(v) for v in np.linspace(0, dsp.N_FFT // 2 + 1, 7, dtype=int)],
        "hann_samples": {str(i): float(np.hanning(dsp.N_FFT).astype(np.float32)[i])
                         for i in (0, 1, 100, 1023, 1024, 2047)},
        "freq_samples": {str(i): float(freqs[i]) for i in (0, 1, 18, 19, 186, 464, 1024)},
        "chroma_first_bin": int(np.argmax(valid)),
        "chroma_pcs": [int(v) for v in pc],
    }


def export_synthetic() -> None:
    out = {"tables": tables(), "signals": {}}
    for name, kind, n in SIGNALS:
        s = gen(kind, n)
        f = extract_samples(s)
        out["signals"][name] = {
            "kind": kind, "samples": n,
            "checksum": int(sum((i + 1) * v for i, v in enumerate(s)) % 1000000007),
            "features": features_json(f),
        }
        print(f"{name:10s} bpm={f.bpm:7.2f} key={f.key_index:2d} "
              f"energy={f.energy:.4f} flat={f.spectral_flatness:.4f}")
    dst = ROOT / "test" / "goldens" / "dsp.json"
    dst.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {dst}")


def export_real(paths: list[str]) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, path in enumerate(paths):
        # dsp.py's own ffmpeg decode, then a stable copy for the Dart test.
        pcm, sr = asyncio.run(dsp._decode_pcm_ffmpeg(path))
        dst = CACHE / f"real_{i}.pcm"
        os.replace(pcm, dst)
        f = dsp.extract_features_from_pcm(str(dst), sr)
        manifest.append({"source": os.path.basename(path), "pcm": dst.name,
                         "sample_rate": sr, "features": features_json(f)})
        print(f"real_{i}: {os.path.basename(path)} bpm={f.bpm:.2f} key={f.key_index}")
    (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"wrote {CACHE / 'manifest.json'}")


def flet_library_paths(n: int) -> list[str]:
    db = PY_APP / ".flet" / "storage" / "data" / "library.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT path FROM tracks ORDER BY id").fetchall()
    con.close()
    paths = [r[0] for r in rows if os.path.exists(r[0])]
    if not paths:
        return []
    step = max(1, len(paths) // n)
    return paths[::step][:n]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", type=int, default=0,
                    help="also export N tracks from the Flet library")
    ap.add_argument("--real-paths", nargs="*", default=[])
    ap.add_argument("--skip-synthetic", action="store_true")
    a = ap.parse_args()
    if not a.skip_synthetic:
        export_synthetic()
    real = list(a.real_paths) or (flet_library_paths(a.real) if a.real else [])
    if real:
        export_real(real)
