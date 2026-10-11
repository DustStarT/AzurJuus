"""Install the local runtime without modifying the system Python."""
from pathlib import Path
import argparse
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
        raise SystemExit("AzurJuus requires Python 3.11-3.13. Run this installer with a compatible interpreter.")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not args.skip_ui and not npm:
        raise SystemExit("Install Node.js 22 LTS before setup.")
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    requirements = "requirements-test.txt" if args.test else "requirements.txt"
    constraints = ["-c", "requirements-windows-py312.lock"] if os.name == "nt" and sys.version_info[:2] == (3,12) else []
    run([python,"-m","pip","install",*constraints,"-r",requirements])
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
