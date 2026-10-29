"""Windows Update driver discovery and selected installation.

Only offers drivers the machine's configured Windows Update service reports.
No vendor download URL or installer is executed directly.
"""
import base64
import json
import subprocess
import threading


def normalize_updates(rows):
    seen, result = set(), []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        update_id = str(row.get("id") or "").strip()
        try:
            revision = int(row.get("revision"))
        except (TypeError, ValueError):
            continue
        if not update_id or revision < 0:
            continue
        key = "%s:%d" % (update_id, revision)
        if key in seen:
            continue
        seen.add(key)
        result.append({"key": key, "id": update_id, "revision": revision,
                       "title": str(row.get("title") or "未命名驱动"),
                       "size": max(0, int(row.get("size") or 0)),
                       "downloaded": bool(row.get("downloaded")),
                       "rebootRequired": bool(row.get("rebootRequired"))})
    return result


def selected_update_ids(keys):
    ids = []
    for key in keys:
        update_id, separator, revision = key.rpartition(":")
        if not separator or not update_id or not revision.isdigit():
            raise ValueError("驱动标识无效，请重新检测。")
        ids.append(update_id)
    if len(ids) != len(set(ids)):
        raise ValueError("驱动选择包含重复项目。")
    return ids


_WUA_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$action = '__ACTION__'
$requestedIds = [string[]](ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__IDS__'))))
try {
  $stage = 'search'
  $session = New-Object -ComObject Microsoft.Update.Session
  $session.ClientApplicationID = 'LxUltra Driver Updates'
  $searcher = $session.CreateUpdateSearcher()
  $rows = @()
  $chosen = $null
  for ($attempt = 0; $attempt -lt $(if ($action -eq 'install') { 2 } else { 1 }); $attempt++) {
    $found = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Driver'")
    $rows = @()
    $chosen = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($update in $found.Updates) {
      $identity = $update.Identity
      $rows += [pscustomobject]@{id=[string]$identity.UpdateID;revision=[int]$identity.RevisionNumber;title=[string]$update.Title;size=[long]$update.MaxDownloadSize;downloaded=[bool]$update.IsDownloaded;rebootRequired=[bool]$update.InstallationBehavior.RebootBehavior}
      if ($action -eq 'install' -and $requestedIds -contains [string]$identity.UpdateID) {
        $stage = 'select'
        if (-not $update.EulaAccepted) { $update.AcceptEula() }
        [void]$chosen.Add($update)
      }
    }
    if ($action -eq 'scan' -or $chosen.Count -eq $requestedIds.Count) { break }
  }
  if ($action -eq 'scan') {
    @{kind='result';ok=$true;updates=@($rows)} | ConvertTo-Json -Compress -Depth 5
  } elseif ($chosen.Count -ne $requestedIds.Count -or $chosen.Count -eq 0) {
    @{kind='result';ok=$false;error=('所选驱动已不在 Windows Update 的可用列表中。已重新核对两次；请重新检测。匹配 ' + $chosen.Count + '/' + $requestedIds.Count + ' 项。');requested=@($requestedIds);available=@($rows | ForEach-Object { $_.id })} | ConvertTo-Json -Compress
  } else {
    $stage = 'download-start'
    Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; [ComVisible(true)] [ClassInterface(ClassInterfaceType.AutoDispatch)] public class LxUltraWuaCallback { [DispId(0)] public void Invoke(object job, object args) {} }'
    $callback = New-Object LxUltraWuaCallback
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $chosen
    $downloadJob = $downloader.BeginDownload($callback, $callback, $null)
    $stage = 'download-progress'
    while (-not $downloadJob.IsCompleted) {
      @{kind='progress';phase='downloading';percent=[int]$downloadJob.GetProgress().PercentComplete} | ConvertTo-Json -Compress
      Start-Sleep -Milliseconds 500
    }
    $download = $downloader.EndDownload($downloadJob)
    $stage = 'download-result'
    $downloadJob.CleanUp()
    if ($download.ResultCode -ne 2) {
      @{kind='result';ok=$false;error=('驱动下载失败，Windows Update 结果代码 ' + [string]$download.ResultCode)} | ConvertTo-Json -Compress
    } else {
      @{kind='progress';phase='installing';percent=0} | ConvertTo-Json -Compress
      $installer = $session.CreateUpdateInstaller()
      $installer.Updates = $chosen
      $stage = 'install-start'
      $installJob = $installer.BeginInstall($callback, $callback, $null)
      $stage = 'install-progress'
      while (-not $installJob.IsCompleted) {
        @{kind='progress';phase='installing';percent=[int]$installJob.GetProgress().PercentComplete} | ConvertTo-Json -Compress
        Start-Sleep -Milliseconds 500
      }
      $installed = $installer.EndInstall($installJob)
      $stage = 'install-result'
      $installJob.CleanUp()
      @{kind='result';ok=($installed.ResultCode -eq 2);resultCode=[int]$installed.ResultCode;rebootRequired=[bool]$installed.RebootRequired;error=$(if($installed.ResultCode -eq 2){''}else{'驱动安装未全部成功，Windows Update 结果代码 ' + [string]$installed.ResultCode})} | ConvertTo-Json -Compress
    }
  }
} catch {
  @{kind='result';ok=$false;stage=$stage;error=$_.Exception.Message;hresult=$_.Exception.HResult} | ConvertTo-Json -Compress
}
'''


def run_windows_update(action, keys=(), on_progress=None):
    if action not in ("scan", "install"):
        raise ValueError("无效的驱动更新操作")
    selected = base64.b64encode(json.dumps(selected_update_ids(keys)).encode("utf-8")).decode("ascii")
    script = _WUA_SCRIPT.replace("__ACTION__", action).replace("__IDS__", selected)
    process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    expired = threading.Event()
    def stop_when_expired():
        expired.set()
        process.kill()
    timer = threading.Timer(600 if action == "scan" else 3600, stop_when_expired)
    timer.start()
    result = None
    diagnostic = []
    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                diagnostic.append(line)
                continue
            if not isinstance(event, dict):
                continue
            if event.get("kind") == "progress" and on_progress:
                on_progress({"phase": event.get("phase"), "percent": event.get("percent")})
            elif "ok" in event:
                result = event
        process.wait()
    finally:
        timer.cancel()
        process.stdout.close()
    if expired.is_set():
        raise TimeoutError("Windows Update 操作超时，请稍后重试。")
    if not isinstance(result, dict):
        raise RuntimeError("Windows Update 未返回结果：" + " ".join(diagnostic)[-500:])
    return result


class DriverUpdateManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._updates = []
        self._state = "idle"
        self._message = "点击检测，查找 Windows Update 为本机提供的驱动。"
        self._error = ""
        self._reboot = False
        self._phase = None
        self._percent = None

    def status(self):
        with self._lock:
            return {"ok": True, "state": self._state, "message": self._message,
                    "error": self._error, "updates": [dict(item) for item in self._updates],
                    "rebootRequired": self._reboot, "phase": self._phase, "percent": self._percent}

    def _start(self, action, keys=()):
        with self._lock:
            if self._state in ("scanning", "installing"):
                return {"ok": False, "error": "驱动任务正在进行，请等待完成。"}
            self._state = "scanning" if action == "scan" else "installing"
            self._message = "正在联系 Windows Update 检测驱动…" if action == "scan" else "正在下载并安装所选驱动…"
            self._error = ""
            self._reboot = False
            self._phase = "scanning" if action == "scan" else "preparing"
            self._percent = None
            self._thread = threading.Thread(target=self._work, args=(action, tuple(keys)), daemon=True)
            self._thread.start()
        return self.status()

    def scan(self):
        return self._start("scan")

    def install(self, keys):
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys):
            return {"ok": False, "error": "请先选择要安装的驱动。"}
        with self._lock:
            available = {row["key"] for row in self._updates}
            if len(set(keys)) != len(keys) or not set(keys).issubset(available):
                return {"ok": False, "error": "所选驱动不在最近一次扫描结果中，请重新检测。"}
        return self._start("install", keys)

    def _work(self, action, keys):
        try:
            result = run_windows_update(action, keys, self._progress)
            with self._lock:
                self._phase = None
                self._percent = None
                if not result.get("ok"):
                    self._state = "error"
                    self._error = str(result.get("error") or "Windows Update 操作失败")
                    self._message = self._error
                elif action == "scan":
                    self._updates = normalize_updates(result.get("updates"))
                    self._state = "ready"
                    self._message = "找到 %d 项可用驱动。" % len(self._updates) if self._updates else "Windows Update 未报告新的驱动。"
                else:
                    self._state = "complete"
                    self._reboot = bool(result.get("rebootRequired"))
                    self._message = "驱动安装完成。" + (" 请重启电脑。" if self._reboot else "")
                    self._updates = []
        except Exception as exc:
            with self._lock:
                self._state = "error"
                self._error = str(exc)
                self._message = "驱动操作失败：" + str(exc)
                self._phase = None
                self._percent = None

    def _progress(self, event):
        phase = event.get("phase")
        if phase not in ("downloading", "installing"):
            return
        try:
            percent = max(0, min(100, int(event.get("percent"))))
        except (TypeError, ValueError):
            percent = None
        with self._lock:
            self._phase = phase
            self._percent = percent
            label = "正在下载所选驱动" if phase == "downloading" else "正在安装所选驱动"
            self._message = label + ("：%d%%" % percent if percent is not None else "…")
