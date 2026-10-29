"""Windows UEFI/GPT system migration stages, embedded into the app wrapper at build time.

The only destructive stage is split_target().  All command arguments originate from
the server-side, short-lived migration plan, never from an unchecked UI command.
"""
import hashlib
import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time


WIMLIB_EXE_SHA256 = "34c0c4165591ad1f592837ed99d08273c58d6ed3fe0ed6360cf34e7b0739b353"
_WINDOWS_RESERVED_DEVICE_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM(?:[1-9]|[¹²³])|LPT(?:[1-9]|[¹²³]))(?:\..*)?$",
    re.IGNORECASE,
)
_DISM_DEFAULT_EXCLUSIONS = (
    r"\$ntfs.log", r"\hiberfil.sys", r"\pagefile.sys", r"\swapfile.sys",
    r"\System Volume Information", r"\RECYCLER", r"\Windows\CSC",
)


class _MigrationCancelled(Exception):
    pass


def _migration_capture_estimate(image_bytes, source_used):
    """Estimate capture progress from bytes written, but never claim it is complete."""
    try:
        image_bytes = max(0, int(image_bytes))
        source_used = int(source_used)
    except (TypeError, ValueError, OverflowError):
        return 15
    if source_used <= 0 or image_bytes <= 0:
        return 15
    fraction = min(1.0, image_bytes / float(source_used))
    return 15 + min(29, int(fraction * 30))


def _migration_is_reserved_device_name(name):
    """True for a Windows DOS device basename, including an optional extension."""
    if not isinstance(name, str):
        return False
    return bool(_WINDOWS_RESERVED_DEVICE_RE.fullmatch(name.rstrip(" .")))


def _migration_wimscript_config(relative_paths):
    """Build a DISM capture config that preserves defaults and excludes exact paths."""
    clean = []
    seen = set()
    for path in relative_paths or []:
        if not isinstance(path, str):
            raise ValueError("保留名排除路径格式无效")
        value = path.replace("/", "\\")
        if not value.startswith("\\") or any(ch in value for ch in "\r\n*?"):
            raise ValueError("保留名排除路径格式无效")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            clean.append(value)
    if not clean:
        return ""
    lines = ["[ExclusionList]"] + list(_DISM_DEFAULT_EXCLUSIONS) + clean
    return "\r\n".join(lines) + "\r\n"


def _migration_reserved_names_in_wim_listing(lines):
    found = []
    for raw_line in lines:
        path = raw_line.strip().replace("/", "\\")
        if path.startswith("\\") and _migration_is_reserved_device_name(path.rsplit("\\", 1)[-1]):
            found.append(path)
    return found


def _migration_scan_reserved_names(root, ps_json):
    """Find Windows device-name entries throughout source user profiles."""
    root = str(root or "")
    drive_root = re.fullmatch(r"[A-Za-z]:\\?", root)
    shadow_root = re.fullmatch(
        r"\\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy[0-9]+\\?", root,
        re.IGNORECASE,
    )
    if not drive_root and not shadow_root:
        raise RuntimeError("无法确认源卷路径，已停止保留名扫描")
    scan_root = (r"\\?" + "\\" + root.rstrip("\\") + "\\") if drive_root else root.rstrip("\\") + "\\"
    users_root = scan_root + "Users"
    if not os.path.isdir(users_root):
        raise RuntimeError("源卷用户目录不可访问，已停止保留名扫描")
    pending = [users_root]
    items = []
    errors = []
    deadline = time.monotonic() + 300
    while pending:
        if time.monotonic() > deadline:
            raise RuntimeError("用户目录保留名扫描超时，目标分区尚未修改")
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        name = entry.name.rstrip(" .")
                        if _migration_is_reserved_device_name(name):
                            is_dir = entry.is_dir(follow_symlinks=False)
                            size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
                            relative = "\\" + entry.path[len(scan_root):].lstrip("\\")
                            items.append({"relative": relative,
                                          "path": root[0:2] + relative if drive_root else entry.path,
                                          "directory": is_dir, "size": max(0, int(size))})
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            attrs = entry.stat(follow_symlinks=False).st_file_attributes
                            if not (attrs & 0x400):  # FILE_ATTRIBUTE_REPARSE_POINT
                                pending.append(entry.path)
                    except OSError as exc:
                        errors.append("%s: %s" % (entry.path, exc))
        except OSError as exc:
            errors.append("%s: %s" % (directory, exc))
    return {"items": items, "errors": errors}


