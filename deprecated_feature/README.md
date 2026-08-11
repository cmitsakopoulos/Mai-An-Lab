# Deprecated features

Code retired from the app but kept for reference. **Nothing here is packaged.**
`flet build` runs from `StreamripApp/`, so this directory sits outside the
packaged tree entirely — it needs no entry in the `[tool.flet.app] exclude` list
in `StreamripApp/pyproject.toml` (that list only covers paths *inside*
`StreamripApp/`).

Nothing in the shipped app imports these modules. They are not on any import
path and will not run.

---

## The semantic intent engine (retired 2026-08-10)

`semantic_intent.py` + `bge_vocab_data.py`

A WordPiece Vector Space Model classifier: a 30,522-token BERT vocabulary
(gzip+base64-embedded, 158 KB of source) driving IDF-weighted cosine similarity
against hand-written anchor phrases. It served as "Stage 2" behind the regex
parser in Jarvis, and as the fuzzy fallback for Settings search.

**Why it was retired — it didn't earn its keep.** Measured over 10 realistic
Settings queries, it surfaced exactly one result that direct substring matching
missed (`haptik` → Haptic Feedback), and that hit was already reachable via the
Levenshtein fuzzy bonus alone. It appended a spurious result on `dark mode`, and
returned nothing at all for the genuinely semantic queries that were its whole
premise (`make it louder`, `stop the vibrating`, `battery`).

It was also broken by construction. The IDF `weights` dict is keyed on whole
words, but WordPiece splits 6 of the 24 — precisely the domain-specific ones:

```
disable   -> ['di', '##sable']        eq        -> ['e', '##q']
treble    -> ['tre', '##ble']         equalizer -> ['equal', '##izer']
qobuz     -> ['q', '##ob', '##uz']    haptic    -> ['ha', '##ptic']
```

Those weights silently never fired.

Note the performance case was *not* the reason: both call sites imported lazily
inside functions, costing 1.4 ms to import, 8.0 ms for one-time tokenizer init,
and 0.3 ms per keystroke on desktop (~1 ms on Android). None of it on the startup
path.

**What replaced it.** `levenshtein_distance` — the part that worked — moved to
`StreamripApp/utils/text_match.py`. Settings search keeps its weights dict,
cosine and 0.15 threshold, now tokenising on `str.split()`, which as a side
effect *fixes* the six dead weight keys above. Jarvis lost nothing: every intent
this classifier could emit already has a regex pattern in
`utils/assistant_intent.py`, and with the LLM agent enabled the semantic stage
was already bypassed.

## The "Edit Metadata" dialog (retired 2026-08-10)

`metadata_editor_dialog.py` + `metadata_physical_writer.py`

The track/album tag editor was broken and removed. It was also the only route to
deleting a track, so **Delete Track** moved to the library long-press context
menu, calling the existing `confirm_delete_track` / `_delete_track` on
`StreamripFletApp`.

Artist metadata is unaffected: `ArtistMetadataDialog` (country/genre overrides,
still live in `ui/player/dialogs.py`) writes to the DB via
`set_manual_artist_enrichment`, never to physical tags. The artist pencil in the
library now opens that dialog.

`extract_artwork` was **not** retired — it stayed in
`StreamripApp/utils/metadata_editor.py`, where the now-playing view still uses it.
