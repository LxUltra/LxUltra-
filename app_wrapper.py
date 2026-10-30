# -*- coding: utf-8 -*-
"""LxUltra 电脑管家 · 分区助手后端包装器(替换 PyInstaller 归档 'app' 主脚本)

机制:
  1. _ORIG_B64 内嵌原始 app 模块(Python 3.11 marshal 字节码, base64)
  2. 以自定义 dict 命名空间 exec 原模块;拦截 'API' 类的 STORE_NAME,
     在类定义完成的瞬间挂载 partition_* 方法(不改动原字节码任何一行)
  3. 原模块尾部逻辑保持原样:worker 分发(Storage 扫描子进程)或 main() 启动窗口

安全铁律(后端强制,前端绕不过):
  - 含系统盘判定:删除全部/擦除/快速分区/转换/克隆目标 一律拒绝系统盘
  - 系统盘上的 启动/系统/EFI/MSR/恢复 分区拒绝 删除/格式化/改盘符
  - 破坏性操作必须管理员权限,返回明确中文错误
"""
import sys
import os
import ntpath
import types
import base64
import marshal
import tempfile

_ORIG_B64 = "@@ORIG_B64@@"

_WRAPPER_TAG = "partition-wrapper/1.2.1-update-handoff"
_DRIVER_UPDATES = DriverUpdateManager() if "DriverUpdateManager" in globals() else None
_CPU_FREQ_SAMPLER = CpuFrequencySampler() if "CpuFrequencySampler" in globals() else None
_SOFTWARE_MANAGER = SoftwareManager() if "SoftwareManager" in globals() else None
_SINGLE_INSTANCE_MUTEX = None
_SINGLE_INSTANCE_EVENT = None
_SINGLE_INSTANCE_API = None


def _acquire_single_instance():
    """A second GUI launch wakes the existing window instead of opening another."""
    if os.name != "nt" or len(sys.argv) > 1:
        return True  # Keep maintenance and frozen multiprocessing workers independent.
    import ctypes
    import threading
    import time
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    mutex_name = r"Local\LxUltra_Desktop_SingleInstance_v1"
    event_name = r"Local\LxUltra_Desktop_Activate_v1"
    ctypes.set_last_error(0)
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    if not mutex:
        raise OSError(ctypes.get_last_error(), "Cannot create LxUltra single-instance mutex")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        event = None
        for _ in range(20):
            event = kernel.OpenEventW(0x0002, False, event_name)  # EVENT_MODIFY_STATE
            if event:
                break
            time.sleep(0.1)
        if event:
            kernel.SetEvent(event)
            kernel.CloseHandle(event)
        kernel.CloseHandle(mutex)
        return False

    global _SINGLE_INSTANCE_MUTEX, _SINGLE_INSTANCE_EVENT
    _SINGLE_INSTANCE_MUTEX = mutex
    event = kernel.CreateEventW(None, False, False, event_name)
    if not event:
        raise OSError(ctypes.get_last_error(), "Cannot create LxUltra activation event")
    _SINGLE_INSTANCE_EVENT = event

    def listen_for_second_launch():
        while kernel.WaitForSingleObject(event, 0xFFFFFFFF) == 0:
            # A second launch may arrive while the first webview is still starting.
            for _ in range(100):
                api = _SINGLE_INSTANCE_API
                if api is not None and getattr(api, "_window", None) is not None:
                    try:
                        api._restore_from_tray()
                    except Exception as exc:
                        _wrap_log("single-instance activation failed: %r" % (exc,))
                    break
                time.sleep(0.1)

    threading.Thread(target=listen_for_second_launch, daemon=True,
                     name="lxultra-single-instance").start()
    return True


def _migration_plan_from_snapshot(disks, source_disk, target_disk, target_part,
                                  scratch_letter):
    """Pure, read-only UEFI migration preflight. No disk command is run here."""
    def reject(reason):
        return {"ok": False, "error": reason}

    if not isinstance(disks, list):
        return reject("无法读取磁盘列表")
    if not isinstance(scratch_letter, str) or len(scratch_letter) != 1 or not scratch_letter.isalpha():
        return reject("必须选择独立暂存卷盘符")
    scratch_letter = scratch_letter.upper()
    try:
        source_disk, target_disk = int(source_disk), int(target_disk)
        target_part = int(target_part)
    except (TypeError, ValueError, OverflowError):
        return reject("磁盘或分区编号无效")
    if source_disk == target_disk:
        return reject("系统源盘和目标盘必须是不同物理磁盘")
    by_number = {d.get("number"): d for d in disks if isinstance(d, dict)}
    source, target = by_number.get(source_disk), by_number.get(target_disk)
    if not source or not target:
        return reject("源盘或目标盘不存在")
    if any(str(d.get("style", "")).upper() != "GPT" for d in (source, target)):
        return reject("系统迁移目前只支持 UEFI/GPT 到 UEFI/GPT")
    if not source.get("isSystemDisk") or target.get("isSystemDisk"):
        return reject("源盘必须是当前系统盘，目标盘不能是正在运行的系统盘")
    if any(d.get("readonly") or d.get("offline") for d in (source, target)):
        return reject("源盘或目标盘离线或只读")

    def find_part(disk, number):
        return next((p for p in disk.get("partitions", []) if p.get("number") == number), None)

    source_windows = next((p for p in source.get("partitions", [])
                           if p.get("isBoot") and str(p.get("letter", "")).upper() == "C"
                           and str(p.get("fs", "")).upper() == "NTFS"), None)
    source_efi = next((p for p in source.get("partitions", [])
                       if p.get("isEFI") and p.get("isSystem")), None)
    if not source_windows or not source_efi:
        return reject("无法锁定当前 C: 系统卷及其 EFI 启动分区")
    windows_target = find_part(target, target_part)
    if not windows_target:
        return reject("目标 Windows 分区不存在")
    if str(windows_target.get("fs", "")).upper() != "NTFS" or not windows_target.get("letter"):
        return reject("目标 Windows 分区必须是带盘符的 NTFS 数据分区")
    if any(windows_target.get(flag) for flag in ("isBoot", "isSystem", "isEFI", "isMSR", "isRecovery")):
        return reject("不能把启动或保留分区当作目标 Windows 分区")
    sector = int(target.get("logicalSectorSize") or 0)
    if sector not in (512, 4096):
        return reject("无法确认目标硬盘扇区大小")
    new_efi_size = (300 if sector == 4096 else 200) * 1024 * 1024
    if any(p.get("bitlocker") != "FullyDecrypted" for p in (source_windows, windows_target)):
        return reject("源卷或目标卷启用了 BitLocker，或加密状态无法确认")
    used = int(source_windows.get("used") or 0)
    if used <= 0:
        return reject("无法读取系统卷已用空间")
    if int(windows_target.get("size") or 0) < used + new_efi_size + 16 * 1024 ** 3:
        return reject("目标分区容量不足，至少需要系统已用空间、EFI 和 16 GB 空余")

    scratch_disk = None
    scratch_part = None
    for disk in disks:
        for part in disk.get("partitions", []):
            if str(part.get("letter", "")).upper() == scratch_letter:
                scratch_disk, scratch_part = disk, part
                break
        if scratch_part:
            break
    if not scratch_part or scratch_disk.get("number") in (source_disk, target_disk):
        return reject("暂存卷必须位于独立于源盘和目标盘的物理磁盘")
    if scratch_disk.get("offline") or scratch_disk.get("readonly") or str(scratch_part.get("fs", "")).upper() != "NTFS":
        return reject("暂存卷必须是在线可写的 NTFS 卷")
    if int(scratch_part.get("free") or 0) < used + 5 * 1024 ** 3:
        return reject("暂存卷容量不足，至少需要系统已用空间再加 5 GB")

    def identity(disk, part):
        return {"disk": disk["number"], "part": part["number"],
                "disk_guid": str(disk.get("guid") or ""),
                "disk_unique_id": str(disk.get("uniqueId") or ""),
                "disk_serial": str(disk.get("serial") or ""),
                "part_guid": str(part.get("guid") or ""),
                "offset": int(part.get("offset") or 0), "size": int(part.get("size") or 0),
                "letter": str(part.get("letter") or "").upper()}

    plan = {"source": identity(source, source_windows), "source_efi": identity(source, source_efi),
            "target": identity(target, windows_target), "new_efi_size": new_efi_size,
            "scratch": identity(scratch_disk, scratch_part), "source_used": used,
            "wipe_parts": [target_part],
            "target_layout": [(p.get("number"), str(p.get("guid") or ""),
                               int(p.get("offset") or 0), int(p.get("size") or 0))
                              for p in target.get("partitions", [])]}
    if any(not item["disk_guid"] or not item["part_guid"] or not item["disk_unique_id"]
           for item in (plan["source"], plan["source_efi"], plan["target"], plan["scratch"])):
        return reject("磁盘或分区缺少稳定标识，无法安全执行迁移")
    return {"ok": True, "plan": plan}


def _migration_verify_identity(plan, fresh_disks):
    """Fail closed if any selected disk/partition has changed since preview."""
    if not isinstance(plan, dict) or not isinstance(fresh_disks, list):
        return False, "迁移预览或磁盘信息无效"
    by_number = {d.get("number"): d for d in fresh_disks if isinstance(d, dict)}
    for name in ("source", "source_efi", "target", "scratch"):
        saved = plan.get(name)
        if not isinstance(saved, dict):
            return False, "迁移预览缺少 %s" % name
        disk = by_number.get(saved.get("disk"))
        if not disk:
            return False, "%s 所在磁盘已变化" % name
        part = next((p for p in disk.get("partitions", [])
                     if p.get("number") == saved.get("part")), None)
        if not part:
            return False, "%s 分区已变化" % name
        current = {"disk_guid": str(disk.get("guid") or ""),
                   "disk_unique_id": str(disk.get("uniqueId") or ""),
                   "disk_serial": str(disk.get("serial") or ""),
                   "part_guid": str(part.get("guid") or ""),
                   "offset": int(part.get("offset") or 0),
                   "size": int(part.get("size") or 0),
                   "letter": str(part.get("letter") or "").upper()}
        if any(current[k] != saved.get(k) for k in current):
            return False, "%s 的磁盘/分区身份或盘符已变化，请重新预览" % name
        if name in ("source", "target") and part.get("bitlocker") != "FullyDecrypted":
            return False, "%s 卷加密状态已变化，拒绝继续迁移" % name
    target_disk = by_number[plan["target"]["disk"]]
    layout = [(p.get("number"), str(p.get("guid") or ""),
               int(p.get("offset") or 0), int(p.get("size") or 0))
              for p in target_disk.get("partitions", [])]
    if layout != [tuple(item) for item in plan.get("target_layout", [])]:
        return False, "目标磁盘的其他分区布局已变化，请重新预览"
    return True, ""


