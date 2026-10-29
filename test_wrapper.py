# -*- coding: utf-8 -*-
"""包装器单元测试(必须用 Python 3.11 运行):
  T1 注入机制:玩具模块代替原字节码,验证 API 类在 exec 中被即时挂上 partition_* 方法
  T2 后端冒烟:真实 partition_disks / partition_admin(只读,安全)
  T3 守卫:对系统盘提交 wipe / 删除EFI / 格式化启动分区 —— 必须全部被拒(安全,不会真执行)
"""
import base64
import json
import marshal
import os
import sys
import time

assert sys.version_info[:2] == (3, 11), "must run with Python 3.11, got %s" % sys.version

HERE = os.path.dirname(os.path.abspath(__file__))
WRAP_SRC = os.path.join(HERE, "app_wrapper.py")
TOY_RESULT = os.path.join(os.environ.get("TEMP", "."), "lxultra_toy_result.json")

TOY = r'''
import json, os
class API:
    def foo(self):
        return "foo-orig"

def main():
    r = {
        "has_partition": hasattr(API, "partition_disks"),
        "has_orig_foo": hasattr(API, "foo"),
        "has_submit": hasattr(API, "partition_job_submit"),
        "has_status": hasattr(API, "partition_job_status"),
        "has_admin": hasattr(API, "partition_admin"),
        "has_vhd": hasattr(API, "partition_vhd_list"),
        "orig_method_intact": API.foo.__code__.co_name == "foo",
    }
    with open(os.path.join(os.environ["TEMP"], "lxultra_toy_result.json"), "w") as f:
        f.write(json.dumps(r))

main()
'''

passed, failed = [], []

def check(name, cond, detail=""):
    (passed if cond else failed).append((name, detail))
    print(("  PASS " if cond else "  FAIL ") + name + (("  | " + str(detail)) if detail and not cond else ""))


def load_wrapper_with(orig_bytes):
    src = open(WRAP_SRC, encoding="utf-8").read()
    src = src.replace("@@ORIG_B64@@", base64.b64encode(orig_bytes).decode(), 1)
    ns = {"__name__": "wrapper_under_test"}
    exec(compile(src, "app_wrapper_test.py", "exec"), ns)
    return ns


print("== T1 注入机制(玩具模块)==")
if os.path.exists(TOY_RESULT):
    os.remove(TOY_RESULT)
toy_bytes = marshal.dumps(compile(TOY, "app.py", "exec"))
wns = load_wrapper_with(toy_bytes)
check("T1 玩具模块执行完成(写了结果文件)", os.path.exists(TOY_RESULT))
r = json.load(open(TOY_RESULT, encoding="utf-8")) if os.path.exists(TOY_RESULT) else {}
for k in ("has_partition", "has_orig_foo", "has_submit", "has_status", "has_admin", "has_vhd", "orig_method_intact"):
    check("T1 " + k, r.get(k) is True, r.get(k))

print("== T2 后端只读冒烟(真实 PowerShell)==")
class Dummy:
    pass
wns["_install_partition_methods"](Dummy)
d = Dummy()
t0 = time.time()
rd = d.partition_disks()
elapsed = time.time() - t0
check("T2 partition_disks ok", rd.get("ok") is True, rd.get("error"))
disks = rd.get("disks") or []
check("T2 返回磁盘数 >=1", len(disks) >= 1, len(disks))
print("     磁盘列表: " + " | ".join("盘%s %s %.0fGB %s%s" % (
    x.get("number"), x.get("style"), (x.get("size") or 0) / 1e9, x.get("name"),
    "[系统盘]" if x.get("isSystemDisk") else "") for x in disks))
sys_disks = [x for x in disks if x.get("isSystemDisk")]
check("T2 恰有 1 个系统盘", len(sys_disks) == 1, len(sys_disks))
if sys_disks:
    sd = sys_disks[0]
    print("     系统盘: 盘%s, 分区数 %d" % (sd["number"], len(sd.get("partitions") or [])))
    check("T2 系统盘有分区", len(sd.get("partitions") or []) >= 1)
    efi = [p for p in sd["partitions"] if p.get("isEFI")]
    check("T2 系统盘含 EFI 分区(GPT)", len(efi) >= 1, len(efi))
