"""
Pure-numpy audio feature extraction for the AutoPlaylist engine.

The pipeline is:

    audio file ──(decoder)──> raw int16 mono PCM @ 22050 Hz
                                        │
                                        ▼
                          extract_features_from_pcm
                            │  (RMS energy, spectral
                            │   centroid, onset-flux
                            │   tempo, MFCC mean)
                            ▼
                     {bpm, energy, brightness, timbre[13]}

The decoder is platform-specific:
  • Android   → flet_audio_service.decode_pcm (MediaCodec)
  • Desktop   → ffmpeg subprocess (or skip if ffmpeg missing)

Math notes: every step is intentionally textbook so future-you can audit it:

  • RMS energy: sqrt(mean(x^2)) on samples in [-1, 1]. Mapped to [0, 1] via a
    dB curve where -60 dBFS → 0 and 0 dBFS → 1. Loudness is roughly linear
    in dB perceptually, so this gives a more useful similarity than raw RMS.

  • Spectral centroid: sum(f * |X|) / sum(|X|) per frame, averaged across
    frames, then divided by Nyquist (sr/2) so the output sits in [0, 1].
    This is the canonical "brightness" descriptor.

  • Tempo: spectral-flux onset envelope → autocorrelation → peak in the lag
    range [60, 200] BPM. A log-Gaussian prior centred on 120 BPM weights the
    autocorrelation so we don't double/halve on borderline cases.

  • MFCC: power spectrum → mel filterbank (40 bands) → log → DCT-II →
    drop coeff 0 (DC / overall loudness, already captured by energy) →
    keep 13 coeffs → mean across frames. This is a compact timbre vector
    that separates "warm pads" from "bright synth leads" even at the same
    tempo and energy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger("dsp")

# Output of the analyser that gets persisted. The features_version is bumped
# when the extraction semantics change so callers can decide whether to honour
# cached values. Keep this in sync with the DB column of the same name.
#
# v1: 13-dim MFCC mean only (timbre BLOB = 13 floats).
# v2: 38-dim sound profile (mfcc_mean[13] + mfcc_std[13] + chroma[12]) plus
#     spectral_rolloff and beat_strength scalars. 90 s decode window (was 60).
FEATURES_VERSION = 2

# Frame parameters. n_fft=2048 / hop=512 at 22050 Hz → ~93 ms windows hopping
# every ~23 ms (~43 frames/sec). These are the conventional defaults from
# librosa and most music IR papers; they balance frequency resolution
# (enough for spectral centroid + mel) with onset time accuracy.
N_FFT = 2048
HOP = 512
N_MELS = 40
N_MFCC = 13
N_CHROMA = 12
# Total length of the packed sound-profile BLOB (mfcc_mean + mfcc_std + chroma).
# Stored as float32 little-endian; kept as a single BLOB rather than three
# columns so adding a new descriptor only touches FEATURES_VERSION.
EMBED_DIMS = N_MFCC + N_MFCC + N_CHROMA
TARGET_SAMPLE_RATE = 22050  # must match the Kotlin decoder's TARGET_SAMPLE_RATE


@dataclass(frozen=True)
class Features:
    """Per-track feature vector. Scalar fields go in their own DB columns;
    the high-dim sound profile is packed into one BLOB.

    Naming `timbre` is kept on the BLOB API (see `timbre_blob`/`unpack_timbre`)
    for back-compat with v1 callers. Conceptually it's a sound-profile
    embedding now, not just MFCC mean.
    """
    bpm: float                 # estimated tempo in BPM, 0.0 if undetermined
    energy: float              # [0, 1] perceived loudness (dB-mapped RMS)
    brightness: float          # [0, 1] mean spectral centroid / Nyquist
    rolloff: float             # [0, 1] mean 85-percentile rolloff / Nyquist
    beat_strength: float       # [0, 1] prominence of the chosen tempo peak
    mfcc_mean: np.ndarray      # (N_MFCC,) timbre; average MFCC across frames
    mfcc_std: np.ndarray       # (N_MFCC,) timbral dynamics; frame-to-frame variance
    chroma: np.ndarray         # (N_CHROMA,) mean pitch-class energy → harmonic profile

    def timbre_blob(self) -> bytes:
        """Pack mfcc_mean + mfcc_std + chroma into a single float32 LE BLOB.

        Layout (38 × 4 = 152 bytes):
            [ 0:13)  mfcc_mean
            [13:26)  mfcc_std
            [26:38)  chroma
        """
        return (
            np.concatenate([self.mfcc_mean, self.mfcc_std, self.chroma])
            .astype("<f4")
            .tobytes()
        )


def unpack_timbre(blob: bytes | None) -> np.ndarray | None:
    """Inverse of Features.timbre_blob. Returns None for missing/malformed.

    Returns the full 38-dim vector. Callers that need the individual groups
    can use `unpack_embedding_groups`. The single-vector form is what the
    AutoPlaylistEngine consumes; it slices internally.
    """
    if not blob or len(blob) != EMBED_DIMS * 4:
        return None
    return np.frombuffer(blob, dtype="<f4").copy()


def unpack_embedding_groups(blob: bytes | None):
    """Returns (mfcc_mean, mfcc_std, chroma) or None for missing/malformed."""
    v = unpack_timbre(blob)
    if v is None:
        return None
    return (
        v[0:N_MFCC],
        v[N_MFCC:2 * N_MFCC],
        v[2 * N_MFCC:],
    )


# ─── Decoder dispatch ──────────────────────────────────────────────────────

def is_android() -> bool:
    # Flet on Android sets ANDROID_DATA / ANDROID_ROOT in the runtime env.
    # sys.platform is 'linux' there, so we can't rely on it alone.
    return "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ


async def decode_to_pcm(audio_service, path: str) -> tuple[str, int]:
    """Decode audio file → raw int16 LE mono PCM file. Returns (path, sr).

    On Android, calls into the flet_audio_service Kotlin decoder. On non-Android
    environments (development fallback), shells out to ffmpeg.
    Raises RuntimeError if no decoder is available.
    """
    if is_android():
        if audio_service is None:
            raise RuntimeError("audio_service not available for decode")
        result = await audio_service.decode_pcm(path)
        return str(result["output_path"]), int(result["sample_rate"])
    return await _decode_pcm_ffmpeg(path)


async def _decode_pcm_ffmpeg(path: str) -> tuple[str, int]:
    """Desktop fallback. Uses ffmpeg to write headerless s16le mono PCM at
    TARGET_SAMPLE_RATE. Decodes the middle 60s to mirror the Android path."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found on PATH; install it (`brew install ffmpeg`) or "
            "skip DSP analysis in this environment"
        )

    # Cache PCM next to the source under a hashed filename so re-analysis is
    # cheap. Falls back to a tempdir if the source dir isn't writable.
    cache_dir = os.path.join(tempfile.gettempdir(), "dsp_pcm")
    os.makedirs(cache_dir, exist_ok=True)
    out_path = os.path.join(cache_dir, f"{abs(hash(path))}.pcm")

    # We don't know the duration up front without an extra ffprobe call. Just
    # ask ffmpeg to skip 15s and read 90s; for short tracks this still
    # does the right thing (it just reads to EOF). The window
    # matches the Kotlin path's MAX_SECONDS.
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", "15",                     # skip first 15s (intro)
        "-t", "90",                      # cap at 90s; matches Kotlin
        "-i", path,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ac", "1",                      # downmix to mono
        "-ar", str(TARGET_SAMPLE_RATE),
        "-y", out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # If the seek-past-EOF failed (very short track), retry without -ss.
        cmd_retry = [c for c in cmd if c not in ("-ss", "30")]
        proc = await asyncio.create_subprocess_exec(
            *cmd_retry,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed for {path}: {stderr.decode(errors='ignore')}"
            )
    return out_path, TARGET_SAMPLE_RATE


