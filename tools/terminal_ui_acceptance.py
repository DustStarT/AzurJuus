"""Final-layout and serialized short-message checks using isolated fixtures."""
import json
import os
from pathlib import Path
import threading
from uuid import uuid4
os.environ['AZURJUUS_UI_TEST_DIRECTORY']='terminal-ui-'+uuid4().hex
from ui_test_server import build_server, ROOT
from playwright.sync_api import sync_playwright


def main():
    out=ROOT/'validation/terminal-ui'
    out.mkdir(parents=True,exist_ok=True)
    server=build_server(port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    report={'status':'failed','checks':[]}
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            context=browser.new_context(viewport={'width':1600,'height':900},record_video_dir=str(out/'video'))
            page=context.new_page()
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            base='http://127.0.0.1:'+str(server.server_address[1])
            page.goto(base);page.get_by_label('消息输入').wait_for()
            boot=page.request.get(base+'/api/bootstrap').json()['snapshot']
            group=next(c for c in boot['conversations'] if c['kind']=='group')
            page.locator('.conversation-card').filter(has_text=group['title']).click()
            fixture=page.request.post(base+'/__acceptance/terminal').json()
            first,second=[page.locator(f'[data-message-id="{mid}"]') for mid in fixture['messageIds']]
            first.locator('.message-bubble').first.wait_for()
            assert second.locator('.message-bubble').count()==0, 'same-conversation messages burst together'
            first.get_by_role('button',name='立即显示').click()
            second.locator('.message-bubble').first.wait_for()
            page.wait_for_timeout(1600)
            assert first.locator('.message-bubble').count()==2 and second.locator('.message-bubble').count()==2
            report['checks'].append('Conversation queue serializes two speakers; show-now releases the next message')
            page.reload();page.get_by_label('消息输入').wait_for()
            assert page.get_by_role('button',name='立即显示').count()==0
            report['checks'].append('Historical replies do not replay')
            alignment=page.request.post(base+'/__acceptance/alignment').json()
            page.locator('.conversation-card').filter(has_text=alignment['conversationTitle']).click()
            for mid in alignment['messageIds']:
                page.locator(f'[data-message-id="{mid}"] .message-bubble').wait_for()
            for width,height in [(1280,720),(1600,900),(1920,1080)]:
                page.set_viewport_size({'width':width,'height':height})
                page.wait_for_timeout(250)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                positions=page.evaluate('''ids=>ids.map(id=>{
                    const row=document.querySelector('[data-message-id="'+id+'"]');
                    const bubble=row.querySelector('.message-bubble').getBoundingClientRect();
                    const avatar=row.querySelector('.avatar').getBoundingClientRect();
                    const stack=row.querySelector('.speech-stack');
                    const box=row.getBoundingClientRect();
                    return {left:bubble.left,right:bubble.right,avatarLeft:avatar.left,avatarRight:avatar.right,
                      rowLeft:box.left,rowRight:box.right,align:getComputedStyle(stack).alignItems};
                })''',alignment['messageIds'])
                short,agent,long=positions
                assert short['align']==long['align']=='flex-end',positions
                assert abs(short['right']-long['right'])<2,positions
                assert short['avatarLeft']>short['right'] and long['avatarLeft']>long['right'],positions
                assert agent['left']>agent['avatarRight'] and agent['left']<short['left'],positions
                assert abs(short['avatarRight']-short['rowRight'])<2,positions
                assert abs(agent['avatarLeft']-agent['rowLeft'])<2,positions
                page.screenshot(path=str(out/f'alignment-{width}.png'))
            report['checks'].append('Short and wrapped user bubbles share the right edge; agent avatar and bubbles remain on the left at all three widths')
            dm=next(c for c in boot['conversations'] if c['kind']=='dm')
            page.locator('.conversation-card').filter(has_text=dm['title']).click()
            page.screenshot(path=str(out/'private.png'))
            page.get_by_label('朋友圈',exact=True).click()
            page.screenshot(path=str(out/'moments.png'))
            page.get_by_label('打开任务中心',exact=True).click()
            page.screenshot(path=str(out/'work.png'))
            report['performance']=page.evaluate("""() => new Promise(resolve=>{
              const frames=[],longTasks=[],memory=[]; let previous;
              const observer=new PerformanceObserver(l=>longTasks.push(...l.getEntries().map(e=>e.duration)));
              observer.observe({type:'longtask',buffered:false});
              function tick(now){if(previous!==undefined)frames.push(now-previous);previous=now;
                const step=frames.length%120;
                if(step===10)document.querySelector('[aria-label="关闭任务详情"]')?.click();
                if(step===60)document.querySelector('[aria-label="打开任务中心"]')?.click();
                if(step===0)memory.push({nodes:document.querySelectorAll('*').length,heap:performance.memory?.usedJSHeapSize});
                if(frames.length<360)requestAnimationFrame(tick);else{observer.disconnect();resolve({frames,longTasks,memory});}
              }requestAnimationFrame(tick);
            })""")
            frames=sorted(report['performance']['frames'])
            report['frameP95Ms']=round(frames[int(len(frames)*.95)],2)
            report['performanceScope']='360 frames and three drawer cycles; not a long-duration stability test'
            report.update(status='passed',errors=errors)
            assert not errors,errors
            context.close()
            browser.close()
    finally:
        server.shutdown();thread.join(15);server.server_close()
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