def _migration_result_with_target(result, plan):
    """Attach the locked source and target to every migration result for UI reporting."""
    output = dict(result or {})
    source = plan.get("source") or {}
    target = plan.get("target") or {}
    output["type"] = "migrate_system"
    output["op"] = {
        "type": "migrate_system",
        "disk": target.get("disk"),
        "part": target.get("part"),
        "letter": target.get("letter"),
        "source_disk": source.get("disk"),
        "source_part": source.get("part"),
    }
    return output


def _migration_request_cancel(job):
    if job.get("state") != "running" or not job.get("migration"):
        return {"ok": False, "error": "没有正在运行的系统迁移任务"}
    if not job.get("cancelable"):
        return {"ok": False, "error": "目标分区已开始重建，为避免留下无法启动的系统盘，当前阶段不能取消"}
    job["cancel_requested"] = True
    job["cancelable"] = False
    return {"ok": True, "message": "正在安全停止当前阶段并清理暂存映像"}


def _migration_reserved_name_consent_error(entry, op):
    if (entry.get("reserved_names") or []) and op.get("skip_reserved_names") is not True:
        return "源系统含有 Windows 保留名路径，必须在确认框确认跳过后才能提交"
    return ""


def _migration_execute_stages(plan, adapter, report):
    """Order irreversible stages only after a verified source image exists."""
    phases = (
        ("preflight", 2), ("hydrate_cloud", 5), ("snapshot", 12), ("capture", 15),
        ("verify_image", 45), ("revalidate", 50),
        ("format_targets", 55), ("apply", 60), ("boot", 90), ("inspect", 98),
    )
    destructive_started = False
    succeeded = False
    current = "preflight"
    try:
        for current, percent in phases:
            if callable(getattr(adapter, "cancelled", None)) and adapter.cancelled():
                raise _MigrationCancelled("用户取消")
            report(current, percent)
            if current == "format_targets":
                if callable(getattr(adapter, "cancelled", None)) and adapter.cancelled():
                    raise _MigrationCancelled("用户取消")
                destructive_started = True
            adapter.step(current, plan)
        succeeded = True
        return {"ok": True, "message": "系统文件与目标引导已写入；独立启动验收仍待完成"}
    except _MigrationCancelled:
        return {"ok": False, "cancelled": True,
                "message": "已取消系统迁移，目标分区未改动",
                "target_may_be_modified": False}
    except Exception as exc:
        return {"ok": False, "message": "%s 阶段失败: %s" % (current, exc),
                "phase": current, "target_may_be_modified": destructive_started}
    finally:
        try:
            adapter.cleanup(keep_image=destructive_started and not succeeded)
        except Exception as exc:
            _wrap_log("migration cleanup error: %r" % exc)

