"""Unit tests for Companion Sync core & db_manager batch updates."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unicodedata
import unittest
from unittest.mock import MagicMock, patch

from utils.db_manager import DatabaseManager
from utils.dsp import FEATURES_VERSION
from utils.sync.discovery import BeaconBroadcaster, BeaconListener, resolve_local_ip, UDP_PORT
from utils.sync.server import CompanionServer
from utils.sync.client import CompanionClient


class FakeApp:
    """Strict stand-in for StreamripFletApp: only the attributes the sync code
    may touch exist. A MagicMock app answered `_load_pref` too, which is how a
    call to that nonexistent method shipped and crashed on device."""

    def __init__(self, prefs=None):
        self._prefs = dict(prefs or {})
        self.page = MagicMock()
        self.page.width = 400
        self.is_background = False
        self.snacks = []
        self.queue_controller = None
        self.db_manager = None
        self.companion_manager = None
        self._companion_manager = None
        self._companion_client = None
        self._companion_server = None

    def _save_pref(self, key, value):
        self._prefs[key] = value

    def show_snackbar(self, text, icon=None, color=None):
        self.snacks.append(text)

    def dismiss_dialog(self, dialog):
        if dialog is not None and getattr(dialog, "open", False):
            dialog.open = False
            return True
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_tone(path: str, seconds: int = 20):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-y", path],
        check=True)


class TestSyncAndBatch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_library.db")
        self.db_manager = DatabaseManager(self.db_path)
        await self.db_manager.initialize()

    async def asyncTearDown(self):
        await self.db_manager.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_update_tracks_features_batch(self):
        # Insert a dummy track first
        track_data = {
            "title": "Sync Test Track",
            "artist": "Sync Artist",
            "album": "Sync Album",
            "path": "/storage/emulated/0/Music/test.m4a",
            "duration": 180.0,
            "track_num": 1,
            "format": "m4a",
        }
        await self.db_manager.insert_track_hierarchical(track_data)

        # Batch insert features
        batch = [
            (
                "/storage/emulated/0/Music/test.m4a",
                128.0,   # bpm
                0.75,    # energy
                0.45,    # brightness
                0.55,    # rolloff
                0.80,    # beat_strength
                0.12,    # spectral_flatness
                0.34,    # spectral_contrast
                5,       # key_index
                b"\x01\x02\x03\x04",  # timbre
                FEATURES_VERSION,
            )
        ]
        await self.db_manager.update_tracks_features_batch(batch)

        # Verify via SQL
        conn = await self.db_manager.get_connection()
        async with conn.execute(
            "SELECT bpm, energy, brightness, timbre, features_version FROM play_counts WHERE track_path = ?",
            ("/storage/emulated/0/Music/test.m4a",)
        ) as cursor:
            row = await cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 128.0)
            self.assertEqual(row[1], 0.75)
            self.assertEqual(row[2], 0.45)
            self.assertEqual(row[3], b"\x01\x02\x03\x04")
            self.assertEqual(row[4], FEATURES_VERSION)

    async def test_server_client_handshake_and_pair(self):
        # Start server on dynamic port
        server = CompanionServer(db_manager=self.db_manager, port=18945, pin="4321")
        bound_port = await server.start()
        self.assertTrue(server.is_running)

        try:
            client = CompanionClient(host="127.0.0.1", port=bound_port)
            
            # Ping probe
            ping_data = await client.ping()
            self.assertEqual(ping_data["status"], "ok")
            self.assertEqual(ping_data["device"], "macOS")

            # Pairing
            with self.assertRaises(PermissionError):
                await client.pair("9999")  # wrong PIN

            token = await client.pair("4321")  # correct PIN
            self.assertIsNotNone(token)
            self.assertEqual(client.auth_token, token)

            # Probe cache with empty list
            probe_res = await client.offload_dsp(
                missing_paths=[],
                db_manager=self.db_manager,
            )
            self.assertEqual(probe_res["analysed"], 0)

            # Test Transfer Queue
            dummy_song = os.path.join(self.test_dir, "test_song.m4a")
            with open(dummy_song, "wb") as f:
                f.write(b"M4A DUMMY AUDIO DATA")

            queued = await server.queue_tracks_for_transfer(
                tracks=[{
                    "id": "trk_123",
                    "path": dummy_song,
                    "title": "Transfer Test Song",
                    "artist": "Test Artist",
                    "album": "Test Album",
                }],
                quality="original",
                precompute_dsp=False,
            )
            self.assertEqual(len(queued), 1)

            # Client fetches pending transfers
            transfers = await client.get_pending_transfers()
            self.assertEqual(len(transfers), 1)
            self.assertEqual(transfers[0]["title"], "Transfer Test Song")
            self.assertIn("/api/v1/tracks/stream", transfers[0]["stream_url"])

            # Client acks transfer
            ack_ok = await client.ack_transfers([transfers[0]["id"]])
            self.assertTrue(ack_ok)
            remaining = await client.get_pending_transfers()
            self.assertEqual(len(remaining), 0)

        finally:
            await server.stop()
            self.assertFalse(server.is_running)

    def test_companion_url_detection(self):
        from utils.streamrip_api import _detect_type_and_id
        url = "http://192.168.1.100:8945/api/v1/tracks/stream?path=/Users/music/song.flac&quality=aac_vbr_max"
        mtype, mid, source = _detect_type_and_id(url)
        self.assertEqual(source, "companion")
        self.assertEqual(mtype, "track")

    def test_sync_hub_pane_responsive_controls(self):
        from unittest.mock import MagicMock, patch
        import flet as ft
        from ui.views.sync_hub import SyncHubPane
        import utils.sync.manager as mgr_mod
        import utils.sync.server as srv_mod

        mock_app = FakeApp()
        mock_app.db_manager = self.db_manager
        patch.object(srv_mod, "_SERVER", None).start()
        patch.object(mgr_mod, "_INSTANCE", None).start()
        self.addCleanup(patch.stopall)

        # Test macOS mode
        with patch("ui.views.sync_hub.is_android", return_value=False):
            pane_mac = SyncHubPane(mock_app)
            self.assertIsNotNone(pane_mac.content)
            self.assertTrue(hasattr(pane_mac, "_pin_display"))
            self.assertTrue(hasattr(pane_mac, "_endpoint_display"))
            self.assertEqual(pane_mac._pin_display.text_align, ft.TextAlign.CENTER)
            self.assertEqual(pane_mac._endpoint_display.text_align, ft.TextAlign.CENTER)

        # Test Android mode
        with patch("ui.views.sync_hub.is_android", return_value=True):
            pane_android = SyncHubPane(mock_app)
            self.assertIsNotNone(pane_android.content)
            # Verify Android switch exists matching macOS design
            self.assertTrue(hasattr(pane_android, "_android_switch"))
            self.assertEqual(pane_android._android_switch.value, False)
            self.assertEqual(pane_android._client_status_text.value, "Offline · Inactive")

    async def test_companion_sync_manager_popup_flow(self):
        from unittest.mock import MagicMock, patch, AsyncMock
        from utils.sync.manager import CompanionSyncManager
        import flet as ft

        mock_app = FakeApp()
        mock_app.db_manager = self.db_manager
        mock_page = mock_app.page

        manager = CompanionSyncManager(mock_app)
        self.assertFalse(manager.enabled)

        # Test start
        with patch("utils.sync.manager.BeaconListener") as mock_bl:
            await manager.start()
            self.assertTrue(manager.enabled)
            self.assertTrue(mock_app._prefs["companion_sync_enabled"])

        # Simulate discovering an active Mac server requiring PIN
        server_info = {"name": "Test MacBook", "ip": "192.168.1.100", "port": 8945}
        await manager.prompt_pin_dialog(server_info)

        # Verify page.show_dialog was called with an AlertDialog
        mock_page.show_dialog.assert_called_once()
        dlg = mock_page.show_dialog.call_args[0][0]
        self.assertIsInstance(dlg, ft.AlertDialog)
        self.assertIn("Pair with Test MacBook", dlg.title.controls[1].value)

        # Test transfer alert dialog flow
        mock_page.show_dialog.reset_mock()
        manager.discovered_server = server_info
        dummy_transfers = [{"id": "t1", "title": "Track One", "artist": "Artist A", "stream_url": "http://192.168.1.100:8945/stream"}]
        await manager.prompt_transfer_dialog(dummy_transfers)
        mock_page.show_dialog.assert_called_once()
        transfer_dlg = mock_page.show_dialog.call_args[0][0]
        self.assertIsInstance(transfer_dlg, ft.AlertDialog)
        self.assertIn("Transfers from Test MacBook", transfer_dlg.title.controls[1].value)

        # Test unpair
        mock_app._prefs["companion_paired_token"] = "abc"
        manager.is_connected = True
        with patch("utils.sync.manager.BeaconListener"):
            await manager.unpair()
        self.assertFalse(manager.is_connected)
        self.assertEqual(mock_app._prefs["companion_paired_token"], "")

        await manager.stop()
        self.assertFalse(manager.enabled)

    async def test_send_to_phone_dialog_and_transfer_action(self):
        from unittest.mock import MagicMock, AsyncMock
        import warnings
        import flet as ft
        import flet.utils.validation as v
        from ui.views.library import LibraryView

        # Setup mock app and page
        mock_app = MagicMock()
        mock_page = MagicMock()
        mock_page.width = 800
        mock_app.page = mock_page
        mock_server = MagicMock()
        mock_server.is_running = True
        mock_server.bound_port = 8945
        mock_server.pin = "1234"
        mock_server.queue_tracks_for_transfer = AsyncMock(return_value=[{"id": "t1"}])
        mock_app._companion_server = mock_server
        import utils.sync.server as srv_mod
        patch.object(srv_mod, "_SERVER", mock_server).start()
        self.addCleanup(patch.stopall)

        # LibraryView dummy instance
        view = LibraryView.__new__(LibraryView)
        view.app = mock_app
        view.page = mock_page
        view._selection_mode = True
        view._exit_selection_mode = MagicMock()

        test_tracks = [{"id": 1, "path": "/test/track.flac", "title": "Test Track"}]

        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            view._open_send_to_phone_dialog(test_tracks)

        # Ensure no deprecation warnings were raised
        dep_warnings = [w for w in recorded_warnings if issubclass(w.category, DeprecationWarning)]
        self.assertEqual(len(dep_warnings), 0, f"Encountered unexpected deprecation warnings: {dep_warnings}")

        # Ensure dialog was shown
        mock_page.show_dialog.assert_called_once()
        dlg = mock_page.show_dialog.call_args[0][0]
        self.assertIsInstance(dlg, ft.AlertDialog)

        # Find the "Send to Phone" action button
        send_btn = next((b for b in dlg.actions if getattr(b, "content", "") == "Send to Phone" or getattr(b, "text", "") == "Send to Phone"), None)
        self.assertIsNotNone(send_btn)

        # Trigger on_click which calls page.run_task(_start_transfer)
        # Capture the coroutine task created by run_task
        task = None
        def _mock_run_task(handler, *args, **kwargs):
            nonlocal task
            task = asyncio.create_task(handler(*args, **kwargs))
            return task
        mock_page.run_task.side_effect = _mock_run_task

        # Calling on_click(None) must not raise TypeError: missing 1 required positional argument
        send_btn.on_click(None)
        self.assertIsNotNone(task)
        await task

        mock_server.queue_tracks_for_transfer.assert_awaited_once()
        mock_app.show_snackbar.assert_called()

    # ── Regression tests for the on-device sync bugs ────────────────────────

    async def test_manager_constructs_with_real_app_shape(self):
        from utils.sync.manager import CompanionSyncManager
        app = FakeApp({"companion_sync_enabled": True})
        self.assertTrue(CompanionSyncManager(app).enabled)

    async def test_token_auth_is_enforced_and_reported_by_ping(self):
        server = CompanionServer(port=_free_port(), pin="4321", trust_loopback=False)
        port = await server.start()
        try:
            anon = CompanionClient("127.0.0.1", port)
            self.assertFalse((await anon.ping())["authorized"])
            with self.assertRaises(PermissionError):
                await anon.get_pending_transfers()

            token = await anon.pair("4321")
            self.assertTrue((await anon.ping())["authorized"])
            self.assertEqual(await anon.get_pending_transfers(), [])

            stale = CompanionClient("127.0.0.1", port, auth_token="deadbeef")
            self.assertFalse((await stale.ping())["authorized"])

            server.reset_pairing()
            self.assertFalse((await CompanionClient("127.0.0.1", port, auth_token=token).ping())["authorized"])
        finally:
            await server.stop()

    async def test_token_survives_server_recreation(self):
        import utils.sync.server as srv_mod
        app = FakeApp()
        with patch.object(srv_mod, "_SERVER", None):
            token = srv_mod.get_companion_server(app).auth_token
        self.assertEqual(app._prefs[srv_mod.TOKEN_PREF], token)
        with patch.object(srv_mod, "_SERVER", None):
            self.assertEqual(srv_mod.get_companion_server(FakeApp(app._prefs)).auth_token, token)

    async def test_pin_rotates_after_repeated_failures(self):
        server = CompanionServer(port=_free_port(), pin="4321", trust_loopback=False)
        port = await server.start()
        try:
            client = CompanionClient("127.0.0.1", port)
            for _ in range(5):
                with self.assertRaises(PermissionError):
                    await client.pair("0000")
            self.assertNotEqual(server.pin, "4321")
            with self.assertRaises(PermissionError):
                await client.pair("4321")
        finally:
            await server.stop()

    async def test_offload_with_greek_path(self):
        server = CompanionServer(port=_free_port(), trust_loopback=False)
        port = await server.start()
        try:
            path = unicodedata.normalize("NFC", os.path.join(self.test_dir, "Μάνος Χατζιδάκις – Τραγούδι.wav"))
            _make_tone(path)
            await self.db_manager.insert_track_hierarchical({
                "title": "Τραγούδι", "artist": "Μάνος", "album": "Άλμπουμ",
                "path": path, "duration": 20.0, "track_num": 1, "format": "wav",
            })
            client = CompanionClient("127.0.0.1", port)
            await client.pair(server.pin)
            res = await client.offload_dsp([path], self.db_manager)
            self.assertEqual((res["analysed"], res["failed"]), (1, 0))
            self.assertEqual(await self.db_manager.get_tracks_missing_features(FEATURES_VERSION), [])

            # Second run is served from the Mac cache without re-uploading.
            res2 = await client.offload_dsp([path], self.db_manager)
            self.assertEqual(res2["analysed"], 1)
        finally:
            await server.stop()

    async def test_transfer_download_end_to_end(self):
        from utils.streamrip_api import download
        server = CompanionServer(port=_free_port(), trust_loopback=False)
        port = await server.start()
        try:
            src_dir = os.path.join(self.test_dir, "mac")
            os.makedirs(src_dir)
            src = os.path.join(src_dir, "01. Ελληνικό.flac")
            _make_tone(src, 5)
            client = CompanionClient("127.0.0.1", port)
            await client.pair(server.pin)

            await server.queue_tracks_for_transfer(
                [{"path": src, "title": "Ελληνικό"}], quality="aac_256k", precompute_dsp=True)
            # Re-sending the same track replaces, not duplicates, its entry.
            await server.queue_tracks_for_transfer(
                [{"path": src, "title": "Ελληνικό"}], quality="aac_256k", precompute_dsp=False)
            pending = await client.get_pending_transfers()
            self.assertEqual(len(pending), 1)

            url = client.stream_url(pending[0])
            self.assertTrue(url.startswith(f"http://127.0.0.1:{port}/api/v1/tracks/stream?"))
            self.assertIn("token=", url)

            # Without the token: refused.
            anon = url.split("&token=")[0]
            out_dir = os.path.join(self.test_dir, "phone")
            # The temp dir counts as app-private on macOS; not under test here.
            patch("utils.storage_paths.is_app_private", return_value=False).start()
            self.addCleanup(patch.stopall)
            with self.assertRaisesRegex(PermissionError, "not paired"):
                await download(anon, out_dir)

            paths = await download(url, out_dir)
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].endswith(".m4a"))
            self.assertGreater(os.path.getsize(paths[0]), 1000)
            self.assertFalse([f for f in os.listdir(out_dir) if f.endswith(".part")])

            # A file not queued for transfer is never served.
            bogus = client.stream_url({"stream_path": "/api/v1/tracks/stream?path=%2Fetc%2Fhosts&quality=original"})
            with self.assertRaises(FileNotFoundError):
                await download(bogus, out_dir)
        finally:
            await server.stop()

    async def test_manager_pairs_heartbeats_and_detects_revocation(self):
        from utils.sync import manager as mgr_mod
        server = CompanionServer(port=_free_port(), pin="2468", trust_loopback=False)
        port = await server.start()
        app = FakeApp({"companion_sync_enabled": True})
        manager = mgr_mod.CompanionSyncManager(app)
        try:
            with patch("utils.sync.manager.BeaconListener"):
                # Unpaired: discovery opens the PIN prompt instead of "connecting".
                await manager.handle_discovered_server({"ip": "127.0.0.1", "port": port})
                self.assertFalse(manager.is_connected)
                app.page.show_dialog.assert_called_once()

                # Pair as the dialog would, then rediscover.
                token = await CompanionClient("127.0.0.1", port).pair("2468")
                app._prefs["companion_paired_token"] = token
                await manager.handle_discovered_server({"ip": "127.0.0.1", "port": port})
                self.assertTrue(manager.is_connected)

                await manager._heartbeat_once()
                self.assertTrue(manager.is_connected)

                server.reset_pairing()
                await manager._heartbeat_once()
                self.assertFalse(manager.is_connected)
                self.assertEqual(app._prefs["companion_paired_token"], "")

                # Mac gone: two missed heartbeats drop the link.
                app._prefs["companion_paired_token"] = server.auth_token
                await manager.handle_discovered_server({"ip": "127.0.0.1", "port": port})
                self.assertTrue(manager.is_connected)
                await server.stop()
                await manager._heartbeat_once()
                self.assertTrue(manager.is_connected)
                await manager._heartbeat_once()
                self.assertFalse(manager.is_connected)
        finally:
            await manager.stop()
            await server.stop()

    async def test_transfers_acked_only_after_successful_download(self):
        from utils.sync import manager as mgr_mod
        server = CompanionServer(port=_free_port(), trust_loopback=False)
        port = await server.start()
        app = FakeApp()
        app.queue_controller = MagicMock()
        manager = mgr_mod.CompanionSyncManager(app)
        try:
            src = os.path.join(self.test_dir, "a.wav")
            _make_tone(src, 2)
            await server.queue_tracks_for_transfer([{"path": src}], quality="original", precompute_dsp=False)
            client = CompanionClient("127.0.0.1", port)
            await client.pair(server.pin)
            manager.client, manager.is_connected = client, True

            transfers = await manager.check_pending_transfers()
            self.assertEqual(manager.download_transfers(transfers), 1)
            tid = transfers[0]["id"]
            item = app.queue_controller.enqueue_many.call_args[0][0][0]
            self.assertEqual(item["companion_transfer_id"], tid)
            # In flight: not offered again, still pending on the Mac.
            self.assertEqual(await manager.check_pending_transfers(), [])
            self.assertEqual(len(server._pending_transfers), 1)

            manager.on_transfer_finished(tid, ok=False)
            self.assertEqual(len(server._pending_transfers), 1)
            self.assertEqual(len(await manager.check_pending_transfers()), 1)

            manager.on_transfer_finished(tid, ok=True)
            await manager._flush_acks()
            self.assertEqual(server._pending_transfers, [])
        finally:
            await server.stop()

    async def test_pending_transfers_survive_server_restart(self):
        state = os.path.join(self.test_dir, "companion_transfers.json")
        keep = os.path.join(self.test_dir, "keep.wav")
        gone = os.path.join(self.test_dir, "gone.wav")
        _make_tone(keep, 1)
        _make_tone(gone, 1)
        first = CompanionServer(transfers_path=state)
        await first.queue_tracks_for_transfer(
            [{"path": keep, "title": "Κράτα"}, {"path": gone}], quality="original", precompute_dsp=False)
        os.remove(gone)

        second = CompanionServer(transfers_path=state)
        self.assertEqual([t["path"] for t in second._pending_transfers], [keep])
        self.assertEqual(second._pending_transfers[0]["title"], "Κράτα")

        second.clear_pending_transfers()
        self.assertEqual(CompanionServer(transfers_path=state)._pending_transfers, [])

    async def test_notification_action_before_handler_is_replayed(self):
        from flet_audio_service.flet_audio_service import AudioServiceControl
        svc = AudioServiceControl()
        svc.on_notification_action(MagicMock(data=json.dumps({"job": "navigate", "action": "sync"})))
        got = []
        svc.register_notification_action_handler("navigate", lambda j, a: got.append((j, a)))
        self.assertEqual(got, [("navigate", "sync")])
        # Replayed once, not on every registration.
        svc.register_notification_action_handler("navigate", lambda j, a: got.append((j, a)))
        self.assertEqual(len(got), 1)

    async def test_discovery_responder_answers_unicast_query(self):
        import utils.sync.discovery as disc
        port = _free_port()
        with patch.object(disc, "UDP_PORT", port):
            bc = disc.BeaconBroadcaster(port=8945, device_name="Test Mac", interval=60)
            bc.start()
            try:
                await asyncio.sleep(0.1)
                loop = asyncio.get_running_loop()
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setblocking(False)
                sock.bind(("127.0.0.1", 0))
                q = json.dumps({"service": disc.BEACON_MAGIC, "type": "query"}).encode()
                await loop.sock_sendto(sock, q, ("127.0.0.1", port))
                data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), 2.0)
                reply = json.loads(data)
                self.assertEqual((reply["name"], reply["port"]), ("Test Mac", 8945))
                sock.close()
            finally:
                bc.stop()

    async def test_listener_reports_beacon_using_sender_address(self):
        import utils.sync.discovery as disc
        port = _free_port()
        found = []
        with patch.object(disc, "UDP_PORT", port):
            lst = disc.BeaconListener(on_device_found=found.append)
            lst.start()
            try:
                loop = asyncio.get_running_loop()
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setblocking(False)
                beacon = json.dumps({"service": disc.BEACON_MAGIC, "type": "beacon",
                                     "name": "Mac", "ip": "10.99.99.99", "port": 8946}).encode()
                await loop.sock_sendto(sock, beacon, ("127.0.0.1", port))
                await asyncio.sleep(0.3)
                sock.close()
            finally:
                lst.stop()
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["ip"], found[0]["port"]), ("127.0.0.1", 8946))


if __name__ == "__main__":
    unittest.main()

