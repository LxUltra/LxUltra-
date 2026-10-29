# -*- coding: utf-8 -*-
"""Run destructive partition checks only against disposable VHDX fixtures."""
import base64
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, HERE)

WRAP_SRC = (Path(HERE, "migration_engine.py").read_text(encoding="utf-8") + "\n" +
            Path(HERE, "app_wrapper.py").read_text(encoding="utf-8"))
TOY = "class API:\n    pass\n"
SRC = WRAP_SRC.replace("@@ORIG_B64@@", base64.b64encode(marshal.dumps(compile(TOY, "app.py", "exec"))).decode(), 1)
NS = {"__name__": "vhd_acceptance_wrapper"}
exec(compile(SRC, "app_wrapper_vhdtest.py", "exec"), NS)


class API:
    pass


NS["_install_partition_methods"](API)
api = API()
passed = 0


def check(name, condition, detail=""):
    global passed
    if not condition:
        raise AssertionError("FAIL %s: %s" % (name, detail))
    passed += 1
    print("  PASS " + name + (" | " + str(detail) if detail else ""))


def run_ps(script, timeout=240):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    cp = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-EncodedCommand", encoded], capture_output=True, timeout=timeout)
    out = cp.stdout.decode("utf-8-sig", "replace").strip()
    err = cp.stderr.decode("utf-8", "replace").strip()
    if cp.returncode:
        raise RuntimeError("PowerShell failed: " + (err or out or str(cp.returncode)))
    return out


def ps_json(script, timeout=240):
    out = run_ps(script + "\nConvertTo-Json -InputObject $result -Compress -Depth 5", timeout)
    return json.loads(out) if out else None


def current_disks():
    r = api.partition_disks()
    if not r.get("ok"):
        raise RuntimeError("partition_disks failed: " + str(r.get("error")))
    return r.get("disks", [])


def wait_job(result, timeout=7200):
    if not result.get("ok"):
        raise RuntimeError("job submit failed: " + str(result.get("error")))
    end = time.time() + timeout
    while time.time() < end:
        st = api.partition_job_status(result["job_id"])
        if not st.get("ok"):
            raise RuntimeError("job status failed: " + str(st.get("error")))
        if st.get("state") in ("done", "error"):
            return st
        time.sleep(.25)
    raise TimeoutError("partition job timed out")


def run_op(name, op, expected=True, expected_text=None, timeout=7200, fixture_target=True):
    # Every physical-disk target must be one of the VHDs attached by this test.
    if fixture_target and op.get("type") in {"format", "delete_partition", "delete_all", "quick_partition", "convert",
                           "rebuild_mbr", "wipe", "set_letter", "set_label", "extend", "shrink",
                           "clone_disk", "clone_partition"}:
        targets = [int(op["disk"])]
        if op.get("type") == "clone_disk":
            targets.append(int(op["source"]))
        if op.get("type") == "clone_partition":
            targets.append(int(op["source_disk"]))
        check(name + " target IDs are VHDs", all(x in fixture_ids for x in targets), targets)
    st = wait_job(api.partition_job_submit([op]), timeout)
    item = (st.get("results") or [{}])[0]
    msg = item.get("message", "")
    if expected:
        check(name, st.get("state") == "done" and item.get("ok") is True, msg)
    else:
        check(name, st.get("state") == "done" and item.get("ok") is False and
              (expected_text is None or expected_text in msg), msg)
    return item


def attach_vhd(path, size_mb):
    before = {int(d["number"]) for d in current_disks()}
    run_ps("New-VHD -Path '%s' -SizeBytes %dMB -Dynamic -ErrorAction Stop | Out-Null; Mount-VHD -Path '%s' -ErrorAction Stop; Start-Sleep -Milliseconds 500" %
           (str(path), size_mb, str(path)))
    after = {int(d["number"]) for d in current_disks()}
    created = sorted(after - before)
    if len(created) != 1:
        raise RuntimeError("Could not uniquely identify newly attached VHD %s: %r" % (path, created))
    fixture_ids.add(created[0])
    return created[0]


def initialize_volume(disk, style, letter, label):
    run_ps("""
$ErrorActionPreference = 'Stop'
Initialize-Disk -Number %d -PartitionStyle %s -PassThru | Out-Null
$part = New-Partition -DiskNumber %d -UseMaximumSize -DriveLetter '%s' -ErrorAction Stop
$part | Format-Volume -FileSystem NTFS -NewFileSystemLabel '%s' -Confirm:$false -Force -ErrorAction Stop | Out-Null
""" % (disk, style, disk, letter, label))


