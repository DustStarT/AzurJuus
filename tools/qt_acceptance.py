"""Render the actual PySide6/QWebEngine desktop shell on isolated local data."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
STATE=ROOT/'.azurjuus'/'qt-acceptance'
STATE.mkdir(parents=True,exist_ok=True)
os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(STATE/'qt.db').as_posix(),AZURJUUS_WORKSPACE_STATE_PATH=str(STATE/'workspace.json'),AZURJUUS_REDIS_URL='',AZURJUUS_CHROMA_URL='',AZURJUUS_SOCIAL_ENABLED='0')
import desktop
from PySide6.QtCore import QTimer


def main():
    out=ROOT/'validation'/'qt'; out.mkdir(parents=True,exist_ok=True)
    desktop.WEB_PROFILE_DIR=STATE/'profile'
    desktop.WEB_CACHE_DIR=STATE/'cache'
    app=desktop.create_application()
    server,thread,url,context=desktop.start_local_server(port=0)
    if not desktop.wait_for_server(url): raise RuntimeError('Local desktop server did not start')
    window=desktop.AzurJuusWindow(url,server,thread,context)
    window.resize(1280,720)
    window.show()
    report={'status':'failed','scale':app.primaryScreen().devicePixelRatio()}
    def inspect():
        window._view.page().runJavaScript("JSON.stringify({title:document.title,messages:document.querySelectorAll('.message-row').length,input:!!document.querySelector('[aria-label=\"消息输入\"]'),height:innerHeight})",finish)
    def finish(value):
        try:
            page=json.loads(value or '{}')
            report.update(page=page,status='passed' if page.get('input') and page.get('messages') else 'failed')
            window.grab().save(str(out/f"desktop-1280-scale-{report['scale']}.png"))
        finally:
            (out/f"report-scale-{report['scale']}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False),flush=True)
            window.close()
    QTimer.singleShot(7000,inspect)
    QTimer.singleShot(30000,app.quit)
    app.exec()
    desktop.stop_local_server(server,thread)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
