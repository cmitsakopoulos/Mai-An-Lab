# Auto-Playlist Engine: $k$-NN Similarity Graph & Walkers

> [!NOTE]
> **Production Status**; The music recommendation system has been upgraded from discrete Markov Clustering (MCL) buckets to a continuous, high-performance **$k$-NN Hybrid Similarity Graph**. This provides the fluid, adaptive, and infinite transitions necessary for the voice assistant's playback pipelines.

> [!TIP]
> **Bioinformatics Origins**; This engine was inspired by similarity matrices in bioinformatics (mapping tracks to a multi-dimensional feature space and building a sparse topological network for traversal walks instead of discrete buckets).

---

## The Hybrid Recommendation Pipeline

The engine bridges low-level digital signal processing (DSP) and high-level relational metadata to construct a sparse, dual-tier directed network of similar tracks:

```text
audio file ──► MediaCodec (Android) ──► mono int16 PCM @ 22050 Hz (90s)
                                                   │
                                                   ▼
                                        Pure-Numpy DSP Analyser
                                                   │
        ┌─────────────────────┬────────────────────┼────────────────────┬─────────────────────┐
        ▼                     ▼                    ▼                    ▼                     ▼
  RMS energy (dB)      Spectral centroid    Spectral rolloff   Onset-flux autocorr   Mel-filterbank STFT
                        (brightness)         (85th-pct freq)    + 120-BPM prior              │
                                                                 │                            ▼
                                                                 ▼                  log → DCT-II → MFCC
                                                           BPM + beat strength               │
                                                                                   ┌──────────┼──────────┐
                                                                                   ▼          ▼          ▼
                                                                              MFCC mean  MFCC Δ-mean  chroma
                                                                                 (20)        (20)       (12)
                                                                                                    (52-D Timbre)
                                                   │
                                                   ▼
                       60-D Unified Feature Vector (52-D Timbre + 8 Scalars, v3)
                                                   │
                                                   ▼
                                        Z-Score Column Standardisation
                                                   │
                                                   ▼
                                         L2 Row Normalisation
                                                   │
                                                   ▼
                                  Acoustic Cosine Similarity Graph 
                                      (Top K=20 nearest neighbours)
                                                   │
         Junction Metadata Edges ◄─────────────────┼────────────────► Same-Artist / Album Edges
      (Album sequence / Recent additions)          │                  (Biased distance weighting)
                                                   ▼
                                     HYBRID k-NN ADJACENCY NETWORK
                                                   │
                                                   ▼
                                   Biased Random Walks (avoidance lists)
                                                   │
                                                   ▼
                                    Seamless Dynamic Playback Queue
```

---

## 1. Feature Extraction & Vector Space (v3)

Acoustic vectors are extracted by a pure-NumPy DSP analyzer (no Librosa, no SciPy — works on every host). The analyser is invoked from the laptop-side offload script (see §5); the on-device fallback exists but is no longer the primary path because library-wide DSP on a phone is impractically slow (10+ s/track vs ~200 ms/track on a laptop).

### The 60-Dimensional Feature Space (v3, `FEATURES_VERSION = 3`)
The analyser produces a 60-dimensional descriptor per track, split between a 52-dim packed timbre BLOB and 8 scalar columns:

1. **52-Dimensional Timbre Profile** (packed as one `float32` LE BLOB on `play_counts.timbre`):
   * **MFCC Mean (20 dimensions)**: Average spectral envelope of the HPSS-harmonic component (cleaner timbre — no kick-drum bleed into the mel cepstrum).
   * **MFCC Δ-Mean (20 dimensions)**: First-order temporal derivative of MFCC, averaged across frames. Captures *how* timbre evolves — strictly more informative than raw standard deviation, which can't tell a smooth drift apart from rapid oscillation between two stable timbres.
   * **Chroma Pitch Profile (12 dimensions)**: Pitch-class profile of the HPSS-harmonic component; the basis for both the harmonic-similarity score and the Krumhansl–Schmuckler key estimate.

