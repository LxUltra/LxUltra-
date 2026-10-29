"""Software inventory and uninstall safety contracts; never uninstalls a real app."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from software_manager import SoftwareManager


class _Key:
    def __init__(self, children=None, values=None):
        self.children = list(children or [])
        self.values = dict(values or {})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Registry:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    KEY_WOW64_32KEY = 2
    KEY_WOW64_64KEY = 4
    REG_SZ = 1
    REG_DWORD = 4

    def __init__(self, records):
        self.records = records

    def OpenKey(self, hive, path, _reserved=0, _access=0):
        if path.endswith("\\Uninstall"):
            children = [name for record_hive, name in self.records if record_hive == hive]
            return _Key(children=children)
        for (record_hive, name), values in self.records.items():
            if record_hive == hive and path.endswith("\\Uninstall\\" + name):
                return _Key(values=values)
        raise OSError("key not found")

    @staticmethod
    def QueryInfoKey(key):
        return (len(key.children), 0, 0)

    @staticmethod
    def EnumKey(key, index):
        return key.children[index]

    @staticmethod
    def QueryValueEx(key, name):
        if name not in key.values:
            raise OSError("value not found")
        return key.values[name], 1


class SoftwareManagerTest(unittest.TestCase):
    def test_scan_returns_visible_app_metadata_and_skips_system_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            uninstaller = Path(temp) / "uninstall.exe"
            uninstaller.write_bytes(b"placeholder")
            records = {
                ("HKLM", "Vendor.App"): {
                    "DisplayName": "Example App", "DisplayVersion": "2.4",
                    "Publisher": "Example Inc", "EstimatedSize": 1536,
                    "InstallDate": "20261028",
                    "UninstallString": f'"{uninstaller}" /uninstall',
                    "DisplayIcon": str(uninstaller),
                },
                ("HKLM", "Windows.Component"): {
                    "DisplayName": "Windows Component", "SystemComponent": 1,
                    "UninstallString": '"C:\\Windows\\System32\\fake.exe" /uninstall',
                },
            }
            manager = SoftwareManager(registry=_Registry(records))
            result = manager.scan()
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["items"]), 1)
            app = result["items"][0]
            self.assertEqual(app["name"], "Example App")
            self.assertEqual(app["publisher"], "Example Inc")
            self.assertEqual(app["sizeBytes"], 1536 * 1024)
            self.assertEqual(app["installDate"], "2026-10-28")
            self.assertTrue(app["canUninstall"])

    def test_scan_exposes_install_location_and_move_eligibility(self):
        with tempfile.TemporaryDirectory() as temp:
            app_dir = Path(temp) / "ExampleApp"
            app_dir.mkdir()
            (app_dir / "app.exe").write_bytes(b"app")
            records = {("HKLM", "Vendor.App"): {
                "DisplayName": "Example App", "InstallLocation": str(app_dir),
                "UninstallString": "C:\\Tools\\uninstall.exe /uninstall",
            }}
            result = SoftwareManager(
                registry=_Registry(records), volume_key=lambda _path: "fixed-drive"
            ).scan()
            app = result["items"][0]
            self.assertEqual(app["installLocation"], str(app_dir))
            self.assertTrue(app["canMove"])

    def test_move_copies_to_other_volume_and_replaces_source_with_link(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "Program Files" / "ExampleApp"
            source.mkdir(parents=True)
            (source / "app.exe").write_bytes(b"application-data")
            target_root = base / "G-drive"
            target_root.mkdir()
            linked = []
            records = {("HKLM", "Vendor.App"): {
                "DisplayName": "Example App", "InstallLocation": str(source),
                "UninstallString": "C:\\Tools\\uninstall.exe /uninstall",
            }}

            def create_link(old, new):
                linked.append((Path(old), Path(new)))
                Path(old).mkdir()
                return True

            manager = SoftwareManager(
                registry=_Registry(records), junction_creator=create_link,
                junction_verifier=lambda _old, _new: True,
                is_elevated=lambda: True,
                volume_key=lambda path: "source" if str(source).casefold() in str(path).casefold() else "target",
            )
            app = manager.scan()["items"][0]
            result = manager.move(app["id"], str(target_root))
            self.assertTrue(result["ok"], result)
            self.assertEqual((target_root / "ExampleApp" / "app.exe").read_bytes(), b"application-data")
            self.assertTrue(source.is_dir())
            self.assertEqual(linked, [(source, target_root / "ExampleApp")])

    def test_move_rolls_back_source_when_link_creation_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "App"
            source.mkdir()
            (source / "app.exe").write_bytes(b"keep-me")
            target_root = base / "other-volume"
            target_root.mkdir()
            records = {("HKLM", "Vendor.App"): {
                "DisplayName": "Example App", "InstallLocation": str(source),
            }}
            manager = SoftwareManager(
                registry=_Registry(records), junction_creator=lambda _old, _new: False,
                is_elevated=lambda: True,
                volume_key=lambda path: "src" if str(source).casefold() in str(path).casefold() else "dst",
            )
            app = manager.scan()["items"][0]
            result = manager.move(app["id"], str(target_root))
            self.assertFalse(result["ok"])
            self.assertEqual((source / "app.exe").read_bytes(), b"keep-me")
            self.assertFalse((target_root / "App").exists())

    def test_move_rolls_back_when_junction_creator_leaves_partial_path(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "App"
            source.mkdir()
            (source / "app.exe").write_bytes(b"keep-me")
            target_root = base / "other-volume"
            target_root.mkdir()
            records = {("HKLM", "Vendor.App"): {
                "DisplayName": "Example App", "InstallLocation": str(source),
            }}

            def partial_link(old, _new):
                Path(old).mkdir()
                return False

            manager = SoftwareManager(
                registry=_Registry(records), junction_creator=partial_link,
                is_elevated=lambda: True,
                volume_key=lambda path: "src" if str(source).casefold() in str(path).casefold() else "dst",
            )
            app = manager.scan()["items"][0]
            result = manager.move(app["id"], str(target_root))
            self.assertFalse(result["ok"])
            self.assertEqual((source / "app.exe").read_bytes(), b"keep-me")
            self.assertFalse((target_root / "App").exists())

    def test_move_rejects_nested_links_and_non_fixed_target(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "App"
            source.mkdir()
            (source / "app.exe").write_bytes(b"keep-me")
            outside = base / "outside"
            outside.mkdir()
            (outside / "private.dat").write_bytes(b"must-not-be-copied")
            (source / "external-data").symlink_to(outside, target_is_directory=True)
            target_root = base / "other-volume"
            target_root.mkdir()
            records = {("HKLM", "Vendor.App"): {
                "DisplayName": "Example App", "InstallLocation": str(source),
            }}
            manager = SoftwareManager(
                registry=_Registry(records), junction_creator=Mock(return_value=True),
                is_elevated=lambda: True,
                volume_key=lambda path: None if str(target_root).casefold() in str(path).casefold() else "src",
            )
            app = manager.scan()["items"][0]
            nested_link = manager.move(app["id"], str(target_root))
            self.assertFalse(nested_link["ok"])
            self.assertIn("联接或符号链接", nested_link["error"])
            self.assertTrue((outside / "private.dat").exists())
            self.assertFalse((target_root / "App").exists())

            (source / "external-data").unlink()
            non_fixed = manager.move(app["id"], str(target_root))
            self.assertFalse(non_fixed["ok"])
            self.assertIn("固定磁盘", non_fixed["error"])
            self.assertTrue((source / "app.exe").exists())

    def test_scan_disables_apps_on_non_fixed_volumes(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "App"
            source.mkdir()
            records = {("HKLM", "Vendor.App"): {
                "DisplayName": "Example App", "InstallLocation": str(source),
            }}
            manager = SoftwareManager(registry=_Registry(records), volume_key=lambda _path: None)
            app = manager.scan()["items"][0]
            self.assertFalse(app["canMove"])
            self.assertIn("固定磁盘", app["moveReason"])

    def test_move_rejects_changed_registry_identity_and_same_volume(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "App"
            source.mkdir()
            (source / "app.exe").write_bytes(b"keep-me")
            target_root = base / "target"
            target_root.mkdir()
            record = {"DisplayName": "Example App", "InstallLocation": str(source)}
            registry = _Registry({("HKLM", "Vendor.App"): record})
            manager = SoftwareManager(
                registry=registry, junction_creator=Mock(return_value=True),
                is_elevated=lambda: True, volume_key=lambda _path: "same",
            )
            app = manager.scan()["items"][0]
            record["InstallLocation"] = str(base / "Changed")
            stale = manager.move(app["id"], str(target_root))
            self.assertFalse(stale["ok"])
            self.assertIn("重新扫描", stale["error"])
            self.assertEqual((source / "app.exe").read_bytes(), b"keep-me")
            record["InstallLocation"] = str(source)
            same_volume = manager.move(app["id"], str(target_root))
            self.assertFalse(same_volume["ok"])
            self.assertTrue(source.exists())

    def test_uninstall_uses_argument_vector_and_never_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            uninstaller = Path(temp) / "uninstall.exe"
            uninstaller.write_bytes(b"placeholder")
            records = {("HKLM", "Vendor.App"): {
                "DisplayName": "Example App", "UninstallString": f'"{uninstaller}" /uninstall',
            }}
            launch = Mock()
            manager = SoftwareManager(registry=_Registry(records), popen=launch)
            item = manager.scan()["items"][0]
            result = manager.uninstall(item["id"])
            self.assertTrue(result["ok"])
            self.assertEqual(launch.call_args.args[0][0], str(uninstaller))
            self.assertFalse(launch.call_args.kwargs.get("shell", False))

    def test_uninstall_refuses_shell_and_script_hosts(self):
        manager = SoftwareManager(registry=_Registry({}), popen=Mock())
        for command in (
            'cmd.exe /c "erase important"',
            'powershell.exe -Command "Remove-Item important"',
            'wscript.exe C:\\Temp\\remove.vbs',
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    manager.parse_uninstall_command(command)


if __name__ == "__main__":
    unittest.main()
