# Walk-graph purge plan (post metadata-pool inversion)

Four independent purges of machinery that Stages 1–2 (metadata-pool walk) left
orphaned. **Do them in the order below** — 1 and 4 are trivial/independent, 2
unblocks 3, and 3 is the only one that touches the unit-test contract.

## The one validation principle

**Every item here is walk-invariant.** None of them change what the walk emits
in production:

- Item 1 is dead code (nothing reads it).
- Item 2 removes Louvain `cluster_id`, which the walk no longer consults.
- Item 3 removes the persisted edge table, but in production the walk reads the
  **coordinate graph**, not that table — so queues are identical.
- Item 4 drops `mfcc_delta` from storage, which the graph already discards at
  read time (`unpack_graph_embedding`).

So the pass condition after **each** item is the same two checks:

```bash
PY=/Users/chrismitsacopoulos/miniconda3/bin/python   # conda base
cd StreamripApp
$PY -m pytest tests/ -q          # green modulo the 2 known-pre-existing
                                 # test_queue_modes failures
# deterministic A/B — must be byte-identical to the frozen baseline:
$PY ../tools/walk_ab_probe.py /tmp/purge_baseline.db > /tmp/after.txt
diff /tmp/baseline_walk.txt /tmp/after.txt && echo "WALK-INVARIANT ✓"
```

### Setup once, before starting

```bash
PY=/Users/chrismitsacopoulos/miniconda3/bin/python
# a throwaway BUILT image to probe against (never point the probe at the app DB)
cp tools/offload_cache/walk_diag_db/library_built.db /tmp/purge_baseline.db
# freeze the reference queues:
$PY tools/walk_ab_probe.py /tmp/purge_baseline.db > /tmp/baseline_walk.txt
cat /tmp/baseline_walk.txt   # sanity: 5 seeds, laiko cohesion≈0.91
```

`tools/walk_ab_probe.py` is the deterministic (temperature=0) harness — five
fixed seeds incl. the laiko boundary case. Regular `walk_probe.py` is stochastic
(temp 0.3) and no good for byte-diffing.

> If you re-run the acoustic build during any item (you shouldn't need to for
> 1/2/4), rebuild `/tmp/purge_baseline.db` and re-freeze `/tmp/baseline_walk.txt`
> from the **new** code first, or the diff is meaningless.
---

## Recommended order & checklist

- [x] **Setup**: freeze `/tmp/baseline_walk.txt` from current code.
- [x] **Item 1** (dead code) → pytest + probe diff.
- [x] **Item 4** (delta drop + migration) → re-freeze baseline from v5, pytest + probe diff.
- [x] **Item 2** (Louvain + report) → grep-guard cluster_id first → pytest + probe diff.
- [x] **Item 3** (edge table) → decide FakeDB approach → pytest + probe diff.

Expected end state: `track_graph.py` well under 1200 lines; the build persists
only Zr coords + genre model; the walk has a single neighbour source; dsp.py has
no delta. Regroup after and we review together.

### Known-good invariants to keep an eye on
- laiko/GR seed cohesion stays ≈0.91 (the country pool).
- 25-seed aggregate genre-cohesion (meta ≈0.59 vs acoustic ≈0.43) — run
  `pytest tests/test_walk_real_library.py -s`.
- The 2 `test_queue_modes` failures are **pre-existing** (they mock `walk`);
  don't chase them.
