# -*- coding: utf-8 -*-
"""Pure validation contracts for destructive and clone operations; no disks are touched."""
import base64
import marshal
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
WRAP = open(os.path.join(HERE, "app_wrapper.py"), encoding="utf-8").read()
toy = "class API:\n    pass\n"
src = WRAP.replace("@@ORIG_B64@@", base64.b64encode(marshal.dumps(compile(toy, "app.py", "exec"))).decode(), 1)
ns = {"__name__": "backend_contract_test"}
exec(compile(src, "app_wrapper_backend_contract_test.py", "exec"), ns)


class API:
    pass


ns["_install_partition_methods"](API)
api = API()
cases = [
    ("克隆分区必须确认目标镜像", [{"type": "clone_partition", "source_disk": 2, "source_part": 2,
        "source_letter": "D", "disk": 3, "part": 2, "dest_letter": "E"}], "确认窗口"),
    ("克隆硬盘必须确认目标镜像", [{"type": "clone_disk", "source": 2, "disk": 3}], "确认窗口"),
    ("克隆硬盘拒绝源目标相同", [{"type": "clone_disk", "source": 2, "disk": 2, "confirmed": True}], "不能相同"),
    ("克隆分区拒绝源目标相同", [{"type": "clone_partition", "source_disk": 2, "source_part": 2,
        "source_letter": "D", "disk": 2, "part": 2, "dest_letter": "D", "confirmed": True}], "不能相同"),
    ("设置卷标要求可定位分区", [{"type": "set_label", "letter": "D", "label": "test"}], "磁盘编号"),
    ("系统迁移保留名确认必须为布尔值", [{"type": "migrate_system", "disk": 0, "part": 15,
        "token": "preview-token", "confirmed": True, "skip_reserved_names": "yes"}], "保留名"),
]
failed = []
for name, ops, message in cases:
    result = api.partition_job_submit(ops)
    passed = result.get("ok") is False and message in result.get("error", "")
    print(("PASS " if passed else "FAIL ") + name + (" | " + result.get("error", "")))
    if not passed:
        failed.append(name)
print("\n======== 后端契约: %d 通过 / %d 失败 ========" % (len(cases) - len(failed), len(failed)))
sys.exit(1 if failed else 0)
