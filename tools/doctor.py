"""Read-only startup diagnostics; never prints provider credentials."""
import importlib.util
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = ROOT / ".vendor" / "hermes-agent"
    expected = json.loads((ROOT / "hermes.lock.json").read_text(encoding="utf-8"))["revision"]
    rev = subprocess.run(["git","-C",str(source),"rev-parse","HEAD"],capture_output=True,text=True)
    modules = {name: importlib.util.find_spec(name) is not None for name in ("fastapi","sqlalchemy","PySide6","playwright","pypdf","docx","openpyxl","mcp")}
    result = {"python":sys.version.split()[0],"platform":platform.platform(),"sqlite":sqlite3.sqlite_version,"modules":modules,"hermesPinned":rev.stdout.strip()==expected,"uiBuilt":(ROOT/"dist/index.html").is_file()}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if all(modules.values()) and result["hermesPinned"] and result["uiBuilt"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
