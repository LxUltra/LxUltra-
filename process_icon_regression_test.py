"""Regression for process-icon path normalization without widening the allowlist."""
import base64
import marshal
import os
from pathlib import Path
import tempfile
import threading


def main():
    here = Path(__file__).resolve().parent
    source = (here / "app_wrapper.py").read_text(encoding="utf-8")
    toy = r'''
class API:
    def process_icons(self, paths):
        allowed = getattr(self, "_process_icon_allowed", set())
        return {path: "data:image/png;base64,icon" for path in paths if path in allowed}
'''
    source = source.replace("@@ORIG_B64@@", base64.b64encode(
        marshal.dumps(compile(toy, "app.py", "exec"))).decode("ascii"), 1)
    namespace = {"__name__": "process_icon_regression_test"}
    exec(compile(source, "app_wrapper_process_icon_test.py", "exec"), namespace)

    class API:
        def __init__(self, allowed):
            self._process_icon_allowed = {allowed}
            self._process_icon_lock = threading.RLock()

        def process_icons(self, paths):
            allowed = self._process_icon_allowed
            return {path: "data:image/png;base64,icon" for path in paths if path in allowed}

    namespace["_install_partition_methods"](API)
    with tempfile.TemporaryDirectory() as temp:
        executable = Path(temp) / "sample.exe"
        executable.write_bytes(b"icon test placeholder")
        raw = str(Path(temp) / "unused" / ".." / "sample.exe")
        canonical = os.path.normcase(os.path.realpath(str(executable)))
        api = API(canonical)
        result = api.process_icons([raw])
        assert result.get(raw) == "data:image/png;base64,icon", result

        other = Path(temp) / "other.exe"
        other.write_bytes(b"not in current process snapshot")
        assert api.process_icons([str(other)]) == {}
    print("PASS process icon paths are normalized while non-snapshot executables stay blocked")


if __name__ == "__main__":
    main()
