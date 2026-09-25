"""Actual QWebEngine visual check at three application zoom levels.

Uses the visual suite's fresh temporary database; opens only its own test window.
No model credentials or user workspace data are loaded.
"""
import json
import os
import sys
from urllib.request import build_opener, HTTPCookieProcessor, Request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_ui_acceptance import STATE, OUT, install_fixture
os.environ['AZURJUUS_WORKSPACE_ROOT'] = str(STATE / 'workspace')
import desktop
from PySide6.QtCore import QTimer


def main():
    out=OUT/'qt'
    out.mkdir(parents=True,exist_ok=True)
    desktop.WEB_PROFILE_DIR=STATE/'profile'
    desktop.WEB_CACHE_DIR=STATE/'cache'
    app=desktop.create_application()
    server,thread,url,context=desktop.start_local_server(port=0)
    install_fixture(server)
    if not desktop.wait_for_server(url):
        raise RuntimeError('Isolated desktop server failed to start')
    client=build_opener(HTTPCookieProcessor())
    client.open(url).read()
    fixture=json.loads(client.open(Request(url+'/__acceptance/visual',data=b'{}',
        headers={'Content-Type':'application/json','Origin':url})).read())
    window=desktop.AzurJuusWindow(url,server,thread,context)
    window.resize(1280,720)
    window.show()
    report={'status':'running','isolatedData':str(STATE),'scales':[],
            'scope':'Application zoom in actual QWebEngine; not an OS DPI or sustained performance benchmark'}
    steps=[(scale,view) for scale in (1.0,1.25,1.5) for view in ('private','group','moments','settings')]
    def save():
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def finish():
        report['status']='passed' if len(report['scales'])==len(steps) and all(r['passed'] for r in report['scales']) else 'failed'
        save()
        window.close()
        QTimer.singleShot(3000,app.quit)
    def step(index=0):
        if index>=len(steps):
            finish();return
        scale,view=steps[index]
        window._view.setZoomFactor(scale)
        title=fixture['dm'] if view=='private' else fixture['group']
        script="""(()=>{
            const click=label=>document.querySelector('[aria-label="'+label+'"]')?.click();
            click('关闭设置');
            if(VIEW==='settings'){click('打开设置');return;}
            if(VIEW==='moments'){click('朋友圈');return;}
            click('聊天');
            setTimeout(()=>{const rows=[...document.querySelectorAll('.conversation-title strong')];rows.find(e=>e.textContent===TITLE)?.closest('button')?.click();},30);
        })()""".replace('VIEW',json.dumps(view)).replace('TITLE',json.dumps(title))
        window._view.page().runJavaScript(script)
        QTimer.singleShot(1200,lambda:inspect(index,scale,view))
    def inspect(index,scale,view):
        script="""JSON.stringify((()=>{
          const input=document.querySelector('[aria-label="消息输入"]');
          const box=input?.getBoundingClientRect();
          const header=document.querySelector('.topbar').getBoundingClientRect();
          const status=document.querySelector('.topbar-status').getBoundingClientRect();
          const modal=document.querySelector('.settings-modal');
          return {width:innerWidth,height:innerHeight,zoom:devicePixelRatio,
            overflow:document.documentElement.scrollWidth>innerWidth,
            inputVisible:!!box&&box.height>0&&box.bottom<=innerHeight&&box.top>=0,
            headerTop:header.top,controlsReserved:status.right<=innerWidth-130,
            modalFits:!modal||(modal.getBoundingClientRect().bottom<=innerHeight&&modal.scrollWidth<=modal.clientWidth+1),
            postCount:document.querySelectorAll('.moments-card').length};
        })())"""
        def measured(raw):
            try:
                data=json.loads(raw or '{}')
                data.update(scale=scale,view=view)
                data['passed']=not data.get('overflow',True) and data.get('controlsReserved',False) and data.get('headerTop',-1)>=0
                if view in ('private','group'):data['passed'] &= data.get('inputVisible',False)
                if view=='settings':data['passed'] &= data.get('modalFits',False)
                if view=='moments':data['passed'] &= data.get('postCount',0)>0
                report['scales'].append(data)
                window.grab().save(str(out/f'{view}-zoom-{scale}.png'))
                save()
                QTimer.singleShot(100,lambda:step(index+1))
            except Exception as error:
                report['error']=str(error);finish()
        window._view.page().runJavaScript(script,measured)
    QTimer.singleShot(4000,step)
    QTimer.singleShot(60000,finish)
    app.exec()
    desktop.stop_local_server(server,thread)
    report['serverStopped']=not thread.is_alive()
    save()
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['status']=='passed' and report['serverStopped'] else 1


if __name__=='__main__':
    raise SystemExit(main())
