"""Real API/WebSocket empty-feed status test; isolated data, no model/tool calls."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(tempfile.mkdtemp(prefix='juus-moments-status-'))
OUT = ROOT / 'validation' / 'moments-foreground-20261006'
os.environ.update(AZURJUUS_UI_TEST_DIRECTORY=str(STATE), AZURJUUS_MIND_ENABLED='0',
    AZURJUUS_EXPRESSION_ENABLED='0', AZURJUUS_SOCIAL_ENGINE_ENABLED='0',
    AZURJUUS_CHROMA_PATH=str(STATE / 'chroma'))
sys.path.insert(0, str(ROOT))
from ui_test_server import build_server


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    server = build_server(port=0)
    fixture = {}

    @server.app.post('/__acceptance/moments_foreground')
    async def foreground(payload: dict):
        from fastapi import HTTPException
        from sqlalchemy import select, func
        from backend.database import session_scope
        from backend.models import WorkspaceSetting
        from backend.mind.mind_models import MindCall
        coordinator = server.app.state.runs
        mode = payload.get('mode')
        if mode not in {None, 'chat', 'task'}:
            raise HTTPException(400, 'Fixture mode must be chat, task or null')
        if fixture:
            fixture['release'].set()
            await fixture['job']
            fixture.clear()
        runtime = coordinator.cognition.runtime
        runtime.enabled = True
        runtime.life.quiet = lambda: False
        async def idle(): pass
        async def no_model(*args, **kwargs):
            raise AssertionError('This UI fixture must not call a model')
        runtime.life.tick = idle
        coordinator.moments.tick = idle
        runtime.generate = no_model
        runtime.control({'paused':False, 'momentsEnabled':True})
        with session_scope() as session:
            session.get(WorkspaceSetting, 1).allow_idle_social = True
            snapshot = server.app.state.service.build_snapshot(session)
        if mode:
            actor = snapshot['agents'][0]
            release = asyncio.Event()
            run, _ = coordinator.store.create({'actorId':actor['id'], 'actors':[actor],
                'conversationId':'port-hub', 'mode':mode, 'collaborative':False,
                'prompt':'隔离界面状态占位；不会执行模型或工具。'}, 'ui-status-'+mode+'-'+str(time.time_ns()))
            async def hold(run_id):
                coordinator.store.update(run_id, status='running')
                await release.wait()
                coordinator.store.update(run_id, status='completed')
                # Reproduce final-publication work continuing beyond run.updated.
                await asyncio.sleep(.4)
            coordinator.run = hold
            coordinator.launch(run['id'])
            job = coordinator.tasks[run['id']]
            fixture.update(id=run['id'], release=release, job=job)
        with session_scope() as session:
            count = session.scalar(select(func.count()).select_from(MindCall))
        return {'reason':coordinator.moments.status()['reason'], 'modelCalls':count}

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {'status':'failed', 'isolatedData':str(STATE), 'checks':[]}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width':1280, 'height':800})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{server.server_address[1]}'
            def route(request):
                if request.request.url.startswith(base):request.continue_()
                else:request.abort()
            page.route('**/*', route)
            page.goto(base)
            page.get_by_label('消息输入', exact=True).wait_for()
            expect(page.get_by_text('本地已连接', exact=True)).to_be_visible()
            def state(mode):
                response = page.request.post(base+'/__acceptance/moments_foreground',
                    data={'mode':mode}, headers={'Origin':base})
                assert response.ok, response.text()
                value = response.json()
                assert value['modelCalls']==0, value
                return value
            assert state('chat')['reason']=='responding'
            page.get_by_label('朋友圈', exact=True).click()
            expect(page.get_by_text('人物正在回复消息，自主动态会稍后再考虑。', exact=True)).to_be_visible()
            expect(page.get_by_text('当前有任务进行', exact=False)).to_have_count(0)
            report['checks'].append('chat is replying, not a work task')
            page.screenshot(path=str(OUT/'chat-empty-feed.png'))
            assert state(None)['reason']=='waiting'
            expect(page.get_by_text('人物正在回复消息，自主动态会稍后再考虑。', exact=True)).to_have_count(0)
            report['checks'].append('reply completion refreshes empty-feed status without navigation')
            assert state('task')['reason']=='task_busy'
            expect(page.get_by_text('当前有工作委托正在处理，人物的自主动态会稍后再考虑。', exact=True)).to_be_visible()
            report['checks'].append('real work category has a separate hint')
            assert state(None)['reason']=='waiting'
            expect(page.get_by_text('当前有工作委托正在处理', exact=False)).to_have_count(0)
            report['checks'].append('work completion refreshes status without navigation')
            assert not errors, errors
            report.update(status='passed', browserErrors=errors, realModelCalls=False)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=15)
        server.server_close()
        report['serverStopped'] = not thread.is_alive()
        (OUT/'ui.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status']=='passed' and report['serverStopped'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
