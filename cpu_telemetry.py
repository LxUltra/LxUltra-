"""Supplement LibreHardwareMonitor with Windows actual frequency readings."""
import json
import re
import subprocess
import threading
import time


def summarize_frequencies(readings, p_cores, e_cores):
    """Use the OS logical-core ordering only when topology exactly matches."""
    p_threads = 2 * int(p_cores or 0)
    e_threads = int(e_cores or 0)
    if p_threads < 2 or p_threads + e_threads != len(readings):
        return None
    if set(readings) != set(range(p_threads + e_threads)):
        return None
    p = [readings[i] for i in range(p_threads)]
    e = [readings[i] for i in range(p_threads, p_threads + e_threads)]
    if any(not 100 <= value <= 10000 for value in p + e):
        return None
    return {"p_average": round(sum(p) / len(p)), "p_peak": round(max(p)),
            "e_average": round(sum(e) / len(e)) if e else None}


def classify_voltage(rows):
    vcore, vid = None, None
    for row in rows or []:
        if row.get("key") != "cpu" or row.get("type") != "Voltage":
            continue
        name = str(row.get("name") or "").lower()
        value = row.get("value")
        if not isinstance(value, (int, float)) or not 0 < value < 5:
            continue
        if "vcore" in name or ("core voltage" in name and "p-core" not in name and "e-core" not in name):
            vcore = value
        elif "p-core" in name and vid is None:
            vid = value
    return {"vcore": vcore, "vid": vid}


def read_actual_frequencies():
    script = r'''$ErrorActionPreference='Stop'; $s=(Get-Counter -Counter '\Processor Information(*)\Actual Frequency').CounterSamples | Where-Object {$_.Path -notmatch '_total'}; @($s | ForEach-Object {@{path=$_.Path;mhz=$_.CookedValue}}) | ConvertTo-Json -Compress'''
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, encoding="utf-8", errors="replace", timeout=12,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip()[-300:])
    rows = json.loads(completed.stdout)
    values = {}
    for row in rows if isinstance(rows, list) else [rows]:
        match = re.search(r"\(\d+,(\d+)\)", row.get("path", ""))
        if match:
            values[int(match.group(1))] = float(row["mhz"])
    return values


class CpuFrequencySampler:
    def __init__(self):
        self._lock = threading.Lock()
        self._readings = {}
        self._read_at = 0
        self._thread = None

    def current(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._read, daemon=True)
                self._thread.start()
            return dict(self._readings) if time.monotonic() - self._read_at < 15 else {}

    def _read(self):
        try:
            data = read_actual_frequencies()
            with self._lock:
                self._readings = data
                self._read_at = time.monotonic()
            time.sleep(3)
        except Exception:
            time.sleep(10)
