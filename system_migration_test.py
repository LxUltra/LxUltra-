"""Pure system-migration planning tests. No disk is modified."""

import base64
import copy
import hashlib
import marshal
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).parent
toy = marshal.dumps(compile("class API:\n    pass\n", "toy_app.py", "exec"))
wrapper = ((ROOT / "migration_engine.py").read_text(encoding="utf-8") + "\n" +
           (ROOT / "app_wrapper.py").read_text(encoding="utf-8"))
wrapper = wrapper.replace("@@ORIG_B64@@", base64.b64encode(toy).decode("ascii"), 1)
namespace = {"__name__": "system_migration_test_wrapper"}
exec(compile(wrapper, "app_wrapper_migration_test.py", "exec"), namespace)


GB = 1024 ** 3
MB = 1024 ** 2


def example_disks():
    return [
        {
            "number": 1, "name": "SOURCE", "serial": "source-serial",
            "guid": "source-guid", "uniqueId": "source-id", "style": "GPT",
            "isSystemDisk": True, "readonly": False, "offline": False,
            "partitions": [
                {"number": 2, "guid": "source-efi", "offset": MB, "size": 100 * MB,
                 "isEFI": True, "isSystem": True, "letter": ""},
                {"number": 3, "guid": "source-win", "offset": 101 * MB, "size": 1024 * GB,
                 "used": 150 * GB, "fs": "NTFS", "letter": "C", "isBoot": True,
                 "bitlocker": "FullyDecrypted"},
            ],
        },
        {
            "number": 0, "name": "TARGET", "serial": "target-serial",
            "guid": "target-guid", "uniqueId": "target-id", "style": "GPT",
            "logicalSectorSize": 512,
            "isSystemDisk": False, "readonly": False, "offline": False,
            "partitions": [
                {"number": 12, "guid": "other-efi", "offset": 100 * GB, "size": 100 * MB,
                 "isEFI": True, "isSystem": False, "letter": ""},
                {"number": 13, "guid": "target-efi", "offset": 101 * GB, "size": 100 * MB,
                 "isEFI": True, "isSystem": False, "letter": ""},
                {"number": 14, "guid": "target-win", "offset": 102 * GB, "size": 390 * GB,
                 "used": 170 * GB, "fs": "NTFS", "letter": "I",
                 "bitlocker": "FullyDecrypted"},
            ],
        },
        {
            "number": 2, "name": "SCRATCH", "serial": "scratch-serial",
            "guid": "scratch-guid", "uniqueId": "scratch-id", "style": "GPT",
            "isSystemDisk": False, "readonly": False, "offline": False,
            "partitions": [
                {"number": 3, "guid": "scratch-vol", "offset": GB, "size": 800 * GB,
                 "free": 500 * GB, "fs": "NTFS", "letter": "G"},
            ],
        },
    ]


