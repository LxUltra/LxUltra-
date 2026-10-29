import unittest
import io
import base64
import json
import subprocess
from unittest.mock import patch

import driver_updates


class DriverUpdatesTest(unittest.TestCase):
    def test_selected_update_revision_change_keeps_same_update_identity(self):
        self.assertEqual(driver_updates.selected_update_ids(["41fe6df9-7a07-4988-a841-ea1d4870fd99:1"]),
                         ["41fe6df9-7a07-4988-a841-ea1d4870fd99"])

    def test_selected_identity_matches_in_encoded_powershell(self):
        update_id = "41fe6df9-7a07-4988-a841-ea1d4870fd99"
        payload = base64.b64encode(json.dumps([update_id]).encode()).decode()
        assignment = next(line for line in driver_updates._WUA_SCRIPT.splitlines()
                          if line.startswith("$requestedIds ="))
        script = assignment.replace("__IDS__", payload) + "\n" + (
            "$requestedIds -contains '%s' | ConvertTo-Json -Compress" % update_id)
        encoded = base64.b64encode(script.encode("utf-16le")).decode()
        output = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                 "-EncodedCommand", encoded], capture_output=True,
                                text=True, timeout=10, check=True)
        self.assertEqual(output.stdout.strip(), "true")

    def test_streamed_windows_update_progress_reaches_manager(self):
        class Process:
            stdout = io.StringIO('{"kind":"progress","phase":"downloading","percent":37}\n'
                                 '{"kind":"progress","phase":"installing","percent":12}\n'
                                 '{"kind":"result","ok":true,"rebootRequired":false}\n')
            returncode = 0

            def wait(self):
                return 0

            def kill(self):
                self.returncode = -9

        events = []
        with patch.object(driver_updates.subprocess, "Popen", return_value=Process()) as launch:
            result = driver_updates.run_windows_update("install", ["A:1"], events.append)
        self.assertEqual(launch.call_args.args[0][3], "-Command")
        self.assertTrue(result["ok"])
        self.assertEqual(events, [{"phase": "downloading", "percent": 37},
                                  {"phase": "installing", "percent": 12}])

    def test_manager_exposes_real_phase_and_percent(self):
        manager = driver_updates.DriverUpdateManager()
        manager._updates = [{"key": "A:1", "title": "蓝牙驱动"}]
        def fake_run(action, keys, progress):
            progress({"phase": "downloading", "percent": 42})
            self.assertEqual(manager.status()["phase"], "downloading")
            self.assertEqual(manager.status()["percent"], 42)
            return {"ok": True}
        with patch.object(driver_updates, "run_windows_update", side_effect=fake_run):
            self.assertTrue(manager.install(["A:1"])["ok"])
            manager._thread.join(timeout=3)
        self.assertEqual(manager.status()["state"], "complete")

    def test_scan_keeps_only_unique_driver_identities(self):
        rows = [
            {"id": "A", "revision": 1, "title": "显示适配器", "size": 1024},
            {"id": "A", "revision": 1, "title": "显示适配器", "size": 1024},
            {"id": "B", "revision": 2, "title": "网卡", "size": 2048},
        ]
        self.assertEqual([r["key"] for r in driver_updates.normalize_updates(rows)], ["A:1", "B:2"])

    def test_install_rejects_update_not_in_latest_scan(self):
        manager = driver_updates.DriverUpdateManager()
        manager._updates = [{"key": "A:1", "title": "显示适配器"}]
        self.assertFalse(manager.install(["B:2"])["ok"])

    def test_scan_runs_in_background_and_reports_failure(self):
        manager = driver_updates.DriverUpdateManager()
        with patch.object(driver_updates, "run_windows_update", side_effect=RuntimeError("服务不可用")):
            self.assertTrue(manager.scan()["ok"])
            manager._thread.join(timeout=3)
        state = manager.status()
        self.assertEqual(state["state"], "error")
        self.assertIn("服务不可用", state["error"])


if __name__ == "__main__":
    unittest.main()