def _migration_hydrate_placeholder(path):
    """Download one Cloud Files placeholder through the owning sync provider."""
    if os.name != "nt":
        raise RuntimeError("云文件下载仅支持 Windows Cloud Files")
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(path, 0x80000000, 0x7, None, 3, 0, None)  # GENERIC_READ, OPEN_EXISTING
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise RuntimeError("打开云文件失败，Windows 错误 %d" % ctypes.get_last_error())
    try:
        cfapi = ctypes.WinDLL("CldApi.dll", use_last_error=True)
        hydrate = cfapi.CfHydratePlaceholder
        hydrate.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.c_longlong,
                            ctypes.c_uint32, ctypes.c_void_p]
        hydrate.restype = ctypes.c_long
        result = int(hydrate(handle, 0, -1, 0, None))
        if result != 0:
            raise RuntimeError("Windows Cloud Files 返回 0x%08X" % (result & 0xFFFFFFFF))
    finally:
        close_handle(handle)


def _migration_run(argv, timeout=86400, progress=None, heartbeat=None, cancelled=None):
    creationflags = 0x08000000 if os.name == "nt" else 0
    if progress is None and heartbeat is None and cancelled is None:
        result = subprocess.run(argv, capture_output=True, timeout=timeout,
                                creationflags=creationflags)
        output = result.stdout.decode("utf-8", "replace")
        if result.returncode:
            tail = (result.stderr or result.stdout)[-3000:].decode("utf-8", "replace")
            raise RuntimeError("%s 退出 %d: %s" % (os.path.basename(argv[0]), result.returncode, tail))
        return output

    # DISM writes progress with carriage returns, so subprocess.run() keeps the
    # percentage hidden until capture/apply finishes. Stream its merged output.
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               bufsize=0, creationflags=creationflags)
    chunks = queue.Queue()

    def read_output():
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                chunks.put(chunk)
        except Exception as exc:
            chunks.put(exc)
        finally:
            chunks.put(None)

    reader = threading.Thread(target=read_output, name="lx-migration-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    output = []
    carry = ""
    last_percent = -1
    next_heartbeat = time.monotonic() + 1.0
    while True:
        if cancelled and cancelled():
            try:
                process.kill()
            except Exception:
                pass
            process.wait()
            raise _MigrationCancelled("用户取消")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            process.wait()
            raise subprocess.TimeoutExpired(argv, timeout, output="".join(output).encode("utf-8"))
        try:
            chunk = chunks.get(timeout=min(0.2, remaining))
        except queue.Empty:
            now = time.monotonic()
            if heartbeat and now >= next_heartbeat:
                try:
                    heartbeat()
                except Exception:
                    pass
                next_heartbeat = now + 1.0
            continue
        if chunk is None:
            break
        if isinstance(chunk, Exception):
            process.kill()
            process.wait()
            raise chunk
        text = chunk.decode("utf-8", "replace")
        output.append(text)
        scan = carry + text
        for match in re.finditer(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%", scan):
            percent = max(0, min(100, int(float(match.group(1)))))
            if percent > last_percent:
                last_percent = percent
                try:
                    progress(percent)
                except Exception:
                    pass
        carry = scan[-32:]
        now = time.monotonic()
        if heartbeat and now >= next_heartbeat:
            try:
                heartbeat()
            except Exception:
                pass
            next_heartbeat = now + 1.0

    try:
        return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise subprocess.TimeoutExpired(argv, timeout, output="".join(output).encode("utf-8"))
    stdout = "".join(output)
    if return_code:
        raise RuntimeError("%s 退出 %d: %s" % (os.path.basename(argv[0]), return_code, stdout[-3000:]))
    return stdout


class _WindowsMigrationAdapter:
    def __init__(self, plan, read_disks, ps_json, executable=None,
                 hydrate_file=None, progress=None, cancelled=None):
        self.plan = plan
        self.read_disks = read_disks
        self.ps_json = ps_json
        self.hydrate_file = hydrate_file or _migration_hydrate_placeholder
        self.progress = progress or (lambda phase, percent: None)
        self.cancelled = cancelled or (lambda: False)
        self.exe = executable or os.path.join(os.path.dirname(sys.executable),
                                               "migration_tools", "wimlib-imagex.exe")
        self.image = os.path.join(plan["scratch"]["letter"] + ":\\", "LxUltraMigration",
                                  "system-%s.wim" % plan["token"])
        self.dism_scratch = self.image + ".scratch"
        self.dism_config_file = os.path.join(self.dism_scratch, "WimScript.ini")
        self.owns_dism_scratch = False
        self.efi_letter = None
        self.efi_guid = None
        self.windows_letter = None
        self.reserved_name_paths = []
        self.shadow_id = None
        self.shadow_device = None
        self.destructive_started = False
        self.cloud_files = []

    def _wimlib(self, args, timeout=86400, progress=None, heartbeat=None):
        return _migration_run([self.exe] + args, timeout, progress=progress,
                              heartbeat=heartbeat, cancelled=self.cancelled)

    def _dism(self, args, timeout=86400, progress=None, heartbeat=None):
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "dism.exe")
        if not os.path.isdir(self.dism_scratch):
            raise RuntimeError("DISM 暂存目录不存在，拒绝开始系统映像操作")
        return _migration_run([exe, "/ScratchDir:" + self.dism_scratch] + args,
                              timeout, progress=progress, heartbeat=heartbeat,
                              cancelled=self.cancelled)

    def _create_snapshot(self, source_letter):
        script = r"""
$ErrorActionPreference='Stop'
$created=Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create -Arguments @{Volume='%s:\';Context='ClientAccessible'}
if ($created.ReturnValue -ne 0) { throw ('VSS snapshot creation failed: ' + $created.ReturnValue) }
$shadow=Get-CimInstance -ClassName Win32_ShadowCopy -Filter ("ID='{0}'" -f $created.ShadowID)
if (-not $shadow) { throw 'VSS snapshot instance was not found' }
@{id=[string]$shadow.ID;device=[string]$shadow.DeviceObject}
""" % source_letter
        result = self.ps_json(script, timeout=300)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "创建系统卷快照失败"))
        data = result.get("data") or {}
        shadow_id = str(data.get("id") or "")
        device = str(data.get("device") or "")
        if not re.fullmatch(r"\{[0-9a-fA-F-]{36}\}", shadow_id):
            raise RuntimeError("VSS 快照返回了无效标识")
        # Keep the ID as soon as it is validated. If the provider returns a
        # malformed device path, cleanup must still release the snapshot.
        self.shadow_id = shadow_id
        if not re.fullmatch(r"\\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy[0-9]+", device):
            raise RuntimeError("VSS 快照返回了无效设备路径")
        self.shadow_device = device

    def _release_snapshot(self):
        if not self.shadow_id:
            return
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "vssadmin.exe")
        _migration_run([exe, "delete", "shadows", "/Shadow=" + self.shadow_id, "/Quiet"], timeout=120)
        self.shadow_id = None
        self.shadow_device = None

    def _check_image(self):
        if not os.path.isfile(self.image) or os.path.getsize(self.image) < 1024:
            raise RuntimeError("系统映像不存在或为空")

    def _check_image_reserved_names(self):
        """Check the actual captured image before any target partition change."""
        listing_path = os.path.join(self.dism_scratch, "wim-paths.txt")
        if os.path.exists(listing_path):
            raise RuntimeError("映像文件名清单路径已存在，拒绝覆盖")
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            with open(listing_path, "wb") as output:
                result = subprocess.run(
                    [self.exe, "dir", self.image, "1"], stdout=output,
                    stderr=subprocess.PIPE, timeout=300, creationflags=creationflags,
                )
            if result.returncode:
                detail = result.stderr[-1000:].decode("utf-8", "replace")
                raise RuntimeError("无法检查系统映像内的文件名: " + detail)
            with open(listing_path, "r", encoding="utf-8", errors="replace") as listing:
                found = _migration_reserved_names_in_wim_listing(listing)
            if found:
                raise RuntimeError(
                    "系统映像仍包含 %d 个 Windows 保留名项目；目标分区尚未修改。请重新预览后重试。样例：%s" %
                    (len(found), "；".join(found[:5]))
                )
        finally:
            if os.path.isfile(listing_path):
                os.remove(listing_path)

    def _find_unavailable_cloud_files(self, source_letter):
        source_letter = str(source_letter).upper()
        if not re.fullmatch(r"[A-Z]", source_letter):
            raise RuntimeError("无法确认系统卷盘符，已停止迁移")
        script = r"""
$sourceRoot = [IO.Path]::GetFullPath('%s:\')
$candidates = New-Object 'System.Collections.Generic.List[string]'
foreach ($p in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
    if (-not [string]::IsNullOrWhiteSpace([string]$p)) { $candidates.Add([string]$p) }
}
$accountBase = 'HKCU:\Software\Microsoft\OneDrive\Accounts'
if (Test-Path -LiteralPath $accountBase) {
    foreach ($account in (Get-ChildItem -LiteralPath $accountBase -ErrorAction SilentlyContinue)) {
        try {
            $p = [string](Get-ItemProperty -LiteralPath $account.PSPath -Name UserFolder -ErrorAction Stop).UserFolder
            if (-not [string]::IsNullOrWhiteSpace($p)) { $candidates.Add($p) }
        } catch {}
    }
}
$roots = @($candidates | ForEach-Object {
    try {
        $full = [IO.Path]::GetFullPath($_).TrimEnd('\')
        if ($full.StartsWith($sourceRoot, [StringComparison]::OrdinalIgnoreCase)) { $full }
    } catch {}
} | Sort-Object -Unique)
$pending = New-Object 'System.Collections.Generic.Stack[string]'
$samples = New-Object 'System.Collections.Generic.List[string]'
$cloudFiles = New-Object 'System.Collections.Generic.List[object]'
$cloudDirectories = New-Object 'System.Collections.Generic.List[string]'
$errors = New-Object 'System.Collections.Generic.List[string]'
$count = 0
$totalBytes = [int64]0
# RecallOnDataAccess is missing from some .NET Framework FileAttributes enums,
# so use the documented Win32 bit values instead of enum names.
$cloudFlags = 0x1000 -bor 0x400000
foreach ($root in $roots) {
    if (-not [IO.Directory]::Exists($root)) { $errors.Add($root + ': 同步目录不可访问'); continue }
    $pending.Push($root)
    while ($pending.Count -gt 0) {
        $dir = $pending.Pop()
        try {
            foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($dir)) {
                try {
                    $attrs = [IO.File]::GetAttributes($entry)
                    if (([int]$attrs -band $cloudFlags) -ne 0) {
                        $count++
                        if ($samples.Count -lt 8) { $samples.Add($entry) }
                    }
                    $isDirectory = (([int]$attrs -band [int][IO.FileAttributes]::Directory) -ne 0)
                    if ($isDirectory -and (([int]$attrs -band $cloudFlags) -ne 0)) {
                        $cloudDirectories.Add($entry)
                    } elseif (-not $isDirectory -and (([int]$attrs -band $cloudFlags) -ne 0)) {
                        $size = [int64]([IO.FileInfo]::new($entry).Length)
                        $totalBytes += $size
                        $cloudFiles.Add(@{path=$entry;size=$size})
                    }
                    if ($isDirectory -and
                        ([int]$attrs -band [int][IO.FileAttributes]::ReparsePoint) -eq 0) {
                        $pending.Push($entry)
                    }
                } catch { $errors.Add($entry + ': ' + $_.Exception.Message) }
            }
        } catch { $errors.Add($dir + ': ' + $_.Exception.Message) }
    }
}
@{count=$count;total_bytes=$totalBytes;samples=@($samples.ToArray());
  files=@($cloudFiles.ToArray());directories=@($cloudDirectories.ToArray());errors=@($errors.ToArray())}
""" % source_letter
        result = self.ps_json(script, timeout=600)
        if not result.get("ok"):
            raise RuntimeError("无法检查源卷 OneDrive 同步目录: " + result.get("error", "未知错误"))
        data = result.get("data") or {}
        files = data.get("files") or []
        directories = data.get("directories") or []
        samples = data.get("samples") or []
        errors = data.get("errors") or []
        if isinstance(files, dict):
            files = [files]
        if isinstance(directories, str):
            directories = [directories]
        if isinstance(samples, str):
            samples = [samples]
        if isinstance(errors, str):
            errors = [errors]
        return {"count": int(data.get("count") or 0),
                "total_bytes": int(data.get("total_bytes") or 0),
                "files": files, "directories": directories,
                "samples": samples, "errors": errors}

    def _find_reserved_name_paths(self, root):
        return _migration_scan_reserved_names(root, self.ps_json)

    def step(self, phase, plan):
        if phase == "preflight":
            if not os.path.isfile(self.exe):
                raise RuntimeError("缺少 wimlib-imagex.exe，请安装迁移工具包")
            with open(self.exe, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() != WIMLIB_EXE_SHA256:
                    raise RuntimeError("wimlib 可执行文件校验失败")
            ok, reason = _migration_verify_identity(plan, self.read_disks())
            if not ok:
                raise RuntimeError(reason)
            cloud_scan = self._find_unavailable_cloud_files(plan["source"]["letter"])
            scan_errors = cloud_scan.get("errors") or []
            if scan_errors:
                samples = scan_errors[:3] if isinstance(scan_errors, list) else [scan_errors]
                raise RuntimeError("无法完整检查 OneDrive 同步目录（%d 处）。为避免生成缺失数据的系统映像，已停止迁移；请确认同步目录可访问后重试。样例：%s" %
                                   (len(scan_errors) if isinstance(scan_errors, list) else 1,
                                    "；".join(str(item) for item in samples)))
            cloud_count = cloud_scan.get("count") or 0
            if cloud_count:
                directories = cloud_scan.get("directories") or []
                if directories:
                    raise RuntimeError("源卷检测到 %d 个 OneDrive 在线文件或文件夹，其中在线文件夹不能由程序单独下载。请在资源管理器中对这些文件夹选择“始终保留在此设备”，等待下载完成后重新预览；目标盘尚未修改。样例：%s" %
                                       (cloud_count, "；".join(str(item) for item in directories[:5])))
                if not plan.get("download_cloud_files"):
                    samples = cloud_scan.get("samples") or []
                    raise RuntimeError("源卷检测到 %d 个尚未下载的 OneDrive 云端占位文件。请在迁移确认框勾选“下载云端文件并继续”后重试；下载可能使用网络流量和磁盘空间，目标盘尚未修改。样例：%s" %
                                       (cloud_count, "；".join(str(item) for item in samples[:5])))
                if len(cloud_scan.get("files") or []) != cloud_count:
                    raise RuntimeError("OneDrive 在线文件清单不完整，为避免漏拷已停止；目标盘尚未修改。请在资源管理器中将这些文件设为“始终保留在此设备”。")
                self.cloud_files = cloud_scan.get("files") or []
            else:
                self.cloud_files = []
            if os.path.exists(self.image):
                raise RuntimeError("暂存映像路径已存在，拒绝覆盖")
            os.makedirs(os.path.dirname(self.image), exist_ok=True)
            if os.path.exists(self.dism_scratch):
                raise RuntimeError("DISM 暂存目录已存在，拒绝覆盖")
            os.makedirs(self.dism_scratch)
            self.owns_dism_scratch = True
        elif phase == "snapshot":
            if not os.path.isdir(plan["source"]["letter"] + ":\\Windows"):
                raise RuntimeError("当前系统 Windows 目录不可访问")
            self._create_snapshot(plan["source"]["letter"])
            if "reserved_names" in plan:
                scan = self._find_reserved_name_paths(self.shadow_device + "\\")
                if scan.get("errors"):
                    samples = scan["errors"][:3]
                    raise RuntimeError("无法完整复核用户目录中的 Windows 保留名；目标分区尚未修改。请重新预览后重试。样例：%s" %
                                        "；".join(str(item) for item in samples))
                expected = sorted(str(item.get("relative") or "").casefold()
                                  for item in (plan.get("reserved_names") or []))
                actual_items = scan.get("items") or []
                actual = sorted(str(item.get("relative") or "").casefold()
                                for item in actual_items)
                if actual != expected:
                    raise RuntimeError("源卷中的 Windows 保留名项目与预览时不同，请重新预览后重试；目标分区尚未修改")
                self.reserved_name_paths = [str(item["relative"]) for item in actual_items]
                if self.reserved_name_paths:
                    config = _migration_wimscript_config(self.reserved_name_paths)
                    with open(self.dism_config_file, "w", encoding="utf-16") as f:
                        f.write(config)
        elif phase == "hydrate_cloud":
            self._hydrate_cloud_files()
        elif phase == "capture":
            if not self.shadow_device:
                raise RuntimeError("系统卷快照不存在，拒绝捕获")

            progress_state = {"percent": 15}

            def dism_progress(percent):
                value = 15 + round(max(0, min(100, percent)) * 30 / 100)
                progress_state["percent"] = max(progress_state["percent"], value)
                self.progress("capture", progress_state["percent"],
                              "DISM 报告映像阶段进度 %d%%" % percent, False)

            def image_heartbeat():
                try:
                    image_bytes = os.path.getsize(self.image)
                except OSError:
                    image_bytes = 0
                source_used = int(self.plan.get("source_used") or 0)
                value = _migration_capture_estimate(image_bytes, source_used)
                progress_state["percent"] = max(progress_state["percent"], value)
                detail = "映像已写入 %.1f GB" % (image_bytes / float(1024 ** 3))
                if source_used > 0:
                    detail += " · 源卷已用空间 %.1f GB（估算）" % (source_used / float(1024 ** 3))
                self.progress("capture", progress_state["percent"], detail, True)

            capture_args = ["/English", "/Capture-Image", "/ImageFile:" + self.image,
                            "/CaptureDir:" + self.shadow_device.rstrip("\\") + "\\",
                            "/Name:LxUltra Windows", "/Compress:fast", "/CheckIntegrity", "/Verify", "/EA"]
            if self.reserved_name_paths:
                capture_args.append("/ConfigFile:" + self.dism_config_file)
            self._dism(capture_args,
                       progress=dism_progress, heartbeat=image_heartbeat)
            self._check_image()
        elif phase == "verify_image":
            self._check_image()
            verify_state = {"percent": 45}
            def wim_progress(percent):
                verify_state["percent"] = max(
                    verify_state["percent"],
                    45 + round(max(0, min(100, percent)) * 5 / 100))
                self.progress("verify_image", verify_state["percent"],
                              "正在校验系统映像 · %d%%" % percent, False)
            self._wimlib(["verify", self.image], progress=wim_progress,
                         heartbeat=lambda: self.progress(
                             "verify_image", verify_state["percent"],
                             "正在读取并校验系统映像…", False))
            self.progress("verify_image", 50, "正在检查映像内的保留文件名…", True)
            self._check_image_reserved_names()
            self._release_snapshot()
        elif phase == "revalidate":
            ok, reason = _migration_verify_identity(plan, self.read_disks())
            if not ok:
                raise RuntimeError(reason)
        elif phase == "format_targets":
            self.destructive_started = True
            self._split_target()
        elif phase == "apply":
            self._check_image()
            dism_progress = lambda percent: self.progress(
                "apply", 60 + round(max(0, min(100, percent)) * 30 / 100),
                "DISM 写入进度 %d%%" % percent, False)
            self._dism(["/English", "/Apply-Image", "/ImageFile:" + self.image, "/Index:1",
                        "/ApplyDir:" + self.windows_letter + ":\\", "/CheckIntegrity", "/Verify", "/EA"],
                       progress=dism_progress)
        elif phase == "boot":
            self._write_boot()
        elif phase == "inspect":
            self._inspect()

    def _hydrate_cloud_files(self):
        files = self.cloud_files
        if not files:
            return
        total = len(files)
        for index, item in enumerate(files):
            if self.cancelled():
                raise _MigrationCancelled("用户取消")
            path = str(item.get("path") or "")
            self.progress("hydrate_cloud", 5 + round(index * 5 / total))
            try:
                self.hydrate_file(path)
            except Exception as exc:
                raise RuntimeError("下载 OneDrive 云端文件失败: %s (%s)" % (path, exc))
            self.progress("hydrate_cloud", 5 + round((index + 1) * 5 / total))
        verified = self._find_unavailable_cloud_files(self.plan["source"]["letter"])
        if verified.get("errors"):
            raise RuntimeError("OneDrive 文件下载后无法完成状态复核，为避免漏拷已停止；目标盘尚未修改。")
        if verified.get("count"):
            samples = verified.get("samples") or []
            raise RuntimeError("仍有 %d 个 OneDrive 云端文件未下载完成；目标盘尚未修改。样例：%s" %
                               (verified["count"], "；".join(str(item) for item in samples[:5])))

    def _split_target(self):
        p = self.plan["target"]
        # The script repeats identity checks immediately before Remove-Partition.
        # All other partitions are fingerprinted again in Python before this call.
        script = r"""
$d=Get-Disk -Number %d -ErrorAction Stop
$p=Get-Partition -DiskNumber %d -PartitionNumber %d -ErrorAction Stop
if ([string]$d.Guid -ne '%s' -or [string]$d.UniqueId -ne '%s' -or
    [string]$p.Guid -ne '%s' -or [int64]$p.Offset -ne %d -or [int64]$p.Size -ne %d -or
    [string]$p.DriveLetter -ne '%s' -or $d.IsBoot -or $d.IsSystem -or
    $d.IsOffline -or $d.IsReadOnly -or $p.IsBoot -or $p.IsSystem) {
    throw '目标分区身份或状态已变化，未执行删除'
}
$efiSize=[uint64]%d
$start=[uint64]%d
$oldSize=[uint64]%d
if ($oldSize -le $efiSize + 16GB) { throw '目标空间不足' }
$occupied=@(Get-Volume | Where-Object DriveLetter | ForEach-Object {[string]$_.DriveLetter})
$efiLetter=@('Z','Y','W','V','U','T','S','R','Q') | Where-Object {$_ -notin $occupied -and $_ -ne '%s'} | Select-Object -First 1
if (-not $efiLetter) { throw '没有可用的临时 EFI 盘符' }
Remove-Partition -DiskNumber %d -PartitionNumber %d -Confirm:$false -ErrorAction Stop
$efi=New-Partition -DiskNumber %d -Offset $start -Size $efiSize -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -ErrorAction Stop
$win=New-Partition -DiskNumber %d -Offset ($start+$efiSize) -Size ($oldSize-$efiSize) -GptType '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}' -ErrorAction Stop
if ($efi.Offset -ne $start -or $win.Offset -ne ($start+$efiSize) -or
    ($win.Offset+$win.Size) -gt ($start+$oldSize)) { throw '新建分区超出原目标范围' }
Add-PartitionAccessPath -DiskNumber %d -PartitionNumber $efi.PartitionNumber -AccessPath ($efiLetter+':\') -ErrorAction Stop
Add-PartitionAccessPath -DiskNumber %d -PartitionNumber $win.PartitionNumber -AccessPath ('%s:\') -ErrorAction Stop
Format-Volume -Partition $efi -FileSystem FAT32 -NewFileSystemLabel 'LXULTRA_EFI' -Confirm:$false -ErrorAction Stop | Out-Null
Format-Volume -Partition $win -FileSystem NTFS -NewFileSystemLabel 'LxUltra Windows' -Confirm:$false -ErrorAction Stop | Out-Null
@{efiLetter=$efiLetter;windowsLetter='%s';efiGuid=[string]$efi.Guid;windowsGuid=[string]$win.Guid}
""" % (p["disk"], p["disk"], p["part"], p["disk_guid"], p["disk_unique_id"],
       p["part_guid"], p["offset"], p["size"], p["letter"],
       self.plan["new_efi_size"], p["offset"], p["size"], p["letter"],
       p["disk"], p["part"], p["disk"], p["disk"], p["disk"], p["disk"],
       p["letter"], p["letter"])
        result = self.ps_json(script, timeout=300)
        if not result["ok"]:
            raise RuntimeError(result.get("error", "目标分区重建失败"))
        data = result["data"]
        self.efi_letter = data["efiLetter"]
        self.efi_guid = data.get("efiGuid")
        self.windows_letter = data["windowsLetter"]

    def _write_boot(self):
        windows = self.windows_letter + ":\\Windows"
        efi = self.efi_letter + ":"
        try:
            _migration_run([os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                                         "System32", "bcdboot.exe"), windows,
                            "/s", efi, "/f", "UEFI", "/c"], 300)
        except RuntimeError as exc:
            # A live Windows host can fail BcdOpenStore with c0000035 while the
            # boot files were copied.  The documented BCD-Template + BCDEdit
            # procedure writes an independent store on the selected EFI only.
            if "183" not in str(exc) and "c0000035" not in str(exc).lower():
                raise
            self._write_bcd_from_template(windows)

    def _write_bcd_from_template(self, windows):
        dest = os.path.join(self.efi_letter + ":\\", "EFI", "Microsoft", "Boot", "BCD")
        template = os.path.join(windows, "System32", "config", "BCD-Template")
        if not os.path.isfile(template):
            raise RuntimeError("应用后的 Windows 缺少 BCD-Template")
        shutil.copyfile(template, dest)
        bcdedit = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "bcdedit.exe")
        created = _migration_run([bcdedit, "/store", dest, "/create", "/application",
                                  "osloader", "/d", "LxUltra Windows"], 30)
        match = re.search(r"\{[0-9a-fA-F-]{36}\}", created)
        if not match:
            raise RuntimeError("无法取得新建的 Windows 启动项标识")
        loader = match.group(0)
        commands = (["/set", loader, "device", "partition=" + self.windows_letter + ":"],
                    ["/set", loader, "osdevice", "partition=" + self.windows_letter + ":"],
                    ["/set", loader, "path", "\\Windows\\System32\\winload.efi"],
                    ["/set", loader, "systemroot", "\\Windows"],
                    ["/set", loader, "locale", "zh-CN"],
                    ["/set", loader, "inherit", "{bootloadersettings}"],
                    ["/set", "{bootmgr}", "device", "partition=" + self.efi_letter + ":"],
                    ["/default", loader],
                    ["/displayorder", loader, "/addfirst"])
        for args in commands:
            _migration_run([bcdedit, "/store", dest] + args, 30)

    def _inspect(self):
        windows = self.windows_letter + ":\\Windows"
        efi = self.efi_letter + ":\\EFI"
        for path in (os.path.join(windows, "System32", "config", "SYSTEM"),
                     os.path.join(windows, "System32", "winload.efi"),
                     os.path.join(efi, "Microsoft", "Boot", "BCD"),
                     os.path.join(efi, "Boot", "bootx64.efi")):
            if not os.path.isfile(path):
                raise RuntimeError("迁移文件检查失败: " + path)
        bcdedit = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "bcdedit.exe")
        store = os.path.join(efi, "Microsoft", "Boot", "BCD")
        output = _migration_run([bcdedit, "/store", store, "/enum", "{default}"], 30)
        if self.windows_letter + ":" not in output or "winload.efi" not in output.lower():
            raise RuntimeError("目标 BCD 未指向新 Windows 分区")

    def cleanup(self, keep_image):
        errors = []
        try:
            self._release_snapshot()
        except Exception as exc:
            errors.append("释放 VSS 快照失败: %s" % exc)
        if not keep_image and self.efi_letter and self.efi_guid:
            # The temporary drive letter is needed for BCD creation and inspection,
            # but an EFI partition should not remain visible in Explorer afterward.
            script = r"""
$p=Get-Partition -DiskNumber %d -ErrorAction Stop | Where-Object { [string]$_.Guid -ieq '%s' } | Select-Object -First 1
if (-not $p -or [string]$p.DriveLetter -ne '%s' -or $p.IsBoot -or $p.IsSystem) {
    throw '目标 EFI 身份或盘符变化，拒绝移除访问路径'
}
Remove-PartitionAccessPath -DiskNumber %d -PartitionNumber $p.PartitionNumber -AccessPath '%s:\' -ErrorAction Stop
@{ hidden = $true }
""" % (self.plan["target"]["disk"], self.efi_guid, self.efi_letter,
       self.plan["target"]["disk"], self.efi_letter)
            try:
                result = self.ps_json(script, timeout=60)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error", "移除临时 EFI 盘符失败"))
                self.efi_letter = None
            except Exception as exc:
                errors.append("移除临时 EFI 盘符失败: %s" % exc)
        if not keep_image and os.path.isfile(self.image):
            try:
                os.remove(self.image)
            except Exception as exc:
                errors.append("清理未验证映像失败: %s" % exc)
        if self.owns_dism_scratch:
            try:
                if os.path.isdir(self.dism_scratch):
                    shutil.rmtree(self.dism_scratch)
                self.owns_dism_scratch = False
            except Exception as exc:
                errors.append("清理 DISM 暂存目录失败: %s" % exc)
        if errors:
            raise RuntimeError("; ".join(errors))
