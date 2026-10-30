"""检查某个 LxUltra exe 是否包含分区助手后端包装器(只读，不启动)"""
import marshal
import re
import struct
import sys
import types
import zlib

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
MAGIC = b"MEI\014\013\012\013\016"


def find_archive(data):
    pos = data.rfind(MAGIC)
    if pos < 0:
        raise SystemExit("未找到 PyInstaller cookie")
    _m, pkg_len, toc, toc_len, pyver = struct.unpack("!8sIIII", data[pos:pos + 24])
    start = (pos + 88) - pkg_len
    return start, toc, toc_len, pyver


def read_app(exe):
    data = open(exe, "rb").read()
    start, toc, toc_len, pyver = find_archive(data)
    p = start + toc
    end = p + toc_len
    while p < end:
        (entry_len,) = struct.unpack("!i", data[p:p + 4])
        epos, csize, usize, cflag = struct.unpack("!IIIB", data[p + 4:p + 4 + 13])
        typ = data[p + 17:p + 18]
        name = data[p + 18:p + entry_len].rstrip(b"\0").decode("utf-8", "ignore")
        if name == "app" and typ == b"s":
            raw = data[start + epos:start + epos + csize]
            if cflag:
                raw = zlib.decompress(raw)
            return raw, pyver, len(data)
        p += entry_len
    raise SystemExit("归档里没有 app 条目")


def names(co, out):
    out.update(co.co_names)
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            names(c, out)
        elif isinstance(c, str):
            out.add(c)


failed = False
for exe in sys.argv[1:]:
    print("=" * 70)
    print(exe)
    try:
        raw, pyver, size = read_app(exe)
        co = marshal.loads(raw)
        bag = set()
        names(co, bag)
        has = sorted(x for x in bag if "partition_" in x)
        print("  exe 大小 %d  归档 app 字节 %d  marshal 版本 %s" % (size, len(raw), pyver))
        print("  顶层模块: %s" % co.co_name)
        print("  partition_* 方法: %d 个 %s" % (len(has), has[:10]))
        print("  结论: %s" % ("包含分区助手后端 ✅" if has else "不含分区助手后端 ❌"))
        if not has:
            failed = True
    except SystemExit as e:
        failed = True
        print("  失败:", e)
    except Exception as e:
        failed = True
        print("  解析异常:", type(e).__name__, e)

if __name__ == "__main__":
    sys.exit(1 if failed else 0)