2. **8-Dimensional Scalar Descriptors** (their own columns on `play_counts`):
   * **BPM**: Onset-flux autocorrelation on the HPSS-percussive component, with a log-Gaussian prior centred on 120 BPM to suppress octave doublings.
   * **Beat Strength**: Autocorrelation height at the chosen tempo lag; serves as a confidence/regularity score in `[0, 1]`.
   * **Energy (RMS)**: dB-mapped loudness in `[0, 1]`.
   * **Brightness (Spectral Centroid)**: Normalised by Nyquist to `[0, 1]`.
   * **Spectral Rolloff**: 85th-percentile cutoff frequency, normalised to `[0, 1]`.
   * **Spectral Flatness** (Wiener entropy): Per-frame geometric/arithmetic-mean ratio, averaged. Tonal content sits near 0, white noise approaches 1; metal/distorted rock pushes high while sparse acoustic stays low.
   * **Spectral Contrast**: Mean peak-to-valley dB difference across six frequency sub-bands, normalised to `[0, 1]`. Complements flatness — clear tonal peaks above a low noise floor produce high contrast, noisy textures produce low contrast.
   * **Key Index** (0–23): Krumhansl–Schmuckler key estimate; 0–11 = C through B major, 12–23 = C through B minor. Drives future "play in the same key" / "play minor-mode tracks" intents.

### Harmonic-Percussive Source Separation (HPSS)
Before MFCC, chroma and onset detection run, the magnitude spectrogram is split into harmonic and percussive components via median filtering (Fitzgerald 2010):
* A time-axis median filter (kernel ≈ 400 ms) reveals sustained harmonic content; a frequency-axis median filter (kernel ≈ 180 Hz) reveals broadband percussive transients.
* A soft Wiener-style mask combines the two filtered spectrograms into `H` and `P`.
* **MFCC + chroma** run on `H` only — much cleaner timbre and pitch estimates because kick-drum transients no longer bleed into the mel cepstrum or the chroma pitch buckets.
* **Onset detection** runs on `P` only — substantially better BPM estimates on slow, sparse or syncopated tracks where the unseparated spectrum used to confuse the autocorrelation.

Implementation is in `StreamripApp/utils/dsp.py` (`_hpss`, `_median_filter_axis`). Pure NumPy — `scipy.signal.medfilt2d` is deliberately avoided so the analyser has zero native dependencies.

---

## 2. Graph Construction Mechanics

