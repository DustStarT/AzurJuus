"""Exercise actual QWebEngine send buttons, cloud replies and responsive close."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATE = ROOT / '.azurjuus' / 'qt-execution' / uuid4().hex
WORK = STATE / 'work'
WORK.mkdir(parents=True)
(WORK / 'source.txt').write_text('Synthetic desktop data: 2 + 3 = 5.\n', encoding='utf-8')
with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
    SAVED = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    if os.getenv('AZURJUUS_QT_COPY_PROFILE') == '1':
        with sqlite3.connect(STATE / 'app.db') as copied:
            db.backup(copied)
os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (STATE / 'app.db').as_posix(),
    AZURJUUS_WORKSPACE_STATE_PATH=str(STATE / 'state' / 'workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(WORK),
    AZURJUUS_EXECUTION_BACKEND='hermes')
if os.getenv('AZURJUUS_QT_REAL_SERVICES') != '1':
    os.environ.update(AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='', AZURJUUS_SOCIAL_ENABLED='0')
import desktop
from backend.database import session_scope
from PySide6.QtCore import QTimer


def main():
    out = ROOT / 'validation' / 'qt-execution'
    out.mkdir(parents=True, exist_ok=True)
    desktop.WEB_PROFILE_DIR, desktop.WEB_CACHE_DIR = STATE / 'profile', STATE / 'cache'
    app = desktop.create_application()
    server, thread, url, context = desktop.start_local_server(port=0)
    if not desktop.wait_for_server(url):
        raise RuntimeError('Desktop backend startup failed')
    with session_scope() as session:
        workspace = server.app.state.service.get_workspace(session)
        workspace.llm_base_url, workspace.llm_model, workspace.llm_api_key = SAVED
        workspace.authorized_workspace_root = str(WORK)
    window = desktop.AzurJuusWindow(url, server, thread, context)
    window.show()
    began = time.monotonic()
    report = {'status': 'failed', 'stateDirectory': str(STATE), 'checks': [], 'samples': []}
    report['realServices'] = os.getenv('AZURJUUS_QT_REAL_SERVICES') == '1'
    phase, submitted = 'chat', False
    closing = False
    timer = QTimer()
    timer.setInterval(500)
    heartbeat, gaps = time.monotonic(), []
    def tick():
        nonlocal heartbeat, phase, submitted, closing
        now = time.monotonic()
        gaps.append(now - heartbeat)
        heartbeat = now
        if closing:
            return
        runs = server.app.state.runs.store.list()
        if os.getenv('AZURJUUS_QT_CLOSE_ACTIVE') == '1' and any(r['status']=='running' for r in runs):
            report['status'] = 'passed'
            report['checks'].append('Close while actual model request is running')
            finish()
            return
        if int(now - began) % 5 == 0:
            sample = {'elapsed': round(now - began, 1), 'phase': phase, 'runs': [{'mode': r['mode'], 'status': r['status'], 'error': r.get('error')} for r in runs]}
            report['samples'].append(sample)
            print(json.dumps(sample, ensure_ascii=False), flush=True)
            for bridge in list(server.app.state.runs.bridges.values()):
                print(json.dumps({'bridgePid': bridge.process.pid if bridge.process else None, 'ready': bridge.ready.is_set(), 'diagnostics': bridge.diagnostics[-3:]}, ensure_ascii=False), flush=True)
        if now - began > float(os.getenv('AZURJUUS_QT_TEST_TIMEOUT', '120')):
            report['error'] = 'Timed out waiting for a visible desktop reply'
            finish()
            return
        if not submitted:
            label = '闲聊' if phase == 'chat' else '任务'
            prompt = '这是桌面验收，请只回复 QT_CHAT_OK。' if phase == 'chat' else '读取 source.txt，把 2+3=5 的依据和结果写入 report.txt，读回核验后交付。只操作这两个文件，不使用命令或网络。'
            script = """(()=>{const input=document.querySelector('[aria-label="消息输入"]');const buttons=[...document.querySelectorAll('.composer-mode button')];const mode=buttons.find(b=>b.textContent.trim()===LABEL);if(!input||!mode)return false;mode.click();input.value=PROMPT;input.dispatchEvent(new Event('input',{bubbles:true}));queueMicrotask(()=>document.querySelector('[aria-label="发送消息"]').click());return true})()""".replace('LABEL', json.dumps(label)).replace('PROMPT', json.dumps(prompt))
            window._page.runJavaScript(script, sent)
        elif runs:
            run = next((r for r in runs if r['mode'] == ('chat' if phase == 'chat' else 'task')), None)
            if run and run['status'] in {'failed', 'paused', 'cancelled'}:
                report['error'] = run.get('error') or run['status']
                finish()
            elif run and run['status'] == 'completed':
                speech_phase = 'chat' if phase == 'chat' else 'result'
                mid = 'speech-' + hashlib.sha256((run['id']+':'+speech_phase).encode()).hexdigest()[:24]
                selector = '[data-message-id="' + mid + '"] .message-bubble'
                marker = 'QT_CHAT_OK' if phase == 'chat' else ''
                script = "(()=>{const el=document.querySelector(SELECTOR);return !!el && !!el.textContent.trim() && el.textContent.includes(MARKER)})()".replace('SELECTOR', json.dumps(selector)).replace('MARKER', json.dumps(marker))
                window._page.runJavaScript(script, visible)
    def sent(value):
        nonlocal submitted
        submitted = bool(value)
    def visible(value):
        nonlocal phase, submitted
        if not value or closing:
            return
        report['checks'].append(phase + ' real cloud reply visible in QWebEngine')
        window.grab().save(str(out / (phase + '.png')))
        if phase == 'chat':
            phase, submitted = 'task', False
        else:
            report['outputExists'] = (WORK / 'report.txt').is_file()
            report['status'] = 'passed' if report['outputExists'] else 'failed'
            finish()
    def finish():
        nonlocal closing
        if closing:
            return
        closing = True
        window.grab().save(str(out / 'before-close.png'))
        before = time.monotonic()
        report['closeStartedAt'] = before
        window.close()
        report['closeCallSeconds'] = round(time.monotonic() - before, 3)
        for bridge in list(server.app.state.runs.bridges.values()):
            report.setdefault('diagnostics', []).append(bridge.diagnostics[-8:])
    timer.timeout.connect(tick)
    timer.start()
    app.exec()
    report['shutdownSeconds'] = round(time.monotonic() - report.pop('closeStartedAt', time.monotonic()), 3)
    report['maxHeartbeatGapSeconds'] = round(max(gaps, default=0), 3)
    report['serverStopped'] = not thread.is_alive()
    report['remainingRuns'] = [r['status'] for r in server.app.state.runs.store.list()]
    (out / ('active-close-report.json' if os.getenv('AZURJUUS_QT_CLOSE_ACTIVE') == '1' else 'report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    desktop.stop_local_server(server, thread)
    return 0 if report['status'] == 'passed' and report.get('closeCallSeconds', 99) < 1 and report['serverStopped'] and report['maxHeartbeatGapSeconds'] < 2 else 1


if __name__ == '__main__':
    raise SystemExit(main())
