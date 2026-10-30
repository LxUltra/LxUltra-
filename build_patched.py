# -*- coding: utf-8 -*-
"""LxUltra.exe PyInstaller 归档重写器:
1. 解析原 exe 的 CArchive
2. 把 'app' 主脚本条目替换为 app_wrapper.py(注入原字节码 base64)的 marshal
3. 重建 blob 区 + TOC + cookie,输出 LxUltra_patched.exe
4. 自校验:重新解析并反序列化新 'app',确认包装器符号存在

运行环境:必须 Python 3.11(与目标运行时一致)
"""
import base64
import marshal
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_EXE = os.path.join(HERE, "_backup_original", "LxUltra.exe.orig")
ORIG_EXE = BACKUP_EXE if os.path.exists(BACKUP_EXE) else r"D:\LxUltra\LxUltra.exe"
ORIG_APP = os.path.join(HERE, "_extract", "s_app")
WRAPPER_SRC = os.path.join(HERE, "app_wrapper.py")
MIGRATION_SRC = os.path.join(HERE, "migration_engine.py")
DRIVER_SRC = os.path.join(HERE, "driver_updates.py")
CPU_TELEMETRY_SRC = os.path.join(HERE, "cpu_telemetry.py")
SOFTWARE_MANAGER_SRC = os.path.join(HERE, "software_manager.py")
STORAGE_SAFETY_SRC = os.path.join(HERE, "storage_safety.py")
OUT_EXE = os.path.join(HERE, "LxUltra_patched.exe")
MAGIC = b"MEI\014\013\012\013\016"


def parse_archive(data):
    pos = data.rfind(MAGIC)
    assert pos >= 0, "cookie not found"
    magic, pkg_len, toc_off, toc_len, pyver = struct.unpack("!8sIIII", data[pos:pos + 24])
    pylib = data[pos + 24:pos + 24 + 64].rstrip(b"\0").decode()
    archive_start = (pos + 88) - pkg_len
    assert archive_start >= 0, "bad pkgLen"
    toc_abs = archive_start + toc_off
    entries = []
    p = toc_abs
    end = toc_abs + toc_len
    while p < end:
        (entry_len,) = struct.unpack("!i", data[p:p + 4])
        epos, csize, usize, cflag = struct.unpack("!IIIB", data[p + 4:p + 4 + 13])
        typ = data[p + 17:p + 18]
        name = data[p + 18:p + entry_len].rstrip(b"\0").decode("utf-8", "ignore")
        entries.append({"name": name, "pos": epos, "csize": csize, "usize": usize,
                        "cflag": cflag, "type": typ, "toc_len": entry_len})
        p += entry_len
    return {"archive_start": archive_start, "pyver": pyver, "pylib": pylib,
            "pkg_len": pkg_len, "entries": entries}


def read_entry(data, arc, e):
    raw = data[arc["archive_start"] + e["pos"]: arc["archive_start"] + e["pos"] + e["csize"]]
    return zlib.decompress(raw) if e["cflag"] else raw


