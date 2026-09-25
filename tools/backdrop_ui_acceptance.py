"""Offline checks for per-character backdrop controls and persistence."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation/juus-background-20260925'
os.environ.update(AZURJUUS_UI_TEST_DIRECTORY=tempfile.mkdtemp(prefix='juus-backdrop-'),
                  AZURJUUS_MIND_ENABLED='0',AZURJUUS_EXPRESSION_ENABLED='0')
from ui_test_server import build_server
from playwright.sync_api import sync_playwright, expect


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    server=build_server(port=0)
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    report={'status':'failed','checks':[]}
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            context=browser.new_context(viewport={'width':1280,'height':720})
            page=context.new_page()
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            base=f'http://127.0.0.1:{server.server_address[1]}'
            def offline(route):
                if route.request.url.startswith(base):return route.continue_()
                asset=ROOT/'validation/juus-visual-20260925/assets'/hashlib.sha256(route.request.url.encode()).hexdigest()
                if route.request.resource_type=='image' and asset.is_file():return route.fulfill(body=asset.read_bytes(),content_type='image/png')
                route.abort()
            page.route('**/*',offline)
            page.goto(base)
            page.get_by_label('消息输入',exact=True).wait_for()
            boot=page.request.get(base+'/api/bootstrap').json()['snapshot']
            dms=[c for c in boot['conversations'] if c['kind']=='dm'][:2]
            group=next(c for c in boot['conversations'] if c['kind']=='group')
            def select(room):
                if page.get_by_label('返回会话列表',exact=True).is_visible():page.get_by_label('返回会话列表',exact=True).click()
                page.locator('.conversation-card').filter(has=page.get_by_text(room['title'],exact=True)).click()
            def slider(label,value):
                page.get_by_role('slider',name=label,exact=True).fill(str(value))
            def style():
                return page.locator('.character-backdrop').evaluate('e=>({position:e.style.objectPosition,transform:e.style.transform,origin:e.style.transformOrigin})')
            select(dms[0])
            # A damaged saved value must fall back without preventing startup.
            page.evaluate("localStorage.setItem('azur-chat-background-v1','{')")
            page.reload();page.get_by_label('调整背景',exact=True).wait_for()
            assert style()['position']=='50% 50%'
            page.get_by_label('调整背景',exact=True).click()
            slider('背景缩放',150);slider('水平位置',20);slider('垂直位置',80)
            assert style()=={'position':'20% 80%','transform':'scale(1.5)','origin':'20% 80%'}
            page.get_by_role('slider',name='水平位置').press('ArrowRight')
            assert style()['position']=='21% 80%'
            page.screenshot(path=str(OUT/'position-controls.png'))
            page.get_by_role('button',name='完成',exact=True).click()
            expect(page.get_by_label('调整背景',exact=True)).to_be_focused()
            select(dms[1]);assert style()['position']=='50% 50%'
            page.get_by_label('调整背景',exact=True).click()
            slider('垂直位置',10)
            page.get_by_role('slider',name='垂直位置').press('Escape')
            expect(page.get_by_label('聊天背景设置',exact=True)).to_have_count(0)
            select(dms[0]);assert style()['position']=='21% 80%'
            page.reload();page.get_by_label('调整背景',exact=True).wait_for()
            select(dms[0])
            assert style()['position']=='21% 80%'
            page.get_by_label('调整背景',exact=True).click()
            page.get_by_role('button',name='恢复默认构图',exact=True).click()
            assert style()=={'position':'50% 50%','transform':'scale(1)','origin':'50% 50%'}
            page.get_by_label('消息输入',exact=True).click()
            expect(page.get_by_label('聊天背景设置',exact=True)).to_have_count(0)
            select(dms[1]);assert style()['position']=='50% 10%'
            select(group);expect(page.get_by_label('调整背景',exact=True)).to_have_count(0)
            for width,height in [(820,560),(768,600)]:
                page.set_viewport_size({'width':width,'height':height})
                select(dms[0]);page.get_by_label('调整背景',exact=True).click()
                box=page.get_by_label('聊天背景设置',exact=True).bounding_box()
                panel=page.locator('.chat-panel').bounding_box()
                assert box['x']>=panel['x'] and box['x']+box['width']<=panel['x']+panel['width']+1,box
                assert box['y']+box['height']<=height,box
                slider('背景缩放',200);slider('水平位置',100);slider('垂直位置',0)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(OUT/f'controls-{width}.png'))
                page.get_by_role('button',name='完成',exact=True).click()
            assert not errors,errors
            report.update(status='passed',checks=['Malformed storage fallback','Live position/zoom and keyboard input',
                'Per-character isolation and reload persistence','Reset affects only current character',
                'Outside click and Escape close controls','Group has no portrait controls','820/768 popup bounds and 200% zoom'],browserErrors=errors)
            context.close();browser.close()
    finally:
        server.shutdown();worker.join(timeout=15);server.server_close()
        (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
