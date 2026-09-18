"""Windowless desktop entry: retain diagnostics without a console window."""
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    logs = Path(os.environ.get('AZURJUUS_DESKTOP_LOG_DIR', str(ROOT / '.azurjuus' / 'logs')))
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / 'desktop.log').open('a', encoding='utf-8', buffering=1) as output:
        sys.stdout = sys.stderr = output
        try:
            from desktop import run_check, run_desktop
            return run_check() if '--check' in sys.argv else run_desktop()
        except Exception:
            traceback.print_exc()
            if os.name == 'nt':
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, '启动失败，详情见：' + str(logs / 'desktop.log'), 'AzurJuus', 16)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