def main():
    assert sys.version_info[:2] == (3, 11), "must run with Python 3.11"
    data = open(ORIG_EXE, "rb").read()
    arc = parse_archive(data)
    print("parsed: %d entries, pyver=%s pylib=%s" % (len(arc["entries"]), arc["pyver"], arc["pylib"]))

    orig_app_bytes = open(ORIG_APP, "rb").read()
    # 校验原模块可反序列化
    marshal.loads(orig_app_bytes)

    # Embed the migration engine so the installed executable has no loose Python
    # module dependency.  The verified wimlib binary remains a separate tool.
    wrapper_src = (open(MIGRATION_SRC, "r", encoding="utf-8").read() + "\n" +
                   open(DRIVER_SRC, "r", encoding="utf-8").read() + "\n" +
                   open(CPU_TELEMETRY_SRC, "r", encoding="utf-8").read() + "\n" +
                   open(SOFTWARE_MANAGER_SRC, "r", encoding="utf-8").read() + "\n" +
                   open(STORAGE_SAFETY_SRC, "r", encoding="utf-8").read() + "\n" +
                   open(WRAPPER_SRC, "r", encoding="utf-8").read())
    b64 = base64.b64encode(orig_app_bytes).decode("ascii")
    final_src = wrapper_src.replace("@@ORIG_B64@@", b64, 1)
    assert "@@ORIG_B64@@" not in final_src, "placeholder not replaced"
    assert len(b64) > 100000, "b64 too small, wrong source?"

    code = compile(final_src, "app.py", "exec")
    new_app_bytes = marshal.dumps(code)
    new_app_c = zlib.compress(new_app_bytes, 9)

    # 重建归档:必须保留原 exe 的 PE 头 + bootloader 前缀,归档从 archive_start 开始
    prefix = data[:arc["archive_start"]]
    blob_area = bytearray()
    new_entries = []
    for e in arc["entries"]:
        if e["name"] == "app" and e["type"] == b"s":
            blob = new_app_c
            ne = dict(e, csize=len(new_app_c), usize=len(new_app_bytes), cflag=1)
        else:
            blob = data[arc["archive_start"] + e["pos"]: arc["archive_start"] + e["pos"] + e["csize"]]
            ne = dict(e)
        ne["new_pos"] = len(blob_area)
        blob_area += blob
        new_entries.append(ne)
    toc_start = len(blob_area)
    for ne in new_entries:
        name_b = ne["name"].encode("utf-8")
        entry_len = 18 + len(name_b) + 1
        toc_entry = struct.pack("!iIIIB", entry_len, ne["new_pos"], ne["csize"], ne["usize"], ne["cflag"])
        toc_entry += ne["type"] + name_b + b"\0"
        pad = entry_len - len(toc_entry)
        if pad > 0:
            toc_entry += b"\0" * pad
        assert len(toc_entry) == entry_len, (len(toc_entry), entry_len)
        blob_area += toc_entry
    toc_len = len(blob_area) - toc_start
    pkg_len = len(blob_area) + 88  # blobs + TOC + cookie
    pylib_b = arc["pylib"].encode()
    assert len(pylib_b) <= 64
    cookie = struct.pack("!8sIIII", MAGIC, pkg_len, toc_start, toc_len, arc["pyver"])
    cookie += pylib_b + b"\0" * (64 - len(pylib_b))
    assert len(cookie) == 88
    out = prefix + bytes(blob_area) + cookie

    open(OUT_EXE, "wb").write(out)
    print("written: %s (%d bytes, prefix %d, orig %d)" % (OUT_EXE, len(out), len(prefix), len(data)))

    # ---- 自校验 ----
    data2 = open(OUT_EXE, "rb").read()
    assert data2[:2] == b"MZ", "PE 头丢失"
    arc2 = parse_archive(data2)
    assert arc2["archive_start"] == len(prefix), "归档起始偏移不一致"
    print("PE 头与 bootloader 前缀完整(MZ 校验通过), archive_start=%d" % arc2["archive_start"])
    app_e = [e for e in arc2["entries"] if e["name"] == "app" and e["type"] == b"s"]
    assert app_e, "app entry missing in patched exe"
    code2 = marshal.loads(read_entry(data2, arc2, app_e[0]))
    names = set()
    def walk(c):
        names.add(c.co_name)
        for k in c.co_consts:
            if hasattr(k, "co_name"):
                walk(k)
    walk(code2)
    for sym in ("_bootstrap", "_install_partition_methods", "partition_disks",
                "partition_job_submit", "partition_migration_plan", "_WindowsMigrationAdapter",
                "fm_software_move", "storage_ai_exclusions_list",
                "storage_ai_exclusions_add", "storage_ai_exclusions_remove",
                "filter_storage_status", "_ai_protected_directory",
                "process_background_windows", "_allow_verified_update_exit"):
        assert sym in names or sym in str(code2.co_consts), "missing symbol %s" % sym
    # 原模块字节码仍内嵌可解码
    consts = [c for c in code2.co_consts if isinstance(c, str) and len(c) > 100000]
    assert consts, "embedded b64 not found"
    inner = marshal.loads(base64.b64decode(consts[0]))
    assert inner.co_name == "<module>"
    print("self-verify OK: app entry replaced, embedded orig module intact, %d entries preserved" % len(arc2["entries"]))
    print("entries:", [(e["type"].decode(), e["name"]) for e in arc2["entries"]])


if __name__ == "__main__":
    main()
