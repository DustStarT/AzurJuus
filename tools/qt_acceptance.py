"""Render the actual PySide6/QWebEngine desktop shell on isolated local data."""
import json
import os
from pathlib import Path
import sys
import time

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
    def measure():
        window._view.page().runJavaScript("""(()=>{window.__perf={frames:[],longTasks:[]};let last;const until=performance.now()+3500;function frame(t){if(last)window.__perf.frames.push(t-last);last=t;if(t<until)requestAnimationFrame(frame)}requestAnimationFrame(frame);try{new PerformanceObserver(l=>window.__perf.longTasks.push(...l.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:true})}catch{}const el=document.querySelector('.chat-panel');if(el)el.animate([{opacity:.85,transform:'translateY(6px)'},{opacity:1,transform:'none'}],{duration:220});})()""")
    def inspect():
        window._view.page().runJavaScript("JSON.stringify({title:document.title,messages:document.querySelectorAll('.message-row').length,input:!!document.querySelector('[aria-label=\"消息输入\"]'),height:innerHeight,performance:window.__perf})",finish)
    def finish(value):
        try:
            page=json.loads(value or '{}')
            frames=sorted(page.get('performance',{}).get('frames',[]))
            report['frameP95Ms']=round(frames[min(len(frames)-1,int(len(frames)*.95))],2) if frames else None
            report['performanceScope']='3.5 seconds foreground rendering sample, not sustained chat workload'
            report.update(page=page,status='passed' if page.get('input') and page.get('messages') else 'failed')
            window.grab().save(str(out/f"desktop-1280-scale-{report['scale']}.png"))
        finally:
            (out/f"report-scale-{report['scale']}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False),flush=True)
            report['closeStarted']=time.monotonic()
            window.close()
    QTimer.singleShot(2500,measure)
    QTimer.singleShot(7000,inspect)
    QTimer.singleShot(30000,app.quit)
    app.exec()
    desktop.stop_local_server(server,thread)
    report['shutdownSeconds']=round(time.monotonic()-report.pop('closeStarted',time.monotonic()),3)
    report['serverStopped']=not thread.is_alive()
    (out/f"report-scale-{report['scale']}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
