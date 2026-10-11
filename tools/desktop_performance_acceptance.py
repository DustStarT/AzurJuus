"""Measure glass costs in an isolated, real QWebEngine window; no model calls.

Both variants use the same renderer and data. The control clears only the page
material marker, restoring full glass; the treatment uses the desktop marker.
Frame callbacks measure scheduling, not guaranteed presentation of every pixel.
"""
import json
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.request import build_opener, HTTPCookieProcessor, Request

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_ui_acceptance import STATE, install_fixture
os.environ['AZURJUUS_WORKSPACE_ROOT'] = str(STATE / 'workspace')
import desktop
from PySide6.QtCore import QTimer, Qt
from qt_capture import capture_window

OUT = Path(os.environ.get('AZURJUUS_DESKTOP_PERFORMANCE_OUTPUT',
    str(Path(__file__).resolve().parents[1] / 'validation' / 'desktop-performance-20261006')))


def cpu_snapshot():
    root = psutil.Process()
    values = {}
    for process in [root, *root.children(recursive=True)]:
        try:
            times = process.cpu_times()
            values[(process.pid, process.create_time())] = times.user + times.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return values


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    desktop.WEB_PROFILE_DIR = STATE / 'profile'
    desktop.WEB_CACHE_DIR = STATE / 'cache'
    app = desktop.create_application()
    server, thread, url, context = desktop.start_local_server(port=0)
    install_fixture(server)

    @server.app.post('/__acceptance/performance')
    async def seed():
        from backend.database import session_scope
        from backend.models import Actor, Conversation
        with session_scope() as session:
            service = server.app.state.service
            data = service.build_snapshot(session)
            room = next(c for c in data['conversations'] if c['kind'] == 'dm')
            actor = next(a for a in data['agents'] if a['id'] in room['memberIds'])
            for item in [data['user'], *data['agents']]:
                row = session.get(Actor, item['id'])
                row.avatar_url = '/resources/ui/javelin.png'
                row.illustration_url = '/resources/ui/javelin.png'
            for i in range(130):
                service._append_message(session, session.get(Conversation, room['id']),
                    'commander' if i % 3 == 0 else actor['id'], 'text',
                    f'隔离性能测试消息 {i}。' + '这段文字只用于滚动绘制，不用于人物验收。' * (i % 4 + 1),
                    message_id=f'performance-{i}')
            return {'title': room['title']}

    if not desktop.wait_for_server(url):
        desktop.stop_local_server(server, thread)
        raise RuntimeError('Isolated server did not start')
    client = build_opener(HTTPCookieProcessor())
    client.open(url).read()
    def post(path):
        return json.loads(client.open(Request(url + path, data=b'{}',
            headers={'Content-Type': 'application/json', 'Origin': url})).read())
    post('/__acceptance/visual')
    fixture = post('/__acceptance/performance')
    window = desktop.AzurJuusWindow(url, server, thread, context)
    window.resize(1280, 800)
    window.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    window.show()
    report = {'status': 'running', 'renderer': desktop.DESKTOP_RENDERER,
        'isolatedData': str(STATE), 'realModelCalls': False, 'samples': [],
        'scope': 'Same renderer, 1280x800 window, synthetic scrolling, two runs per variant/scene; not sustained or OS DPI acceptance.'}
    steps = [(scene, variant) for scene in ('chat', 'moments')
        for variant in ('full-glass', 'software-surfaces', 'software-surfaces', 'full-glass')]
    finished = False

    def finish(error=''):
        nonlocal finished
        if finished:
            return
        finished = True
        if error:
            report['error'] = error
        report['status'] = 'passed' if not error and len(report['samples']) == len(steps) else 'failed'
        report['summary'] = {}
        for scene, variant in steps:
            rows = [r for r in report['samples'] if r['scene'] == scene and r['variant'] == variant]
            if rows:
                report['summary'][scene + '/' + variant] = {key: round(statistics.mean(r[key] for r in rows), 2)
                    for key in ('fps', 'p95FrameMs', 'longFramePercent', 'cpuOneCorePercent')}
        window.close()
        QTimer.singleShot(3000, app.quit)

    def step(index=0):
        if finished:
            return
        if index == len(steps):
            finish()
            return
        scene, variant = steps[index]
        script = """(()=>{
          document.querySelector('[aria-label="关闭设置"]')?.click();
          document.querySelector('[aria-label="SCENE"]')?.click();
          if(SCENE_CHAT) setTimeout(()=>[...document.querySelectorAll('.conversation-title strong')]
            .find(e=>e.textContent===TITLE)?.closest('button')?.click(),50);
          document.documentElement.dataset.desktopRenderer=VARIANT;
        })()""".replace('SCENE_CHAT', str(scene == 'chat').lower()).replace('SCENE', '聊天' if scene == 'chat' else '朋友圈')
        script = script.replace('TITLE', json.dumps(fixture['title'])).replace('VARIANT', json.dumps('software' if variant == 'software-surfaces' else 'control'))
        window._page.runJavaScript(script)
        QTimer.singleShot(1000, lambda: measure(index, scene, variant))

    def measure(index, scene, variant, attempt=0):
        if finished:
            return
        selector = '.message-scroll' if scene == 'chat' else '.moments-feed'
        window._page.runJavaScript(f"!!document.querySelector({json.dumps(selector)})", lambda ready:
            begin(index, scene, variant, selector) if ready else
            QTimer.singleShot(200, lambda: measure(index, scene, variant, attempt + 1)) if attempt < 20 else
            finish('Missing scroll surface: ' + selector))

    def begin(index, scene, variant, selector):
        before = cpu_snapshot()
        wall_start = time.monotonic()
        script = """(()=>{
          const el=document.querySelector(SELECTOR), max=el.scrollHeight-el.clientHeight;
          if(max<=0){window.__performanceSample={error:'Fixture cannot scroll'};return;}
          window.__performanceSample=null;
          const frames=[],start=performance.now();let last=start;
          const filters=[...document.querySelectorAll('.message-bubble,.conversation-panel,.moments-card')]
            .map(e=>getComputedStyle(e).backdropFilter);
          function tick(now){
            frames.push(now-last);last=now;
            el.scrollTop=max*(.5+.5*Math.sin((now-start)/450));
            if(now-start<3000){requestAnimationFrame(tick);return;}
            const sorted=[...frames].sort((a,b)=>a-b);
            window.__performanceSample={fps:frames.length*1000/(now-start),
              p95FrameMs:sorted[Math.floor((sorted.length-1)*.95)],
              longFramePercent:100*frames.filter(n=>n>33.4).length/frames.length,
              backdropFilters:filters.filter(v=>v&&v!=='none').length,frames:frames.length};
          }requestAnimationFrame(tick);
        })()""".replace('SELECTOR', json.dumps(selector))
        window._page.runJavaScript(script)
        def collect(attempt=0):
            if finished:
                return
            def done(raw):
                if not raw:
                    if attempt < 50:
                        QTimer.singleShot(200, lambda: collect(attempt + 1))
                    else:
                        finish('Frame measurement timed out')
                    return
                result = json.loads(raw)
                if result.get('error'):
                    finish(result['error'])
                    return
                after = cpu_snapshot()
                result.update(scene=scene, variant=variant,
                    cpuOneCorePercent=100*sum(max(0, value-before.get(pid, value)) for pid, value in after.items())/(time.monotonic()-wall_start))
                if variant == 'software-surfaces' and result['backdropFilters']:
                    finish('Software surfaces still have live backdrop filters')
                    return
                report['samples'].append(result)
                print(json.dumps(result), flush=True)
                result.update(capture_window(window, OUT / f'{scene}-{variant}.png'))
                if not result['captureUnobscured'] or not result['captureSaved']:
                    finish('Could not capture unobscured fixture window')
                    return
                QTimer.singleShot(100, lambda: step(index + 1))
            window._page.runJavaScript('window.__performanceSample ? JSON.stringify(window.__performanceSample) : ""', done)
        QTimer.singleShot(3100, collect)

    def ready(attempt=0):
        def inspected(raw):
            if raw:
                report['pageRenderer'] = raw
                if raw != desktop.DESKTOP_RENDERER:
                    finish('Desktop renderer marker was not injected')
                    return
                step()
            elif attempt < 60:
                QTimer.singleShot(200, lambda: ready(attempt + 1))
            else:
                finish('Application did not load')
        window._page.runJavaScript('document.querySelector(".message-scroll") ? document.documentElement.dataset.desktopRenderer : ""', inspected)
    QTimer.singleShot(1000, ready)
    QTimer.singleShot(90000, lambda: finish('Overall timeout'))
    app.exec()
    desktop.stop_local_server(server, thread)
    report['serverStopped'] = not thread.is_alive()
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'summary': report['summary'], 'report': str(OUT / 'report.json')}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' and report['serverStopped'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
