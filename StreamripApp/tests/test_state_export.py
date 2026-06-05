"""Round-trip tests for the bundle export/import path.

Primary concerns:
  * custom_moods.json — included in the bundle so user-created islets and
    their thresholds/blacklists survive export/import.
  * bundle_version gate — refuses imports from incompatible bundles.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import types as _types
import utils.config as _orig_config
_cfg = _types.ModuleType("utils.config")
for _k, _v in _orig_config.__dict__.items():
    _cfg.__dict__[_k] = _v
_cfg.APP_DIR = tempfile.mkdtemp(prefix="dsptest_app_")
sys.modules["utils.config"] = _cfg

from utils import state_export
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
    def test_snapshot_writes_deterministic_filename(self):
        """export_state_snapshot must write exactly
        `mai_an_lab_state_latest.zip` (no timestamp), and the resulting
        bundle must be importable by the standard import_state path."""
        with open(self.custom_moods_path, "w", encoding="utf-8") as f:
            json.dump({"test": True}, f)

        out = state_export.export_state_snapshot(
            db_path=self.db_path,
            config_path=self.config_path,
            out_dir=self.bundle_dir,
            search_history_path=None,
            custom_moods_path=self.custom_moods_path,
        )
        self.assertEqual(os.path.basename(out), "mai_an_lab_state_latest.zip")
        self.assertTrue(os.path.isfile(out))

        # Verify the bundle is importable.
        manifest = state_export.inspect_bundle(out)
        self.assertEqual(manifest["bundle_version"], state_export.BUNDLE_VERSION)
        self.assertTrue(manifest.get("auto_snapshot"))

        # Import into a clean target to prove round-trip works.
        target_db = os.path.join(self.tmpdir, "imported.db")
        target_cfg = os.path.join(self.tmpdir, "imported.toml")
        _run(self.db.close())
        state_export.import_state(
            zip_path=out,
            db_path=target_db,
            config_path=target_cfg,
            search_history_path=None,
            custom_moods_path=None,
        )
        self.assertTrue(os.path.exists(target_db))
        self.assertTrue(os.path.exists(target_cfg))
        # Reopen for tearDown.
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())

    def test_snapshot_overwrites_previous(self):
        """A second call to export_state_snapshot must overwrite the file,
        not create a second timestamped copy."""
        state_export.export_state_snapshot(
            db_path=self.db_path,
            config_path=self.config_path,
            out_dir=self.bundle_dir,
        )
        first_mtime = os.path.getmtime(
            os.path.join(self.bundle_dir, "mai_an_lab_state_latest.zip")
        )

        import time
        time.sleep(0.05)  # ensure mtime resolution differs

        state_export.export_state_snapshot(
            db_path=self.db_path,
            config_path=self.config_path,
            out_dir=self.bundle_dir,
        )
        second_mtime = os.path.getmtime(
            os.path.join(self.bundle_dir, "mai_an_lab_state_latest.zip")
        )
        self.assertGreater(second_mtime, first_mtime)

        # There must be exactly one file, not two.
        zips = [f for f in os.listdir(self.bundle_dir) if f.endswith(".zip")]
        self.assertEqual(len(zips), 1,
                         f"Expected exactly 1 snapshot file, found: {zips}")

    def test_import_state_cleans_up_stale_journals(self):
        """Importing a state bundle must delete any stale library.db-wal and
        library.db-shm files on disk to prevent database corruption."""
        bundle = self._export()
        
        target_db = os.path.join(self.tmpdir, "imported.db")
        target_cfg = os.path.join(self.tmpdir, "imported.toml")
        
        # Create dummy WAL and SHM files
        wal_path = target_db + "-wal"
        shm_path = target_db + "-shm"
        with open(wal_path, "w") as f: f.write("dummy wal data")
        with open(shm_path, "w") as f: f.write("dummy shm data")
        
        self.assertTrue(os.path.exists(wal_path))
        self.assertTrue(os.path.exists(shm_path))
        
        _run(self.db.close())
        state_export.import_state(
            zip_path=bundle,
            db_path=target_db,
            config_path=target_cfg,
            search_history_path=None,
            custom_moods_path=None,
        )
        
        self.assertTrue(os.path.exists(target_db))
        # Stale journals must be deleted
        self.assertFalse(os.path.exists(wal_path), "Stale WAL file was not deleted.")
        self.assertFalse(os.path.exists(shm_path), "Stale SHM file was not deleted.")
        
        # Reopen for tearDown
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())

if __name__ == "__main__":
    unittest.main()