def find_part(disk, letter=None):
    d = next(x for x in current_disks() if int(x["number"]) == int(disk))
    parts = d.get("partitions", [])
    if letter:
        parts = [p for p in parts if str(p.get("letter", "")).upper() == letter.upper()]
    return parts[0]


temp_root = Path(tempfile.gettempdir()).resolve()
test_dir = (temp_root / ("LxUltraVhdAcceptance-" + uuid.uuid4().hex)).resolve()
test_dir.mkdir()
fixture_paths = []
fixture_ids = set()

try:
    check("管理员权限", api.partition_admin().get("admin") is True)
    used = {str(p.get("letter", "")).upper() for d in current_disks() for p in d.get("partitions", []) if p.get("letter")}
    letters = [x for x in "ZYXWVUTSRQPONMLKJIHGFED" if x not in used]
    if len(letters) < 6:
        raise RuntimeError("Not enough free drive letters for isolated VHD tests")
    src_l, dst_l, scratch_l, mbr_l, conv_l, mount_l = letters[:6]

    src_path = test_dir / "source.vhdx"
    dst_path = test_dir / "target.vhdx"
    scratch_path = test_dir / "scratch.vhdx"
    mbr_path = test_dir / "mbr.vhdx"
    convert_path = test_dir / "convert.vhdx"
    mount_path = test_dir / "mount-control.vhdx"
    wipe_path = test_dir / "wipe.vhdx"
    fixture_paths = [src_path, dst_path, scratch_path, mbr_path, convert_path, mount_path, wipe_path]

    src_disk = attach_vhd(src_path, 1024)
    dst_disk = attach_vhd(dst_path, 1024)
    scratch_disk = attach_vhd(scratch_path, 1024)
    mbr_disk = attach_vhd(mbr_path, 256)
    convert_disk = attach_vhd(convert_path, 256)
    mount_disk = None
    wipe_disk = attach_vhd(wipe_path, 128)
    for n in (src_disk, dst_disk, scratch_disk, mbr_disk, convert_disk, wipe_disk):
        d = next(x for x in current_disks() if int(x["number"]) == n)
        check("VHD 标记为非系统盘", not d.get("isSystemDisk"), n)

    initialize_volume(src_disk, "GPT", src_l, "LX_SRC")
    initialize_volume(dst_disk, "GPT", dst_l, "LX_DST")
    initialize_volume(mbr_disk, "MBR", mbr_l, "LX_MBR")
    initialize_volume(convert_disk, "GPT", conv_l, "LX_CONVERT")
    Path(src_l + ":\\lx-source-marker.txt").write_text("isolated source payload", encoding="utf-8")
    Path(dst_l + ":\\delete-this-extra.txt").write_text("must be removed by mirror", encoding="utf-8")

    vhd_listing = api.partition_vhd_list()
    check("VHD 列表可读", vhd_listing.get("ok") is True)
    check("VHD 列表包含本轮挂载镜像", any(str(x.get("Path", "")).lower() == str(src_path).lower()
        for x in vhd_listing.get("vhds", [])))
    src_part = find_part(src_disk, src_l)
    dst_part = find_part(dst_disk, dst_l)
    run_op("分区文件镜像", {"type": "clone_partition", "source_disk": src_disk,
        "source_part": src_part["number"], "source_letter": src_l, "disk": dst_disk,
        "part": dst_part["number"], "dest_letter": dst_l, "confirmed": True})
    check("分区镜像复制源文件", Path(dst_l + ":\\lx-source-marker.txt").read_text(encoding="utf-8") == "isolated source payload")
    check("分区镜像删除目标多余文件", not Path(dst_l + ":\\delete-this-extra.txt").exists())
    Path(dst_l + ":\\disk-extra.txt").write_text("remove", encoding="utf-8")
    run_op("整盘数据卷镜像", {"type": "clone_disk", "source": src_disk, "disk": dst_disk, "confirmed": True})
    check("整盘镜像保留源文件", Path(dst_l + ":\\lx-source-marker.txt").exists())
    check("整盘镜像同步清除目标额外文件", not Path(dst_l + ":\\disk-extra.txt").exists())

    # Exercise the scratch VHD's write operations, tracking its current volume letter.
    run_op("快速分区", {"type": "quick_partition", "disk": scratch_disk, "style": "GPT",
        "layout": [{"size_mb": "rest", "fs": "NTFS", "label": "LX_QUICK", "letter": scratch_l}]})
    scratch_part = find_part(scratch_disk, scratch_l)
    run_op("格式化虚拟卷", {"type": "format", "disk": scratch_disk, "part": scratch_part["number"], "fs": "NTFS", "label": "LX_FORMAT"})
    run_op("修改虚拟卷标", {"type": "set_label", "disk": scratch_disk, "part": scratch_part["number"], "letter": scratch_l, "label": "LX_LABEL"})
    alt_l = next(x for x in letters[6:] if x not in used) if len(letters) > 6 else None
    if not alt_l:
        raise RuntimeError("Need one additional free drive letter for set_letter")
    run_op("更改虚拟卷盘符", {"type": "set_letter", "disk": scratch_disk, "part": scratch_part["number"], "letter": alt_l})
    scratch_l = alt_l
    scratch_part = find_part(scratch_disk, scratch_l)
    run_op("缩容虚拟分区", {"type": "shrink", "disk": scratch_disk, "part": scratch_part["number"], "size_mb": 128})
    run_op("扩展虚拟分区", {"type": "extend", "disk": scratch_disk, "part": scratch_part["number"], "size_mb": 128})
    run_op("虚拟卷检查", {"type": "scan", "letter": scratch_l, "fix": False})
    run_op("虚拟卷检查并修复", {"type": "scan", "letter": scratch_l, "fix": True})
    run_op("虚拟卷碎片整理", {"type": "defrag", "letter": scratch_l})
    run_op("虚拟卷测速", {"type": "benchmark", "letter": scratch_l})
    run_op("删除虚拟分区", {"type": "delete_partition", "disk": scratch_disk, "part": scratch_part["number"]})
    run_op("再次快速分区", {"type": "quick_partition", "disk": scratch_disk, "style": "GPT",
        "layout": [{"size_mb": "rest", "fs": "NTFS", "label": "LX_DELETE_ALL", "letter": scratch_l}]})
    run_op("删除虚拟盘全部分区", {"type": "delete_all", "disk": scratch_disk})

    run_op("转换虚拟盘分区表", {"type": "convert", "disk": convert_disk, "to": "MBR"})
    converted = next(x for x in current_disks() if int(x["number"]) == convert_disk)
    check("转换结果为 MBR", converted.get("style") == "MBR", converted.get("style"))
    run_op("虚拟盘转换回 GPT", {"type": "convert", "disk": convert_disk, "to": "GPT"})
    converted_back = next(x for x in current_disks() if int(x["number"]) == convert_disk)
    check("转换结果恢复为 GPT", converted_back.get("style") == "GPT", converted_back.get("style"))
    run_op("重建虚拟 MBR 引导", {"type": "rebuild_mbr", "disk": mbr_disk})
    run_op("擦除虚拟磁盘", {"type": "wipe", "disk": wipe_disk}, timeout=1800)

    # Protected system volume must be rejected at execution, without changing its label.
    system = next(d for d in current_disks() if d.get("isSystemDisk"))
    cpart = next(p for p in system.get("partitions", []) if str(p.get("letter", "")).upper() == "C")
    run_op("拒绝修改系统卷标", {"type": "set_label", "disk": system["number"], "part": cpart["number"],
        "letter": "C", "label": "SHOULD_NOT_APPLY"}, expected=False, expected_text="安全保护", fixture_target=False)

    # Test backend mount/dismount against a fresh unattached test image.
    run_ps("New-VHD -Path '%s' -SizeBytes 128MB -Dynamic -ErrorAction Stop | Out-Null" % mount_path)
    before_mount = {int(d["number"]) for d in current_disks()}
    run_op("挂载虚拟磁盘", {"type": "vhd_mount", "path": str(mount_path)})
    mounted = {int(d["number"]) for d in current_disks()} - before_mount
    check("挂载后出现虚拟磁盘", len(mounted) == 1, sorted(mounted))
    mount_disk = next(iter(mounted)); fixture_ids.add(mount_disk)
    run_op("卸载虚拟磁盘", {"type": "vhd_dismount", "path": str(mount_path)})
    check("卸载后测试磁盘消失", mount_disk not in {int(d["number"]) for d in current_disks()})

    print("\n======== VHDX 验收：%d 项通过 ========" % passed)
finally:
    # Only detach/delete the unique directory created by this run.
    try:
        for path in fixture_paths:
            if path.exists():
                try:
                    run_ps("Dismount-VHD -Path '%s' -ErrorAction SilentlyContinue | Out-Null" % path, timeout=60)
                except Exception:
                    pass
        if test_dir.parent == temp_root and test_dir.name.startswith("LxUltraVhdAcceptance-"):
            shutil.rmtree(test_dir, ignore_errors=True)
    except Exception as cleanup_error:
        print("CLEANUP WARNING:", cleanup_error, file=sys.stderr)
