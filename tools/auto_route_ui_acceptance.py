"""Offline browser smoke test for the automatic composer and consent card."""
import asyncio
import os
import sys
import tempfile
import threading
import time
from uuid import uuid4
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ['AZURJUUS_TEST_BUILD']).resolve()
if not (BUILD / 'index.html').is_file():
    raise SystemExit('AZURJUUS_TEST_BUILD must point to a built frontend.')


async def check(front, backend, conversation_title, group_title, out):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={'width':1280, 'height':800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        async def proxy(route):
            parsed = urlsplit(route.request.url)
            if parsed.path.startswith(('/api/', '/resources/')):
                response = await route.fetch(url=backend + parsed.path +
                    ('?' + parsed.query if parsed.query else ''),
                    headers={**route.request.headers, 'origin':backend})
                await route.fulfill(response=response)
            else:
                await route.continue_()

        await page.route('**/*', proxy)
        await page.goto(front, wait_until='networkidle')
        await page.get_by_text(conversation_title, exact=True).first.click()
        controls=[page.get_by_label('图片和文件'), page.get_by_role('button',name='表情包'),
            page.get_by_label('处理方式：自动判断')]
        positions=[await control.bounding_box() for control in controls]
        assert all(box and abs(box['y']-positions[0]['y']) < 4 for box in positions), positions
        await controls[0].click()
        await expect(page.get_by_text('选择图片或文件')).to_be_visible()
        await controls[0].click()
        await controls[1].click()
        await expect(page.get_by_label('选择表情包')).to_be_visible()
        await controls[1].click()
        await expect(page.locator('.inline-task')).to_have_count(0)
        await expect(page.get_by_label('处理方式：自动判断')).to_be_visible()
        await expect(page.get_by_text('协作确认', exact=True)).to_be_visible()
        await page.get_by_label('处理方式：自动判断').click()
        await expect(page.get_by_role('button', name='闲聊', exact=True)).to_be_visible()
        await page.get_by_role('button', name='取消', exact=True).click()
        await expect(page.get_by_text('已取消', exact=True)).to_be_visible()
        await page.screenshot(path=str(out/'composer.png'))
        await page.get_by_text(group_title, exact=True).first.click()
        await expect(page.locator('.welcome-note h3')).to_have_text('群聊')
        await page.screenshot(path=str(out/'group.png'))
        assert not errors, errors
        await page.unroute_all(behavior='wait')
        await browser.close()


def main():
    isolated = Path(tempfile.mkdtemp(prefix='azur-route-ui-'))
    os.environ['AZURJUUS_UI_TEST_DIRECTORY'] = str(isolated / 'data')
    os.environ['AZURJUUS_MIND_ENABLED'] = '0'
    os.environ['AZURJUUS_EXPRESSION_ENABLED'] = '0'
    sys.path.insert(0, str(ROOT / 'tools'))
    from ui_test_server import build_server
    from backend.database import session_scope
    from backend.models import Conversation, Message
    from backend.chat.intent_router import IntentRouter

    backend = build_server(port=0)
    coordinator = backend.app.state.runs
    service = coordinator.social_engine.service
    with session_scope() as session:
        service.ensure_seed(session)
        snapshot = service.build_snapshot(session)
        room_info = next(c for c in snapshot['conversations'] if c['kind'] == 'dm')
        group_info = next(c for c in snapshot['conversations'] if c['kind'] == 'group')
        assert not snapshot['messages'].get(group_info['id'])
        actor_ids = [a['id'] for a in snapshot['agents']]
        participants = ([aid for aid in actor_ids if aid in room_info['memberIds']][:1]
            + [aid for aid in actor_ids if aid not in room_info['memberIds']][:1])
        room = session.get(Conversation, room_info['id'])
        service._append_message(session, room, 'commander', 'route_proposal', '请一起整理资料',
            metadata={'routeKind':'swarm', 'routeProposal':{'requestId':'ui-route-proposal',
                'actorIds':participants, 'summary':'请一起整理资料', 'status':'proposed'}},
            message_id='route-proposal-ui')
    IntentRouter(coordinator.store, coordinator.expression, coordinator.settings_loader).save(
        'ui-route-proposal', room_info['id'], {'kind':'swarm','actorIds':participants,
            'summary':'请一起整理资料','content':'请一起整理资料','attachments':[],
            'at':time.time()}, 'proposed')
    completed,_=coordinator.store.create({'actorId':participants[0],
        'actors':[next(a for a in snapshot['agents'] if a['id']==participants[0])],
        'mode':'task','collaborative':False,'conversationId':room_info['id'],
        'prompt':'已完成的界面验收任务','history':[]},uuid4().hex)
    coordinator.store.update(completed['id'],status='completed')
    static = ThreadingHTTPServer(('127.0.0.1',0),
        partial(SimpleHTTPRequestHandler, directory=str(BUILD)))
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
        for server in (backend, static)]
    for thread in threads:
        thread.start()
    try:
        asyncio.run(check(f'http://127.0.0.1:{static.server_address[1]}',
            f'http://127.0.0.1:{backend.server_address[1]}', room_info['title'], group_info['title'], isolated))
    finally:
        for server in (backend, static):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=10)
    print('Auto route UI acceptance passed:', isolated)


if __name__ == '__main__':
    main()
