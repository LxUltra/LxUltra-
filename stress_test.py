# -*- coding: utf-8 -*-
"""后端压力与泄漏测试:
  1) 顺序长跑:40 次 partition_disks,记录句柄数/内存(RSS)变化,判断泄漏
  2) 并发风暴:8 线程 × 5 次,验证线程安全与不崩
  3) 异常输入:非法磁盘号/空操作/超大参数,验证被安全处理
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import statistics
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import base64
import marshal

WRAP = open(os.path.join(HERE, "app_wrapper.py"), encoding="utf-8").read()
_toy = "class API:\n    pass\n"
ns = {"__name__": "stress_wrapper"}
exec(compile(WRAP.replace("@@ORIG_B64@@", base64.b64encode(marshal.dumps(compile(_toy, "app.py", "exec"))).decode(), 1),
             "app_wrapper_stress.py", "exec"), ns)
INSTALL = ns["_install_partition_methods"]


class B:
    pass


INSTALL(B)
BK = B()

k32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.GetProcessHandleCount.restype = wt.BOOL
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
psapi.GetProcessMemoryInfo.restype = wt.BOOL


def handles():
    n = wt.DWORD(0)
    if not k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n)):
        return -1
    return n.value


def rss_mb():
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return -1.0
    return c.WorkingSetSize / 1048576.0


passed, failed = [], []


def ok(name, cond, detail=""):
    (passed if cond else failed).append((name, detail))
    print(("  PASS " if cond else "  FAIL ") + name + (("  | " + str(detail)) if detail and not cond else ""))


print("== 1) 顺序长跑 40 次 partition_disks ==")
h0, m0 = handles(), rss_mb()
times = []
for i in range(40):
    t = time.time()
    r = BK.partition_disks()
    times.append(time.time() - t)
    if not r.get("ok"):
        ok("第 %d 次调用成功" % (i + 1), False, r.get("error"))
        break
h1, m1 = handles(), rss_mb()
times_sorted = sorted(times)
p50 = statistics.median(times)
p95 = times_sorted[int(len(times_sorted) * 0.95) - 1]
print("     耗时 p50=%.2fs p95=%.2fs max=%.2fs | 句柄 %d→%d | RSS %.1f→%.1f MB" % (
    p50, p95, max(times), h0, h1, m0, m1))
ok("40 次全部成功", len(times) == 40)
ok("句柄无泄漏(增长 <= 40)", h1 - h0 <= 40, "%d→%d" % (h0, h1))
ok("内存平稳(增长 <= 80MB)", m1 - m0 <= 80, "%.1f→%.1f MB" % (m0, m1))
ok("单次耗时稳定(p95 < 6s)", p95 < 6, "%.2fs" % p95)

print("== 2) 并发风暴 8 线程 x 5 次 ==")
errs = []
results_holder = []


def worker():
    for _ in range(5):
        try:
            r = BK.partition_disks()
            results_holder.append(bool(r.get("ok")))
        except Exception as e:
            errs.append(repr(e))


ths = [threading.Thread(target=worker) for _ in range(8)]
h2, m2 = handles(), rss_mb()
t0 = time.time()
for t in ths:
    t.start()
for t in ths:
    t.join()
conc_s = time.time() - t0
h3, m3 = handles(), rss_mb()
print("     并发 40 次调用耗时 %.1fs,成功 %d,异常 %d,句柄 %d→%d,RSS %.1f→%.1f MB" % (
    conc_s, sum(results_holder), len(errs), h2, h3, m2, m3))
ok("并发全部成功无异常", len(errs) == 0 and sum(results_holder) == 40, errs[:2])
ok("并发后句柄无泄漏(增长 <= 60)", h3 - h2 <= 60, "%d→%d" % (h2, h3))

print("== 3) 非法/边界输入 ==")
cases = [
    ("空操作列表", lambda: BK.partition_job_submit([])),
    ("非列表操作", lambda: BK.partition_job_submit("bad")),
    ("缺磁盘号的删除", lambda: BK.partition_job_submit([{"type": "delete_partition"}])),
    ("不存在的任务状态", lambda: BK.partition_job_status("no_such_job")),
    ("健康-非法盘号", lambda: BK.partition_health(99)),
    ("健康-字符串盘号", lambda: BK.partition_health("abc")),
    ("未知操作类型", lambda: BK.partition_job_submit([{"type": "no_such_op", "disk": 0}])),
]
for name, fn in cases:
    try:
        r = fn()
        bad = (r or {}).get("ok") is True
        ok("非法输入安全拒绝: " + name, not bad, r)
    except Exception as e:
        ok("非法输入安全拒绝: " + name, False, "抛异常 %r" % (e,))

# 未知操作的 job 允许创建(但执行时应报未知) —— 验证结果里 ok=False
r = BK.partition_job_submit([{"type": "no_such_op", "disk": 0}])
if r.get("ok"):
    jid = r["job_id"]
    for _ in range(60):
        st = BK.partition_job_status(jid)
        if st.get("state") in ("done", "error"):
            break
        time.sleep(0.3)
    res0 = (st.get("results") or [{}])[0]
    ok("未知操作在执行期被拒", res0.get("ok") is False, res0)

print()
print("======== 压力测试: %d 通过 / %d 失败 ========" % (len(passed), len(failed)))
for n, d in failed:
    print("FAIL:", n, "|", d)
sys.exit(0 if not failed else 1)
