import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app
from backend.config import get_settings
from backend.database import session_scope
from backend.models import Message, SkillRun, MemoryChunk
from backend.run_store import RunStore
from conftest import configure_test_env


@pytest.mark.parametrize('submit', [True, False, 'delayed'])
def test_listing_review_retains_reply_and_only_records_verified_skill(monkeypatch, tmp_path, submit):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv('AZURJUUS_EXECUTION_BACKEND', 'hermes')
    monkeypatch.setenv('AZURJUUS_WORKSPACE_STATE_PATH', str(tmp_path / 'state/workspace.json'))
    get_settings.cache_clear()
    (tmp_path / 'workspace/alpha.txt').write_text('synthetic')
    app = create_app()
    c = app.state.runs
    phases = []
    class ListingBridge:
        session_id = None
        def __init__(self, home, settings, endpoint, token, event):
            self.phase, self.token, self.event = home.name, token, event
        async def start(self): pass
        async def close(self): pass
        async def prompt(self, prompt, workspace, history=None):
            phases.append(self.phase)
            assert history is None or any('方法参考' in m['content'] for m in history)
            result = await c.tool(self.token, {'callId':uuid4().hex, 'name':'list_dir', 'args':{'path':'.'}})
            assert result['status'] == 'completed'
            if history is None:
                with pytest.raises(PermissionError):
                    await c.tool(self.token, {'callId':uuid4().hex, 'name':'write_file', 'args':{'path':'duplicate.txt','content':'must not write'}})
            if submit and (submit != 'delayed' or self.phase == 'reviewer' or history is None):
                result = await c.tool(self.token, {'callId':uuid4().hex, 'name':'deliver', 'args':{
                    'summary':'工作区包含 alpha.txt', 'artifacts':[], 'checks':[{'callId':result['callId']}],
                    'unresolved':['没有读取文件正文；用户仅要求目录清单。'] if self.phase == 'main' else []}})
                assert result['status'] == 'completed'
            await self.event('message.start', {})
            await self.event('message.delta', {'delta':'工作区包含 alpha.txt'})
            await self.event('message.complete', {'text':'工作区包含 alpha.txt', 'status':'complete'})
            return '工作区包含 alpha.txt'
    c.bridge_factory = ListingBridge
    with TestClient(app) as client:
        workspace = client.get('/api/workspace/load').json()['workspace']
        workspace['settings'].update(llmApiKey='synthetic', authorizedWorkspaceRoot=str(tmp_path / 'workspace'))
        client.post('/api/workspace/save', json={'workspace':workspace}).raise_for_status()
        cid = next(v['id'] for v in workspace['data']['conversations'] if v['kind'] == 'dm')
        payload = {'conversationId':cid, 'mode':'task', 'content':'工作区内的文件都有些什么？', 'requestId':uuid4().hex}
        response = client.post('/api/messages/send', json=payload)
        response.raise_for_status()
        rid = response.json()['runId']
        for _ in range(200):
            run = c.store.get(rid)
            if run['status'] in {'completed','failed'} and rid not in c.tasks: break
            time.sleep(.02)
        assert run['status'] == ('completed' if submit else 'failed'), run
        assert phases == (['main','main','reviewer'] if submit == 'delayed' else ['main','reviewer'] if submit else ['main','main','main'])
        assert run['artifacts'] == []
        with session_scope() as session:
            messages = session.scalars(select(Message).where(Message.conversation_id == cid)).all()
            assert any(m.body == '工作区包含 alpha.txt' for m in messages), 'Reply must survive validation failure'
            records = session.scalars(select(SkillRun).where(SkillRun.source_kind == 'hermes')).all()
            assert len(records) == (2 if submit else 0)
            memories = session.scalars(select(MemoryChunk).where(MemoryChunk.source_kind == 'verified_task')).all()
            assert len(memories) == (1 if submit else 0)
            if submit:
                assert memories[0].metadata_json == {'runId':rid, 'verified':True}
        assert client.post(f'/api/runs/{rid}/remove', json={}).status_code == 200
        assert rid not in [r['id'] for r in client.get('/api/runtime/state').json()['runs']]
        assert (tmp_path / 'workspace/alpha.txt').read_text() == 'synthetic'
        assert c.store.calls(rid), 'Retain audit and idempotency history'
        assert RunStore(c.store.path).get(rid)['deletedAt']
        with session_scope() as session:
            assert any(m.body == '工作区包含 alpha.txt' for m in session.scalars(select(Message)).all())


def test_clear_only_terminal_records_and_keeps_idempotency(tmp_path):
    store = RunStore(tmp_path / 'runs.db')
    data = {'prompt':'synthetic', 'conversationId':'x', 'mode':'task', 'collaborative':False, 'workspace':str(tmp_path)}
    run, _ = store.create(data, 'key')
    with pytest.raises(ValueError): store.remove(run['id'])
    store.update(run['id'], status='cancelled')
    store.remove(run['id'])
    store.remove(run['id'])
    duplicate, created = store.create(data, 'key')
    assert not created and duplicate['id'] == run['id']
    assert store.list() == []
    assert len([e for e in store.events(0) if e['type'] == 'run.removed']) == 1


def test_clear_api_covers_older_records_and_protects_active(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv('AZURJUUS_EXECUTION_BACKEND', 'hermes')
    monkeypatch.setenv('AZURJUUS_WORKSPACE_STATE_PATH', str(tmp_path / 'state/workspace.json'))
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        store = app.state.runs.store
        data = {'prompt':'synthetic', 'conversationId':'x', 'mode':'task', 'collaborative':False, 'workspace':str(tmp_path)}
        for index in range(201):
            run, _ = store.create(data, str(index))
            store.update(run['id'], status='failed')
        active, _ = store.create(data, 'active')
        store.update(active['id'], status='running')
        paused, _ = store.create(data, 'paused')
        store.update(paused['id'], status='paused')
        assert client.post('/api/runs/' + active['id'] + '/remove', json={}).status_code == 409
        result = client.post('/api/runs/clear', json={})
        assert result.status_code == 200 and len(result.json()['removed']) == 201
        assert {r['id'] for r in store.list()} == {active['id'], paused['id']}
        assert client.post('/api/runs/' + paused['id'] + '/remove', json={}).status_code == 200
        assert store.get(paused['id'])['status'] == 'cancelled'