# ─── Feature extraction ────────────────────────────────────────────────────

def load_pcm(path: str) -> np.ndarray:
    """Load int16 LE mono PCM file as float32 in [-1, 1]."""
    raw = np.fromfile(path, dtype="<i2")
    if raw.size == 0:
        raise ValueError(f"empty PCM file: {path}")
    return raw.astype(np.float32) / 32768.0


def _frame(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """Slice signal into overlapping frames. Returns shape (n_frames, n_fft).

    Pads the tail with zeros so the last partial frame is included; this only
    affects the very last few frames so it's fine for our average statistics.
    """
    if x.size < n_fft:
        # Pad short signals up to one frame so downstream code doesn't crash.
        x = np.pad(x, (0, n_fft - x.size))
    n_frames = 1 + (x.size - n_fft) // hop
    # stride_tricks gives a view; no copy. Read-only because writing would
    # corrupt overlapping frames.
    return np.lib.stride_tricks.as_strided(
        x,
        shape=(n_frames, n_fft),
        strides=(x.strides[0] * hop, x.strides[0]),
        writeable=False,
    )


def _stft_magnitude(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Returns (magnitude_spectrogram[n_frames, n_fft//2+1], freqs[n_fft//2+1])."""
    frames = _frame(x, N_FFT, HOP)
    # Hann window: standard choice; reduces spectral leakage. .copy() because
    # frames is a read-only stride view and we need to multiply in place...
    # actually rfft accepts read-only input, so we can multiply via broadcast.
    window = np.hanning(N_FFT).astype(np.float32)
    spec = np.fft.rfft(frames * window, n=N_FFT, axis=1)
    mag = np.abs(spec).astype(np.float32)
    freqs = np.fft.rfftfreq(N_FFT, d=1.0 / sr).astype(np.float32)
    return mag, freqs


def _energy_db(x: np.ndarray) -> float:
    """RMS energy mapped to [0, 1] via dB. Silence ≈ 0, full-scale ≈ 1."""
    rms = float(np.sqrt(np.mean(x * x) + 1e-12))
    db = 20.0 * np.log10(rms + 1e-9)  # dBFS, ranges roughly [-90, 0]
    # Linear ramp from -60 dB → 0 to 0 dB → 1. -60 dB is ~ inaudible-quiet.
    return float(np.clip((db + 60.0) / 60.0, 0.0, 1.0))


def _brightness(mag: np.ndarray, freqs: np.ndarray, sr: int) -> float:
    """Mean spectral centroid normalised to [0, 1] by Nyquist."""
    # Per-frame centroid = sum(f * |X|) / sum(|X|). Frames with near-zero
    # magnitude (silence) get assigned centroid 0 to avoid NaN.
    mag_sum = mag.sum(axis=1)
    safe = mag_sum > 1e-6
    centroids = np.zeros(mag.shape[0], dtype=np.float32)
    centroids[safe] = (mag[safe] * freqs[None, :]).sum(axis=1) / mag_sum[safe]
    if not safe.any():
        return 0.0
    mean_centroid = float(centroids[safe].mean())
    return float(np.clip(mean_centroid / (sr / 2.0), 0.0, 1.0))


def _spectral_rolloff(mag: np.ndarray, freqs: np.ndarray, sr: int,
                      pct: float = 0.85) -> float:
    """Mean spectral rolloff at percentile `pct`, normalised to [0, 1] by
    Nyquist. The rolloff frequency is the lowest f below which `pct` of the
    spectrum's energy lies. Complements `brightness` (centroid); two tracks
    can have similar centroids but very different roll-offs (e.g. a bright
    pad vs a track with a sharp high-frequency cymbal hit)."""
    cum = np.cumsum(mag, axis=1)
    totals = cum[:, -1:]
    safe = totals[:, 0] > 1e-6
    if not safe.any():
        return 0.0
    threshold = pct * totals[safe]
    # For each safe frame, find the first bin index where cumsum >= threshold.
    # argmax over a boolean array returns the first True index; exactly what
    # we want; provided at least one True exists, which it does because
    # cum[:, -1] == totals.
    idx = np.argmax(cum[safe] >= threshold, axis=1)
    rolloff_hz = freqs[idx]
    return float(np.clip(rolloff_hz.mean() / (sr / 2.0), 0.0, 1.0))


def _chroma_mean(mag: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """Mean chromagram across frames. Returns shape (N_CHROMA,), L1-normalised
    so it represents pitch-class *proportion* rather than loudness.

    For each FFT bin we compute its pitch class as
        pc = round(12 * log2(f / 440)) mod 12
    using A4 = 440 Hz as reference. Bins below 80 Hz (sub-bass rumble) and
    above 5 kHz (predominantly noise / harmonics with weak pitch identity)
    are excluded. Per-frame chroma is the sum of magnitudes per pitch class;
    we L1-normalise per frame so a quiet harmonic frame contributes equally
    to a loud one; chroma is about *which notes*, not *how loud*."""
    valid = (freqs >= 80.0) & (freqs <= 5000.0)
    f = freqs[valid]
    if f.size == 0:
        return np.zeros(N_CHROMA, dtype=np.float32)
    pc = (np.round(12.0 * np.log2(f / 440.0)).astype(int)) % N_CHROMA
    # Bucket the magnitudes per pitch class. For each frame we want
    # sum of mag[:, valid] grouped by pc. np.add.at gives unbuffered
    # accumulation across frames in one call.
    n_frames = mag.shape[0]
    bucket = np.zeros((n_frames, N_CHROMA), dtype=np.float32)
    sub = mag[:, valid]                              # (n_frames, n_valid_bins)
    for c in range(N_CHROMA):
        cols = np.where(pc == c)[0]
        if cols.size:
            bucket[:, c] = sub[:, cols].sum(axis=1)
    # L1 normalise per frame so quiet/loud frames contribute equally.
    row_sum = bucket.sum(axis=1, keepdims=True)
    safe = row_sum[:, 0] > 1e-6
    if not safe.any():
        return np.zeros(N_CHROMA, dtype=np.float32)
    norm = bucket[safe] / row_sum[safe]
    return norm.mean(axis=0).astype(np.float32)


def _onset_envelope(mag: np.ndarray) -> np.ndarray:
    """Spectral flux onset envelope. Sums positive frame-to-frame differences
    of log-magnitude across all frequency bins. Standard onset detector."""
    log_mag = np.log1p(mag)  # log(1+x); keeps zeros tame, no -inf
    diff = np.diff(log_mag, axis=0)
    flux = np.maximum(diff, 0.0).sum(axis=1)
    # Subtract a local mean to suppress slow drifts; this is what makes the
    # autocorrelation peak at the beat lag instead of at lag 0.
    if flux.size > 8:
        # Simple boxcar high-pass: subtract running mean over ~250 ms.
        win = max(8, flux.size // 32)
        kernel = np.ones(win, dtype=np.float32) / win
        local_mean = np.convolve(flux, kernel, mode="same")
        flux = np.maximum(flux - local_mean, 0.0)
    # Normalise so autocorrelation peak heights are comparable.
    s = flux.std() + 1e-9
    return (flux - flux.mean()) / s


def _estimate_bpm(onset_env: np.ndarray, sr: int) -> tuple[float, float]:
    """Autocorrelation-based BPM estimate over the onset envelope.

    Returns (bpm, beat_strength). `beat_strength` is the autocorrelation
    height at the chosen lag, normalised so ac[0]=1; it's roughly in [0, 1]
    and acts as a confidence/prominence score (steady kick-driven dance
    tracks land near 0.4–0.7, drone/ambient stays near 0).

    Lag k corresponds to a period of (k * HOP / sr) seconds, i.e. a tempo of
    60 / (k * HOP / sr) BPM. We search lags whose tempo falls in [60, 200]
    BPM and weight by a log-Gaussian prior centred on 120 BPM to discourage
    octave-error doublings (e.g. reporting 160 BPM for an 80 BPM track).
    """
    n = onset_env.size
    if n < 16:
        return 0.0, 0.0

    # Compute autocorrelation via FFT (linear, zero-padded). Standard trick:
    # autocorrelation = IFFT(|FFT(x)|^2) on a zero-padded signal.
    pad = 1 << (int(np.ceil(np.log2(2 * n))))
    spec = np.fft.rfft(onset_env, n=pad)
    ac = np.fft.irfft(spec * np.conj(spec), n=pad)[:n]
    ac = ac / (ac[0] + 1e-9)  # normalise so ac[0] = 1

    # Map lag → BPM. Lag in frames; one frame = HOP/sr seconds.
    lags = np.arange(1, n)
    bpms = 60.0 * sr / (lags * HOP)
    valid = (bpms >= 60.0) & (bpms <= 200.0)
    if not valid.any():
        return 0.0, 0.0

    # Log-Gaussian prior on 120 BPM, sigma ≈ 0.9 octaves (broad: we only
    # want to break octave ties, not force every track to 120).
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    score = ac[1:] * prior
    score[~valid] = -np.inf
    best = int(np.argmax(score))
    if not np.isfinite(score[best]):
        return 0.0, 0.0
    bpm = float(bpms[best])
    # Beat strength = the *unweighted* AC height at the chosen lag. The prior
    # only steers selection; the reported strength should reflect the actual
    # rhythmic regularity, not how close to 120 BPM the track happens to be.
    strength = float(np.clip(ac[1:][best], 0.0, 1.0))
    return bpm, strength


# ─── Mel filterbank + MFCC ─────────────────────────────────────────────────

_MEL_BANK_CACHE: dict[tuple[int, int, int, int], np.ndarray] = {}


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (10.0 ** (np.asarray(mel) / 2595.0) - 1.0)


def _mel_filterbank(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
    """Build a (n_mels, n_fft//2+1) triangular mel filterbank.

    Standard Slaney-style construction: n_mels+2 mel-spaced points, each
    filter is a triangle whose peak is at the (i+1)-th point.
    """
    key = (sr, n_fft, n_mels, 0)
    cached = _MEL_BANK_CACHE.get(key)
    if cached is not None:
        return cached

    fmin, fmax = 0.0, sr / 2.0
    mel_pts = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2)
    hz_pts = _mel_to_hz(mel_pts)
    bin_pts = np.floor((n_fft + 1) * hz_pts / sr).astype(int)
    bin_pts = np.clip(bin_pts, 0, n_fft // 2)

    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        l, c, r = bin_pts[i], bin_pts[i + 1], bin_pts[i + 2]
        if c == l or c == r:
            # Degenerate filter (happens at very low frequencies). Skip: the
            # row stays zero and that band contributes nothing, which is
            # better than a divide-by-zero.
            continue
        fb[i, l:c] = (np.arange(l, c) - l) / (c - l)
        fb[i, c:r] = (r - np.arange(c, r)) / (r - c)
    _MEL_BANK_CACHE[key] = fb
    return fb


_DCT_CACHE: dict[tuple[int, int], np.ndarray] = {}


def _dct_matrix(n_in: int, n_out: int) -> np.ndarray:
    """Type-II DCT matrix (n_out x n_in). Same convention as scipy with
    norm='ortho' so coefficients are scale-comparable across configurations."""
    key = (n_in, n_out)
    cached = _DCT_CACHE.get(key)
    if cached is not None:
        return cached
    n = np.arange(n_in)
    k = np.arange(n_out)[:, None]
    mat = np.cos(np.pi * k * (2 * n + 1) / (2 * n_in)).astype(np.float32)
    # Ortho normalisation: row 0 gets 1/sqrt(N), others get sqrt(2/N).
    mat[0, :] *= 1.0 / np.sqrt(n_in)
    mat[1:, :] *= np.sqrt(2.0 / n_in)
    _DCT_CACHE[key] = mat
    return mat


def _mfcc_per_frame(mag: np.ndarray, sr: int) -> np.ndarray:
    """Per-frame MFCC matrix. Returns (n_frames, N_MFCC) float32.

    Pipeline: power spectrum → mel filterbank → log → DCT-II → drop coeff 0
    (overall loudness, already captured by `energy`) → take coeffs [1, N_MFCC].
    The caller computes mean and std along axis 0; keeping per-frame around
    lets us derive both descriptors from a single FFT pass.
    """
    power = mag * mag
    fb = _mel_filterbank(sr, N_FFT, N_MELS)
    mel = power @ fb.T  # (n_frames, n_mels)
    log_mel = np.log(mel + 1e-9)
    dct = _dct_matrix(N_MELS, N_MFCC + 1)
    mfcc_full = log_mel @ dct.T  # (n_frames, N_MFCC+1)
    return mfcc_full[:, 1:].astype(np.float32)  # drop coeff 0


def extract_features_from_pcm(pcm_path: str, sr: int) -> Features:
    """Run the full feature pipeline on a decoded PCM file.

    All numpy work is synchronous; callers should `asyncio.to_thread` this if
    they're on the main loop.
    """
    x = load_pcm(pcm_path)
    mag, freqs = _stft_magnitude(x, sr)

    energy = _energy_db(x)
    brightness = _brightness(mag, freqs, sr)
    rolloff = _spectral_rolloff(mag, freqs, sr)
    chroma = _chroma_mean(mag, freqs)
    onset_env = _onset_envelope(mag)
    bpm, beat_strength = _estimate_bpm(onset_env, sr)
    mfcc = _mfcc_per_frame(mag, sr)
    mfcc_mean = mfcc.mean(axis=0).astype(np.float32)
    mfcc_std = mfcc.std(axis=0).astype(np.float32)

    return Features(
        bpm=bpm,
        energy=energy,
        brightness=brightness,
        rolloff=rolloff,
        beat_strength=beat_strength,
        mfcc_mean=mfcc_mean,
        mfcc_std=mfcc_std,
        chroma=chroma,
    )


async def analyze_track(audio_service, path: str) -> Features:
    """High-level: decode, then extract. Cleans up the temp PCM file afterwards
    to avoid filling the cache on long sessions (the cache lives in app
    storage and isn't auto-pruned by the OS)."""
    pcm_path, sr = await decode_to_pcm(audio_service, path)
    try:
        return await asyncio.to_thread(extract_features_from_pcm, pcm_path, sr)
    finally:
        try:
            os.remove(pcm_path)
        except OSError:
            pass
