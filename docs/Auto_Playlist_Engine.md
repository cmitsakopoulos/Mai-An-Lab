# Auto-Playlist Engine: Playlist Creation

> [!CAUTION]
> **EXPERIMENTAL FEATURE**; the metadata-blending clustering pipeline is currently in active development. While it significantly improves genre conciseness, it may produce unexpected clusters on libraries with highly inconsistent tagging.

> [!TIP]
> **Bioinformatics Origins**; this engine was transplanted from bioinformatics research. Instead of clustering gene-expression profiles, we cluster *songs* in a 43-dimensional acoustic space, augmented by string-similarity metadata, using Markov Clustering (MCL). The engine identifies tracks that flow toward the same attractor as your seed, sequencing them into a smooth listening arc.

## The Hybrid Pipeline

```text
audio file ──► MediaCodec (Android) ──► mono int16 PCM @ 22050 Hz (90s)
                                                  │
                                                  ▼
                                       Pure-numpy DSP analyser
                                                  │
        ┌─────────────────────┬───────────────────┼────────────────────┬─────────────────────┐
        ▼                     ▼                   ▼                    ▼                     ▼
 RMS energy (dB)      Spectral centroid    Spectral rolloff   Onset-flux autocorr   Mel-filterbank STFT
                       (brightness)         (85th-pct freq)    + 120-BPM prior              │
                                                                │                            ▼
                                                                ▼                  log → DCT-II → MFCC
                                                          BPM + beat strength               │
                                                                                  ┌──────────┼──────────┐
                                                                                  ▼          ▼          ▼
                                                                              MFCC mean   MFCC std   chroma
                                                                                (13)        (13)      (12)
                                                                                                       ▲
                                                  │                             FFT bins → pitch class ┘
                                                  ▼
                       43-D feature vector per track; z-scored, axis-weighted (sound-profile bias)
                                                  │
                                                  ▼
                                       Acoustic Similarity Graph (Gaussian Kernel)
                                                  │
                                                  ▼
    Artist/Album Tags  ──► Token-Set Jaccard ──► Metadata Similarity Matrix
                                                  │
                                                  ▼
                                       BLENDED SIMILARITY GRAPH
                       (0.8 × Acoustic + 0.2 × Metadata @ 50/50 Artist/Album)
                                                  │
                                                  ▼
                                   Markov Clustering (MCL: expand → inflate → prune)
                                                  │
                                                  ▼
                             Seed's attractor row → cluster of similar tracks
                                                  │
                                                  ▼
                        Resize to user-chosen length, sequence by greedy nearest-neighbour
```

## Technical Implementation

- **Pure-numpy DSP**; no `librosa`, no `scipy`. Just pure `numpy` math. This allows the engine to run on Android without complex native C++ wheels.
- **Hybrid Similarity Blending**; the engine computes an independent metadata similarity matrix alongside the acoustic Gaussian kernel. This uses a **Token-Set Jaccard Similarity** algorithm which is robust to metadata inconsistencies between download sources (e.g., "Daft Punk ft. Pharrell" vs "Daft Punk").
- **Weighted Blending**; by default, the metadata signal contributes 20% to the final similarity graph. This ensures tracks with matching artist/album names are encouraged to co-cluster without allowing metadata to override acoustic similarity entirely.
- **Timbre & Dynamics**; captures both the average "color" of a track (MFCC mean) and its timbral variance (MFCC std), discriminating between static and dynamic textures.
- **Harmonic Profile (Chroma)**; a 12-dimensional signature of musical notes, separating tracks by key and harmonic content.
- **MCL Clustering**; a stochastic process that alternates expansion (simulating random walks) and inflation (sharpening probabilities) until the matrix converges to a set of attractors.
