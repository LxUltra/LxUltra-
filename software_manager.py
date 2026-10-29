# -*- coding: utf-8 -*-
"""Read installed-app metadata and launch the app's interactive uninstaller."""
import base64
import ctypes
import hashlib
import json
import ntpath
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import uuid
from pathlib import Path

try:
    import winreg as _winreg
except ImportError:  # pragma: no cover - the product runs on Windows
    _winreg = None


_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
_PRODUCT_CODE = re.compile(r"(?i)(?:^|\s)/(?:i|x)\s*(\{[0-9a-f-]{36}\}|[0-9a-f]{32})(?:\s|$)")
_BLOCKED_HOSTS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe",
                  "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe"}
_ICON_SCRIPT = r"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Drawing
$payload='__PAYLOAD__'
$items=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload)))
$icons=[ordered]@{}
foreach($item in $items){
  $icon=$null;$bitmap=$null;$stream=$null
  try{
    $icon=[System.Drawing.Icon]::ExtractAssociatedIcon([string]$item.path)
    if($icon){$bitmap=$icon.ToBitmap();$stream=New-Object System.IO.MemoryStream
      $bitmap.Save($stream,[System.Drawing.Imaging.ImageFormat]::Png)
      $icons[[string]$item.id]=[Convert]::ToBase64String($stream.ToArray())}
  }catch{}finally{
    if($stream){$stream.Dispose()};if($bitmap){$bitmap.Dispose()};if($icon){$icon.Dispose()}
  }
}
ConvertTo-Json -InputObject $icons -Compress -Depth 3
"""


def _text(value):
    return str(value or "").strip()


def _install_date(value):
    value = _text(value)
    if re.fullmatch(r"\d{8}", value):
        return "%s-%s-%s" % (value[:4], value[4:6], value[6:8])
    return ""


def _icon_executable(value):
    value = os.path.expandvars(_text(value))
    if not value:
        return ""
    if value.startswith('"'):
        end = value.find('"', 1)
        if end > 1:
            value = value[1:end]
    else:
        value = re.sub(r",-?\d+\s*$", "", value)
        match = re.search(r"(?i)^(.+?\.exe)(?:\s|$)", value)
        if match:
            value = match.group(1)
    path = Path(value)
    try:
        return str(path.resolve(strict=True)) if path.suffix.casefold() == ".exe" and path.is_file() else ""
    except (OSError, RuntimeError, ValueError):
        return ""


def _is_reparse_point(info):
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _contains_reparse_point(root):
    """Avoid moving nested junctions/symlinks whose contents belong elsewhere."""
    root = Path(root)
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in [*directories, *files]:
            path = base / name
            try:
                info = path.lstat()
            except OSError:
                return True
            if path.is_symlink() or _is_reparse_point(info):
                return True
    return False


def _drive_volume_key(path):
    absolute = os.path.abspath(os.path.expandvars(str(path or "")))
    drive = ntpath.splitdrive(absolute)[0]
    if not re.fullmatch(r"[A-Za-z]:", drive):
        return None
    if os.name == "nt":
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 3:
                return None
        except (AttributeError, OSError):
            return None
    return drive.casefold()


def _is_elevated():
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _directory_manifest(root):
    """Build a content manifest without following links outside the app folder."""
    root = Path(root)
    manifest = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if path.is_symlink() or _is_reparse_point(info):
                manifest[relative] = ("link", os.readlink(path))
                directories.remove(name)
            else:
                manifest[relative] = ("directory", 0)
        for name in files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if path.is_symlink() or _is_reparse_point(info):
                manifest[relative] = ("link", os.readlink(path))
                continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest[relative] = ("file", info.st_size, digest.hexdigest())
    return manifest


def _create_directory_junction(source, target):
    """Create a Windows junction with paths passed as data, not shell syntax."""
    if os.name != "nt":
        return False
    try:
        payload = base64.b64encode(json.dumps([source, target]).encode("utf-8")).decode("ascii")
        script = (
            "$ErrorActionPreference='Stop';"
            "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))|ConvertFrom-Json;"
            "New-Item -ItemType Junction -Path $p[0] -Target $p[1] -ErrorAction Stop | Out-Null"
        ) % payload
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        return result.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _verify_directory_junction(source, target):
    try:
        return Path(source).is_dir() and os.path.samefile(source, target)
    except (OSError, ValueError):
        return False


def _remove_directory_link(path):
    try:
        candidate = Path(path)
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.exists():
            os.rmdir(candidate)
    except OSError:
        pass


def _command_line_to_argv(command):
    """Use Windows' own command-line parser; never pass registry text to a shell."""
    if os.name != "nt":
        raise ValueError("只能在 Windows 上解析卸载命令")
    count = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise ValueError("无法解析卸载命令")
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel32.LocalFree(argv)


