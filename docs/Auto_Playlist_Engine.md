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
                                                                               MFCC mean   MFCC std   chroma
                                                                                 (13)        (13)      (12)
                                                                                                    (38-D Timbre)
                                                   │
                                                   ▼
                         42-D Unified Feature Vector (38-D Timbre + 4-D Scalars)
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

## 1. Feature Extraction & Vector Space

Acoustic vectors are extracted directly on-device using a pure-Python/NumPy DSP analyzer (bypassing heavy native libraries like Librosa or SciPy to ensure smooth Android performance).

### The 42-Dimensional Feature Space
For every track, the analyzer produces a 42-dimensional vector that merges timbre, dynamics, tempo, and spectrum:
1. **38-Dimensional Timbre Profile**:
   * **MFCC Mean (13 dimensions)**: Captures the average spectral envelope ("color") of the track.
   * **MFCC Standard Deviation (13 dimensions)**: Tracks dynamic variance and textural changes over time.
   * **Chroma Pitch Profile (12 dimensions)**: Represents the harmonic footprint and musical key.
2. **4-Dimensional Physical Scalars**:
   * **BPM (Beats Per Minute)**: Extracted via onset-flux autocorrelation with a 120-BPM prior.
   * **Brightness (Spectral Centroid)**: Represents high-frequency energy.
   * **Energy (RMS)**: Measures acoustic dynamics and loudness.
   * **Spectral Rolloff**: Identifies the 85th-percentile frequency cutoff.

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
    # ...
}
```

* **The Scoring Algorithm**:
  1. The mood is mapped to a direction-vector over the scalar features (`bpm`, `brightness`, `energy`, `rolloff`, `beat_strength`).
  2. The scalar columns are **z-scored across the active library**. 
  3. The engine computes a weighted dot product of the z-scored matrix against the mood vector.
* **Why it's library-relative**: A "fast" or "intense" track in a library consisting of ambient music is slower than a "fast" track in a library of drum & bass. Standardizing features *relative to the active database distribution* ensures mood recommendations adapt perfectly to the user's specific tastes.