def _wrap_log(msg):
    try:
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".").strip()
        d = os.path.join(base, "LxUltra")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = base
        import datetime
        with open(os.path.join(d, "partition_wrapper.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


_wrap_log("module import: argv=%r meipass=%r frozen=%r" % (
    sys.argv, getattr(sys, "_MEIPASS", None), getattr(sys, "frozen", None)))


# ============================================================
# 分区助手后端:挂在 API 类上的方法实现
# ============================================================
def _install_partition_methods(API):
    if "install_storage_guards" in globals():
        try:
            install_storage_guards(API)
        except Exception as e:
            _wrap_log("storage safety guards failed to install: %r" % (e,))
    original_metrics = getattr(API, "metrics", None)
    original_process_icons = getattr(API, "process_icons", None)

    def metrics(self):
        result = original_metrics(self)
        if not isinstance(result, dict):
            return result
        telemetry = result.get("temperatures") or {}
        cpu = result.get("cpu") or {}
        rows = telemetry.get("sensors") or []
        p_names = {str(r.get("name")) for r in rows if r.get("key") == "cpu"
                   and r.get("type") == "Clock" and str(r.get("name", "")).startswith("P-Core #")}
        e_names = {str(r.get("name")) for r in rows if r.get("key") == "cpu"
                   and r.get("type") == "Clock" and str(r.get("name", "")).startswith("E-Core #")}
        if _CPU_FREQ_SAMPLER:
            readings = _CPU_FREQ_SAMPLER.current()
            summary = summarize_frequencies(readings, len(p_names), len(e_names))
            if not summary:
                topology = _CPU_FREQ_SAMPLER.topology()
                if topology:
                    summary = summarize_frequencies(readings, *topology)
            if summary:
                cpu["frequency"] = summary["p_average"]
                cpu["peakFrequency"] = summary["p_peak"]
                cpu["eCoreFrequency"] = summary["e_average"]
                cpu["frequencySource"] = "Windows 实际频率"
            else:
                cpu["frequency"] = None
                cpu["peakFrequency"] = None
                cpu["eCoreFrequency"] = None
                cpu["frequencySource"] = "Windows 实际频率暂不可用"
        voltage = classify_voltage(rows) if "classify_voltage" in globals() else {}
        if isinstance(telemetry.get("cpu"), dict):
            telemetry["cpu"]["vcore"] = voltage.get("vcore")
            telemetry["cpu"]["voltageSource"] = "传感器各核心报告值；与 CPU-Z 采样口径可能不同"
        return result

    def driver_updates_scan(self):
        return _DRIVER_UPDATES.scan() if _DRIVER_UPDATES else {"ok": False, "error": "驱动检测组件未加载"}

    def driver_updates_status(self):
        return _DRIVER_UPDATES.status() if _DRIVER_UPDATES else {"ok": False, "error": "驱动检测组件未加载"}

    def driver_updates_install(self, keys):
        return _DRIVER_UPDATES.install(keys) if _DRIVER_UPDATES else {"ok": False, "error": "驱动检测组件未加载"}

    def fm_software_list(self):
        return _SOFTWARE_MANAGER.scan() if _SOFTWARE_MANAGER else {
            "ok": False, "error": "软件清单组件未加载", "items": []}

    def fm_software_uninstall(self, identifier):
        return _SOFTWARE_MANAGER.uninstall(identifier) if _SOFTWARE_MANAGER else {
            "ok": False, "error": "软件清单组件未加载"}

    def fm_software_move(self, identifier, destination_root):
        return _SOFTWARE_MANAGER.move(identifier, destination_root) if _SOFTWARE_MANAGER else {
            "ok": False, "error": "软件搬家组件未加载"}

    def fm_software_icons(self, identifiers):
        return _SOFTWARE_MANAGER.icons(identifiers) if _SOFTWARE_MANAGER else {
            "ok": False, "error": "软件图标组件未加载", "icons": {}}

    def process_icons(self, paths):
        """Normalize caller paths before checking the current process snapshot allowlist."""
        if not callable(original_process_icons) or not isinstance(paths, list) or len(paths) > 48:
            return {}
        lock = getattr(self, "_process_icon_lock", None)
        if lock is None:
            return {}
        try:
            with lock:
                allowed = tuple(getattr(self, "_process_icon_allowed", set()))
        except Exception:
            return {}
        canonical_allowed = set()
        for value in allowed:
            try:
                candidate = os.path.normcase(os.path.realpath(str(value)))
                if candidate and os.path.isfile(candidate):
                    canonical_allowed.add(candidate)
            except (OSError, TypeError, ValueError):
                continue
        canonical_paths, raw_by_canonical = [], {}
        for value in paths:
            if not isinstance(value, str) or not value:
                continue
            try:
                if os.path.splitext(value)[1].casefold() != ".exe":
                    continue
                canonical = os.path.normcase(os.path.realpath(value))
                if canonical not in canonical_allowed or not os.path.isfile(canonical):
                    continue
                if canonical not in raw_by_canonical:
                    raw_by_canonical[canonical] = []
                    canonical_paths.append(canonical)
                raw_by_canonical[canonical].append(value)
            except (OSError, TypeError, ValueError):
                continue
        if not canonical_paths:
            return {}
        try:
            icons = original_process_icons(self, canonical_paths)
        except Exception:
            return {}
        if not isinstance(icons, dict):
            return {}
        result = {}
        for canonical, raw_values in raw_by_canonical.items():
            icon = icons.get(canonical)
            if isinstance(icon, str) and icon:
                for raw in raw_values:
                    result[raw] = icon
        return result

    def process_background_windows(self):
        """Return PIDs that own visible top-level windows for conservative suggestions."""
        if os.name != "nt":
            return {"ok": False, "error": "后台窗口识别仅支持 Windows", "visiblePids": []}
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            visible_pids = set()
            user32.IsWindowVisible.argtypes = (wintypes.HWND,)
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                                        ctypes.POINTER(wintypes.DWORD))
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD

            def visit(hwnd, _lparam):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value:
                        visible_pids.add(int(pid.value))
                except Exception:
                    pass
                return True

            callback = enum_proc(visit)
            user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)
            user32.EnumWindows.restype = wintypes.BOOL
            if not user32.EnumWindows(callback, 0):
                code = ctypes.get_last_error()
                return {"ok": False, "error": "无法枚举可见窗口（Windows 错误 %s）" % code,
                        "visiblePids": []}
            return {"ok": True, "visiblePids": sorted(visible_pids)}
        except Exception as e:
            _wrap_log("visible process window scan failed: %r" % (e,))
            return {"ok": False, "error": "无法可靠识别当前可见窗口", "visiblePids": []}

    import json
    import re
    import subprocess
    import tempfile
    import threading
    import ctypes

    _plock = threading.RLock()
    _jobs = {}          # job_id -> {"state","results","current","progress","done","error"}
    _job_seq = [0]
    _migration_plans = {}  # preview token -> immutable selected identities

    # ---------- 基础设施 ----------
    def _is_admin():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _ps(script, timeout=180):
        """执行 PowerShell(Windows PowerShell 5.1),输出走临时文件规避控制台编码问题。
        返回 dict(ok, rc, out, err)。"""
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            wrapped = (
                "$ErrorActionPreference='Stop';\n"
                "$__out = $null;\n"
                "try { $__out = & {\n" + script + "\n} } catch { $__out = @{ ok = $false; error = ($_.Exception.Message | Out-String) } }\n"
                "if ($null -ne $__out) { $__out | ConvertTo-Json -Depth 10 | Out-File -FilePath '%s' -Encoding utf8 }\n"
            ) % tmp
            cp = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapped],
                capture_output=True, timeout=timeout,
                creationflags=0x08000000,
            )
            out = ""
            if os.path.exists(tmp):
                raw = open(tmp, "rb").read()
                if raw:
                    out = raw.decode("utf-8-sig", "replace").strip()
            return {"ok": cp.returncode == 0 and out != "", "rc": cp.returncode, "out": out,
                    "err": cp.stderr.decode("utf-8", "replace") if cp.stderr else ""}
        except subprocess.TimeoutExpired:
            return {"ok": False, "rc": -2, "out": "", "err": "PowerShell 执行超时(%d 秒)" % timeout}
        except Exception as e:
            return {"ok": False, "rc": -1, "out": "", "err": repr(e)}
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _ps_json(script, timeout=180):
        r = _ps(script, timeout)
        if not r["out"]:
            return {"ok": False, "error": (r["err"] or "PowerShell 无输出(rc=%s)" % r["rc"]).strip()}
        try:
            data = json.loads(r["out"])
        except Exception as e:
            return {"ok": False, "error": "结果解析失败: %s" % e}
        if isinstance(data, dict) and data.get("ok") is False:
            return {"ok": False, "error": str(data.get("error", "PowerShell 脚本失败")).strip()}
        return {"ok": True, "data": data}

    # ---------- 磁盘信息(只读,不需要管理员) ----------
    _PS_DISKS = r"""
$r = @()
$sysLetter = $env:SystemDrive.TrimEnd(':')
$sysDisk = $null
try { $sysDisk = (Get-Partition -DriveLetter $sysLetter -ErrorAction Stop).DiskNumber } catch {}
$protectedDisks = @{}
if ($null -ne $sysDisk) { $protectedDisks[[string]([int]$sysDisk)] = $true }
foreach ($criticalDisk in (Get-Disk -ErrorAction SilentlyContinue | Where-Object { $_.IsBoot -or $_.IsSystem })) {
    $protectedDisks[[string]([int]$criticalDisk.Number)] = $true
}
$volByLetter = @{}
$blByLetter = @{}
if ($readBitLocker) {
    try {
        foreach ($b in (Get-BitLockerVolume -ErrorAction Stop)) {
            $mp = [string]$b.MountPoint
            if ($mp -match '^([A-Z]):') { $blByLetter[$Matches[1]] = [string]$b.VolumeStatus }
        }
    } catch {}
}
foreach ($v in (Get-Volume -ErrorAction SilentlyContinue)) {
    if ($v.DriveLetter -and [string]$v.DriveLetter -match '^[A-Z]$') { $volByLetter[[string]$v.DriveLetter] = $v }
}
foreach ($d in (Get-Disk | Sort-Object Number)) {
    $parts = @()
    try { $pl = Get-Partition -DiskNumber $d.Number -ErrorAction Stop | Sort-Object Offset } catch { $pl = @() }
    foreach ($p in $pl) {
        $gt = [string]$p.GptType
        $tname = '其他'
        if ($gt -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}') { $tname = 'EFI 系统分区' }
        elseif ($gt -eq '{e3c9e316-0b5c-4db8-817d-f92df00215ae}') { $tname = 'MSR 保留分区' }
        elseif ($gt -eq '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}') { $tname = '恢复分区' }
        elseif ($gt -eq '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}') { $tname = '主分区' }
        elseif ($p.MbrType -eq 7 -or $p.MbrType -eq 12) { $tname = '主分区' }
        elseif ($p.MbrType -eq 15 -or $p.MbrType -eq 5) { $tname = '扩展分区' }
        $letter = ''
        if ($p.DriveLetter -and [string]$p.DriveLetter -match '^[A-Z]$') { $letter = [string]$p.DriveLetter }
        $used = 0; $free = 0; $label = $null; $fs = $null
        $bitlocker = 'Unknown'
        if ($letter -and $volByLetter.ContainsKey($letter)) {
            $vol = $volByLetter[$letter]
            if ($vol.Size) { $used = [int64]($vol.Size - $vol.SizeRemaining) }
            $free = [int64]$vol.SizeRemaining
            $label = $vol.FileSystemLabel
            $fs = $vol.FileSystem
        }
        if ($letter -and $blByLetter.ContainsKey($letter)) { $bitlocker = $blByLetter[$letter] }
        $status = '无'
        if ($p.IsBoot) { $status = '启动' }
        if ($p.IsSystem) { $status = '系统' }
        if ($p.IsBoot -and $p.IsSystem) { $status = '系统、启动' }
        if ($p.IsHidden) { $status = $status + '(隐藏)' }
        $parts += [ordered]@{
            number = [int]$p.PartitionNumber
            guid = [string]$p.Guid
            offset = [int64]$p.Offset
            size = [int64]$p.Size
            typeCode = $gt
            typeName = $tname
            letter = $letter
            label = $label
            fs = $fs
            bitlocker = $bitlocker
            used = [int64]$used
            free = [int64]$free
            isBoot = [bool]$p.IsBoot
            isSystem = [bool]$p.IsSystem
            isEFI = ($gt -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}')
            isMSR = ($gt -eq '{e3c9e316-0b5c-4db8-817d-f92df00215ae}')
            isRecovery = ($gt -eq '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}')
            hidden = [bool]$p.IsHidden
            active = [bool]$p.IsActive
            aligned = [bool]$p.IsOffsetAlignmentAdjusted
            status = $status
        }
    }
    $r += [ordered]@{
        number = [int]$d.Number
        name = [string]$d.FriendlyName
        serial = [string]$d.SerialNumber
        guid = [string]$d.Guid
        uniqueId = [string]$d.UniqueId
        logicalSectorSize = [int]$d.LogicalSectorSize
        bus = [string]$d.BusType
        size = [int64]$d.Size
        style = [string]$d.PartitionStyle
        health = [string]$d.HealthStatus
        operational = [string]$d.OperationalStatus
        readonly = [bool]$d.IsReadOnly
        offline = [bool]$d.IsOffline
        isSystemDisk = [bool]$protectedDisks.ContainsKey([string]([int]$d.Number))
        isBootDisk = [bool]$d.IsBoot
        partitions = $parts
    }
}
$r
"""

    def _fresh_migration_disks():
        r = _ps_json("$readBitLocker=$true\n" + _PS_DISKS, timeout=120)
        if not r["ok"]:
            raise RuntimeError(r.get("error", "无法读取实时磁盘信息"))
        return r["data"] if isinstance(r["data"], list) else [r["data"]]

    def _ps_system_disks():
        r = _ps_json(r"""
$numbers = @()
$sysLetter = $env:SystemDrive.TrimEnd(':')
try { $numbers += [int](Get-Partition -DriveLetter $sysLetter -ErrorAction Stop).DiskNumber } catch {}
foreach ($d in (Get-Disk -ErrorAction SilentlyContinue | Where-Object { $_.IsBoot -or $_.IsSystem })) {
    $numbers += [int]$d.Number
}
@($numbers | Sort-Object -Unique)
""")
        if r["ok"]:
            try:
                v = r["data"]
                if isinstance(v, dict):
                    v = v.get("value")
                if v is None:
                    return None
                if not isinstance(v, list):
                    v = [v]
                numbers = {int(item) for item in v if item is not None}
                return numbers or None
            except Exception:
                return None
        return None

    def _fmt_bytes(n):
        try:
            n = float(n)
            for u in ("B", "KB", "MB", "GB", "TB", "PB"):
                if n < 1024 or u == "PB":
                    return ("%.1f %s" % (n, u)) if u != "B" else ("%d B" % n)
                n /= 1024.0
        except Exception:
            return "?"

    # ---------- 操作执行器 ----------
    _DISKLESS_OPS = ("benchmark", "scan", "defrag", "vhd_mount", "vhd_dismount")
    _KNOWN_OPS = ("format", "delete_partition", "delete_all", "quick_partition", "convert",
                  "rebuild_mbr", "wipe", "set_letter", "set_label", "extend", "shrink",
                  "scan", "defrag", "benchmark", "clone_disk", "clone_partition",
                  "migrate_system", "vhd_mount", "vhd_dismount")

    _FILESYSTEMS = {"NTFS", "EXFAT", "FAT32"}

    def _as_int(value, minimum=0):
        if isinstance(value, bool):
            return None
        try:
            if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
                return None
            result = int(value)
            max_value = (2 ** 63 - 1) // (1024 * 1024)
            return result if minimum <= result <= max_value else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _validate_label(value):
        if value is None:
            return None
        if not isinstance(value, str):
            return "卷标必须是文本"
        if len(value) > 32 or any(ord(ch) < 32 for ch in value):
            return "卷标最多 32 个字符，且不能包含控制字符"
        return None

    def _validate_op(op):
        """提交前的前置校验:返回 None(合法)或错误描述。"""
        if not isinstance(op, dict):
            return "操作项不是对象"
        t = op.get("type")
        if t not in _KNOWN_OPS:
            return "未知操作类型: %r" % (t,)
        if t == "migrate_system":
            if op.get("confirmed") is not True or not isinstance(op.get("token"), str):
                return "系统迁移必须先预览并确认精确目标"
            if _as_int(op.get("disk"), 0) is None or _as_int(op.get("part"), 1) is None:
                return "系统迁移目标编号无效"
            if "download_cloud_files" in op and not isinstance(op.get("download_cloud_files"), bool):
                return "OneDrive 文件下载确认无效"
            if "skip_reserved_names" in op and not isinstance(op.get("skip_reserved_names"), bool):
                return "Windows 保留名跳过确认无效"
            return None
        if t not in _DISKLESS_OPS:
            if _as_int(op.get("disk"), 0) is None:
                return "操作 %s 缺少有效的磁盘编号" % t
            if t in ("format", "delete_partition", "set_letter", "set_label", "extend", "shrink", "clone_partition"):
                if _as_int(op.get("part"), 1) is None:
                    return "操作 %s 缺少有效的分区编号" % t
        if t in ("scan", "defrag", "benchmark", "set_label"):
            if not re.match(r"^[A-Za-z]$", str(op.get("letter") or "")):
                return "操作 %s 缺少有效盘符" % t
        if t == "scan" and "fix" in op and not isinstance(op.get("fix"), bool):
            return "分区修复选项无效"
        if t in ("format", "quick_partition"):
            fs_values = [op.get("fs", "NTFS")] if t == "format" else [
                item.get("fs", "NTFS") for item in (op.get("layout") or []) if isinstance(item, dict)]
            if any(not isinstance(fs, str) or fs.upper() not in _FILESYSTEMS for fs in fs_values):
                return "文件系统仅支持 NTFS、exFAT 或 FAT32"
        if t in ("format", "set_label"):
            label_error = _validate_label(op.get("label", ""))
            if label_error:
                return label_error
        if t == "quick_partition":
            layout = op.get("layout")
            if not isinstance(layout, list) or not 1 <= len(layout) <= 4:
                return "快速分区需要 1 到 4 个分区布局项"
            if str(op.get("style", "GPT")).upper() not in ("GPT", "MBR"):
                return "分区表类型必须是 GPT 或 MBR"
            if not isinstance(op.get("boot_style", False), bool):
                return "启动分区选项无效"
            if op.get("boot_style", False) and str(op.get("style", "GPT")).upper() != "GPT":
                return "MBR 分区表不支持 GPT 启动分区布局"
            letters = set()
            for index, item in enumerate(layout):
                if not isinstance(item, dict):
                    return "快速分区布局项必须是对象"
                size = item.get("size_mb")
                if size in (None, "rest"):
                    if index != len(layout) - 1:
                        return "只有最后一个分区可以使用剩余空间"
                elif _as_int(size, 1) is None:
                    return "分区大小必须是正整数 MB"
                label_error = _validate_label(item.get("label", ""))
                if label_error:
                    return label_error
                fs = item.get("fs", "NTFS")
                if not isinstance(fs, str) or fs.upper() not in _FILESYSTEMS:
                    return "文件系统仅支持 NTFS、exFAT 或 FAT32"
                letter = item.get("letter")
                if letter not in (None, ""):
                    if not isinstance(letter, str) or not re.fullmatch(r"[A-Za-z]", letter):
                        return "分区盘符必须是单个英文字母"
                    letter = letter.upper()
                    if letter in letters:
                        return "快速分区不能重复分配同一盘符"
                    letters.add(letter)
        if t == "convert" and str(op.get("to", "")).upper() not in ("GPT", "MBR"):
            return "目标分区表类型必须是 GPT 或 MBR"
        if t == "set_letter" and not re.fullmatch(r"[A-Za-z]", str(op.get("letter") or "")):
            return "新盘符必须是 A-Z 之间的单个字母"
        if t in ("extend", "shrink"):
            size = op.get("size_mb")
            if t == "shrink" and _as_int(size, 1) is None:
                return "缩容需要填写正整数 MB"
            if t == "extend" and size not in (None, "") and _as_int(size, 1) is None:
                return "扩容大小必须是正整数 MB"
        if t == "clone_disk":
            if op.get("confirmed") is not True:
                return "克隆操作需要先在确认窗口确认目标数据将被镜像"
            if _as_int(op.get("source"), 0) is None:
                return "克隆操作缺少有效的源磁盘编号"
            if int(op["source"]) == int(op["disk"]):
                return "源磁盘和目标磁盘不能相同"
        if t == "clone_partition":
            if op.get("confirmed") is not True:
                return "克隆操作需要先在确认窗口确认目标数据将被镜像"
            if _as_int(op.get("source_disk"), 0) is None or _as_int(op.get("source_part"), 1) is None:
                return "克隆分区缺少有效的源分区编号"
            if not re.fullmatch(r"[A-Za-z]", str(op.get("source_letter") or "")):
                return "克隆分区缺少有效的源卷盘符"
            if not re.fullmatch(r"[A-Za-z]", str(op.get("dest_letter") or "")):
                return "克隆分区缺少有效的目标卷盘符"
            if int(op["source_disk"]) == int(op["disk"]) and int(op["source_part"]) == int(op["part"]):
                return "源分区和目标分区不能相同"
        if t in ("vhd_mount", "vhd_dismount"):
            path = op.get("path")
            if not isinstance(path, str) or not path.strip() or len(path) > 32760 or any(ord(ch) < 32 for ch in path):
                return "操作 %s 缺少有效 VHD 路径" % t
            if not ntpath.isabs(path) or ntpath.splitext(path)[1].lower() not in (".vhd", ".vhdx"):
                return "VHD 路径必须是 .vhd 或 .vhdx 的完整路径"
        return None

    def _op_guard(op, sys_disks):
        """返回 None(放行)或错误字符串。系统盘安全铁律在此。"""
        t = op.get("type")
        if t in _DISKLESS_OPS:      # 按盘符/路径操作,天然不涉及磁盘编号
            return None
        disk = op.get("disk")
        try:
            disk = int(disk)
        except Exception:
            return "缺少有效的磁盘编号"
        if not sys_disks:
            return "无法定位系统盘(拒绝执行破坏性操作)"
        if disk in sys_disks and t in ("delete_all", "wipe", "quick_partition", "convert", "clone_disk", "clone_partition"):
            return "安全保护:系统盘(含 C:)不允许 %s 操作" % {
                "delete_all": "删除所有分区", "wipe": "擦除", "quick_partition": "快速分区",
                "convert": "分区表转换", "clone_disk": "作为克隆目标", "clone_partition": "作为克隆目标"}.get(t, t)
        if t == "rebuild_mbr":
            if disk in sys_disks:
                return "安全保护:不能对系统或启动磁盘重建 MBR"
            check = _ps_json(r"""
$d = Get-Disk -Number %d -ErrorAction Stop
if ([string]$d.PartitionStyle -ne 'MBR') { @{ ok = $false; error = '目标磁盘不是 MBR 分区表' } }
elseif ($d.IsOffline -or $d.IsReadOnly) { @{ ok = $false; error = '目标磁盘离线或只读' } }
elseif (-not (Get-Partition -DiskNumber %d -ErrorAction Stop | Where-Object { $_.DriveLetter } | Select-Object -First 1)) { @{ ok = $false; error = '目标磁盘没有带盘符的分区，无法定位引导卷' } }
else { @{ ready = $true } }
""" % (disk, disk))
            if not check["ok"]:
                return check.get("error", "目标磁盘 MBR 预检查失败")
        if t in ("clone_disk", "clone_partition"):
            try:
                source_disk = int(op.get("source") if t == "clone_disk" else op.get("source_disk", -1))
                if source_disk in sys_disks:
                    return "安全保护:不允许对运行中的系统盘做文件级克隆"
            except Exception:
                return "克隆操作缺少源盘编号"
        return None

    def _protected_partition(disk, part, sys_disks):
        """分区级守卫:系统盘上的 启动/系统/EFI/MSR/恢复 分区受保护。
        返回 None(可操作)或原因字符串。含一次实时 PS 查询。"""
        if not sys_disks:
            return "无法定位系统盘(拒绝执行破坏性操作)"
        try:
            disk = int(disk); part = int(part)
        except Exception:
            return "缺少有效的分区编号"
        r = _ps_json(r"""
$p = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$gt = [string]$p.GptType
$protected = ($p.IsBoot -or $p.IsSystem -or $p.IsActive -or $gt -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -or $gt -eq '{e3c9e316-0b5c-4db8-817d-f92df00215ae}' -or $gt -eq '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}')
@{ protected = [bool]$protected; isBoot = [bool]$p.IsBoot; isSystem = [bool]$p.IsSystem; isActive = [bool]$p.IsActive }
""" % (disk, part))
        if not r["ok"]:
            return "无法读取分区信息: %s" % r.get("error", "")
        d = r["data"]
        if isinstance(d, dict) and d.get("protected"):
            return "安全保护:系统盘上的 启动/系统/活动/EFI/MSR/恢复分区不允许此操作"
        return None

    def _exec_op(op, sys_disks, progress=None):
        """执行单条操作,返回 dict(ok, message)。前置:已过 _op_guard + 管理员校验。"""
        t = op.get("type")
        disk = op.get("disk")
        part = op.get("part")
        letter = str(op.get("letter") or "").strip().rstrip(":").upper()
        label = str(op.get("label") or "").strip()
        fs = str(op.get("fs") or "NTFS").upper()
        size_mb = op.get("size_mb")

        # 分区级守卫:系统盘受保护分区拒绝 删除/格式化/改盘符
        if t in ("format", "delete_partition", "set_letter", "set_label", "extend", "shrink", "clone_partition"):
            guard = _protected_partition(disk, part, sys_disks)
            if guard:
                return {"ok": False, "message": guard}
        if t == "clone_partition":
            source_disk = int(op.get("source_disk", -1))
            source_part = int(op.get("source_part", -1))
            guard = _protected_partition(source_disk, source_part, sys_disks)
            if guard:
                return {"ok": False, "message": "源分区: " + guard}

        if t == "format":
            if fs == "FAT32":
                size_check = _ps_json(r"""
$p = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
if ($p.Size -gt 32GB) { @{ ok = $false; error = 'Windows 不支持将大于 32 GB 的卷格式化为 FAT32' } }
else { @{ size = [int64]$p.Size } }
""" % (int(disk), int(part)))
                if not size_check["ok"]:
                    return {"ok": False, "message": size_check.get("error", "分区容量检查失败")}
            r = _ps_json(r"""
$p = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$vol = Get-Volume -Partition $p -ErrorAction Stop
$fs = '%s'
$params = @{ FileSystem = $fs; Confirm = $false; Force = $true }
if ('%s') { $params.NewFileSystemLabel = '%s' }
if ($fs -in @('NTFS')) { $params.Full = $false }
$vol | Format-Volume @params | Select-Object FileSystemLabel, FileSystem, Size
""" % (disk, part, fs, label, label.replace("'", "''")))
            return {"ok": r["ok"], "message": "格式化完成" if r["ok"] else r.get("error", "格式化失败")}

        if t == "delete_partition":
            r = _ps_json(r"""
Remove-Partition -DiskNumber %d -PartitionNumber %d -Confirm:$false -ErrorAction Stop
@{ done = $true }
""" % (disk, part))
            return {"ok": r["ok"], "message": "分区已删除" if r["ok"] else r.get("error", "删除失败")}

        if t == "delete_all":
            r = _ps_json(r"""
Clear-Disk -Number %d -RemoveData -RemoveOEM -Confirm:$false -ErrorAction Stop
@{ done = $true }
""" % disk)
            return {"ok": r["ok"], "message": "已删除磁盘 %s 的全部分区" % disk if r["ok"] else r.get("error", "操作失败")}

        if t == "quick_partition":
            style = str(op.get("style") or "GPT").upper()
            layout = op.get("layout") or []
            boot_style = bool(op.get("boot_style"))
            if not layout:
                return {"ok": False, "message": "快速分区缺少分区布局"}
            # 先核对磁盘容量和布局，再执行 Clear-Disk，避免错误输入先擦除磁盘。
            fixed_mb = sum(int(item["size_mb"]) for item in layout
                           if item.get("size_mb") not in (None, "rest"))
            reserved_mb = 276 if style == "GPT" and boot_style else 0
            required_mb = fixed_mb + reserved_mb + 4
            preflight = _ps_json(r"""
$d = Get-Disk -Number %d -ErrorAction Stop
$required = %d * 1MB
if ($d.IsOffline -or $d.IsReadOnly) { @{ ok = $false; error = '磁盘离线或只读，无法分区' } }
elseif ($required -gt $d.Size) { @{ ok = $false; error = '分区布局总容量超过磁盘容量' } }
else { @{ size = [int64]$d.Size; number = [int]$d.Number } }
""" % (int(disk), required_mb))
            if not preflight["ok"]:
                return {"ok": False, "message": preflight.get("error", "磁盘容量预检查失败")}
            lines = []
            lines.append(r"$target = Get-Disk -Number %d -ErrorAction Stop" % int(disk))
            lines.append(r"if ((%d * 1MB) -gt $target.Size) { throw '分区布局总容量超过磁盘容量' }" % required_mb)
            requested_letters = {str(item.get("letter") or "").strip().rstrip(":").upper()
                                 for item in layout} - {""}
            for letter in sorted(requested_letters):
                if re.fullmatch(r"[A-Z]", letter):
                    lines.append(r"$existing = Get-Partition -DriveLetter '%s' -ErrorAction SilentlyContinue" % letter)
                    lines.append(r"if ($existing -and $existing.DiskNumber -ne %d) { throw '盘符 %s: 已被其他磁盘占用' }" % (int(disk), letter))
            lines.append(r"if ([string]$target.PartitionStyle -ne 'RAW') { Clear-Disk -Number %d -RemoveData -RemoveOEM -Confirm:$false -ErrorAction Stop }" % disk)
            lines.append(r"Initialize-Disk -Number %d -PartitionStyle %s -ErrorAction Stop" % (disk, style))
            if style == "GPT" and boot_style:
                lines.append(r"$efi = New-Partition -DiskNumber %d -Size 260MB -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -ErrorAction Stop" % disk)
                lines.append(r"$efi | Format-Volume -FileSystem FAT32 -NewFileSystemLabel 'SYSTEM' -Confirm:$false | Out-Null")
                lines.append(r"New-Partition -DiskNumber %d -Size 16MB -GptType '{e3c9e316-0b5c-4db8-817d-f92df00215ae}' -ErrorAction Stop | Out-Null" % disk)
            for i, item in enumerate(layout):
                size = item.get("size_mb")
                ltr = str(item.get("letter") or "").strip().rstrip(":").upper()
                lbl = str(item.get("label") or "").strip().replace("'", "''")
                fsys = str(item.get("fs") or "NTFS").upper()
                is_last = (i == len(layout) - 1)
                if size in (None, "", "rest", 0, -1):
                    if not is_last:
                        return {"ok": False, "message": "只有最后一个分区可以使用剩余全部空间"}
                    cmd = r"$np = New-Partition -DiskNumber %d -UseMaximumSize" % disk
                else:
                    cmd = r"$np = New-Partition -DiskNumber %d -Size %dMB" % (disk, int(size))
                if style == "MBR":
                    cmd += " -MbrType 7"
                else:
                    cmd += r" -GptType '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'"
                if ltr and re.match(r"^[A-Z]$", ltr):
                    cmd += " -DriveLetter %s" % ltr
                else:
                    cmd += " -AssignDriveLetter"
                cmd += " -ErrorAction Stop"
                lines.append(cmd)
                fmt = r"$np | Format-Volume -FileSystem %s -Confirm:$false" % fsys
                if lbl:
                    fmt += " -NewFileSystemLabel '%s'" % lbl
                fmt += " | Select-Object FileSystemLabel, Size"
                lines.append(fmt)
            r = _ps_json("\n".join(lines), timeout=600)
            return {"ok": r["ok"], "message": "快速分区完成" if r["ok"] else r.get("error", "快速分区失败")}

        if t == "convert":
            to = str(op.get("to") or "").upper()
            if to not in ("GPT", "MBR"):
                return {"ok": False, "message": "目标分区表类型必须是 GPT 或 MBR"}
            r = _ps_json(r"""
Clear-Disk -Number %d -RemoveData -RemoveOEM -Confirm:$false -ErrorAction Stop
Initialize-Disk -Number %d -PartitionStyle %s -ErrorAction Stop
@{ done = $true }
""" % (disk, disk, to))
            return {"ok": r["ok"], "message": "已转换为 %s(磁盘已清空)" % to if r["ok"] else r.get("error", "转换失败")}

        if t == "rebuild_mbr":
            r = _ps_json(r"""
$bf = Join-Path $env:SystemRoot 'System32\bootsect.exe'
if (-not (Test-Path $bf)) { @{ ok = $false; error = '未找到 bootsect.exe' } }
else {
    $d = Get-Disk -Number %d -ErrorAction Stop
    if ([string]$d.PartitionStyle -ne 'MBR' -or $d.IsBoot -or $d.IsSystem) { throw '目标不是可修复的非系统 MBR 磁盘' }
    $p = Get-Partition -DiskNumber %d -ErrorAction Stop | Where-Object { $_.DriveLetter } | Select-Object -First 1
    if (-not $p) { throw '目标磁盘没有带盘符的分区，无法定位引导卷' }
    $target = ([string]$p.DriveLetter) + ':'
    $o = & $bf /nt60 $target /mbr /force 2>&1 | Out-String
            if ($LASTEXITCODE -ne 0) { throw $o }
    @{ done = $true; output = $o; target = $target; disk = [int]$d.Number }
}
""" % (int(disk), int(disk)), timeout=120)
            return {"ok": r["ok"], "message": "已重建硬盘 %s (%s) 的 MBR 引导" % (disk, r.get("data", {}).get("target", "")) if r["ok"] else r.get("error", "重建失败")}

        if t == "wipe":
            r = _ps_json(r"""
$bf = Join-Path $env:SystemRoot 'System32\diskpart.exe'
$script = "select disk %d`r`nclean all`r`n"
$tmp = [System.IO.Path]::GetTempFileName()
Set-Content -Path $tmp -Value $script -Encoding Ascii
$o = & $bf /s $tmp 2>&1 | Out-String
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
if ($LASTEXITCODE -eq 0) { @{ done = $true; output = $o } } else { @{ ok = $false; error = $o } }
""" % disk, timeout=86400)
            return {"ok": r["ok"], "message": "磁盘已擦除(整盘清零)" if r["ok"] else r.get("error", "擦除失败")}

        if t == "set_letter":
            if not re.match(r"^[A-Z]$", letter or ""):
                return {"ok": False, "message": "盘符必须是 A-Z 之间的单个字母"}
            r = _ps_json(r"""
Set-Partition -DiskNumber %d -PartitionNumber %d -NewDriveLetter '%s' -ErrorAction Stop
@{ done = $true }
""" % (disk, part, letter))
            return {"ok": r["ok"], "message": "盘符已改为 %s:" % letter if r["ok"] else r.get("error", "修改盘符失败")}

        if t == "set_label":
            identity = _ps_json(r"""
$p = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
if ([string]$p.DriveLetter -ne '%s') { @{ ok = $false; error = '分区盘符已变化，请刷新后重试' } }
else { @{ drive = [string]$p.DriveLetter } }
""" % (int(disk), int(part), letter))
            if not identity["ok"]:
                return {"ok": False, "message": identity.get("error", "无法确认卷标操作目标")}
            r = _ps_json(r"""
Set-Volume -DriveLetter '%s' -NewFileSystemLabel '%s' -ErrorAction Stop
@{ done = $true }
""" % (letter, label.replace("'", "''")))
            return {"ok": r["ok"], "message": "卷标已更新" if r["ok"] else r.get("error", "设置卷标失败")}

        if t == "extend":
            if size_mb:
                r = _ps_json(r"""
$p = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$supported = Get-PartitionSupportedSize -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$target = $p.Size + (%d * 1MB)
if ($target -gt $supported.SizeMax) { @{ ok = $false; error = '可扩展空间不足(请求容量超过相邻未分配空间)' } }
else { $resized = Resize-Partition -DiskNumber %d -PartitionNumber %d -Size $target -ErrorAction Stop; @{ size = [int64]$resized.Size } }
""" % (disk, part, disk, part, int(size_mb), disk, part))
            else:
                r = _ps_json(r"""
 $target = Get-PartitionSupportedSize -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$resized = Resize-Partition -DiskNumber %d -PartitionNumber %d -Size $target.SizeMax -ErrorAction Stop
@{ size = [int64]$resized.Size }
""" % (disk, part, disk, part))
            return {"ok": r["ok"], "message": "分区已扩展" if r["ok"] else r.get("error", "扩展失败")}

        if t == "shrink":
            if not size_mb or int(size_mb) <= 0:
                return {"ok": False, "message": "缩容需要指定大小(MB)"}
            r = _ps_json(r"""
$p = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$supported = Get-PartitionSupportedSize -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$target = $p.Size - (%d * 1MB)
if ($target -lt $supported.SizeMin) { @{ ok = $false; error = ('可缩容空间不足(最小 ' + [math]::Round($supported.SizeMin/1MB) + ' MB)') } }
else { $resized = Resize-Partition -DiskNumber %d -PartitionNumber %d -Size $target -ErrorAction Stop; @{ size = [int64]$resized.Size } }
""" % (disk, part, disk, part, int(size_mb), disk, part))
            return {"ok": r["ok"], "message": "分区已缩容 %s MB" % size_mb if r["ok"] else r.get("error", "缩容失败")}

        if t == "scan":
            fix = bool(op.get("fix"))
            mode = "OfflineScanAndFix" if fix else "Scan"
            r = _ps_json(r"""
Repair-Volume -DriveLetter '%s' -%s -ErrorAction Stop | Select-Object DriveLetter, OperationStatus, StatusDescription
""" % (letter, mode), timeout=3600)
            return {"ok": r["ok"], "message": "卷检查完成" if r["ok"] else r.get("error", "检查失败")}

        if t == "defrag":
            # /O chooses TRIM for SSDs and defragmentation for HDDs; /U emits
            # real progress instead of hiding a long Optimize-Volume call.
            exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "defrag.exe")
            try:
                output = _migration_run(
                    [exe, letter + ":", "/O", "/U", "/V"], timeout=7200,
                    progress=(lambda pct: progress(pct, "Windows 正在优化 %s:" % letter)) if progress else None,
                    heartbeat=(lambda: progress(None, "Windows 正在优化 %s:，请等待" % letter)) if progress else None,
                )
                return {"ok": True, "message": "Windows 已完成 %s: 的介质适配优化" % letter,
                        "detail": output[-800:]}
            except Exception as exc:
                return {"ok": False, "message": "磁盘优化失败: %s" % exc}

        if t == "benchmark":
            r = _ps_json(r"""
$root = '%s:\'
$path = Join-Path $root ('__lxultra_bench_' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    $data = New-Object byte[] (64MB)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($data) } finally { $rng.Dispose() }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    [System.IO.File]::WriteAllBytes($path, $data)
    $sw.Stop()
    $write = [math]::Round(64 / [math]::Max($sw.Elapsed.TotalSeconds, 0.001), 1)
    $sw.Restart()
    $null = [System.IO.File]::ReadAllBytes($path)
    $sw.Stop()
    $read = [math]::Round(64 / [math]::Max($sw.Elapsed.TotalSeconds, 0.001), 1)
    @{ writeMBs = $write; readMBs = $read }
} finally {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
}
""" % letter, timeout=300)
            if r["ok"] and isinstance(r.get("data"), dict):
                d = r["data"]
                return {"ok": True, "message": "写入 %s MB/s · 读取 %s MB/s(64MB 顺序)" % (d.get("writeMBs"), d.get("readMBs"))}
            return {"ok": r["ok"], "message": r.get("error", "测速失败")}

        if t == "clone_disk":
            src = int(op.get("source", -1))
            snapshot = _ps_json(_PS_DISKS, timeout=120)
            if not snapshot.get("ok"):
                return {"ok": False, "message": "读取克隆磁盘信息失败: " + snapshot.get("error", "")}
            disk_items = snapshot.get("data") or []
            if isinstance(disk_items, dict): disk_items = [disk_items]
            by_number = {int(x.get("number", -1)): x for x in disk_items}
            src_disk, dst_disk = by_number.get(src), by_number.get(int(disk))
            if not src_disk or not dst_disk:
                return {"ok": False, "message": "源磁盘或目标磁盘已不存在，请刷新后重试"}
            def eligible(d):
                return [p for p in sorted(d.get("partitions", []), key=lambda x: x.get("number", 0))
                        if p.get("letter") and p.get("fs") and not any(p.get(k) for k in
                        ("isBoot", "isSystem", "isEFI", "isMSR", "isRecovery", "active"))]
            sources, targets = eligible(src_disk), eligible(dst_disk)
            if not sources:
                return {"ok": False, "message": "源磁盘没有可镜像的带盘符数据分区"}
            if len(sources) != len(targets):
                return {"ok": False, "message": "源盘和目标盘的数据分区数量不同，已拒绝镜像"}
            pairs = list(zip(sources, targets))
            for i, (sp, dp) in enumerate(pairs):
                for label_name, disk_number, part_number in (("源分区", src, int(sp["number"])),
                                                               ("目标分区", int(disk), int(dp["number"]))):
                    guard = _protected_partition(disk_number, part_number, sys_disks)
                    if guard:
                        return {"ok": False, "message": "第 %d 组%s: %s" % (i + 1, label_name, guard)}
                pre = _ps_json(r"""
$sd = Get-Disk -Number %d -ErrorAction Stop
$dd = Get-Disk -Number %d -ErrorAction Stop
$sp = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$dp = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
if ($sd.IsOffline -or $dd.IsOffline -or $dd.IsReadOnly) { @{ ok = $false; error = '源磁盘或目标磁盘离线，或目标磁盘只读' } }
elseif ([string]$sp.DriveLetter -ne '%s' -or [string]$dp.DriveLetter -ne '%s') { @{ ok = $false; error = '分区盘符已变化，请刷新后重试' } }
else {
  $sv = Get-Volume -Partition $sp -ErrorAction Stop
  $dv = Get-Volume -Partition $dp -ErrorAction Stop
  $used = [int64]($sv.Size - $sv.SizeRemaining)
  if ([string]$sv.FileSystem -notin @('NTFS','exFAT','FAT32') -or [string]$dv.FileSystem -notin @('NTFS','exFAT','FAT32')) { @{ ok = $false; error = '源卷或目标卷的文件系统不支持文件镜像' } }
  elseif ($dv.Size -lt $used) { @{ ok = $false; error = '目标分区容量不足以容纳源盘已用数据' } }
  else { @{ ready = $true; sourceUsed = $used; targetSize = [int64]$dv.Size } }
}
""" % (src, int(disk), src, int(sp["number"]), int(disk), int(dp["number"]), str(sp["letter"]).upper(), str(dp["letter"]).upper()))
                if not pre["ok"]:
                    return {"ok": False, "message": "第 %d 组分区预检查失败: %s" % (i + 1, pre.get("error", ""))}
            for i, (sp, dp) in enumerate(pairs):
                copied = _exec_op({"type": "clone_partition", "source_disk": src,
                    "source_part": int(sp["number"]), "source_letter": sp["letter"],
                    "disk": int(disk), "part": int(dp["number"]), "dest_letter": dp["letter"]}, sys_disks)
                if not copied.get("ok"):
                    return {"ok": False, "message": "第 %d/%d 组数据卷镜像失败: %s" % (i + 1, len(pairs), copied.get("message", "未知错误"))}
            return {"ok": True, "message": "已完成 %d 组数据卷文件镜像；分区表和启动能力未复制" % len(pairs)}

        if t == "clone_partition":
            source_disk = int(op["source_disk"]); source_part = int(op["source_part"])
            source_letter = str(op["source_letter"]).upper()
            dest_letter = str(op["dest_letter"]).upper()
            if source_letter == dest_letter:
                return {"ok": False, "message": "源卷和目标卷盘符相同，拒绝镜像"}
            r = _ps_json(r"""
$sp = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$dp = Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
$sd = Get-Disk -Number %d -ErrorAction Stop
$dd = Get-Disk -Number %d -ErrorAction Stop
if ($sd.IsOffline -or $dd.IsOffline -or $dd.IsReadOnly) { throw '源磁盘或目标磁盘离线，或目标磁盘只读' }
if ([string]$sp.DriveLetter -ne '%s' -or [string]$dp.DriveLetter -ne '%s') { throw '分区盘符已变化，请刷新后重试' }
if ($sp.IsBoot -or $sp.IsSystem -or $sp.IsActive -or $dp.IsBoot -or $dp.IsSystem -or $dp.IsActive) { throw '系统、启动或活动分区不可镜像' }
$sv = Get-Volume -Partition $sp -ErrorAction Stop
$dv = Get-Volume -Partition $dp -ErrorAction Stop
$used = [int64]($sv.Size - $sv.SizeRemaining)
if ($dv.Size -lt $used) { throw '目标分区容量不足以容纳源分区已用数据' }
if ([string]$sv.FileSystem -notin @('NTFS','exFAT','FAT32') -or [string]$dv.FileSystem -notin @('NTFS','exFAT','FAT32')) { throw '源卷或目标卷的文件系统不支持文件镜像' }
$from = '%s:\'
$to = '%s:\'
$log = & robocopy.exe $from $to /MIR /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NP /NFL /NDL /NJH /NJS /XD 'System Volume Information' '$RECYCLE.BIN' /XF 'pagefile.sys' 'hiberfil.sys' 'swapfile.sys' 2>&1 | Out-String
$rc = $LASTEXITCODE
if ($rc -ge 8) { throw ('文件镜像失败，Robocopy 退出码 ' + $rc + '. ' + $log) }
@{ done = $true; code = $rc; source = $from; destination = $to; sourceUsed = $used }
""" % (source_disk, source_part, int(disk), int(part), source_disk, int(disk), source_letter, dest_letter, source_letter, dest_letter), timeout=7200)
            if not r["ok"]:
                return {"ok": False, "message": r.get("error", "文件镜像失败")}
            return {"ok": True, "message": "已镜像 %s → %s；目标多余文件已清理，分区结构未复制" % (source_letter + ":", dest_letter + ":")}

        if t == "vhd_mount":
            path = str(op.get("path") or "")
            r = _ps_json(r"""
Mount-VHD -Path '%s' -ErrorAction Stop
@{ done = $true }
""" % path.replace("'", "''"))
            return {"ok": r["ok"], "message": "VHD 已挂载" if r["ok"] else r.get("error", "挂载失败(需要 Hyper-V 模块)")}

        if t == "vhd_dismount":
            path = str(op.get("path") or "")
            r = _ps_json(r"""
Dismount-VHD -Path '%s' -ErrorAction Stop
@{ done = $true }
""" % path.replace("'", "''"))
            return {"ok": r["ok"], "message": "VHD 已卸载" if r["ok"] else r.get("error", "卸载失败")}

        return {"ok": False, "message": "未知操作类型: %s" % t}

    # ---------- 挂到 API 的公开方法 ----------
    def partition_disks(self):
        with _plock:
            r = _ps_json(_PS_DISKS, timeout=120)
        if not r["ok"]:
            return {"ok": False, "error": r.get("error", "读取磁盘信息失败")}
        disks = r["data"]
        if isinstance(disks, dict):
            disks = [disks]
        return {"ok": True, "disks": disks or [], "admin": _is_admin(), "wrapper": _WRAPPER_TAG}

    def partition_migration_plan(self, target_disk, target_part, scratch_letter):
        """Return exact target scope and a short-lived token; never modifies a disk."""
        import secrets
        import time

        if _as_int(target_disk, 0) is None or _as_int(target_part, 1) is None:
            return {"ok": False, "error": "目标磁盘和 Windows 分区编号必须有效"}
        if not isinstance(scratch_letter, str) or not re.fullmatch(r"[A-Za-z]", scratch_letter):
            return {"ok": False, "error": "请选择一个带盘符的独立暂存卷"}
        try:
            disks = _fresh_migration_disks()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        sources = [d for d in disks for p in d.get("partitions", [])
                   if p.get("isBoot") and str(p.get("letter", "")).upper() == "C"]
        if len(sources) != 1:
            return {"ok": False, "error": "无法唯一锁定当前 Windows C: 所在磁盘"}
        planned = _migration_plan_from_snapshot(
            disks, sources[0]["number"], int(target_disk), int(target_part),
            scratch_letter)
        if not planned["ok"]:
            return planned
        token = secrets.token_urlsafe(24)
        planned["plan"]["token"] = token
        try:
            scanner = _WindowsMigrationAdapter(planned["plan"], _fresh_migration_disks, _ps_json)
            cloud_scan = scanner._find_unavailable_cloud_files(planned["plan"]["source"]["letter"])
            reserved_scan = scanner._find_reserved_name_paths(
                planned["plan"]["source"]["letter"] + ":\\")
        except Exception as exc:
            return {"ok": False, "error": "迁移文件预检失败: %s" % exc}
        if cloud_scan.get("errors"):
            samples = cloud_scan["errors"][:3]
            return {"ok": False, "error": "无法完整检查 OneDrive 同步目录，请先确认目录可访问后重试。样例：%s" %
                    "；".join(str(item) for item in samples)}
        if reserved_scan.get("errors"):
            samples = reserved_scan["errors"][:3]
            return {"ok": False, "error": "无法完整检查用户目录中的 Windows 保留名，无法安全预览迁移。样例：%s" %
                    "；".join(str(item) for item in samples)}
        reserved_names = reserved_scan.get("items") or []
        planned["plan"]["reserved_names"] = reserved_names
        cloud_summary = {
            "count": cloud_scan.get("count", 0),
            "total_bytes": cloud_scan.get("total_bytes", 0),
            "manual_count": len(cloud_scan.get("directories") or []),
            "samples": (cloud_scan.get("samples") or [])[:5],
        }
        with _plock:
            now = time.time()
            for key, item in list(_migration_plans.items()):
                if now - item["created"] > 1800:
                    del _migration_plans[key]
            _migration_plans[token] = {"created": now, "plan": planned["plan"],
                                       "cloud_files": cloud_summary,
                                       "reserved_names": reserved_names}
        return {"ok": True, "token": token, "plan": planned["plan"],
                "cloud_files": cloud_summary, "reserved_names": reserved_names}

    def partition_health(self, disk):
        """按需查询单盘健康(SMART 温度/通电时长/磨损)。列表页不调用,避免拖慢加载。"""
        try:
            disk = int(disk)
        except Exception:
            return {"ok": False, "error": "无效磁盘编号"}
        r = _ps_json(r"""
$d = Get-Disk -Number %d -ErrorAction Stop
$temp = $null; $hours = $null; $wear = $null; $reads = $null; $writes = $null
try {
    $pd = Get-PhysicalDisk -DeviceNumber %d -ErrorAction Stop
    $rel = Get-StorageReliabilityCounter -PhysicalDisk $pd -ErrorAction Stop
    if ($rel) { $temp = $rel.Temperature; $hours = $rel.PowerOnHours; $wear = $rel.Wear;
                $reads = $rel.ReadErrorsTotal; $writes = $rel.WriteErrorsTotal }
} catch {}
@{ number = [int]$d.Number; health = [string]$d.HealthStatus; operational = ([string]($d.OperationalStatus -join ','))
   temperature = $temp; powerOnHours = $hours; wear = $wear; readErrors = $reads; writeErrors = $writes }
""" % (disk, disk))
        if not r["ok"]:
            return {"ok": False, "error": r.get("error", "读取健康信息失败")}
        d = r["data"]
        if isinstance(d, dict):
            return {"ok": True, "health": d}
        return {"ok": False, "error": "健康信息为空"}

    def partition_job_submit(self, ops):
        """提交操作列表,立即返回 job_id;后台线程顺序执行。"""
        if not isinstance(ops, list) or not ops:
            return {"ok": False, "error": "操作列表为空"}
        problems = []
        for i, op in enumerate(ops):
            err = _validate_op(op)
            if err:
                problems.append("第 %d 项:%s" % (i + 1, err))
        if problems:
            return {"ok": False, "error": "操作校验失败 —— " + ";".join(problems)}
        migration = len(ops) == 1 and ops[0].get("type") == "migrate_system"
        if any(o.get("type") == "migrate_system" for o in ops) and not migration:
            return {"ok": False, "error": "系统迁移必须单独提交，不能和其他操作混合"}
        with _plock:
            if any(j.get("state") == "running" and j.get("migration") for j in _jobs.values()):
                return {"ok": False, "error": "系统迁移正在运行，暂不能提交其他磁盘任务"}
        admin = _is_admin()
        destructive = {"format", "delete_partition", "delete_all", "quick_partition", "convert",
                       "rebuild_mbr", "wipe", "set_letter", "set_label", "extend", "shrink",
                       "clone_disk", "migrate_system", "vhd_mount", "vhd_dismount"}
        needs_admin = any(o.get("type") in destructive or o.get("type") == "defrag"
                          or (o.get("type") == "scan" and o.get("fix") is True) for o in ops)
        if needs_admin and not admin:
            return {"ok": False, "error": "需要管理员权限:请以管理员身份运行 LxUltra 电脑管家"}
        requires_disk_guard = any(op.get("type") not in _DISKLESS_OPS for op in ops)
        sys_disks = _ps_system_disks() if requires_disk_guard else None
        if requires_disk_guard and not sys_disks:
            return {"ok": False, "error": "无法定位系统或启动磁盘，已拒绝磁盘操作"}
        migration_plan = None
        if migration:
            import time
            op = ops[0]
            with _plock:
                if any(j.get("state") == "running" for j in _jobs.values()):
                    return {"ok": False, "error": "已有磁盘任务运行，无法同时迁移系统"}
                entry = _migration_plans.pop(op["token"], None)
            if not entry or time.time() - entry["created"] > 1800:
                return {"ok": False, "error": "迁移预览已过期，请重新选择目标"}
            cloud_files = entry.get("cloud_files") or {"count": 0, "manual_count": 0}
            if cloud_files.get("manual_count"):
                return {"ok": False, "error": "目标源卷含在线 OneDrive 文件夹，请先在资源管理器中将文件夹设为“始终保留在此设备”后重新预览"}
            allow_cloud_download = op.get("download_cloud_files") is True
            if cloud_files.get("count") and not allow_cloud_download:
                return {"ok": False, "error": "系统迁移包含 OneDrive 在线文件，必须在确认框单独勾选下载后才能提交"}
            reserved_names = entry.get("reserved_names") or []
            skip_reserved_names = op.get("skip_reserved_names") is True
            consent_error = _migration_reserved_name_consent_error(entry, op)
            if consent_error:
                return {"ok": False, "error": consent_error}
            migration_plan = dict(entry["plan"])
            migration_plan["download_cloud_files"] = allow_cloud_download
            migration_plan["reserved_names"] = reserved_names
            target = migration_plan["target"]
            if target["disk"] != int(op["disk"]) or target["part"] != int(op["part"]):
                return {"ok": False, "error": "提交目标与预览不一致"}
            try:
                fresh = _fresh_migration_disks()
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            ok, reason = _migration_verify_identity(migration_plan, fresh)
            if not ok:
                return {"ok": False, "error": reason}
        with _plock:
            if any(j.get("state") == "running" and (migration or j.get("migration"))
                   for j in _jobs.values()):
                return {"ok": False, "error": "已有磁盘任务运行，请等待完成后再提交系统迁移"}
            _job_seq[0] += 1
            job_id = "pj%d" % _job_seq[0]
            job = {"state": "running", "results": [], "current": 0, "total": len(ops),
                   "progress": "", "migration": migration, "cancelable": migration,
                   "cancel_requested": False, "detail": "", "estimated": False}
            _jobs[job_id] = job

        def _worker():
            try:
                for i, op in enumerate(ops):
                    job["current"] = i + 1
                    job["percent"] = None
                    job["detail"] = ""
                    job["progress"] = "正在执行第 %d/%d 项:%s" % (i + 1, len(ops), op.get("type", "?"))
                    if op.get("type") == "migrate_system":
                        def report(phase, percent, detail=None, estimated=False):
                            with _plock:
                                job["phase"] = phase
                                job["percent"] = percent
                                job["detail"] = detail or ""
                                job["estimated"] = bool(estimated)
                                job["cancelable"] = bool(
                                    not job.get("cancel_requested") and
                                    phase not in ("format_targets", "apply", "boot", "inspect", "complete"))
                                job["progress"] = "系统迁移: %s (%d%% 阶段进度)" % (phase, percent)
                        try:
                            fresh = _fresh_migration_disks()
                            ok, reason = _migration_verify_identity(migration_plan, fresh)
                            if not ok:
                                res = {"ok": False, "message": reason}
                            else:
                                adapter = _WindowsMigrationAdapter(
                                    migration_plan, _fresh_migration_disks, _ps_json,
                                    progress=report,
                                    cancelled=lambda: bool(job.get("cancel_requested")))
                                res = _migration_execute_stages(migration_plan, adapter, report)
                        except Exception as exc:
                            res = {"ok": False, "message": "系统迁移实时校验异常: %s" % exc}
                        res = _migration_result_with_target(res, migration_plan)
                        job["results"].append(res)
                        if res.get("cancelled"):
                            job["state"] = "cancelled"
                            job["cancelable"] = False
                            job["cancel_requested"] = False
                            job["detail"] = "暂存映像和快照已清理，目标分区未改动"
                            job["progress"] = res["message"]
                        elif not res["ok"]:
                            job["state"] = "error"
                            job["cancelable"] = False
                            job["progress"] = res["message"]
                        continue
                    current_sys_disks = (_ps_system_disks()
                                         if op.get("type") not in _DISKLESS_OPS else None)
                    guard = _op_guard(op, current_sys_disks)
                    if guard:
                        job["results"].append({"ok": False, "message": guard, "type": op.get("type")})
                        continue
                    try:
                        def report_op(percent, detail):
                            with _plock:
                                if percent is not None:
                                    job["percent"] = min(99, percent)
                                job["detail"] = detail
                                job["progress"] = detail + (
                                    " · %d%%" % percent if percent is not None else "")
                        res = _exec_op(op, current_sys_disks,
                                       progress=report_op if op.get("type") == "defrag" else None)
                    except Exception as e:
                        res = {"ok": False, "message": "执行异常: %r" % e}
                    res["type"] = op.get("type")
                    res["op"] = op
                    job["results"].append(res)
                if job["state"] == "running":
                    job["state"] = "done"
                    job["cancelable"] = False
                    job["progress"] = "系统文件和引导已写入；独立启动验证仍待完成" if migration else "全部完成"
                    if migration:
                        job["phase"] = "complete"
                        job["percent"] = 100
            except Exception as e:
                job["state"] = "error"
                job["progress"] = "任务异常: %r" % e
                _wrap_log("job error: %r" % e)
            finally:
                job["current"] = len(ops)

        threading.Thread(target=_worker, daemon=True, name="lxultra-partition-job").start()
        return {"ok": True, "job_id": job_id}

    def partition_job_cancel(self, job_id):
        with _plock:
            job = _jobs.get(str(job_id))
            if not job:
                return {"ok": False, "error": "任务不存在"}
            result = _migration_request_cancel(job)
            if result.get("ok"):
                job["progress"] = "正在安全取消系统迁移…"
                job["detail"] = result["message"]
            return result

    def partition_job_status(self, job_id):
        with _plock:
            job = _jobs.get(str(job_id))
            if not job:
                return {"ok": False, "error": "任务不存在"}
            return {"ok": True, "state": job["state"], "current": job["current"],
                    "total": job["total"], "progress": job["progress"], "results": job["results"],
                    "phase": job.get("phase"), "percent": job.get("percent"),
                    "migration": job.get("migration", False),
                    "cancelable": job.get("cancelable", False),
                    "cancel_requested": job.get("cancel_requested", False),
                    "detail": job.get("detail", ""),
                    "estimated": job.get("estimated", False)}

    def partition_admin(self):
        return {"ok": True, "admin": _is_admin()}

    def partition_vhd_list(self):
        r = _ps_json(r"""
$items = @()
foreach ($d in (Get-Disk -ErrorAction Stop)) {
    try {
        $v = Get-VHD -DiskNumber ([uint32]$d.Number) -ErrorAction Stop
        $items += [ordered]@{ Path = [string]$v.Path; Attached = [bool]$v.Attached; SizeGB = [math]::Round($v.Size / 1GB, 1) }
    } catch { }
}
@{ vhds = $items }
""")
        if not r["ok"]:
            return {"ok": False, "error": r.get("error", "读取 VHD 列表失败"), "vhds": []}
        data = r.get("data")
        if isinstance(data, dict):
            data = data.get("vhds", [])
        elif data is not None and not isinstance(data, list):
            data = [data]
        return {"ok": True, "vhds": data or []}

    methods = {
        "driver_updates_scan": driver_updates_scan,
        "driver_updates_status": driver_updates_status,
        "driver_updates_install": driver_updates_install,
        "fm_software_list": fm_software_list,
        "fm_software_uninstall": fm_software_uninstall,
        "fm_software_move": fm_software_move,
        "fm_software_icons": fm_software_icons,
        "process_icons": process_icons,
        "process_background_windows": process_background_windows,
        "partition_disks": partition_disks,
        "partition_migration_plan": partition_migration_plan,
        "partition_health": partition_health,
        "partition_job_submit": partition_job_submit,
        "partition_job_cancel": partition_job_cancel,
        "partition_job_status": partition_job_status,
        "partition_admin": partition_admin,
        "partition_vhd_list": partition_vhd_list,
    }
    for name, fn in methods.items():
        setattr(API, name, fn)
    if original_metrics:
        API.metrics = metrics
    _wrap_log("partition methods installed on API: %s" % ", ".join(methods))