class SoftwareManager:
    MAX_ITEMS = 2500

    def __init__(self, registry=None, popen=None, icon_runner=None, *,
                 junction_creator=None, junction_verifier=None,
                 is_elevated=None, volume_key=None):
        self._registry = registry if registry is not None else _winreg
        self._popen = popen or subprocess.Popen
        self._icon_runner = icon_runner
        self._junction_creator = junction_creator or _create_directory_junction
        self._junction_verifier = junction_verifier or _verify_directory_junction
        self._is_elevated = is_elevated or _is_elevated
        self._volume_key = volume_key or _drive_volume_key
        self._lock = threading.RLock()
        self._entries = {}

    def _roots(self):
        registry = self._registry
        if registry is None:
            return []
        access = registry.KEY_READ
        return [
            (registry.HKEY_LOCAL_MACHINE, _UNINSTALL_KEY, access | registry.KEY_WOW64_64KEY),
            (registry.HKEY_LOCAL_MACHINE, _UNINSTALL_KEY, access | registry.KEY_WOW64_32KEY),
            (registry.HKEY_CURRENT_USER, _UNINSTALL_KEY, access | registry.KEY_WOW64_64KEY),
            (registry.HKEY_CURRENT_USER, _UNINSTALL_KEY, access | registry.KEY_WOW64_32KEY),
        ]

    @staticmethod
    def _value(registry, key, name, default=None):
        try:
            return registry.QueryValueEx(key, name)[0]
        except (OSError, FileNotFoundError, ValueError):
            return default

    def scan(self):
        registry = self._registry
        if registry is None:
            return {"ok": False, "error": "当前系统无法读取已安装软件清单", "items": []}
        items, entries, seen = [], {}, set()
        try:
            for hive, base, access in self._roots():
                try:
                    with registry.OpenKey(hive, base, 0, access) as root:
                        child_count = registry.QueryInfoKey(root)[0]
                        subkeys = [registry.EnumKey(root, index)
                                   for index in range(min(child_count, self.MAX_ITEMS))]
                except (OSError, PermissionError):
                    continue
                for subkey in subkeys:
                    try:
                        key_path = base + "\\" + subkey
                        with registry.OpenKey(hive, key_path, 0, access) as key:
                            name = _text(self._value(registry, key, "DisplayName"))
                            if not name or self._value(registry, key, "SystemComponent", 0) in (1, True):
                                continue
                            if self._value(registry, key, "ParentKeyName"):
                                continue
                            release_type = _text(self._value(registry, key, "ReleaseType")).casefold()
                            if release_type in {"update", "security update", "hotfix", "更新"}:
                                continue
                            uninstall = _text(self._value(registry, key, "UninstallString"))
                            product = _PRODUCT_CODE.search(uninstall)
                            no_remove = bool(self._value(registry, key, "NoRemove", 0))
                            can_uninstall = bool((uninstall or product) and not no_remove)
                            publisher = _text(self._value(registry, key, "Publisher"))
                            version = _text(self._value(registry, key, "DisplayVersion"))
                            install_location = _text(self._value(registry, key, "InstallLocation"))
                            move_reason = self._move_location_reason(install_location)
                            estimate = self._value(registry, key, "EstimatedSize")
                            try:
                                size_bytes = max(0, int(estimate)) * 1024 if estimate is not None else None
                            except (TypeError, ValueError, OverflowError):
                                size_bytes = None
                            install_date = _install_date(self._value(registry, key, "InstallDate"))
                            identity = (hive, key_path, name, version, uninstall, install_location)
                            duplicate = (name.casefold(), publisher.casefold(), version.casefold())
                            if duplicate in seen:
                                continue
                            seen.add(duplicate)
                            token = secrets.token_urlsafe(24)
                            icon = _icon_executable(self._value(registry, key, "DisplayIcon"))
                            if not icon:
                                location = _text(self._value(registry, key, "InstallLocation"))
                                icon = _icon_executable(str(Path(location) / (name + ".exe"))) if location else ""
                            item = {"id": token, "name": name, "publisher": publisher,
                                    "version": version, "sizeBytes": size_bytes,
                                    "installDate": install_date, "canUninstall": can_uninstall,
                                    "installLocation": install_location, "canMove": not move_reason,
                                    "moveReason": move_reason}
                            items.append(item)
                            entries[token] = {"hive": hive, "key": key_path, "access": access,
                                              "name": name, "uninstall": uninstall, "icon": icon,
                                              "identity": identity, "version": version,
                                              "installLocation": install_location}
                    except (OSError, PermissionError, ValueError, TypeError):
                        continue
            items.sort(key=lambda item: (item["installDate"] or "0000-00-00", item["name"].casefold()), reverse=True)
            with self._lock:
                self._entries = entries
            return {"ok": True, "items": items[:self.MAX_ITEMS], "count": len(items),
                    "reportedSizeBytes": sum(int(item["sizeBytes"] or 0) for item in items)}
        except Exception as exc:
            return {"ok": False, "error": "读取软件清单失败：%s" % exc, "items": []}

    def parse_uninstall_command(self, command):
        command = _text(command)
        if not command or any(ord(char) < 32 for char in command):
            raise ValueError("卸载命令为空或包含无效字符")
        product = _PRODUCT_CODE.search(command)
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        if product and re.search(r"(?i)msiexec(?:\.exe)?", command):
            executable = str(Path(system_root) / "System32" / "msiexec.exe")
            if not Path(executable).is_file():
                raise ValueError("找不到 Windows Installer")
            return executable, ["/x", product.group(1)]
        argv = _command_line_to_argv(command)
        if not argv:
            raise ValueError("卸载命令为空")
        executable = os.path.expandvars(argv[0].strip('"'))
        if not os.path.isabs(executable):
            raise ValueError("卸载程序必须使用完整路径")
        path = Path(executable)
        if path.suffix.casefold() != ".exe" or path.name.casefold() in _BLOCKED_HOSTS:
            raise ValueError("该卸载方式需要脚本或命令解释器，已阻止自动启动")
        if not path.is_file():
            raise ValueError("卸载程序不存在，请使用 Windows 设置卸载")
        return str(path.resolve(strict=True)), argv[1:]

    def uninstall(self, identifier):
        if not isinstance(identifier, str):
            return {"ok": False, "error": "软件条目标识无效"}
        with self._lock:
            entry = self._entries.get(identifier)
        if not entry:
            return {"ok": False, "error": "软件清单已更新，请重新扫描后再试"}
        registry = self._registry
        try:
            with registry.OpenKey(entry["hive"], entry["key"], 0, entry["access"]) as key:
                current_name = _text(self._value(registry, key, "DisplayName"))
                current_command = _text(self._value(registry, key, "UninstallString"))
            if current_name != entry["name"] or current_command != entry["uninstall"]:
                return {"ok": False, "error": "软件信息已变化，请重新扫描并核对后再试"}
            executable, arguments = self.parse_uninstall_command(current_command)
            self._popen([executable, *arguments], cwd=str(Path(executable).parent),
                        close_fds=True, shell=False)
            return {"ok": True, "started": True, "message": "已启动该软件自己的卸载程序；请在卸载窗口确认后续步骤"}
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": "无法安全启动卸载程序：%s" % exc}

    def _move_location_reason(self, location):
        """Return a user-facing reason when a registered install directory is unsafe."""
        location = _text(location)
        if not location:
            return "软件没有登记安装位置，无法安全搬家"
        expanded = os.path.expandvars(location.strip('"'))
        normalized = ntpath.normpath(expanded).rstrip("\\/")
        drive, tail = ntpath.splitdrive(normalized)
        if not drive or not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
            return "仅支持本机固定磁盘上的软件目录"
        if not self._volume_key(normalized):
            return "仅支持本机固定磁盘上的软件目录"
        parts = [part.casefold() for part in tail.split("\\") if part]
        if len(parts) < 2:
            return "不能搬动磁盘根目录或公共安装目录"
        if any(part in {"windows", "programdata", "windowsapps", "packages", "system volume information"}
               for part in parts):
            return "系统目录和 Microsoft Store 应用受到保护"
        if len(parts) >= 2 and parts[0] in {"program files", "program files (x86)"} and parts[1] == "common files":
            return "公共组件目录受到保护"
        try:
            path = Path(normalized)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                return "登记的安装位置不是文件夹"
            if _is_reparse_point(info):
                return "安装目录是目录联接或符号链接，不能再次搬家"
        except (OSError, ValueError, RuntimeError):
            return "安装目录不存在或无法读取"
        return ""

    def move(self, identifier, destination_root):
        """Move one verified app folder to another volume and preserve its old path."""
        if not isinstance(identifier, str) or not identifier:
            return {"ok": False, "error": "软件条目标识无效，请重新扫描。"}
        with self._lock:
            entry = self._entries.get(identifier)
        if not entry:
            return {"ok": False, "error": "软件清单已更新，请重新扫描后再搬家。"}
        registry = self._registry
        try:
            with registry.OpenKey(entry["hive"], entry["key"], 0, entry["access"]) as key:
                current_name = _text(self._value(registry, key, "DisplayName"))
                current_version = _text(self._value(registry, key, "DisplayVersion"))
                current_command = _text(self._value(registry, key, "UninstallString"))
                current_location = _text(self._value(registry, key, "InstallLocation"))
        except (OSError, PermissionError, TypeError):
            return {"ok": False, "error": "无法复核软件登记信息，请重新扫描。"}
        if (current_name, current_version, current_command, current_location) != (
                entry["name"], entry["version"], entry["uninstall"], entry["installLocation"]):
            return {"ok": False, "error": "软件信息已变化，请重新扫描并核对后再搬家。"}
        reason = self._move_location_reason(current_location)
        if reason:
            return {"ok": False, "error": reason + "。"}
        if not self._is_elevated():
            return {"ok": False, "error": "搬家需要访问安装目录。请以管理员身份重新启动 LxUltra 后再试；不会绕过 Windows 权限。"}

        try:
            source = Path(os.path.abspath(os.path.expandvars(current_location.strip('"'))))
            target_root = Path(os.path.abspath(os.path.expandvars(str(destination_root or "").strip('"'))))
            if _contains_reparse_point(source):
                raise ValueError("软件目录中包含目录联接或符号链接，为避免误搬外部数据暂不支持。")
            root_info = target_root.lstat()
            if not stat.S_ISDIR(root_info.st_mode) or _is_reparse_point(root_info):
                raise ValueError("目标位置必须是已存在的普通文件夹。")
            if not re.fullmatch(r"[A-Za-z]:", ntpath.splitdrive(str(target_root))[0]):
                raise ValueError("只支持本机固定磁盘上的目标文件夹。")
            source_volume = self._volume_key(source)
            target_volume = self._volume_key(target_root)
            if not source_volume or not target_volume:
                raise ValueError("源和目标都必须位于本机固定磁盘。")
            if source_volume == target_volume:
                raise ValueError("目标文件夹必须位于另一块磁盘。")
            source_norm = os.path.normcase(os.path.abspath(str(source)))
            target_norm = os.path.normcase(os.path.abspath(str(target_root)))
            try:
                common = os.path.commonpath((source_norm, target_norm))
            except ValueError:
                common = ""
            if common in (source_norm, target_norm):
                raise ValueError("目标位置不能与软件安装目录重叠。")
            destination = target_root / source.name
            if os.path.lexists(destination):
                raise ValueError("目标位置已存在同名文件夹，请选择另一个目标目录。")
            staging = target_root / (".lxultra-stage-" + uuid.uuid4().hex)
            backup = source.with_name(source.name + ".lxultra-original-" + uuid.uuid4().hex)
            if os.path.lexists(staging) or os.path.lexists(backup):
                raise ValueError("无法分配安全的临时目录，请重试。")
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

        source_renamed = False
        destination_promoted = False
        junction_attempted = False
        try:
            shutil.copytree(source, staging, symlinks=True, copy_function=shutil.copy2)
            source_manifest = _directory_manifest(source)
            staged_manifest = _directory_manifest(staging)
            if source_manifest != staged_manifest:
                raise OSError("复制校验未通过，原软件目录保持不变。")
            os.replace(staging, destination)
            destination_promoted = True
            os.replace(source, backup)
            source_renamed = True
            junction_attempted = True
            if not self._junction_creator(str(source), str(destination)):
                raise OSError("Windows 未能创建原位置目录联接。")
            if not self._junction_verifier(str(source), str(destination)):
                raise OSError("无法验证原位置目录联接。")
            try:
                shutil.rmtree(backup)
            except OSError:
                return {"ok": True, "source": str(source), "destination": str(destination),
                        "bytes": sum(item[1] for item in source_manifest.values() if item[0] == "file"),
                        "warning": "搬家已完成，但原位置的临时备份未能删除；请稍后检查磁盘空间。"}
            return {"ok": True, "source": str(source), "destination": str(destination),
                    "bytes": sum(item[1] for item in source_manifest.values() if item[0] == "file")}
        except (OSError, shutil.Error, ValueError, TypeError) as exc:
            if junction_attempted:
                _remove_directory_link(source)
            if source_renamed and os.path.lexists(backup):
                try:
                    os.replace(backup, source)
                except OSError:
                    return {"ok": False, "error": "搬家失败且原目录回滚未完成。原数据仍保留在：%s" % backup}
            if os.path.lexists(staging):
                shutil.rmtree(staging, ignore_errors=True)
            if destination_promoted and os.path.lexists(destination):
                shutil.rmtree(destination, ignore_errors=True)
            return {"ok": False, "error": "搬家失败，原软件目录已保留：%s" % exc}

    def icons(self, identifiers):
        if not isinstance(identifiers, list) or len(identifiers) > 48 or any(not isinstance(x, str) for x in identifiers):
            return {"ok": False, "error": "软件图标请求范围无效", "icons": {}}
        with self._lock:
            entries = dict(self._entries)
        payload = [{"id": identifier, "path": entries[identifier]["icon"]}
                   for identifier in dict.fromkeys(identifiers)
                   if identifier in entries and entries[identifier].get("icon")]
        if not payload:
            return {"ok": True, "icons": {}}
        try:
            if self._icon_runner:
                icons = self._icon_runner(payload)
            else:
                encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
                script = _ICON_SCRIPT.replace("__PAYLOAD__", encoded)
                command = base64.b64encode(script.encode("utf-16le")).decode("ascii")
                result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
                                         "-EncodedCommand", command], capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=20,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
                if result.returncode != 0:
                    raise RuntimeError("图标服务暂不可用")
                icons = json.loads(result.stdout.strip() or "{}")
            if not isinstance(icons, dict):
                raise ValueError("图标响应格式无效")
            return {"ok": True, "icons": {key: "data:image/png;base64," + value
                    for key, value in icons.items() if key in entries and isinstance(value, str)
                    and re.fullmatch(r"[A-Za-z0-9+/=]+", value)}}
        except Exception:
            return {"ok": True, "icons": {}}
