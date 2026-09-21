"""Launch the real VBS desktop on isolated data, inspect windows, and close it."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4
import psutil
import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    home = ROOT / '.azurjuus/windowless-acceptance' / uuid4().hex
    home.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'AZURJUUS_PORT':str(port), 'AZURJUUS_DATABASE_URL':'sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        'AZURJUUS_WORKSPACE_STATE_PATH':str(home/'state/workspace.json'), 'AZURJUUS_WORKSPACE_ROOT':str(home),
        'AZURJUUS_DESKTOP_LOG_DIR':str(home/'logs'), 'AZURJUUS_SOCIAL_ENABLED':'0', 'AZURJUUS_REFLECTION_ENABLED':'0',
        'AZURJUUS_SKILL_TRIALS_ENABLED':'0', 'AZURJUUS_REDIS_URL':'', 'AZURJUUS_CHROMA_URL':''}
    before = set(psutil.pids())
    desktop = None
    children = []
    report = {'entry':'launch_azurjuus.vbs','passed':False}
    user32 = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    def windows():
        found=[]
        @callback
        def visit(hwnd, _):
            pid=wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            if pid.value in {desktop.pid, *(p.pid for p in children)} and user32.IsWindowVisible(hwnd):
                title=ctypes.create_unicode_buffer(256); kind=ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd,title,256); user32.GetClassNameW(hwnd,kind,256)
                found.append({'handle':hwnd,'title':title.value,'class':kind.value})
            return True
        user32.EnumWindows(visit,0)
        return found
    try:
        subprocess.run(['wscript.exe',str(ROOT/'launch_azurjuus.vbs')],env=env,cwd=ROOT,timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,check=True)
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            if desktop is None:
                for p in psutil.process_iter(['pid','name','cmdline']):
                    if p.pid not in before and p.info['name'].lower()=='pythonw.exe' and any('desktop_entry.py' in s for s in p.info['cmdline'] or []):
                        # Match our inherited isolation marker, never an unrelated desktop.
                        if p.environ().get('AZURJUUS_DESKTOP_LOG_DIR')==env['AZURJUUS_DESKTOP_LOG_DIR']:
                            desktop=p
                            break
            if desktop:
                children=desktop.children(recursive=True)
                visible=windows()
                if any(w['title']=='AzurJuus' for w in visible):
                    try:
                        requests.get(f'http://127.0.0.1:{port}/api/bootstrap',timeout=1).raise_for_status()
                        break
                    except requests.RequestException: pass
            time.sleep(.2)
        assert desktop, 'Desktop process was not started'
        visible=windows()
        report['windows']=visible
        assert any(w['title']=='AzurJuus' for w in visible), visible
        assert not any('Console' in w['class'] or w['class']=='CASCADIA_HOSTING_WINDOW_CLASS' for w in visible), visible
        started=time.monotonic()
        for w in visible:
            if w['title']=='AzurJuus': user32.PostMessageW(w['handle'],0x0010,0,0)
        desktop.wait(timeout=10)
        report['shutdownSeconds']=round(time.monotonic()-started,3)
        _, alive=psutil.wait_procs(children,timeout=5)
        report['remainingChildren']=[p.pid for p in alive]
        assert not alive
        report['passed']=True
    finally:
        if desktop and desktop.is_running():
            for w in windows(): user32.PostMessageW(w['handle'],0x0010,0,0)
            try: desktop.wait(timeout=10)
            except psutil.TimeoutExpired:
                for child in desktop.children(recursive=True):
                    try: child.kill()
                    except psutil.NoSuchProcess: pass
                desktop.kill()
        out=ROOT/'validation/windowless'; out.mkdir(parents=True,exist_ok=True)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__': main()
