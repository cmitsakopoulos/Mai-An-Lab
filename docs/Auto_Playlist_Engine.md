# Auto-Playlist Engine: Technical Deep-Dive

> [!TIP]
> **Bioinformatics Origins** — This engine was transplanted from bioinformatics research. Instead of clustering gene-expression profiles, we cluster *songs* in a 42-dimensional feature space using Markov Clustering (MCL). The engine identifies tracks that flow toward the same attractor as your seed, sequencing them into a smooth listening arc.

## The DSP Pipeline

```text
audio file ──► MediaCodec / ffmpeg ──► mono int16 PCM @ 22050 Hz  (middle 90 s)
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
                                                                                FFT bins → pitch class ┘
                                                                                  (mod 12, A=440 ref)
                                                  │
                                                  ▼
                       42-D feature vector per track  ── z-scored, axis-weighted (sound-profile bias)
                                                  │
                                                  ▼
                                   Gaussian-kernel similarity graph
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

- **Pure-numpy DSP** — No `librosa`, no `scipy`. Just pure `numpy` math. This allows the engine to run on Android without complex native C++ wheels.
- **Timbre (MFCC mean & std)** — Captures both the average "color" of a track and how much that color shifts over time (discriminating a static drone from a swelling pad).
- **Harmonic profile (Chroma)** — A 12-dimensional signature of which musical notes are present, separating tracks in different keys even if they share the same timbre.
- **MCL Clustering** — Alternates expansion and inflation until the similarity matrix converges to a set of attractors. This produces "genre-faithful" clusters based on sound profile rather than metadata.
