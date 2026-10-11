"""Launch the windowless Python entry, drive QWebEngine via CDP, close via WM_CLOSE.

Uses only synthetic history/files in a new workspace; reads only saved model credentials.
"""
import ctypes
import hashlib
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATE = ROOT / 'validation' / 'batch-execution' / uuid4().hex
from tools.acceptance_reviews import pending_reviews

def reply_id(rid, phase='result'):
    return 'speech-' + hashlib.sha256((rid + ':' + phase).encode()).hexdigest()[:24]
WORK = STATE / 'work'
WORK.mkdir(parents=True)
(WORK / 'source.txt').write_text('Synthetic batch test: 2 + 3 = 5.\n', encoding='utf-8')
with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
    SAVED = db.execute('SELECT llm_base_url,llm_model,llm_api_key,connected_agent_ids,secretary_agent_id FROM workspace_settings LIMIT 1').fetchone()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    from playwright.sync_api import sync_playwright
    import psutil
    port, debug = free_port(), free_port()
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (STATE / 'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(STATE / 'state' / 'workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(WORK),
        AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='', AZURJUUS_SOCIAL_ENABLED='0',
        AZURJUUS_EXECUTION_BACKEND='deepseek-harness', AZURJUUS_PORT=str(port), AZURJUUS_DESKTOP_LOG_DIR=str(STATE/'logs'),
        QTWEBENGINE_REMOTE_DEBUGGING='127.0.0.1:' + str(debug))
    # Match .env.example and the user's actual .bat launch configuration.
    os.environ['AZURJUUS_WORKSPACE_STATE_PATH'] = (STATE / 'state' / 'workspace.json').relative_to(ROOT).as_posix()
    from backend.app import create_app
    from backend.database import session_scope
    from fastapi.testclient import TestClient
    app = create_app()
    with TestClient(app):
        with session_scope() as session:
            settings = app.state.service.get_workspace(session)
            settings.llm_base_url, settings.llm_model, settings.llm_api_key = SAVED[:3]
            settings.connected_agent_ids=json.loads(SAVED[3])
            settings.secretary_agent_id=SAVED[4]
            settings.authorized_workspace_root = str(WORK)
    out = ROOT / 'validation' / 'batch-execution'
    out.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'stateDirectory': str(STATE), 'checks': [], 'entry': 'pythonw tools/desktop_entry.py',
        'relativeStatePath': os.environ['AZURJUUS_WORKSPACE_STATE_PATH']}
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    process = None
    owned = []
    def close_window():
        nonlocal owned
        owned = psutil.Process(process.pid).children(recursive=True)
        pids = {process.pid, *(p.pid for p in owned)}
        @enum_callback
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            return True
        user32.EnumWindows(visit, 0)
    try:
        with (STATE / 'launcher.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(ROOT/'.venv/Scripts/pythonw.exe'), '-X', 'utf8', str(ROOT/'tools/desktop_entry.py')], cwd=ROOT,
                stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            with sync_playwright() as pw:
                browser = None
                connection_error = ''
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    try:
                        browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{debug}', timeout=2000, no_defaults=True)
                        break
                    except Exception as exc:
                        connection_error = str(exc)
                        time.sleep(.25)
                assert browser is not None, 'QWebEngine CDP connection failed: ' + connection_error
                page = browser.contexts[0].pages[0]
                page.get_by_label('消息输入').wait_for(timeout=20000)
                greeting = os.getenv('AZURJUUS_BATCH_GREETING') == '1'
                if greeting:
                    page.get_by_text('能代', exact=True).first.click()
                def send(label, text):
                    # The mode selector is a native details menu; open it before
                    # selecting a mode so the click also works in QWebEngine.
                    page.locator('.composer-mode summary').click()
                    page.locator('.composer-mode button').filter(has_text=label).click()
                    page.get_by_label('消息输入').fill(text)
                    with page.expect_response(lambda r: r.url.endswith('/api/messages/send') and r.request.method == 'POST') as response:
                        page.get_by_label('发送消息', exact=True).click()
                    assert response.value.ok, response.value.text()
                    return response.value.json()['runId']
                def wait_run(mode, rid=None):
                    deadline = time.monotonic() + 360
                    while time.monotonic() < deadline:
                        state = page.evaluate("fetch('/api/runtime/state').then(r=>r.json())")
                        run = next((r for r in state['runs'] if r['mode'] == mode and (not rid or r['id'] == rid)), None)
                        if run and run['status'] in {'failed', 'paused', 'cancelled'}:
                            raise AssertionError(run.get('error') or run['status'])
                        if run and run['status'] == 'completed':
                            return run
                        if run and mode!='chat':
                            for review_id in pending_reviews(run,WORK):
                                response=page.request.post(f"http://127.0.0.1:{port}/api/runs/{run['id']}/reviews/{review_id}",
                                    data={'decision':'approved','note':'隔离合成桌面场景，已检查阶段证据。'})
                                assert response.ok,response.text()
                                report.setdefault('userStageApprovals',[]).append(review_id)
                        time.sleep(.5)
                    raise AssertionError('No completed ' + mode + ' run')
                send('闲聊', '你好' if greeting else '这是合成测试，请只回复 BATCH_CHAT_OK。')
                chat_run = wait_run('chat')
                reply = page.locator('[data-message-id="' + reply_id(chat_run['id'], 'chat') + '"]')
                reply.wait_for(timeout=10000)
                assert reply.inner_text().strip() if greeting else 'BATCH_CHAT_OK' in reply.inner_text()
                page.screenshot(path=str(out / 'chat.png'))
                report['checks'].append('bat > desktop.py > QWebEngine > expression > configured model chat completed')
                print('BATCH_CHAT_PASSED', flush=True)
                send('任务', '读取 source.txt，将来源和 2+3=5 写入 report.txt，读回核验后交付。只操作这两个文件，不使用命令或网络。')
                task_run = wait_run('task')
                page.locator('[data-message-id="' + reply_id(task_run['id']) + '"]').wait_for(timeout=35000)
                assert (WORK / 'report.txt').is_file()
                assert '5' in (WORK / 'report.txt').read_text(encoding='utf-8')
                page.screenshot(path=str(out / 'task.png'))
                report['checks'].append('bat task produced and verified report.txt')
                print('BATCH_TASK_PASSED', flush=True)
                if os.getenv('AZURJUUS_BATCH_EXTENDED') == '1':
                    rid = send('任务', '工作区内的文件都有些什么？')
                    listing = wait_run('task', rid)
                    # A directory query may cite existing files as artifacts; it
                    # must not need to create an artificial report to pass.
                    assert sorted(p.name for p in WORK.iterdir()) == ['report.txt', 'source.txt']
                    calls = page.evaluate("rid => fetch('/api/runs/' + rid).then(r=>r.json())", rid)['calls']
                    assert any(c['name'] == 'list_dir' and c['status'] == 'completed' for c in calls)
                    assert not any(c['name'] in {'write_file','move_file','command','patch_file'} for c in calls)
                    assert all(a['status'] == 'completed' for a in listing['assignments'])
                    page.locator('[data-message-id="' + reply_id(rid) + '"]').wait_for(timeout=35000)
                    report['listingRunId'] = rid
                    report['checks'].append('Exact directory-listing query completed with read-only evidence and no mandatory file artifact')
                    page.screenshot(path=str(out / 'listing.png'))
                    print('BATCH_LISTING_PASSED', flush=True)
                    boot = page.evaluate("fetch('/api/bootstrap').then(r=>r.json())")['workspace']
                    settings = boot['settings']
                    configured_ids = set(settings.get('connectedAgentIds') or [])
                    available = [a for a in boot['data']['agents'] if a['id'] in configured_ids and not a.get('socialOnly')]
                    report['configuredCollaborationRoster'] = [{'id': a['id'], 'name': a['name']} for a in available]
                    if len(available) < 2:
                        report['checks'].append('Collaboration skipped: fewer than two configured task-capable connected members')
                    else:
                        roster = ', '.join(f"{a['name']} ({a['id']})" for a in available)
                        collab_prompt = (
                            'Collaborative acceptance. Use only these configured connected members: ' + roster +
                            '. Do not invent, add, or mention any person outside this list. '
                            'Assign two different listed members to two dependent subtasks. '
                            'The first worker should discuss whether source.txt could be incomplete, read source.txt, '
                            'and write facts.txt with the source name and 2+3=5. The second worker must read facts.txt '
                            'and write team-report.txt with the evidence and conclusion, then verify it. '
                            'Only operate on source.txt, facts.txt, and team-report.txt; do not use command or network tools.'
                        )
                        rid = send('协作', collab_prompt)
                        team = wait_run('task', rid)
                        assert team['teamConversationId'] == team['conversationId'] != team['originConversationId']
                        assignments = team['assignments']
                        assert len({a['actorId'] for a in assignments}) >= 2, assignments
                        assert {a['actorId'] for a in assignments} <= configured_ids, assignments
                        assert any(a['dependsOn'] for a in assignments), assignments
                        assert all(a['status'] == 'completed' for a in assignments)
                        for name in ('facts.txt', 'team-report.txt'):
                            assert '5' in (WORK / name).read_text(encoding='utf-8')
                        # Execution completes before the separate expression callback.
                        page.locator('[data-message-id="' + reply_id(rid) + '"]').wait_for(timeout=35000)
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            latest = page.evaluate("rid => fetch('/api/runs/' + rid).then(r=>r.json())", rid)['run']
                            if latest.get('resultMessageId'):
                                break
                            time.sleep(.1)
                        boot = page.evaluate("fetch('/api/bootstrap').then(r=>r.json())")['workspace']['data']
                        group = next(v for v in boot['conversations'] if v['id'] == team['conversationId'])
                        assert {a['actorId'] for a in assignments} <= set(group['memberIds'])
                        messages = boot['messages'][team['conversationId']]
                        assert {a['actorId'] for a in assignments} <= {m['speakerId'] for m in messages}
                        assert any(m['id'] == rid + '-origin-result' for m in boot['messages'][team['originConversationId']])
                        assert team.get('methods')
                        assert any(d['status'] == 'completed' for d in team.get('discussions', [])), team.get('discussions')
                        notice = next(m for m in boot['messages'][team['originConversationId']] if m['id'] == rid + '-origin-result')
                        assert notice['type'] == 'task_notice' and notice['speakerId'] == 'commander'
                        report['checks'].append('Live peer discussion and persona-isolated private task notice')
                        report['collaborationRunId'] = rid
                        report['collaborationAssignments'] = [{'id':a['id'], 'actorId':a['actorId'], 'dependsOn':a['dependsOn']} for a in assignments]
                        report['checks'].append('Collaboration: dedicated group, two distinct workers, dependency handoff, two real verified files, member replies and origin summary')
                        page.locator('[data-message-id="' + reply_id(rid) + '"]').wait_for(timeout=35000)
                        page.screenshot(path=str(out / 'collaboration.png'))
                        print('BATCH_COLLABORATION_PASSED', flush=True)
                send('闲聊', '这是合成退出测试，请用几句话描述晴天。')
                deadline = time.monotonic() + 10
                active = None
                while time.monotonic() < deadline:
                    state = page.evaluate("fetch('/api/runtime/state').then(r=>r.json())")
                    active = next((r for r in state['runs'] if r['status'] == 'running'), None)
                    if active:
                        break
                    time.sleep(.1)
                assert active, 'Expected an active run before closing'
                before = time.monotonic()
                close_window()
                process.wait(timeout=20)
                report['shutdownSeconds'] = round(time.monotonic() - before, 3)
                report['exitCode'] = process.returncode
                # Chromium may finish releasing its utility processes after the GUI exits.
                psutil.wait_procs(owned, timeout=5)
                report['remainingChildPids'] = [p.pid for p in owned if p.is_running()]
                assert not report['remainingChildPids'], 'Test child processes survived the exit grace period'
                assert process.returncode == 0
                with sqlite3.connect(STATE / 'state' / 'runs.db') as db:
                    status = db.execute('SELECT status FROM runs WHERE id=?', (active['id'],)).fetchone()[0]
                assert status == 'paused', status
                assert 'Traceback' not in (STATE / 'launcher.log').read_text(encoding='utf-8')
                assert 'Traceback' not in (STATE / 'logs/desktop.log').read_text(encoding='utf-8')
                homes = list((STATE / 'state/deepseek-harness').glob('*/*'))
                assert homes and all((home / 'azurjuus.cordis.patch.yml').is_file() for home in homes)
                assert all((home / 'profiles').is_dir() and (home / 'sessions').is_dir() for home in homes)
                assert not (ROOT / '.vendor/deepseek-harness' / os.environ['AZURJUUS_WORKSPACE_STATE_PATH']).parent.exists()
                report['checks'].append('relative state path resolved before child cwd; DeepSeek Harness profiles, sessions and Cordis patches share the intended directory')
                report['checks'].append('active run saved as paused; launcher and child processes exited without traceback')
                report['status'] = 'passed'
    except Exception as exc:
        import traceback
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        report['traceback'] = traceback.format_exc()
    finally:
        if process and process.poll() is None:
            close_window()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # Only this test's captured descendants; never unrelated Python processes.
                for child in reversed(owned):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                process.wait(timeout=5)
        filename = 'extended-report.json' if os.getenv('AZURJUUS_BATCH_EXTENDED') == '1' else 'greeting-report.json' if os.getenv('AZURJUUS_BATCH_GREETING') == '1' else 'report.json'
        (out / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