class PlanTests(unittest.TestCase):
    def plan(self, disks=None, scratch="G"):
        return namespace["_migration_plan_from_snapshot"](
            example_disks() if disks is None else disks, 1, 0, 14, scratch
        )

    def test_locks_source_and_exact_target_partitions(self):
        result = self.plan()
        self.assertTrue(result["ok"], result)
        plan = result["plan"]
        self.assertEqual((plan["source"]["disk"], plan["source"]["part"]), (1, 3))
        self.assertEqual((plan["target"]["disk"], plan["target"]["part"]), (0, 14))
        self.assertEqual(plan["new_efi_size"], 200 * MB)
        self.assertEqual(plan["target"]["letter"], "I")
        self.assertEqual(plan["wipe_parts"], [14])
        self.assertNotIn(13, plan["wipe_parts"])

    def test_4k_native_target_gets_larger_new_efi(self):
        disks = example_disks()
        disks[1]["logicalSectorSize"] = 4096
        self.assertEqual(self.plan(disks)["plan"]["new_efi_size"], 300 * MB)

    def test_rejects_target_that_cannot_fit_used_system_bytes(self):
        disks = example_disks()
        disks[1]["partitions"][2]["size"] = 100 * GB
        self.assertIn("容量", self.plan(disks)["error"])

    def test_rejects_scratch_on_target_disk(self):
        disks = example_disks()
        disks[1]["partitions"].append({"number": 15, "guid": "scratch", "offset": 500 * GB,
            "size": 300 * GB, "free": 300 * GB, "fs": "NTFS", "letter": "H"})
        self.assertIn("独立", self.plan(disks, scratch="H")["error"])

    def test_rejects_encrypted_source(self):
        disks = example_disks()
        disks[0]["partitions"][1]["bitlocker"] = "FullyEncrypted"
        self.assertIn("BitLocker", self.plan(disks)["error"])

    def test_rejects_running_system_as_target(self):
        result = namespace["_migration_plan_from_snapshot"](example_disks(), 1, 1, 3, "G")
        self.assertFalse(result["ok"])

    def test_rejects_identity_drift_before_any_write(self):
        plan = self.plan()["plan"]
        disks = copy.deepcopy(example_disks())
        disks[1]["partitions"][2]["guid"] = "different-volume"
        ok, reason = namespace["_migration_verify_identity"](plan, disks)
        self.assertFalse(ok)
        self.assertIn("变化", reason)

    def test_rejects_target_layout_drift(self):
        plan = self.plan()["plan"]
        disks = copy.deepcopy(example_disks())
        disks[1]["partitions"][0]["size"] += MB
        ok, reason = namespace["_migration_verify_identity"](plan, disks)
        self.assertFalse(ok)
        self.assertIn("布局", reason)

    def test_rejects_bitlocker_drift_before_write(self):
        plan = self.plan()["plan"]
        disks = copy.deepcopy(example_disks())
        disks[1]["partitions"][2]["bitlocker"] = "EncryptionInProgress"
        ok, reason = namespace["_migration_verify_identity"](plan, disks)
        self.assertFalse(ok)
        self.assertIn("加密", reason)

    def test_rejects_missing_stable_disk_identifier(self):
        disks = example_disks()
        disks[1]["uniqueId"] = ""
        self.assertIn("稳定标识", self.plan(disks)["error"])

    def test_rejects_legacy_mbr_source(self):
        disks = example_disks()
        disks[0]["style"] = "MBR"
        self.assertIn("UEFI/GPT", self.plan(disks)["error"])

    def test_backend_exposes_read_only_planner(self):
        class API:
            pass

        namespace["_install_partition_methods"](API)
        self.assertTrue(hasattr(API, "partition_migration_plan"))
        self.assertTrue(hasattr(API, "partition_job_cancel"))
        result = API().partition_migration_plan("bad", 14, "G")
        self.assertFalse(result["ok"])


class ExecutorTests(unittest.TestCase):
    class Adapter:
        def __init__(self, fail_at=None):
            self.fail_at = fail_at
            self.calls = []

        def step(self, name, plan):
            self.calls.append(name)
            if name == self.fail_at:
                raise RuntimeError(name + " failed")
            return {"name": name}

        def cleanup(self, keep_image):
            self.calls.append(("cleanup", keep_image))

    def run_execution(self, fail_at=None):
        adapter = self.Adapter(fail_at)
        progress = []
        result = namespace["_migration_execute_stages"](
            {"wipe_parts": [14]}, adapter, lambda phase, pct: progress.append((phase, pct)))
        return result, adapter.calls, progress

    def test_capture_is_verified_before_formatting(self):
        result, calls, progress = self.run_execution()
        self.assertTrue(result["ok"])
        self.assertLess(calls.index("verify_image"), calls.index("format_targets"))
        self.assertLess(calls.index("revalidate"), calls.index("format_targets"))
        self.assertEqual(calls[-1], ("cleanup", False))
        self.assertEqual(progress[-1][0], "inspect")

    def test_capture_failure_leaves_target_intact(self):
        result, calls, _ = self.run_execution("capture")
        self.assertFalse(result["ok"])
        self.assertNotIn("format_targets", calls)
        self.assertEqual(calls[-1], ("cleanup", False))

    def test_identity_drift_leaves_target_intact(self):
        result, calls, _ = self.run_execution("revalidate")
        self.assertFalse(result["ok"])
        self.assertNotIn("format_targets", calls)

    def test_post_format_failure_keeps_verified_image(self):
        result, calls, _ = self.run_execution("apply")
        self.assertFalse(result["ok"])
        self.assertIn("format_targets", calls)
        self.assertEqual(calls[-1], ("cleanup", True))

    def test_failed_final_inspection_keeps_verified_image(self):
        result, calls, _ = self.run_execution("inspect")
        self.assertFalse(result["ok"])
        self.assertEqual(calls[-1], ("cleanup", True))

    def test_cancel_before_formatting_keeps_target_untouched(self):
        cancelled = {"value": False}

        class CancelAfterRevalidation(self.Adapter):
            def cancelled(self):
                return cancelled["value"]

            def step(self, name, plan):
                super().step(name, plan)
                if name == "revalidate":
                    cancelled["value"] = True

        adapter = CancelAfterRevalidation()
        result = namespace["_migration_execute_stages"](
            {"wipe_parts": [14]}, adapter, lambda phase, pct: None)

        self.assertTrue(result.get("cancelled"))
        self.assertIn("目标分区未改动", result["message"])
        self.assertNotIn("format_targets", adapter.calls)
        self.assertEqual(adapter.calls[-1], ("cleanup", False))


