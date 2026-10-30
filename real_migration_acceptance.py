"""One-shot acceptance of the specifically authorized C: to L: migration.

Requires --execute. The disk, partition, and stable identifiers below are pinned to
the user's already selected target. No other target is accepted.
"""
import base64
import marshal
import pathlib
import sys
import time


def main():
    if sys.argv[1:] != ["--execute"]:
        raise SystemExit("Use --execute for the authorized C: to L: run")
    root = pathlib.Path(__file__).resolve().parent
    toy = marshal.dumps(compile("class API: pass", "toy.py", "exec"))
    src = ((root / "migration_engine.py").read_text(encoding="utf-8") + "\n" +
           (root / "app_wrapper.py").read_text(encoding="utf-8"))
    src = src.replace("@@ORIG_B64@@", base64.b64encode(toy).decode("ascii"), 1)
    namespace = {"__name__": "real_migration_acceptance_wrapper"}
    exec(compile(src, "app_wrapper_real_acceptance.py", "exec"), namespace)
    # The packaged app resolves its verified wimlib beside its executable.
    # This source-run acceptance must resolve the same installed tool location.
    sys.executable = r"D:\LxUltra\LxUltra_健康与磁盘优化修复版.exe"

    class API:
        pass

    namespace["_install_partition_methods"](API)
    api = API()
    plan = api.partition_migration_plan(0, 19, "A")
    if not plan.get("ok"):
        raise RuntimeError("Preview failed: " + str(plan.get("error")))
    data = plan["plan"]
    source, target = data["source"], data["target"]
    if (source.get("disk"), source.get("letter"), source.get("disk_unique_id")) != (
            1, "C", "eui.6479A74B502010EF"):
        raise RuntimeError("Source disk no longer matches the approved C:")
    if (target.get("disk"), target.get("part"), target.get("letter"),
            target.get("disk_unique_id"), target.get("part_guid")) != (
            0, 19, "L", "eui.6479A74E40200392",
            "{830ee0e8-9330-4812-a85e-40aa91015d84}"):
        raise RuntimeError("Target partition no longer matches the approved L:")
    cloud = plan.get("cloud_files") or {}
    if cloud.get("count") or cloud.get("manual_count"):
        raise RuntimeError("Cloud placeholders found; stop before modifying L:")
    reserved = plan.get("reserved_names") or []
    if len(reserved) != 2 or any(item.get("size") != 0 or
                                 not item.get("relative", "").lower().endswith("\\nul")
                                 for item in reserved):
        raise RuntimeError("Reserved-name exclusion differs from the reviewed two empty NUL entries")
    print("PREVIEW OK source=C: disk 1; target=L: disk 0 part 19; scratch=A:", flush=True)
    print("Excluding exactly two zero-byte NUL entries from the image", flush=True)
    submit = api.partition_job_submit([{
        "type": "migrate_system", "disk": 0, "part": 19, "token": plan["token"],
        "confirmed": True, "download_cloud_files": False, "skip_reserved_names": True,
    }])
    if not submit.get("ok"):
        raise RuntimeError("Submit failed: " + str(submit.get("error")))
    job_id = submit["job_id"]
    print("JOB", job_id, flush=True)
    last = None
    last_print = 0
    while True:
        status = api.partition_job_status(job_id)
        if not status.get("ok"):
            raise RuntimeError("Status failed: " + str(status.get("error")))
        marker = (status.get("state"), status.get("phase"), status.get("percent"),
                  status.get("detail"))
        now = time.monotonic()
        if marker != last or now - last_print >= 30:
            print("STATUS", status.get("state"), status.get("phase"),
                  status.get("percent"), status.get("detail") or status.get("progress"), flush=True)
            last, last_print = marker, now
        if status.get("state") in ("done", "error", "cancelled"):
            for item in status.get("results") or []:
                print("RESULT", item.get("ok"), item.get("message"), flush=True)
            return 0 if status.get("state") == "done" and all(
                item.get("ok") for item in status.get("results") or []) else 1
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
