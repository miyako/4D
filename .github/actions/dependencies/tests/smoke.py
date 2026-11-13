"""Run tool4d against a package prepared by the dependencies action and check the loaded components.

usage: smoke.py REPORT TOOL4D PACKAGE COMPONENT...
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def main(report_file, tool4d, package, *expected):
    report = json.loads(Path(report_file).read_text(encoding="utf-8"))
    package = Path(package)
    project = next((package / "Project").glob("*.4DProject"))
    result = package / "result.json"
    result.unlink(missing_ok=True)
    env = dict(os.environ, **report["env"])
    command = [tool4d, f"--project={project}", "--startup-method=probe", "--dataless", "--skip-onstartup"]
    completed = subprocess.run(command, env=env, timeout=300)
    if not result.exists():
        print(f"::error::tool4d exited with {completed.returncode} without writing {result}")
        return 1
    loaded = json.loads(result.read_text(encoding="utf-8"))
    print(json.dumps({"resolved": report["resolved"], "loaded": loaded}, indent=2))
    missing = sorted(set(expected) - set(loaded["components"]))
    if missing:
        print(f"::error::components not loaded: {', '.join(missing)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
