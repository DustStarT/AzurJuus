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
from PySide6.QtCore import QTimer, Qt
from qt_capture import capture_window


def main():
    out=Path(os.environ.get('AZURJUUS_QT_VISUAL_OUTPUT',str(OUT/'qt')))
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
    # Native screen captures otherwise include unrelated windows covering this
    # fixture. Only our temporary test window is kept on top until it closes.
    window.setWindowFlag(Qt.WindowStaysOnTopHint,True)
    window.show()
    window.raise_()
    window.activateWindow()
    report={'status':'running','isolatedData':str(STATE),'renderer':desktop.DESKTOP_RENDERER,'scales':[],
            'scope':'Application zoom in actual QWebEngine; not an OS DPI or sustained performance benchmark'}
    steps=[(scale,view) for scale in (1.0,1.25,1.5) for view in ('private','group','stickers','moments','settings')]
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
            window.__azurjuusAcceptanceFrame=false;
            const click=label=>document.querySelector('[aria-label="'+label+'"]')?.click();
            click('关闭设置');
            document.querySelector('.sticker-picker section header button:last-child')?.click();
            if(VIEW==='settings'){click('打开设置');return;}
            if(VIEW==='moments'){click('朋友圈');return;}
            click('聊天');
            setTimeout(()=>{const rows=[...document.querySelectorAll('.conversation-title strong')];rows.find(e=>e.textContent===TITLE)?.closest('button')?.click();
                if(VIEW==='stickers')setTimeout(()=>click('表情包'),100);},30);
        })()""".replace('VIEW',json.dumps(view)).replace('TITLE',json.dumps(title))
        window._view.page().runJavaScript(script)
        QTimer.singleShot(800,lambda:window._view.page().runJavaScript(
            'requestAnimationFrame(()=>requestAnimationFrame(()=>window.__azurjuusAcceptanceFrame=true))'))
        QTimer.singleShot(1200,lambda:inspect(index,scale,view))
    def inspect(index,scale,view,attempt=0):
        script="""JSON.stringify((()=>{
          const input=document.querySelector('[aria-label="消息输入"]');
          const box=input?.getBoundingClientRect();
          const header=document.querySelector('.topbar').getBoundingClientRect();
          const status=document.querySelector('.topbar-status').getBoundingClientRect();
          const modal=document.querySelector('.settings-modal');
          const scrim=document.querySelector('.modal-scrim');
          const picker=document.querySelector('.sticker-picker section');
          const pr=picker?.getBoundingClientRect();
          const hit=pr&&document.elementFromPoint(pr.left+20,pr.top+20);
          return {width:innerWidth,height:innerHeight,zoom:devicePixelRatio,
            renderer:document.documentElement.dataset.desktopRenderer,
            composerBackdrop:document.querySelector('.composer')&&getComputedStyle(document.querySelector('.composer')).backdropFilter,
            conversationTitle:document.querySelector('.chat-heading h1')?.textContent,
            presentedFrame:window.__azurjuusAcceptanceFrame===true,
            pickerVisible:!!pr&&pr.height>0,
            pickerOnTop:!!hit&&picker.contains(hit),
            pickerFits:!!pr&&pr.top>=0&&pr.bottom<=innerHeight&&pr.right<=innerWidth,
            overflow:document.documentElement.scrollWidth>innerWidth,
            inputVisible:!!box&&box.height>0&&box.bottom<=innerHeight&&box.top>=0,
            headerTop:header.top,controlsReserved:status.right<=innerWidth-130,
            modalVisible:!!modal&&modal.getBoundingClientRect().height>0,
            modalSettled:!!scrim&&getComputedStyle(scrim).opacity==='1'&&getComputedStyle(modal).transform==='none'&&!/modal-(enter|leave)/.test(scrim.className),
            modalFits:!!modal&&modal.getBoundingClientRect().bottom<=innerHeight&&modal.scrollWidth<=modal.clientWidth+1,
            panelTransform:getComputedStyle(document.querySelector('.chat-panel')||document.body).transform,
            postCount:document.querySelectorAll('.moments-card').length};
        })())"""
        def measured(raw):
            try:
                data=json.loads(raw or '{}')
                chat_view=view in ('private','group','stickers')
                title=fixture['dm'] if view=='private' else fixture['group']
                ready=(data.get('modalSettled') if view=='settings' else
                    not data.get('modalVisible') and data.get('panelTransform')=='none' and data.get('conversationTitle')==title if chat_view else data.get('postCount',0)>0)
                ready=ready and data.get('presentedFrame')
                if view=='stickers':ready=ready and data.get('pickerVisible')
                if not ready and attempt<10:
                    QTimer.singleShot(250,lambda:inspect(index,scale,view,attempt+1));return
                data.update(scale=scale,view=view)
                data['passed']=bool(ready) and not data.get('overflow',True) and data.get('controlsReserved',False) and data.get('headerTop',-1)>=0
                data['passed'] &= data.get('renderer')==desktop.DESKTOP_RENDERER
                if chat_view:data['passed'] &= ready and data.get('inputVisible',False)
                if chat_view and desktop.DESKTOP_RENDERER=='software':data['passed'] &= data.get('composerBackdrop')=='none'
                if view=='stickers':data['passed'] &= data.get('pickerOnTop',False) and data.get('pickerFits',False)
                if view=='settings':data['passed'] &= ready and data.get('modalFits',False)
                if view=='moments':data['passed'] &= data.get('postCount',0)>0
                report['scales'].append(data)
                # A JS callback precedes WebEngine's presentation of that frame.
                # Let Qt paint it before taking pixels or changing the next view.
                def capture():
                    # Capture the presented native window, rather than asking
                    # QWidget to re-render cached WebEngine child textures.
                    data.update(capture_window(window,out/f'{view}-zoom-{scale}.png'))
                    if not data['captureUnobscured'] or not data['captureSaved']:
                        data['passed']=False
                        save();finish();return
                    save()
                    QTimer.singleShot(100,lambda:step(index+1))
                QTimer.singleShot(600,capture)
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
