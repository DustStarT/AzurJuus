"""Install the pinned local runtime without modifying the system Python."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def run(argv, cwd=ROOT):
    subprocess.run([str(v) for v in argv], cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--skip-ui", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    if not (3,11) <= sys.version_info[:2] <= (3,13):
        raise SystemExit("Hermes requires Python 3.11–3.13. Run this installer with a compatible interpreter.")
    git = shutil.which("git")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not git or (not args.skip_ui and not npm):
        raise SystemExit("Install Git and Node.js 22 LTS before setup.")
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    pin = json.loads((ROOT / "hermes.lock.json").read_text(encoding="utf-8"))
    source = ROOT / ".vendor" / "hermes-agent"
    if not source.exists():
        source.parent.mkdir(exist_ok=True)
        run([git,"init",source])
        run([git,"remote","add","origin","https://github.com/NousResearch/hermes-agent.git"], source)
    head = subprocess.run([git,"rev-parse","HEAD"],cwd=source,capture_output=True,text=True)
    if head.stdout.strip() != pin["revision"]:
        dirty = subprocess.run([git,"status","--porcelain"],cwd=source,capture_output=True,text=True,check=True)
        if dirty.stdout.strip():
            raise SystemExit("Hermes checkout has local edits. Preserve them before changing the pinned revision.")
        run([git,"fetch","--depth","1","origin",pin["revision"]],source)
        run([git,"checkout","--detach",pin["revision"]],source)
    requirements = "requirements-test.txt" if args.test else "requirements.txt"
    constraints = ["-c", "requirements-windows-py312.lock"] if os.name == "nt" and sys.version_info[:2] == (3,12) else []
    run([python,"-m","pip","install",*constraints,"-r",requirements,"-e",str(source)+"[mcp]"])
    run([python,"-m","pip","check"])
    if not args.skip_browser:
        run([python,"-m","playwright","install","chromium"])
    if not args.skip_ui:
        run([npm,"ci"])
        run([npm,"run","build"])
    run([python,"-X","utf8",ROOT / "tools" / "doctor.py"])
    print("Setup complete. Launch launch_azurjuus.bat or .venv/Scripts/python.exe desktop.py")


if __name__ == "__main__":
    main()
