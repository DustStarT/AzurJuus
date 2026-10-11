"""Read-only startup diagnostics; never prints provider credentials."""
import importlib.util
import json
from pathlib import Path
import platform
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    modules = {name: importlib.util.find_spec(name) is not None for name in ("fastapi","sqlalchemy","PySide6","playwright","pypdf","pypdfium2","rapidocr_onnxruntime","onnxruntime","docx","openpyxl","pandas","deepseek_harness","deepseek_harness_runtime")}
    result = {"python":sys.version.split()[0],"platform":platform.platform(),"sqlite":sqlite3.sqlite_version,"modules":modules,"engine":"deepseek-harness","uiBuilt":(ROOT/"dist/index.html").is_file()}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if all(modules.values()) and result["uiBuilt"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
