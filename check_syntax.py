"""Check the current UI's JavaScript syntax and local script assets.

Usage: python check_syntax.py [UI directory]
"""

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else __file__).resolve()
    if root.is_file():
        root = root.parent
    index = root / "index.html"
    html = index.read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        print("Node.js was not found; JavaScript syntax could not be checked.")
        return 1

    scripts = []
    for attributes, body in re.findall(r"<script\b([^>]*)>(.*?)</script\s*>", html, re.I | re.S):
        source = re.search(r'\bsrc\s*=\s*["\']([^"\']+)', attributes, re.I)
        if source:
            path = root / source.group(1)
            if not path.is_file():
                print(f"Missing script: {path}")
                return 1
            scripts.append(path)
        elif body.strip():
            scripts.append(body)

    with tempfile.TemporaryDirectory(prefix="lxultra-syntax-") as temp:
        for number, script in enumerate(scripts, 1):
            path = script if isinstance(script, pathlib.Path) else pathlib.Path(temp) / f"inline-{number}.js"
            if isinstance(script, str):
                path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            if result.returncode:
                print(f"JavaScript syntax failed: {path}")
                print(result.stderr)
                return result.returncode

    print(f"JavaScript syntax OK: {len(scripts)} scripts in {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