# ============================================================
# 加载并执行原始模块
# ============================================================
_API_PATCHED = [False]
_WRAPPED_FUNCS = set()

class _BootstrapNS(dict):
    """exec 命名空间:拦截 'API' 类定义,注入分区方法后原样放行。"""
    def __setitem__(self, key, value):
        if key == "API" and isinstance(value, type) and not _API_PATCHED[0]:
            _API_PATCHED[0] = True
            try:
                _install_partition_methods(value)
            except Exception as e:
                _wrap_log("install partition methods failed: %r" % e)
            try:
                _allow_explicit_tray_exit(value)
                _allow_verified_update_exit(value)
            except Exception as e:
                _wrap_log("install tray exit guard failed: %r" % e)
            try:
                _instrument_webview(self.get("webview"))
            except Exception as e:
                _wrap_log("instrument webview failed: %r" % e)
        if key in ("main", "_run_window") and callable(value) and key not in _WRAPPED_FUNCS:
            _WRAPPED_FUNCS.add(key)
            value = _trace_wrap(key, value)
        dict.__setitem__(self, key, value)


def _trace_wrap(name, fn):
    def wrapper(*a, **k):
        _wrap_log("%s() entered, argv=%r" % (name, sys.argv))
        try:
            r = fn(*a, **k)
            _wrap_log("%s() returned %r" % (name, r))
            return r
        except BaseException as e:
            _wrap_log("%s() EXC %r" % (name, e))
            raise
    return wrapper