ra = d.partition_admin()
check("T2 partition_admin ok", ra.get("ok") is True)
print("     管理员: %s | partition_disks 耗时 %.2fs" % (ra.get("admin"), elapsed))

print("== T3 安全守卫(全部必须被拒,不会真执行)==")
if not sys_disks:
    check("T3 前置:找到系统盘", False, "无系统盘")
else:
    sysnum = sys_disks[0]["number"]
    efi_part = None
    for p in sys_disks[0]["partitions"]:
        if p.get("isEFI"):
            efi_part = p["number"]
            break

    # T3a: 系统盘 wipe
    rj = d.partition_job_submit([{"type": "wipe", "disk": sysnum}])
    if rj.get("ok") is False:
        check("T3a 系统盘 wipe 被拒(权限前置)", "管理员" in (rj.get("error") or ""), rj.get("error"))
    else:
        jid = rj["job_id"]
        for _ in range(120):
            st = d.partition_job_status(jid)
            if st.get("state") in ("done", "error"):
                break
            time.sleep(0.5)
        res0 = (st.get("results") or [{}])[0]
        check("T3a 系统盘 wipe 被拒", res0.get("ok") is False and "安全保护" in (res0.get("message") or ""), res0)

    # T3b: 删除系统盘 EFI 分区
    if efi_part is not None:
        rj = d.partition_job_submit([{"type": "delete_partition", "disk": sysnum, "part": efi_part}])
        if rj.get("ok") is False:
            check("T3b 删除EFI被拒(权限前置)", True)
        else:
            jid = rj["job_id"]
            for _ in range(120):
                st = d.partition_job_status(jid)
                if st.get("state") in ("done", "error"):
                    break
                time.sleep(0.5)
            res0 = (st.get("results") or [{}])[0]
            check("T3b 删除系统盘EFI分区被拒", res0.get("ok") is False and "安全保护" in (res0.get("message") or ""), res0)
    else:
        check("T3b 跳过(无EFI)", True)

    # T3c: C: 通常标记 IsBoot，而非 IsSystem；必须据此测试实际保护分支。
    cpart = next((p for p in sys_disks[0]["partitions"]
                  if p.get("letter") == "C" and (p.get("isBoot") or p.get("isSystem"))), None)
    if cpart:
        rj = d.partition_job_submit([{"type": "format", "disk": sysnum, "part": cpart["number"], "fs": "NTFS"}])
        if rj.get("ok") is False:
            check("T3c 格式化C:被拒(权限前置)", True)
        else:
            jid = rj["job_id"]
            for _ in range(120):
                st = d.partition_job_status(jid)
                if st.get("state") in ("done", "error"):
                    break
                time.sleep(0.5)
            res0 = (st.get("results") or [{}])[0]
            check("T3c 格式化系统分区被拒", res0.get("ok") is False and "安全保护" in (res0.get("message") or ""), res0)
    else:
        check("T3c C: 被标记为启动或系统分区", False, "保护测试未执行")

    def check_protected(name, op):
        rj = d.partition_job_submit([op])
        if rj.get("ok") is False:
            check(name + "(权限前置拒绝)", "管理员" in (rj.get("error") or ""), rj.get("error"))
            return
        for _ in range(120):
            st = d.partition_job_status(rj["job_id"])
            if st.get("state") in ("done", "error"):
                break
            time.sleep(0.25)
        item = (st.get("results") or [{}])[0]
        check(name, item.get("ok") is False and "安全保护" in (item.get("message") or ""), item)

    # T3d/e: critical system volumes and disks stay protected.
    if cpart:
        check_protected("T3d 系统卷标修改被拒", {"type": "set_label", "disk": sysnum,
            "part": cpart["number"], "letter": "C", "label": "SHOULD_NOT_APPLY"})
    check_protected("T3e 系统盘 MBR 重建被拒", {"type": "rebuild_mbr", "disk": sysnum})

    # T3f: 空操作列表
    rj = d.partition_job_submit([])
    check("T3f 空操作列表报错", rj.get("ok") is False)

print()
print("======== 结果: %d 通过, %d 失败 ========" % (len(passed), len(failed)))
if failed:
    for n, dt in failed:
        print("FAIL:", n, "|", dt)
    sys.exit(1)
