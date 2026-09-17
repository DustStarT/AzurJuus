import asyncio
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.app import create_app
from backend.config import get_settings
from backend.database import session_scope
from backend.models import Message, SkillRun
from backend.run_coordinator import RunCoordinator
from backend.run_store import RunStore
from conftest import configure_test_env


def test_peer_discussion_private_notice_and_group_removal(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv('AZURJUUS_EXECUTION_BACKEND', 'hermes')
    monkeypatch.setenv('AZURJUUS_WORKSPACE_STATE_PATH', str(tmp_path / 'state/workspace.json'))
    get_settings.cache_clear()
    app = create_app()
    c = app.state.runs
    discussion_replied = []
    histories = []

    class Bridge:
        session_id = None
        def __init__(self, home, settings, endpoint, token, event):
            self.phase, self.token, self.event = home.name, token, event
            self.allowed = settings['_allowedActions']
        async def start(self): pass
        async def close(self): pass
        async def call(self, name, args):
            result = await c.tool(self.token, {'callId':uuid4().hex, 'name':name, 'args':args})
            assert result['status'] == 'completed', result
            return result
        async def prompt(self, prompt, workspace, history=None):
            histories.append((self.phase, history))
            assert '不向用户复述设定' in history[0]['content']
            rid = c.tokens[self.token][0]
            run = c.store.get(rid)
            if self.phase == 'planner':
                await self.call('delegate', {'tasks':[
                    {'id':'a','actorId':run['actors'][0]['id'],'brief':'draft','dependsOn':[],'acceptance':['check']},
                    {'id':'b','actorId':run['actors'][-1]['id'],'brief':'review draft','dependsOn':['a'],'acceptance':['check']}]})
            elif self.phase.startswith('discussion_'):
                assert self.allowed == []
                assert run['assignments'][0]['status'] == 'running', 'Discussion must happen before upstream finishes'
                discussion_replied.append(True)
            else:
                if self.phase == 'a':
                    question = await self.call('discuss', {'actorId':run['actors'][-1]['id'], 'text':'我想省一步检查，你觉得会漏什么？'})
                    assert question['result']['status'] == 'queued'
                    for _ in range(100):
                        if discussion_replied: break
                        await asyncio.sleep(.01)
                    assert discussion_replied
                    forged = await c.tool(self.token, {'callId':uuid4().hex,'name':'deliver','args':{'summary':'not evidence','checks':[{'callId':question['callId']}],'artifacts':[]}})
                    assert forged['status'] == 'failed'
                result = await self.call('list_dir', {'path':'.'})
                if self.phase == 'a':
                    assert result['peerMessages']
                await self.call('deliver', {'summary':'checked','checks':[{'callId':result['callId']}],'artifacts':[],'unresolved':[]})
            reply = '先别急。\n\n我担心遗漏核验，读一遍再交吧。'
            await self.event('message.complete', {'text':reply})
            return reply

    c.bridge_factory = Bridge
    with TestClient(app) as client:
        workspace = client.get('/api/workspace/load').json()['workspace']
        workspace['settings'].update(llmApiKey='synthetic', authorizedWorkspaceRoot=str(tmp_path / 'workspace'))
        client.post('/api/workspace/save', json={'workspace':workspace}).raise_for_status()
        secretary = workspace['settings']['secretaryAgentId']
        origin = next(v for v in workspace['data']['conversations'] if v['kind'] == 'dm' and secretary not in v['memberIds'])
        accepted = client.post('/api/messages/send', json={'conversationId':origin['id'],'content':'协作检查目录','mode':'task','collaborative':True,'requestId':uuid4().hex})
        accepted.raise_for_status()
        rid, cid = accepted.json()['runId'], accepted.json()['conversationId']
        for _ in range(300):
            run = c.store.get(rid)
            if run['status'] in {'completed','failed'} and rid not in c.tasks: break
            time.sleep(.02)
        assert run['status'] == 'completed', run
        assert run['discussions'][0]['status'] == 'completed'
        with session_scope() as session:
            notice = session.get(Message, rid + '-origin-result')
            assert notice.speaker_id == 'commander' and notice.type == 'task_notice'
            assert not any(m.speaker_id == secretary for m in session.scalars(select(Message).where(Message.conversation_id == origin['id'])))
            records = session.scalars(select(SkillRun).where(SkillRun.source_kind == 'hermes')).all()
            discussion_records = [r for r in records if r.extra_json['phase'].startswith('discussion_')]
            assert discussion_records and all(not r.extra_json['verified'] for r in discussion_records)
        methods = client.get('/api/actors/' + run['actors'][-1]['id'] + '/methods')
        assert methods.status_code == 200 and methods.json()['skills']
        assert any(not s['enabled'] and s['sourceRunId'] == rid for s in methods.json()['skills'])
        response = client.post('/api/conversations/' + cid + '/remove', json={})
        assert response.status_code == 200, response.text
        assert not c.store.list()
        assert client.post('/api/messages/send', json={'conversationId':cid,'content':'hello'}).status_code == 409
        assert client.post('/api/conversations/port-hub/remove', json={}).status_code == 409
        class WaitingBridge(Bridge):
            async def start(self):
                await asyncio.Event().wait()
        c.bridge_factory = WaitingBridge
        pending = client.post('/api/messages/send', json={'conversationId':origin['id'],'content':'pending','mode':'task','collaborative':True,'requestId':uuid4().hex}).json()
        for _ in range(100):
            if c.store.get(pending['runId'])['status'] == 'running': break
            time.sleep(.01)
        assert client.post('/api/conversations/' + pending['conversationId'] + '/remove', json={}).status_code == 200
        assert c.store.get(pending['runId'])['status'] == 'cancelled'
        assert pending['runId'] not in c.tasks


@pytest.mark.asyncio
async def test_scheduler_starts_unblocked_dependency_before_unrelated_slow_member(tmp_path):
    async def finish(*args): pass
    c = RunCoordinator(RunStore(tmp_path / 'runs.db'), lambda:{}, finish, '')
    run, _ = c.store.create({'prompt':'synthetic','conversationId':'x'}, 'test')
    c.store.update(run['id'], assignments=[{'id':i,'status':'pending','dependsOn':['a'] if i == 'c' else []} for i in 'abc'])
    third_started = asyncio.Event()
    async def assignment(rid, aid, settings):
        if aid == 'b': await third_started.wait()
        if aid == 'c': third_started.set()
        c.patch_assignment(rid, aid, status='completed')
    c.assignment = assignment
    await asyncio.wait_for(c.execute_assignments(run['id'], {}), 2)
    assert third_started.is_set()