def _allow_explicit_tray_exit(api_cls):
    """Let the tray menu's explicit Exit action close the window normally."""
    original = getattr(api_cls, "_quit_from_tray", None)
    if not callable(original) or getattr(original, "_lxultra_exit_wrapped", False):
        return

    def quit_from_tray(self, *args, **kwargs):
        self._lxultra_exit_requested = True
        return original(self, *args, **kwargs)

    quit_from_tray._lxultra_exit_wrapped = True
    api_cls._quit_from_tray = quit_from_tray


def _allow_verified_update_exit(api_cls):
    """Release the app only after the original verified installer launcher succeeds."""
    original = getattr(api_cls, "_download_and_install_update", None)
    if not callable(original) or getattr(original, "_lxultra_handoff_wrapped", False):
        return

    def download_and_install_update(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        with self._update_lock:
            installing = self._update_state.get("state") == "installing"
        if installing:
            previous_exit = getattr(self, "_lxultra_exit_requested", False)
            try:
                # This goes through the explicit tray-exit path, bypassing close-to-tray.
                self._quit_from_tray()
            except Exception as error:
                self._lxultra_exit_requested = previous_exit
                with self._update_lock:
                    self._update_state = dict(self._update_state, state="failed",
                        message="安装程序已启动，但应用未能退出。请从托盘菜单退出 LxUltra 后继续安装。")
                _wrap_log("verified update exit failed: %r" % (error,))
        return result

    download_and_install_update._lxultra_handoff_wrapped = True
    api_cls._download_and_install_update = download_and_install_update


def _instrument_webview(wv):
    """诊断插桩:记录 create_window / start 的调用与返回,便于定位窗口未启动的问题。"""
    if not wv or getattr(wv, "_lxultra_instrumented", False):
        return
    _wrap_log("webview module: %r" % (getattr(wv, "__file__", "?"),))
    oc = wv.create_window
    os_ = wv.start

    def create_window(*a, **k):
        _wrap_log("create_window(a=%r kw=%r)" % (a[:3], list(k.keys())))
        try:
            w = oc(*a, **k)
            try:
                js_api = k.get("js_api") or (a[2] if len(a) > 2 else None)
                if js_api is not None:
                    global _SINGLE_INSTANCE_API
                    _SINGLE_INSTANCE_API = js_api
                closing = getattr(getattr(w, "events", None), "closing", None)
                if js_api is not None and closing is not None:
                    def minimize_on_close(*_event_args, _api=js_api):
                        if getattr(_api, "_lxultra_exit_requested", False):
                            return None
                        try:
                            result = _api.minimize_to_tray()
                        except Exception as e:
                            _wrap_log("close-to-tray failed: %r" % e)
                            return None
                        if isinstance(result, dict) and result.get("ok"):
                            _wrap_log("window close redirected to system tray")
                            return False
                        _wrap_log("close-to-tray unavailable; allowing window to close: %r" % (result,))
                        return None

                    closing += minimize_on_close
                    _wrap_log("registered window close-to-tray handler")
            except Exception as e:
                _wrap_log("register close-to-tray handler failed: %r" % e)
            _wrap_log("create_window -> %r" % (w,))
            return w
        except BaseException as e:
            _wrap_log("create_window EXC %r" % (e,))
            raise

    def start(*a, **k):
        _wrap_log("webview.start(a=%r kw=%r)" % (a[:3], list(k.keys())))
        try:
            r = os_(*a, **k)
            _wrap_log("webview.start RETURNED r=%r" % (r,))
            return r
        except BaseException as e:
            _wrap_log("webview.start EXC %r" % (e,))
            raise

    wv.create_window = create_window
    wv.start = start
    wv._lxultra_instrumented = True


def _bootstrap():
    _wrap_log("bootstrap start, tag=%s" % _WRAPPER_TAG)
    if not _acquire_single_instance():
        _wrap_log("existing LxUltra instance activated; duplicate launch exiting")
        return
    try:
        raw = base64.b64decode(_ORIG_B64)
        orig = marshal.loads(raw)
    except Exception as e:
        _wrap_log("FATAL: cannot load original module: %r" % e)
        raise
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = meipass
    elif getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        try:
            base = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            base = os.getcwd()
    ns = _BootstrapNS()
    # 原模块尾部有 `if __name__ == '__main__':` 守卫,必须保持 __main__ 才能启动窗口
    ns["__name__"] = "__main__"
    ns["__file__"] = os.path.join(base, "app.py")
    ns["__doc__"] = None
    ns["__spec__"] = None
    _wrap_log("executing original module (ROOT=%s)" % ns["__file__"])
    exec(orig, ns)   # 模块尾部:worker 分发或 main(),保持原行为
    _wrap_log("original module returned")


try:
    _bootstrap()
except SystemExit as e:
    _wrap_log("SystemExit raised, code=%r" % (getattr(e, "code", None),))
    raise
except BaseException:
    import traceback
    _wrap_log("FATAL during bootstrap:\n" + traceback.format_exc())
    raise