class ReservedNameConsentTests(unittest.TestCase):
    def test_migration_submission_requires_consent_when_preview_found_reserved_names(self):
        require = namespace["_migration_reserved_name_consent_error"]
        entry = {"reserved_names": [{"relative": r"\Users\test\nul"}]}
        self.assertIn("必须在确认框确认跳过", require(entry, {}))
        self.assertIn("必须在确认框确认跳过", require(entry, {"skip_reserved_names": False}))
        self.assertEqual(require(entry, {"skip_reserved_names": True}), "")
        self.assertEqual(require({"reserved_names": []}, {}), "")


class WindowsMigrationAdapterTests(unittest.TestCase):
    SHADOW_ID = "{A57C7228-E9C0-4B03-8160-0352CEFC528C}"
    SHADOW_DEVICE = r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy42"

    def setUp(self):
        self.calls = []
        self.ps_calls = []
        self.plan = {"source": {"letter": "C"}, "target": {"letter": "I"},
                     "scratch": {"letter": "G"}, "token": "test-token"}

        def ps_json(script, timeout=180):
            self.ps_calls.append((script, timeout))
            return {"ok": True, "data": {"id": self.SHADOW_ID, "device": self.SHADOW_DEVICE}}

        self.ps_json = ps_json
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = namespace["_WindowsMigrationAdapter"](
            self.plan, lambda: [], ps_json, executable=r"D:\tools\wimlib-imagex.exe")
        self.adapter.image = os.path.join(self.temp.name, "system.wim")
        self.adapter.dism_scratch = os.path.join(self.temp.name, "dism-scratch")
        pathlib.Path(self.adapter.dism_scratch).mkdir()
        self.adapter.dism_config_file = os.path.join(self.adapter.dism_scratch, "WimScript.ini")
        pathlib.Path(self.adapter.image).write_bytes(b"x" * 2048)
        self.adapter.windows_letter = "T"

    def tearDown(self):
        self.temp.cleanup()

    def test_cloud_hydration_uses_windows_cfapi_for_full_file(self):
        calls = []

        class FakeFunction:
            def __init__(self, value):
                self.value = value
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                calls.append(args)
                return self.value

        kernel = type("Kernel", (), {
            "CreateFileW": FakeFunction(123),
            "CloseHandle": FakeFunction(1),
        })()
        cloud_api = type("CloudApi", (), {
            "CfHydratePlaceholder": FakeFunction(0),
        })()
        with patch.object(namespace["ctypes"], "WinDLL",
                          side_effect=lambda name, **kwargs: kernel if name == "kernel32.dll" else cloud_api):
            namespace["_migration_hydrate_placeholder"](r"C:\Users\test\OneDrive\file.docx")
        self.assertEqual(calls[0][0], r"C:\Users\test\OneDrive\file.docx")
        self.assertEqual(calls[0][1], 0x80000000)
        self.assertEqual(calls[1][1:4], (0, -1, 0))
        self.assertEqual(calls[2], (123,))

    def test_cloud_hydration_reports_provider_hresult_and_closes_handle(self):
        calls = []

        class FakeFunction:
            def __init__(self, value):
                self.value = value
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                calls.append(args)
                return self.value

        kernel = type("Kernel", (), {
            "CreateFileW": FakeFunction(123),
            "CloseHandle": FakeFunction(1),
        })()
        cloud_api = type("CloudApi", (), {
            "CfHydratePlaceholder": FakeFunction(-2147024515),
        })()
        with patch.object(namespace["ctypes"], "WinDLL",
                          side_effect=lambda name, **kwargs: kernel if name == "kernel32.dll" else cloud_api):
            with self.assertRaisesRegex(RuntimeError, "0x8007017D"):
                namespace["_migration_hydrate_placeholder"](r"C:\Users\test\OneDrive\file.docx")
        self.assertEqual(calls[-1], (123,))


    def test_capture_and_apply_use_dism_on_snapshot_data(self):
        reported = []
        self.adapter.progress = lambda *args: reported.append(args)

        def fake_run(argv, timeout=86400, progress=None, heartbeat=None, cancelled=None):
            self.calls.append((argv, timeout))
            if progress:
                progress(40)
            return "ok"

        with patch.dict(namespace, {"_migration_run": fake_run}):
            self.adapter.step("snapshot", self.plan)
            self.adapter.step("capture", self.plan)
            self.adapter.step("apply", self.plan)

        self.assertEqual(len(self.ps_calls), 1)
        self.assertIn("Win32_ShadowCopy", self.ps_calls[0][0])
        capture, apply = [call[0] for call in self.calls]
        self.assertTrue(capture[0].lower().endswith("dism.exe"))
        self.assertIn("/Capture-Image", capture)
        self.assertIn("/CaptureDir:" + self.SHADOW_DEVICE + "\\", capture)
        self.assertIn("/CheckIntegrity", capture)
        self.assertIn("/ScratchDir:" + self.adapter.dism_scratch, capture)
        self.assertLess(capture.index("/ScratchDir:" + self.adapter.dism_scratch),
                        capture.index("/Capture-Image"))
        self.assertTrue(apply[0].lower().endswith("dism.exe"))
        self.assertIn("/Apply-Image", apply)
        self.assertIn("/ApplyDir:T:\\", apply)
        self.assertIn("/ScratchDir:" + self.adapter.dism_scratch, apply)
        self.assertLess(apply.index("/ScratchDir:" + self.adapter.dism_scratch),
                        apply.index("/Apply-Image"))
        self.assertIn("/EA", apply)
        self.assertEqual(reported, [
            ("capture", 27, "DISM 报告映像阶段进度 40%", False),
            ("apply", 72, "DISM 写入进度 40%", False),
        ])

    def test_capture_reports_estimated_progress_from_image_growth(self):
        self.plan["source_used"] = 8192
        reported = []
        self.adapter.progress = lambda *args: reported.append(args)

        def fake_run(argv, timeout=86400, progress=None, heartbeat=None, cancelled=None):
            if "/Capture-Image" in argv:
                pathlib.Path(self.adapter.image).write_bytes(b"x" * 4096)
                heartbeat()
            return "ok"

        with patch.dict(namespace, {"_migration_run": fake_run}):
            self.adapter.step("snapshot", self.plan)
            self.adapter.step("capture", self.plan)

        self.assertEqual(reported[0][0:2], ("capture", 30))
        self.assertTrue(reported[0][2].endswith("（估算）"))
        self.assertTrue(reported[0][3])

    def test_cleanup_removes_only_adapter_owned_scratch_directory(self):
        scratch = pathlib.Path(self.adapter.dism_scratch)
        (scratch / "transient.tmp").write_bytes(b"temporary")
        self.adapter.owns_dism_scratch = True

        self.adapter.cleanup(keep_image=False)
        self.assertFalse(scratch.exists())

    def test_preflight_rejects_cloud_only_files_before_creating_staging(self):
        os.remove(self.adapter.image)
        os.rmdir(self.adapter.dism_scratch)
        fake_exe = pathlib.Path(self.temp.name, "wimlib-imagex.exe")
        fake_exe.write_bytes(b"trusted test binary")
        self.adapter.exe = str(fake_exe)
        self.adapter._find_unavailable_cloud_files = lambda source: {
            "count": 2,
            "files": [], "directories": [], "total_bytes": 0,
            "samples": [r"C:\Users\test\OneDrive\report.docx", r"C:\Users\test\OneDrive\video.mp4"],
            "errors": [],
        }
        patches = {"WIMLIB_EXE_SHA256": hashlib.sha256(fake_exe.read_bytes()).hexdigest(),
                   "_migration_verify_identity": lambda plan, disks: (True, "")}
        with patch.dict(namespace, patches), self.assertRaisesRegex(RuntimeError, "2 个.*云端占位文件.*勾选.*下载") as raised:
            self.adapter.step("preflight", self.plan)
        self.assertIn("report.docx", str(raised.exception))
        self.assertFalse(os.path.exists(self.adapter.image))
        self.assertFalse(self.adapter.owns_dism_scratch)

    def test_consented_cloud_files_hydrate_before_snapshot(self):
        os.remove(self.adapter.image)
        os.rmdir(self.adapter.dism_scratch)
        fake_exe = pathlib.Path(self.temp.name, "wimlib-imagex.exe")
        fake_exe.write_bytes(b"trusted test binary")
        self.adapter.exe = str(fake_exe)
        self.plan["download_cloud_files"] = True
        pending = {"count": 2, "files": [
            {"path": r"C:\Users\test\OneDrive\report.docx", "size": 123},
            {"path": r"C:\Users\test\OneDrive\video.mp4", "size": 456},
        ], "directories": [], "total_bytes": 579,
            "samples": [r"C:\Users\test\OneDrive\report.docx"], "errors": []}
        hydrated = []
        progress = []
        scans = [pending, {"count": 0, "files": [], "directories": [], "total_bytes": 0,
                           "samples": [], "errors": []}]
        self.adapter._find_unavailable_cloud_files = lambda source: scans.pop(0)
        self.adapter.hydrate_file = lambda path: hydrated.append(path)
        self.adapter.progress = lambda phase, percent: progress.append((phase, percent))
        patches = {"WIMLIB_EXE_SHA256": hashlib.sha256(fake_exe.read_bytes()).hexdigest(),
                   "_migration_verify_identity": lambda plan, disks: (True, "")}
        with patch.dict(namespace, patches):
            self.adapter.step("preflight", self.plan)
            self.adapter.step("hydrate_cloud", self.plan)
        self.assertEqual(hydrated, [item["path"] for item in pending["files"]])
        self.assertTrue(progress)
        self.assertEqual(progress[-1], ("hydrate_cloud", 10))
        self.assertTrue(os.path.isdir(self.adapter.dism_scratch))

    def test_cloud_folder_placeholders_require_manual_download(self):
        fake_exe = pathlib.Path(self.temp.name, "wimlib-imagex.exe")
        fake_exe.write_bytes(b"trusted test binary")
        self.adapter.exe = str(fake_exe)
        self.plan["download_cloud_files"] = True
        self.adapter._find_unavailable_cloud_files = lambda source: {
            "count": 1, "files": [], "directories": [r"C:\Users\test\OneDrive\Archive"],
            "total_bytes": 0, "samples": [r"C:\Users\test\OneDrive\Archive"], "errors": []}
        patches = {"WIMLIB_EXE_SHA256": hashlib.sha256(fake_exe.read_bytes()).hexdigest(),
                   "_migration_verify_identity": lambda plan, disks: (True, "")}
        with patch.dict(namespace, patches), self.assertRaisesRegex(RuntimeError, "文件夹.*资源管理器"):
            self.adapter.step("preflight", self.plan)

    def test_hydration_failure_stops_before_formatting_target(self):
        os.remove(self.adapter.image)
        os.rmdir(self.adapter.dism_scratch)
        fake_exe = pathlib.Path(self.temp.name, "wimlib-imagex.exe")
        fake_exe.write_bytes(b"trusted test binary")
        self.adapter.exe = str(fake_exe)
        self.plan["download_cloud_files"] = True
        self.adapter._find_unavailable_cloud_files = lambda source: {
            "count": 1, "files": [{"path": r"C:\Users\test\OneDrive\report.docx", "size": 123}],
            "directories": [], "total_bytes": 123,
            "samples": [r"C:\Users\test\OneDrive\report.docx"], "errors": []}
        self.adapter.hydrate_file = lambda path: (_ for _ in ()).throw(RuntimeError("provider offline"))
        patches = {"WIMLIB_EXE_SHA256": hashlib.sha256(fake_exe.read_bytes()).hexdigest(),
                   "_migration_verify_identity": lambda plan, disks: (True, "")}
        with patch.dict(namespace, patches):
            self.adapter.step("preflight", self.plan)
            with self.assertRaisesRegex(RuntimeError, "provider offline"):
                self.adapter.step("hydrate_cloud", self.plan)
        self.assertFalse(self.adapter.destructive_started)

    def test_preflight_fails_closed_when_cloud_root_scan_is_incomplete(self):
        os.remove(self.adapter.image)
        os.rmdir(self.adapter.dism_scratch)
        fake_exe = pathlib.Path(self.temp.name, "wimlib-imagex.exe")
        fake_exe.write_bytes(b"trusted test binary")
        self.adapter.exe = str(fake_exe)
        self.adapter._find_unavailable_cloud_files = lambda source: {
            "count": 0, "samples": [], "errors": [r"C:\Users\test\OneDrive: access denied"],
        }
        patches = {"WIMLIB_EXE_SHA256": hashlib.sha256(fake_exe.read_bytes()).hexdigest(),
                   "_migration_verify_identity": lambda plan, disks: (True, "")}
        with patch.dict(namespace, patches), self.assertRaisesRegex(RuntimeError, "无法完整检查 OneDrive"):
            self.adapter.step("preflight", self.plan)
        self.assertFalse(self.adapter.owns_dism_scratch)

    def test_cloud_scan_uses_win32_offline_and_recall_attribute_bits(self):
        scripts = []
        self.adapter.ps_json = lambda script, timeout=180: (
            scripts.append((script, timeout)) or
            {"ok": True, "data": {"count": 0, "samples": [], "errors": []}})
        result = self.adapter._find_unavailable_cloud_files("C")
        self.assertEqual(result["count"], 0)
        self.assertIn("$cloudFlags = 0x1000 -bor 0x400000", scripts[0][0])
        self.assertIn("$cloudFiles", scripts[0][0])
        self.assertIn("$cloudDirectories", scripts[0][0])
        self.assertIn("C:\\", scripts[0][0])
        self.assertEqual(scripts[0][1], 600)

    def test_reserved_name_scan_finds_windows_device_names_and_extensions(self):
        from types import SimpleNamespace

        class Entry:
            def __init__(self, path, name, directory=False):
                self.path, self.name, self.directory = path, name, directory

            def is_dir(self, follow_symlinks=False):
                return self.directory

            def stat(self, follow_symlinks=False):
                return SimpleNamespace(st_file_attributes=0, st_size=0)

        class Scan:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return self.entries

            def __exit__(self, *args):
                pass

        prefix = "\\\\?\\C:\\"
        users = prefix + "Users"
        profile = users + r"\testuser"
        qianfan = profile + r"\.qianfan"
        workspace = qianfan + r"\workspace"
        workbuddy = profile + r"\.workbuddy"
        skills = workbuddy + r"\skills"
        skill = skills + r"\nihaisha"
        tree = {
            users: [Entry(profile, "testuser", True)],
            profile: [Entry(qianfan, ".qianfan", True), Entry(workbuddy, ".workbuddy", True)],
            qianfan: [Entry(workspace, "workspace", True)],
            workspace: [Entry(workspace + r"\nul", "nul")],
            workbuddy: [Entry(skills, "skills", True)],
            skills: [Entry(skill, "nihaisha", True)],
            skill: [Entry(skill + r"\NUL.txt", "NUL.txt")],
        }
        with patch("os.path.isdir", return_value=True), patch(
            "os.scandir", side_effect=lambda path: Scan(tree.get(path, []))
        ):
            result = self.adapter._find_reserved_name_paths("C:\\")

        self.assertEqual(sorted(item["relative"] for item in result["items"]), sorted([
            r"\Users\testuser\.qianfan\workspace\nul",
            r"\Users\testuser\.workbuddy\skills\nihaisha\NUL.txt",
        ]))
        self.assertEqual(result["errors"], [])
    def test_snapshot_rechecks_confirmed_reserved_names_before_capture(self):
        expected = [{"relative": r"\Users\testuser\.qianfan\workspace\nul",
                     "path": r"C:\Users\test\workspace\nul",
                     "directory": False, "size": 0}]
        self.plan["reserved_names"] = expected
        self.adapter._find_reserved_name_paths = lambda root: {"items": [], "errors": []}

        with self.assertRaisesRegex(RuntimeError, "重新预览"):
            self.adapter.step("snapshot", self.plan)

        self.assertFalse(self.adapter.destructive_started)

    def test_capture_passes_exact_confirmed_reserved_names_to_dism_config(self):
        relative = r"\Users\testuser\.qianfan\workspace\nul"
        self.plan["reserved_names"] = [{"relative": relative, "path": "C:" + relative,
                                        "directory": False, "size": 0}]
        self.adapter.shadow_device = self.SHADOW_DEVICE
        self.adapter._find_reserved_name_paths = lambda root: {"items": self.plan["reserved_names"], "errors": []}
        captured = {}

        def fake_dism(args, **kwargs):
            captured["args"] = args
            return "captured"

        self.adapter._dism = fake_dism
        self.adapter.step("snapshot", self.plan)
        self.adapter.step("capture", self.plan)

        config_args = [arg for arg in captured["args"] if arg.startswith("/ConfigFile:")]
        self.assertEqual(len(config_args), 1)
        config_path = config_args[0].split(":", 1)[1]
        config = pathlib.Path(config_path).read_text(encoding="utf-16")
        self.assertIn("[ExclusionList]", config)
        self.assertIn(r"\pagefile.sys", config)
        self.assertIn(relative, config)
        self.assertEqual(config.count("\\Users\\testuser\\.qianfan\\workspace\\nul"), 1)

    def test_verified_wim_releases_snapshot_before_destructive_phase(self):
        reported = []
        self.adapter.progress = lambda *args: reported.append(args)
        self.adapter._check_image_reserved_names = lambda: None

        def fake_run(argv, timeout=86400, progress=None, heartbeat=None, cancelled=None):
            self.calls.append((argv, timeout))
            if progress:
                progress(60)
            return "verified"

        with patch.dict(namespace, {"_migration_run": fake_run}):
            self.adapter.step("snapshot", self.plan)
            self.adapter.step("verify_image", self.plan)

        self.assertTrue(self.calls[0][0][0].lower().endswith("wimlib-imagex.exe"))
        self.assertEqual(self.calls[0][0][1], "verify")
        self.assertEqual(reported, [
            ("verify_image", 48, "正在校验系统映像 · 60%", False),
            ("verify_image", 50, "正在检查映像内的保留文件名…", True),
        ])
        self.assertTrue(self.calls[1][0][0].lower().endswith("vssadmin.exe"))
        self.assertIn("/Shadow=" + self.SHADOW_ID, self.calls[1][0])
        self.assertIsNone(self.adapter.shadow_id)

    def test_verify_heartbeat_never_moves_progress_backwards(self):
        reported = []
        self.adapter.progress = lambda *args: reported.append(args)
        self.adapter._check_image_reserved_names = lambda: None
        def fake_verify(args, progress=None, heartbeat=None):
            progress(40)
            heartbeat()
            progress(100)
            heartbeat()
            return "verified"
        self.adapter._wimlib = fake_verify
        self.adapter.step("verify_image", self.plan)
        self.assertEqual([item[1] for item in reported], [47, 47, 50, 50, 50])

    def test_success_cleanup_hides_only_the_created_efi_drive_letter(self):
        self.adapter.plan["target"]["disk"] = 0
        self.adapter.efi_letter = "V"
        self.adapter.efi_guid = "{25f8ea09-ad15-4dae-b907-be0a41aa5b4d}"
        self.adapter.cleanup(keep_image=False)
        script = self.ps_calls[-1][0]
        self.assertIn("Get-Partition -DiskNumber 0", script)
        self.assertIn(self.adapter.efi_guid, script)
        self.assertIn("-AccessPath 'V:\\'", script)
        self.assertIsNone(self.adapter.efi_letter)

    def test_wim_name_gate_rejects_unlisted_reserved_name_before_target_change(self):
        from types import SimpleNamespace

        def fake_dir(*args, **kwargs):
            kwargs["stdout"].write(b"\\\n\\Users\\LxUltra\\.workbuddy\\skills\\nihaisha\\nul\n")
            return SimpleNamespace(returncode=0, stderr=b"")

        with patch("subprocess.run", side_effect=fake_dir):
            with self.assertRaisesRegex(RuntimeError, "系统映像仍包含"):
                self.adapter._check_image_reserved_names()
        self.assertFalse(self.adapter.destructive_started)
        self.assertFalse(os.path.exists(os.path.join(self.adapter.dism_scratch, "wim-paths.txt")))

    def test_snapshot_volume_uses_a_single_trailing_separator(self):
        self.adapter.step("snapshot", self.plan)
        script = self.ps_calls[0][0]
        self.assertIn("Volume='C:\\'", script)
        self.assertNotIn("Volume='C:\\\\'", script)

    def test_invalid_snapshot_device_still_releases_created_snapshot(self):
        self.ps_json = lambda script, timeout=180: {
            "ok": True, "data": {"id": self.SHADOW_ID, "device": "invalid-device"}}
        self.adapter.ps_json = self.ps_json

        def fake_run(argv, timeout=86400):
            self.calls.append((argv, timeout))
            return "deleted"

        with patch.dict(namespace, {"_migration_run": fake_run}):
            with self.assertRaisesRegex(RuntimeError, "无效设备路径"):
                self.adapter.step("snapshot", self.plan)
            self.adapter.cleanup(keep_image=False)

        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][0][0].lower().endswith("vssadmin.exe"))
        self.assertIn("/Shadow=" + self.SHADOW_ID, self.calls[0][0])