The backend [DatabaseManager](file:///c:/Users/CHMI/Downloads/Music_Local/StreamripApp/utils/db_manager.py#L10) and [track_graph.py](file:///c:/Users/CHMI/Downloads/Music_Local/StreamripApp/utils/track_graph.py#L1) build a sparse $k$-NN database table (`track_neighbors`) split into two distinct tiers.

### A. The Acoustic Similarity Tier (`edge_kind = 'acoustic'`)
To build the acoustic similarity tier, the engine loads all tracks that have valid feature BLOBs:
* **Standardisation (Z-Scoring)**: Features have wildly different scales (e.g., BPM sits around 60–180 while MFCCs are logarithmic). The engine z-scores each dimension across the entire library:
  $$Z = \frac{X - \mu}{\sigma}$$
  This scales all axes equally, preventing tempo or volume from dominating the timbre profile.
* **Row Normalisation**: The standardized vectors are L2-normalized:
  $$\hat{Z}_i = \frac{Z_i}{\|Z_i\|_2}$$
  Taking the dot product $\hat{Z} \hat{Z}^T$ calculates cosine similarity values directly.
* **Top-K Selection ($K=20$)**: To keep database storage efficient, only the top-20 nearest neighbours per track are retained. To scale smoothly for large libraries, pairwise similarity calculation is run in 256-row batches using `np.argpartition` ($O(N)$ operations).

### B. The Metadata Co-Occurrence Tier
When acoustic data is missing or needs to be augmented, the engine falls back to relational metadata connections:
* **Same-Album Edges (`edge_kind = 'album'`)**: Connects all tracks belonging to the same album. The weight scales inversely with their track distance in the tracklist to preserve natural listening transitions:
  $$w = \frac{1.0}{1.0 + 0.1 \times (\text{distance} - 1)}$$
* **Same-Artist Edges (`edge_kind = 'artist'`)**: Links tracks from the same artist (capped at $K=30$ per track to prevent prolific artists from flooding the table), sorted and biased toward newer releases (`added_date DESC`).

---

## 3. Stateful Graph Traversal (The Random Walker)

To replace the static, blocky playlists produced by traditional clustering (MCL), the voice assistant utilizes **stateful random walks** to generate smooth, continuous listening arcs:

### Weighted Biased Choices
When a user says *"play more like this"*, the walker begins traversing the graph starting at the current track's seed path:
1. **Cosine Power Weighting**: To ensure recommendations remain highly relevant while retaining enough variety to prevent loops, candidate transition weights are squared ($w^2$), biasing the random walker heavily toward close acoustic neighbours.
2. **Dynamic Avoidance Lists**: The player passes an active exclusion set (`avoid`) containing recently played track paths. The walker avoids these nodes entirely, preventing repetitive loops.
3. **Graceful Degradation**: If a track runs out of acoustic neighbours, the walker automatically falls back to metadata (same-artist/album) edges, keeping the playback queue flowing.

---

## 4. Library-Relative Mood Profiles

Vocal commands like *"play something chill"* or *"play happy tracks"* utilize a dynamic, library-relative scoring algorithm:

```python
MOOD_PROFILES = {
    "chill":    {"bpm": -1.0, "energy": -1.0, "brightness": -0.5, "beat_strength": -0.5},
    "intense":  {"energy": 2.0, "beat_strength": 1.5, "brightness": 0.5},
    "dark":     {"brightness": -1.5, "rolloff": -1.0},
    # v3 timbre-based moods, powered by the new spectral_flatness /
    # spectral_contrast scalars:
    "tonal":    {"spectral_flatness": -1.5, "spectral_contrast": 1.0},
    "noisy":    {"spectral_flatness": 1.5},
    "acoustic": {"spectral_flatness": -1.0, "energy": -0.5, "beat_strength": -0.3},
    # ...
}
```

* **The Scoring Algorithm**:
  1. The mood is mapped to a direction-vector over the scalar features (`bpm`, `brightness`, `energy`, `rolloff`, `beat_strength`, `spectral_flatness`, `spectral_contrast`).
  2. The scalar columns are **z-scored across the active library**.
  3. The engine computes a weighted dot product of the z-scored matrix against the mood vector.
* **Why it's library-relative**: A "fast" or "intense" track in a library consisting of ambient music is slower than a "fast" track in a library of drum & bass. Standardizing features *relative to the active database distribution* ensures mood recommendations adapt perfectly to the user's specific tastes.
* **Key index excluded from mood scoring**: `key_index` is categorical (0–23), so direction-based z-scoring is meaningless. Key-aware filtering is handled separately (e.g. future "play in same key" intent).

---

## 5. Laptop-Offload Analysis Pipeline

Library-wide DSP on a phone is impractically slow — a typical Android device takes 10+ seconds per track for decode + feature extraction. The same numpy pipeline runs in roughly 200 ms per track on a modern laptop, so all bulk feature extraction has been moved to a host-side script. The on-device app never needs to compute features itself; it just consumes whatever is already in the `play_counts.timbre` BLOB.

> [!IMPORTANT]
> **Prerequisite: USB Debugging Enabled**
> In order for `adb` to perform the file transfers and setup, you must enable **USB Debugging** on your Android device:
> 1. On your phone, go to **Settings** → **About phone** and tap **Build number** 7 times to unlock Developer Options.
> 2. Go to **Settings** → **System** → **Developer options** (or search for it in Settings).
> 3. Scroll down and enable **USB debugging**.
> 4. Connect your phone to your laptop via a USB cable.
> 5. Open a terminal on your laptop, run `adb devices`, and authorize the debugging connection on your phone when prompted.

The mechanism reuses the existing **App State Bundle** export/import plumbing (see Settings → Advanced → Export State / Import State) — there is no separate sync protocol; the bundle is the wire format.

### 5.1 Automated Offload Script

The automated offload script (`tools/auto_offload.sh`) runs feature extraction by exchanging files through the public external storage directory (`/sdcard/Download`). This mechanism avoids using `run-as` permissions or direct access to the app's internal sandbox, making it compatible with both release and debug APK builds.

To execute the automated offload:

1. **Export State**: In the app on the phone, navigate to **Settings** → **Advanced** → **Export State** and save the ZIP archive to the phone's **Download** folder.
2. **Run Script**: Execute the offload script from the laptop terminal:
   ```bash
   ./tools/auto_offload.sh
   ```

#### Script Operations:
1. **Pull State Bundle**: Finds the most recently exported state ZIP file in `/sdcard/Download`, pulls it to a temporary directory on the host machine, and extracts it.
2. **Feature Extraction**: Invokes `tools/dsp_offload.py` to analyze missing tracks in parallel using a ThreadPoolExecutor with 4 concurrent workers.
3. **Resource Cleanup**: 
   * Deletes intermediate decoded `.pcm` files immediately after feature extraction.
   * Deletes downloaded raw compressed audio files (`.mp3`, `.flac`, etc.) immediately after feature extraction.
4. **Archive Preservation**: Saves a persistent copy of the finished analyzed state bundle to `tools/analyzed_states/` on the laptop, preserved under its original timestamped name with an `.analysed.zip` suffix.
5. **Import and Phone Cleanup**:
   * Pushes the analyzed bundle to the phone's `/sdcard/Download/mai_an_lab_state_import.zip`.
   * Deletes the original exported ZIP file from `/sdcard/Download` on the phone.
   * Restarts the app process to trigger the startup hook, which imports the state database and deletes the `mai_an_lab_state_import.zip` file.

### How the Script Works

`tools/dsp_offload.py` is a self-contained CLI. It requires `adb` and `ffmpeg` on `PATH` and reuses the on-device feature pipeline directly (via `sys.path` injection of `StreamripApp/utils/dsp.py`) so any change to the analyser is picked up automatically — there is no duplicated DSP math.

For each tracks-needing-features bundle:

1. **Inspect**: opens the bundle ZIP, queries `library.db` for paths whose `features_version` is absent or stale.
2. **Chunked ADB transfer**: groups missing paths into chunks (default 100), writes each chunk's list to `/sdcard/.dsp_offload_pull_list.txt`, then streams the chunk through a single `adb exec-out tar -cf - -T <list>` pipe into a local `tar -xf -`. Paying the ~50–200 ms ADB handshake once per chunk instead of once per file is the dominant speed win — for 1100 small files this saves several minutes of pure overhead before any bytes have moved.
3. **Parallel decode + extract** per chunk: `ffmpeg` decodes each cached file to a 120 s mono PCM clip; numpy runs the full v3 pipeline (HPSS, MFCC + deltas, chroma, scalars). A `ThreadPoolExecutor` overlaps `ffmpeg` subprocesses with numpy work because both release the GIL.
4. **Batched DB write per chunk**: a single `BEGIN/COMMIT` transaction upserts the entire chunk's features. Orders of magnitude faster than per-track commits; the chunk boundary is also a safe checkpoint — if the script is killed mid-run, all *finished* chunks survive in the bundle DB and the next run picks up cleanly from the audio cache.
5. **Repackage**: writes `<bundle>.analysed.zip` (or `--in-place` to overwrite). The repackager preserves any file in the original bundle the script didn't touch (`config.toml`, `recent_searches.json`, future manifest extensions).

### CLI Flags

| Flag | Default | Notes |
|---|---|---|
| `bundle` | — (required) | Path to the exported `.zip`. |
| `--out PATH` | `<bundle>.analysed.zip` | Output bundle path. |
| `--in-place` | off | Overwrite the input bundle. |
| `--serial SERIAL` | auto | Pick a specific ADB device. |
| `--concurrency N` | 4 | Parallel decode+extract workers per chunk (CPU-bound). |
| `--chunk-size N` | 100 | Tracks per ADB tar batch. Higher = fewer handshakes, more disk used between batches. |
| `--workdir DIR` | tempdir | Reusable cache for pulled audio + intermediate PCMs. Persistent across runs if specified. |
| `--keep-workdir` | off | Don't delete the workdir on exit. |

### Idempotency & Caching

The work directory holds two parallel caches:

* `audio_cache/storage/emulated/0/...` — pulled audio files, keyed by their on-device absolute path. `tar` preserves the full path structure, so a re-run with the same `--workdir` skips the pull for any file that already exists locally.
* `<audio>.pcm` next to each cached audio file — the intermediate decoded PCM. Survives between runs so re-running the script after a feature-pipeline tweak skips re-decoding (cheap) but redoes extraction.

If a track's `features_version` is current in the bundle DB, it's already filtered out by step 1, so the script naturally resumes from where it left off without any explicit checkpoint logic.

### Safety Properties

* **Bundle DB schema is defensively migrated**: the script's `_upsert_chunk` runs `ALTER TABLE ADD COLUMN` for any v3 columns missing from a pre-v3 bundle. The script works standalone against any bundle without requiring the user to bump the app first.
* **No host-side audio files retained beyond the work directory.** No upload to remote services; everything stays on your laptop.
* **Bundle round-trip is non-destructive**: the original `<bundle>.zip` is untouched unless `--in-place` is passed.
* **`FEATURES_VERSION` is read from the app's `utils/dsp.py`** — the script can never write features that disagree with the on-device extractor's expected version, because they share one source of truth.
