"""Round-trip tests for the bundle export/import path.

Primary concerns:
  * bundle_version gate — refuses imports from incompatible bundles.
  * DB + config round-trip through export/import.
"""

import json
import os
import sys
import asyncio
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
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bundle_test_")
        self.db_path = os.path.join(self.tmpdir, "library.db")
        self.config_path = os.path.join(self.tmpdir, "config.toml")
        self.bundle_dir = os.path.join(self.tmpdir, "bundles")
        os.makedirs(self.bundle_dir, exist_ok=True)
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
        )

    def test_bundle_version_mismatch_rejected(self):
        """A non-current bundle must be refused with a clear error."""
        bundle = self._export()
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
            )
        self.assertIn("bundle_version", str(ctx.exception).lower())
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())

    def test_snapshot_writes_deterministic_filename(self):
        out = state_export.export_state_snapshot(
            db_path=self.db_path,
            config_path=self.config_path,
            out_dir=self.bundle_dir,
            search_history_path=None,
        )
        self.assertEqual(os.path.basename(out), "mai_an_lab_state_latest.zip")
        self.assertTrue(os.path.isfile(out))

        manifest = state_export.inspect_bundle(out)
        self.assertEqual(manifest["bundle_version"], state_export.BUNDLE_VERSION)
        self.assertTrue(manifest.get("auto_snapshot"))

        target_db = os.path.join(self.tmpdir, "imported.db")
        target_cfg = os.path.join(self.tmpdir, "imported.toml")
        _run(self.db.close())
        state_export.import_state(
            zip_path=out,
            db_path=target_db,
            config_path=target_cfg,
            search_history_path=None,
        )
        self.assertTrue(os.path.exists(target_db))
        self.assertTrue(os.path.exists(target_cfg))
        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())

    def test_snapshot_overwrites_previous(self):
        state_export.export_state_snapshot(
            db_path=self.db_path,
            config_path=self.config_path,
            out_dir=self.bundle_dir,
        )
        first_mtime = os.path.getmtime(
            os.path.join(self.bundle_dir, "mai_an_lab_state_latest.zip")
        )

        import time
        time.sleep(0.05)

        state_export.export_state_snapshot(
            db_path=self.db_path,
            config_path=self.config_path,
            out_dir=self.bundle_dir,
        )
        second_mtime = os.path.getmtime(
            os.path.join(self.bundle_dir, "mai_an_lab_state_latest.zip")
        )
        self.assertGreater(second_mtime, first_mtime)

        zips = [f for f in os.listdir(self.bundle_dir) if f.endswith(".zip")]
        self.assertEqual(len(zips), 1,
                         f"Expected exactly 1 snapshot file, found: {zips}")

    def test_import_state_cleans_up_stale_journals(self):
        bundle = self._export()

        target_db = os.path.join(self.tmpdir, "imported.db")
        target_cfg = os.path.join(self.tmpdir, "imported.toml")

        wal_path = target_db + "-wal"
        shm_path = target_db + "-shm"
        with open(wal_path, "w") as f: f.write("dummy wal data")
        with open(shm_path, "w") as f: f.write("dummy shm data")

        _run(self.db.close())
        state_export.import_state(
            zip_path=bundle,
            db_path=target_db,
            config_path=target_cfg,
            search_history_path=None,
        )

        self.assertTrue(os.path.exists(target_db))
        self.assertFalse(os.path.exists(wal_path), "Stale WAL file was not deleted.")
        self.assertFalse(os.path.exists(shm_path), "Stale SHM file was not deleted.")

        self.db = DatabaseManager(self.db_path)
        _run(self.db.initialize())


if __name__ == "__main__":
    unittest.main()