class MigrationResultTests(unittest.TestCase):
    def test_migration_result_carries_actual_target_identity(self):
        plan = {"source": {"disk": 1, "part": 3},
                "target": {"disk": 0, "part": 15, "letter": "L"}}
        result = namespace["_migration_result_with_target"](
            {"ok": False, "message": "capture failed"}, plan)
        self.assertEqual(result["op"], {"type": "migrate_system", "disk": 0,
                                        "part": 15, "letter": "L",
                                        "source_disk": 1, "source_part": 3})


class MigrationCommandProgressTests(unittest.TestCase):
    def test_capture_estimate_advances_from_written_image_bytes(self):
        estimate = namespace["_migration_capture_estimate"]
        self.assertEqual(estimate(0, 100), 15)
        self.assertEqual(estimate(50, 100), 30)
        self.assertEqual(estimate(1000, 100), 44)

    def test_migration_run_streams_dism_progress_updates(self):
        class FakeStdout:
            def __init__(self):
                self.chunks = iter((b"DISM: 10", b".0%\rDISM: 32.5", b"%\rThe operation completed successfully.\n", b""))

            def read(self, size):
                return next(self.chunks)

        class FakeProcess:
            def __init__(self):
                self.stdout = FakeStdout()
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                raise AssertionError("successful command must not be killed")

        progress = []
        with patch.object(namespace["subprocess"], "Popen", return_value=FakeProcess()):
            output = namespace["_migration_run"](["dism.exe", "/Capture-Image"],
                                                  progress=progress.append)

        self.assertEqual(progress, [10, 32])
        self.assertIn("The operation completed successfully.", output)

    def test_migration_run_cancels_a_running_child_process(self):
        released = namespace["threading"].Event()

        class FakeStdout:
            def read(self, size):
                released.wait(2)
                return b""

        class FakeProcess:
            def __init__(self):
                self.stdout = FakeStdout()
                self.returncode = -9
                self.killed = False

            def kill(self):
                self.killed = True
                released.set()

            def wait(self, timeout=None):
                return self.returncode

        process = FakeProcess()
        with patch.object(namespace["subprocess"], "Popen", return_value=process):
            with self.assertRaises(namespace["_MigrationCancelled"]):
                namespace["_migration_run"](["dism.exe", "/Capture-Image"],
                                              progress=lambda percent: None,
                                              cancelled=lambda: True)
        self.assertTrue(process.killed)

    def test_cancel_request_is_only_accepted_before_target_rebuild(self):
        safe_job = {"state": "running", "migration": True, "cancelable": True}
        accepted = namespace["_migration_request_cancel"](safe_job)
        self.assertTrue(accepted["ok"])
        self.assertTrue(safe_job["cancel_requested"])

        destructive_job = {"state": "running", "migration": True, "cancelable": False}
        rejected = namespace["_migration_request_cancel"](destructive_job)
        self.assertFalse(rejected["ok"])
        self.assertNotIn("cancel_requested", destructive_job)

if __name__ == "__main__":
    unittest.main()
