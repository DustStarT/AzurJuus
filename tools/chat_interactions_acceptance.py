"""Real browser regression for compact chat UI; synthetic data, no model calls."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    home = Path(tempfile.mkdtemp(prefix='azur-chat-interactions-'))
    (home / 'work').mkdir()
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home / 'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home / 'state/workspace.json'),
        AZURJUUS_WORKSPACE_ROOT=str(home / 'work'), AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='',
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='0', AZURJUUS_EXPRESSION_ENABLED='0',
        AZURJUUS_REFLECTION_ENABLED='0', AZURJUUS_SKILL_TRIALS_ENABLED='0')
    from server import create_server
    from starlette.responses import JSONResponse
    server = create_server('127.0.0.1', 0)
    control = {'fail': False}

    @server.app.middleware('http')
    async def delayed_send(request, call_next):
        if request.url.path == '/api/messages/send':
            await asyncio.sleep(2)
            if control['fail']:
                control['fail'] = False
                return JSONResponse({'detail': '合成网络故障'}, status_code=503)
        return await call_next(request)

    @server.app.post('/__acceptance/seed')
    async def seed():
        from backend.database import session_scope
        c = server.app.state.runs
        c.launch = lambda *_: None
        with session_scope() as session:
            data = server.app.state.service.build_snapshot(session)
        dm = next(r for r in data['conversations'] if r['kind'] == 'dm')
        actor = next(a for a in data['agents'] if a['id'] in dm['memberIds'])
        return {'title': dm['title'], 'cid': dm['id'], 'actorId': actor['id']}

    @server.app.post('/__acceptance/fail-next')
    async def fail_next():
        control['fail'] = True
        return {'ok': True}

    @server.app.post('/__acceptance/speech')
    async def speech(payload: dict):
        from backend.database import session_scope
        from backend.models import Conversation
        service, c = server.app.state.service, server.app.state.runs
        delivery = payload.get('delivery', 'social')
        run, _ = c.store.create({'conversationId': payload['cid'], 'actorId': payload['actorId'],
            'actors': [], 'mode': 'chat', 'collaborative': False, 'prompt': '合成消息投递'}, uuid4().hex)
        if delivery == 'stream':
            ids = ['ui-paced-' + uuid4().hex for _ in range(2)]
            async def stream():
                for mid, paragraphs in zip(ids, [['流式第一段。', '\n\n流式第二段。', '\n\n流式第三段。'], ['下一条流式消息。']]):
                    envelope = {'messageId': mid, 'actorId': payload['actorId'], 'assignmentId': 'chat'}
                    c.store.event(run['id'], 'message.start', envelope)
                    body = ''
                    for delta in paragraphs:
                        await asyncio.sleep(.1)
                        body += delta
                        c.store.event(run['id'], 'message.delta', {**envelope, 'delta': delta})
                    with session_scope() as session:
                        service._append_message(session, session.get(Conversation, payload['cid']), payload['actorId'], 'text', body,
                            message_id=mid, metadata={'expression': True})
                    c.store.event(run['id'], 'conversation.changed', {'conversationId': payload['cid']})
                    c.store.event(run['id'], 'message.complete', {**envelope, 'text': body, 'expression': True})
            asyncio.create_task(stream())
            return {'ids': ids}
        ids = []
        for text in ['第一段。' + '这是用于验收长回答逐条出现的合成内容。' * 12 + '\n\n第二段单独弹出。\n\n第三段最后弹出。', '另一条消息也应当排队出现。']:
            mid = 'ui-paced-' + uuid4().hex
            with session_scope() as session:
                service._append_message(session, session.get(Conversation, payload['cid']), payload['actorId'], 'text', text,
                    message_id=mid, metadata={'expression': True})
            if delivery == 'complete':
                c.store.event(run['id'], 'conversation.changed', {'conversationId': payload['cid']})
                c.store.event(run['id'], 'message.complete', {'messageId': mid, 'actorId': payload['actorId'],
                    'assignmentId': 'chat', 'text': text, 'expression': True})
            elif delivery in {'snapshot', 'late'}:
                c.store.event(None, 'workspace.changed', {})
            else:
                c.store.event(None, 'social.message', {'messageId': mid})
            ids.append(mid)
        if delivery == 'late':
            async def notify_later():
                await asyncio.sleep(.8)
                for mid in ids:
                    c.store.event(None, 'social.message', {'messageId': mid})
            asyncio.create_task(notify_later())
        return {'ids': ids}

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_address[1]}'
    out = ROOT / 'validation/chat-interactions'
    out.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'checks': []}
    try:
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 800})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/stickers', lambda route: route.fulfill(json={'stickers': []}))
            for _ in range(100):
                try:
                    if page.request.get(url + '/api/health').ok:
                        break
                except Exception:
                    pass
                time.sleep(.1)
            info = page.request.post(url + '/__acceptance/seed').json()
            page.goto(url)
            page.get_by_text(info['title'], exact=True).first.click()
            expect(page.locator('.topbar-status')).to_have_count(0)
            expect(page.get_by_label('查看工作记录', exact=True)).to_have_count(0)
            for label in ['图片和文件', '表情包', '处理方式：自动判断', '图片和文件']:
                page.get_by_label(label, exact=True).click()
                expect(page.locator('.composer-tools .composer-tool-panel')).to_have_count(1)
                expect(page.locator('.composer-tools [aria-expanded="true"]')).to_have_count(1)
            page.keyboard.press('Escape')
            expect(page.locator('.composer-tool-panel')).to_have_count(0)
            page.get_by_label('图片和文件', exact=True).click()
            page.locator('.chat-heading').click()
            expect(page.locator('.composer-tool-panel')).to_have_count(0)
            report['checks'].append('redundant header controls removed; tools are mutually exclusive and close on Escape/outside click')

            listing = page.locator('.conversation-panel')
            before = listing.bounding_box()['width']
            divider = page.get_by_role('separator', name='调整会话列表宽度')
            box = divider.bounding_box()
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + 100)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] / 2 + 75, box['y'] + 100, steps=6)
            page.mouse.up()
            assert abs(listing.bounding_box()['width'] - before - 75) < 3
            field = page.get_by_label('消息输入', exact=True)
            height = field.bounding_box()['height']
            grip = page.get_by_role('separator', name='调整输入框高度').bounding_box()
            page.mouse.move(grip['x'] + grip['width'] / 2, grip['y'] + 4)
            page.mouse.down()
            page.mouse.move(grip['x'] + grip['width'] / 2, grip['y'] - 76, steps=6)
            page.mouse.up()
            assert field.bounding_box()['height'] >= height + 75
            dimensions = (listing.bounding_box()['width'], field.bounding_box()['height'])
            page.reload()
            field.wait_for()
            assert abs(listing.bounding_box()['width'] - dimensions[0]) < 3
            assert abs(field.bounding_box()['height'] - dimensions[1]) < 3
            report['checks'].append('pointer resizing adjusts list/main split and input height; dimensions persist after reload')

            page.get_by_label('查看成员资料', exact=True).click()
            page.get_by_role('button', name='方法与成长', exact=True).first.click()
            expect(page.locator('.member-popover')).to_have_count(0)
            page.get_by_role('button', name='经历与关系', exact=True).click()
            expect(page.get_by_role('heading', name='生活记录', exact=True)).to_be_visible()
            expect(page.get_by_role('heading', name='关系与共同经历', exact=True)).to_have_count(0)
            page.screenshot(path=str(out / 'experience-drawer.png'))
            page.get_by_label('关闭方法与成长', exact=True).click()
            report['checks'].append('experience drawer has consistent controls and separates records from relationship editing')

            page.locator('.composer-mode > button').click()
            page.get_by_role('button', name='闲聊', exact=True).click()
            field.fill('立即显示的发送测试')
            started = time.monotonic()
            page.get_by_label('发送消息', exact=True).click()
            expect(page.locator('.message-row--self').filter(has_text='立即显示的发送测试')).to_be_visible(timeout=600)
            report['optimisticDisplayMs'] = round((time.monotonic() - started) * 1000)
            expect(page.get_by_text('发送中…', exact=True)).to_be_visible()
            field.fill('发送期间的新草稿')
            expect(page.get_by_text('发送中…', exact=True)).to_have_count(0, timeout=8000)
            expect(field).to_have_value('发送期间的新草稿')
            page.wait_for_timeout(500)
            expect(page.locator('.message-row--self').filter(has_text='立即显示的发送测试')).to_have_count(1)
            page.request.post(url + '/__acceptance/fail-next')
            field.fill('失败后仍可重试')
            page.get_by_label('发送消息', exact=True).click()
            expect(page.get_by_text('发送失败', exact=True)).to_be_visible(timeout=8000)
            page.get_by_role('button', name='重试', exact=True).click()
            expect(page.get_by_text('发送失败', exact=True)).to_have_count(0)
            expect(page.get_by_text('发送中…', exact=True)).to_have_count(0, timeout=8000)
            page.wait_for_timeout(500)
            expect(page.locator('.message-row--self').filter(has_text='失败后仍可重试')).to_have_count(1)
            report['checks'].append('messages render before a delayed send response; next draft survives confirmation; failed retry produces one confirmed message')
            page.screenshot(path=str(out / 'resized-chat.png'))

            page.evaluate("""() => {
              window.__bubbleLog = [];
              const seen = new WeakSet();
              new MutationObserver(() => {
                for(const row of document.querySelectorAll('[data-message-id^="ui-paced-"]')) {
                  [...row.querySelectorAll('.message-bubble')].forEach((element,index) => {
                    if(seen.has(element)) return;
                    seen.add(element);
                    const style=getComputedStyle(element);
                    const item={id:row.dataset.messageId,index,at:performance.now(),opacity:Number(style.opacity),
                      animation:style.animationName,activeAnimations:element.getAnimations().length};
                    window.__bubbleLog.push(item);
                    setTimeout(()=>{item.laterOpacity=Number(getComputedStyle(element).opacity)},100);
                  });
                }
              }).observe(document.querySelector('.message-scroll'), {childList:true,subtree:true});
            }""")
            result = page.request.post(url + '/__acceptance/speech', data={**info, 'delivery': 'complete'}).json()
            first = page.locator(f'[data-message-id="{result["ids"][0]}"]')
            second = page.locator(f'[data-message-id="{result["ids"][1]}"]')
            expect(first.locator('.message-bubble')).to_have_count(1, timeout=5000)
            expect(second).to_be_hidden()
            page.wait_for_timeout(800)
            expect(first.locator('.message-bubble')).to_have_count(1)
            expect(first.locator('.message-bubble')).to_have_count(2, timeout=4000)
            expect(first.locator('.message-bubble')).to_have_count(3, timeout=4000)
            expect(second).to_be_visible(timeout=4000)
            animation = first.locator('.message-bubble').first.evaluate('(e)=>getComputedStyle(e).animationName')
            assert 'speech-in' in animation
            samples = page.evaluate('window.__bubbleLog')
            own = [sample for sample in samples if sample['id'] == result['ids'][0]]
            report['bubbleIntervalsMs'] = [round(own[i]['at'] - own[i-1]['at']) for i in range(1, len(own))]
            assert len(own) == 3 and all(gap >= 1850 for gap in report['bubbleIntervalsMs'])
            assert all(sample['activeAnimations'] and sample['opacity'] < sample['laterOpacity'] for sample in own)
            report['checks'].append('actual message.complete delivery queues bubbles; sampled browser frames confirm opacity animation and two-second intervals')
            page.screenshot(path=str(out / 'paced-bubbles.png'))
            for delivery in ['snapshot', 'late', 'stream']:
                result_extra = page.request.post(url + '/__acceptance/speech', data={**info, 'delivery': delivery}).json()
                extra = page.locator(f'[data-message-id="{result_extra["ids"][0]}"]')
                expect(extra.locator('.message-bubble')).to_have_count(1, timeout=5000)
                page.wait_for_timeout(900)
                expect(extra.locator('.message-bubble')).to_have_count(1)
                expect(extra.locator('.message-bubble')).to_have_count(3, timeout=6000)
                expect(page.locator(f'[data-message-id="{result_extra["ids"][1]}"]')).to_be_visible(timeout=4000)
            report['checks'].append('snapshot-only arrivals, delayed duplicate notices and completed streams preserve pacing without replay or final flush')
            page.reload()
            expect(first.locator('.message-bubble')).to_have_count(3, timeout=5000)
            expect(second).to_be_visible()
            page.set_viewport_size({'width': 900, 'height': 620})
            page.wait_for_timeout(300)
            assert page.locator('.chat-panel').bounding_box()['width'] >= 320
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            report['checks'].append('history opens immediately; smaller windows retain usable chat width without horizontal overflow')
            report['status'] = 'passed'
            browser.close()
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        server.shutdown()
        thread.join(20)
        server.server_close()
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
