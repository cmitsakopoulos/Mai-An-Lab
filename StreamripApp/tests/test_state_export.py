"""Round-trip tests for the bundle export/import path.

Three primary concerns:
  * custom_moods.json — added to the bundle in phase 3 task 9.
  * mood_regressors BLOB column — rides inside library.db automatically;
    these tests prove byte-identity of the weights across export/import.
  * manifest summary — phase 3 task 11 adds a per-mood n_samples block so
    the import dialog can preview what's being shipped.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Isolate APP_DIR so the custom-mood JSON probe doesn't see the user's real file.
import types as _types
_cfg = _types.ModuleType("utils.config")
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_app_")
sys.modules["utils.config"] = _cfg

import numpy as np
from utils import state_export
from utils import mood_regressor as mr
from utils import track_graph as tg
from utils.db_manager import DatabaseManager


def _run(coro):
    return asyncio.run(coro)


class TestBundleRoundTrip(unittest.TestCase):
    """Each test builds a fresh DB + custom_moods JSON in a tempdir, runs
    export, then either inspects the bundle directly or runs import into a
    second clean DB and asserts state survived."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bundle_test_")
        self.db_path = os.path.join(self.tmpdir, "library.db")
        self.config_path = os.path.join(self.tmpdir, "config.toml")
        self.custom_moods_path = os.path.join(self.tmpdir, "custom_moods.json")
        self.bundle_dir = os.path.join(self.tmpdir, "bundles")
        os.makedirs(self.bundle_dir, exist_ok=True)
        # Minimal config file so export has something to zip.
        with open(self.config_path, "w") as f:
            f.write("# test config\n")
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())

    def tearDown(self):
        _run(self.db.close())
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _export(self) -> str:
        return state_export.export_state(
            db_path=self.db_path,
            config_path=self.config_path,
            search_history_path=None,
            out_dir=self.bundle_dir,
            custom_moods_path=self.custom_moods_path,
        )

    # ── Task 9 ──────────────────────────────────────────────────────────

    def test_custom_moods_round_trips_through_bundle(self):
        """A custom_moods.json present at export time must reappear in the
        live filesystem after import — even if we delete the live file in
        between (simulating a device wipe / migration)."""
        payload = {
            "my_islet": {
                "centroid":      [0.1] * 52,
                "exemplar_path": "/music/some_track.flac",
                "threshold":     0.78,
            }
        }
        with open(self.custom_moods_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with open(self.custom_moods_path, "rb") as f:
            original_bytes = f.read()

        bundle = self._export()

        # Wipe the live file to prove the import isn't just relying on it.
        os.remove(self.custom_moods_path)
        self.assertFalse(os.path.exists(self.custom_moods_path))

        # Close DB before import (real caller does this too).
        _run(self.db.close())
        state_export.import_state(
            zip_path=bundle,
            db_path=self.db_path,
            config_path=self.config_path,
            search_history_path=None,
            custom_moods_path=self.custom_moods_path,
        )
        self.assertTrue(os.path.exists(self.custom_moods_path),
                        "Import should have restored custom_moods.json.")
        with open(self.custom_moods_path, "rb") as f:
            restored = f.read()
        self.assertEqual(restored, original_bytes,
                         "Restored custom_moods.json must be byte-identical.")
        # Reopen the DB so tearDown's close() doesn't double-close.
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())

    def test_export_omits_custom_moods_when_absent(self):
        """If no custom_moods.json exists on disk at export time, the bundle
        simply doesn't carry the member — no crash, no empty file written."""
        self.assertFalse(os.path.exists(self.custom_moods_path))
        bundle = self._export()
        with zipfile.ZipFile(bundle, "r") as zf:
            self.assertNotIn("custom_moods.json", zf.namelist())

    # ── Task 10 ─────────────────────────────────────────────────────────

    def test_mood_regressor_weights_survive_bundle_round_trip(self):
        """Plant a known regressor, export, wipe DB, import. Re-read the
        regressor and assert byte identity on the weights BLOB + equality
        on bias + n_samples. mood_regressors lives in library.db so the
        existing sqlite-snapshot path carries it — but this test catches
        any future schema change that silently breaks the round-trip."""
        w_original = np.array(
            [0.11, -0.22, 0.33, -0.44, 0.55, -0.66, 0.77, -0.88],
            dtype=np.float32,
        )
        b_original = 0.42
        n_original = 17
        _run(self.db.save_mood_regressor(
            "chill", mr.pack_weights(w_original), b_original, n_original,
            tg.FEATURES_VERSION,
        ))

        bundle = self._export()

        # Wipe the DB so the import really has to restore it.
        _run(self.db.close())
        os.remove(self.db_path)
        # Pre-initialise an empty DB so import overwrites a real schema.
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())
        _run(self.db.close())

        state_export.import_state(
            zip_path=bundle,
            db_path=self.db_path,
            config_path=self.config_path,
            search_history_path=None,
            custom_moods_path=self.custom_moods_path,
        )

        # Reopen the imported DB and read the regressor back.
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())
        reg = _run(self.db.get_mood_regressor("chill", tg.FEATURES_VERSION))
        self.assertIsNotNone(reg, "Regressor row vanished across bundle round-trip.")
        weights_blob, bias, n_samples = reg
        w_restored = mr.unpack_weights(weights_blob)
        np.testing.assert_array_equal(w_restored, w_original,
                                      err_msg="Weights BLOB mutated.")
        self.assertAlmostEqual(bias, b_original, places=5)
        self.assertEqual(n_samples, n_original)

    def test_bundle_version_mismatch_rejected(self):
        """A v1 (or any non-current) bundle must be refused with a clear
        error. We bumped BUNDLE_VERSION 1 → 2 in task 9."""
        bundle = self._export()
        # Rewrite the manifest to claim version 1 (old).
        import shutil
        forged = os.path.join(self.tmpdir, "forged.zip")
        with zipfile.ZipFile(bundle, "r") as src, \
             zipfile.ZipFile(forged, "w", compression=zipfile.ZIP_DEFLATED) as dst:
            for name in src.namelist():
                data = src.read(name)
                if name == "manifest.json":
                    manifest = json.loads(data.decode("utf-8"))
                    manifest["bundle_version"] = 1
                    data = json.dumps(manifest).encode("utf-8")
                dst.writestr(name, data)

        _run(self.db.close())
        with self.assertRaises(ValueError) as ctx:
            state_export.import_state(
                zip_path=forged,
                db_path=self.db_path,
                config_path=self.config_path,
                search_history_path=None,
                custom_moods_path=self.custom_moods_path,
            )
        self.assertIn("bundle_version", str(ctx.exception).lower())
        # Reopen for tearDown.
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())

    # ── Task 11 (added below in the implementation) ─────────────────────

    def test_manifest_includes_regressor_summary(self):
        """The bundle manifest must carry a per-mood n_samples summary so
        the import dialog can preview what's being shipped without parsing
        the SQLite blob."""
        w = np.zeros(mr.MOOD_REGRESSOR_DIM, dtype=np.float32)
        _run(self.db.save_mood_regressor(
            "energetic", mr.pack_weights(w), 0.0, 42, tg.FEATURES_VERSION,
        ))
        _run(self.db.save_mood_regressor(
            "intense", mr.pack_weights(w), 0.0, 7, tg.FEATURES_VERSION,
        ))

        bundle = self._export()
        manifest = state_export.inspect_bundle(bundle)
        self.assertIn("mood_regressors", manifest,
                      "Manifest must carry a mood_regressors block.")
        self.assertEqual(manifest["mood_regressors"].get("energetic"), 42)
        self.assertEqual(manifest["mood_regressors"].get("intense"), 7)


if __name__ == "__main__":
    unittest.main()
