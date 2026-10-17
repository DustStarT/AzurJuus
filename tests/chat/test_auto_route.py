"""Auto routing is advisory; only the existing admission path can grant tools."""
import asyncio
import json
import sqlite3
from uuid import uuid4

from backend.database import session_scope
from backend.models import Conversation, MemoryChunk, Message
from backend.chat.task_reference import resolve_task_reference
from backend.tasks.run_store import RunStore


def authorize_workspace(client, tmp_path):
    workspace = client.get('/api/workspace/load').json()['workspace']
    workspace['settings']['authorizedWorkspaceRoot'] = str(tmp_path / 'workspace')
    assert client.post('/api/workspace/save', json={'workspace':workspace}).status_code == 200


def test_existing_run_store_is_backed_up_before_route_table(tmp_path):
    path = tmp_path / 'runs.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY, request_key TEXT UNIQUE, status TEXT, data TEXT, updated REAL)')
        db.execute("INSERT INTO runs VALUES('old','old','completed','{}',0)")
    RunStore(path)
    backups = list((tmp_path / 'migration-backups').glob('*-runs-route.db'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_routes'").fetchone()
    RunStore(path)
    assert len(list((tmp_path / 'migration-backups').glob('*-runs-route.db'))) == 1


def test_auto_chat_task_and_retry_use_one_decision(world, monkeypatch, tmp_path):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)
    calls = []

    async def decide(messages, _settings):
        calls.append(messages)
        kind = 'chat' if '你好' in json.loads(messages[-1]['content'])['current'] else 'task'
        return {'kind':kind, 'actorIds':[ids[0]], 'confidence':.96, 'reason':'合成测试'}

    monkeypatch.setattr(coordinator.expression, 'complete', decide)
    cid = 'dm-' + ids[0]
    greeting = {'conversationId':cid, 'content':'你好', 'requestId':uuid4().hex}
    first = client.post('/api/messages/send', json=greeting)
    assert first.status_code == 200, first.text
    assert coordinator.store.get(first.json()['runId'])['mode'] == 'chat'
    repeated = client.post('/api/messages/send', json=greeting)
    assert repeated.status_code == 200
    assert repeated.json()['runId'] == first.json()['runId']
    assert len(calls) == 1
    assert client.post('/api/messages/send', json={**greeting, 'content':'另一条消息'}).status_code == 409
    with session_scope() as session:
        assert session.get(Message, first.json()['runId']+'-user').metadata_json['routeKind'] == 'chat'
        assert session.get(Message, first.json()['runId']+'-user').metadata_json['requestId'] == greeting['requestId']

    authorize_workspace(client, tmp_path)
    task = client.post('/api/messages/send', json={'conversationId':cid,
        'content':'请实际读取工作区里的书并概括', 'requestId':uuid4().hex})
    assert task.status_code == 200, task.text
    assert coordinator.store.get(task.json()['runId'])['mode'] == 'task'
    assert len(calls) == 2


def test_book_reference_without_verified_read_stays_clarification(world, monkeypatch):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)

    async def decide(_messages, _settings):
        return {'kind':'clarify', 'actorIds':[ids[0]], 'confidence':.95, 'reason':'未见书名'}

    monkeypatch.setattr(coordinator.expression, 'complete', decide)
    response = client.post('/api/messages/send', json={'conversationId':'dm-'+ids[0],
        'content':'你觉得这本书怎么样？', 'requestId':uuid4().hex})
    assert response.status_code == 200, response.text
    assert response.json()['route']['kind'] == 'clarify'
    run = coordinator.store.get(response.json()['runId'])
    assert run['mode'] == 'chat' and not run['taskEvidenceHint']


def test_inferred_dm_collaboration_waits_for_consent(world, monkeypatch, tmp_path):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)

    async def decide(_messages, _settings):
        return {'kind':'swarm', 'actorIds':ids[:2], 'confidence':.93, 'reason':'两人适合分工'}

    monkeypatch.setattr(coordinator.expression, 'complete', decide)
    authorize_workspace(client, tmp_path)
    request_id = uuid4().hex
    payload = {'conversationId':'dm-'+ids[0], 'content':'请整理工作区的文件，分工检查',
        'requestId':request_id}
    proposed = client.post('/api/messages/send', json=payload)
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()['route']['status'] == 'proposed'
    assert proposed.json()['runId'] is None
    assert not coordinator.store.list()
    with session_scope() as session:
        row = session.get(Message, 'route-proposal-' + __import__('hashlib').sha256(request_id.encode()).hexdigest()[:24])
        assert row and row.metadata_json['routeProposal']['actorIds'] == ids[:2]
    confirmed = client.post(f'/api/messages/route/{request_id}/confirm', json={})
    assert confirmed.status_code == 200, confirmed.text
    run = coordinator.store.get(confirmed.json()['runId'])
    assert run['collaborative'] and {a['id'] for a in run['actors']} == set(ids[:2])
    assert run['originConversationId'] == payload['conversationId']
    assert confirmed.json()['conversationId'] != payload['conversationId']
    assert client.post('/api/messages/send', json=payload).json()['runId'] == run['id']


def test_explicitly_named_dm_collaborators_can_start_directly(world, monkeypatch, tmp_path):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)
    authorize_workspace(client, tmp_path)
    other = next(a['name'] for a in client.get('/api/workspace/load').json()['workspace']['data']['agents']
        if a['id'] == ids[1])

    async def decide(_messages, _settings):
        return {'kind':'swarm', 'actorIds':ids[:2], 'confidence':.94, 'reason':'明确点名'}

    monkeypatch.setattr(coordinator.expression, 'complete', decide)
    response = client.post('/api/messages/send', json={'conversationId':'dm-'+ids[0],
        'content':f'请你和{other}一起分工核查工作区两份资料', 'requestId':uuid4().hex})
    assert response.status_code == 200, response.text
    assert response.json()['route']['status'] == 'executed'
    assert coordinator.store.get(response.json()['runId'])['collaborative']


def test_route_failure_keeps_message_unsent_and_manual_chat_works(world, monkeypatch):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)

    async def broken(_messages, _settings):
        raise TimeoutError('synthetic timeout')

    monkeypatch.setattr(coordinator.expression, 'complete', broken)
    cid = 'dm-' + ids[0]
    payload = {'conversationId':cid, 'content':'你好', 'requestId':uuid4().hex}
    failed = client.post('/api/messages/send', json=payload)
    assert failed.status_code == 503
    assert not coordinator.store.list()
    manual = client.post('/api/messages/send', json={**payload, 'mode':'chat'})
    assert manual.status_code == 200, manual.text
    assert coordinator.store.get(manual.json()['runId'])['mode'] == 'chat'


def test_router_sees_only_current_members_visible_messages(world, monkeypatch):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)
    cid = 'dm-' + ids[0]
    with session_scope() as session:
        coordinator.social_engine.service._append_message(session, session.get(Conversation, cid),
            ids[1], 'text', '另一人物的私密说明', message_id='foreign-private-for-route')
    seen = []

    async def decide(messages, _settings):
        seen.append(json.loads(messages[-1]['content']))
        return {'kind':'chat', 'actorIds':[], 'confidence':.95, 'reason':'问候'}

    monkeypatch.setattr(coordinator.expression, 'complete', decide)
    response = client.post('/api/messages/send', json={'conversationId':cid,
        'content':'你好', 'requestId':uuid4().hex})
    assert response.status_code == 200, response.text
    assert '另一人物的私密说明' not in json.dumps(seen[0], ensure_ascii=False)


def test_active_team_distinguishes_discussion_from_guidance(world, monkeypatch, tmp_path):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)
    authorize_workspace(client, tmp_path)
    origin = 'dm-' + ids[0]
    started = client.post('/api/messages/send', json={'conversationId':origin,
        'content':'请共同整理目录', 'mode':'swarm', 'requestId':uuid4().hex})
    assert started.status_code == 200, started.text
    team_id = started.json()['runId']
    team_room = started.json()['conversationId']

    async def decide(messages, _settings):
        current = json.loads(messages[-1]['content'])['current']
        kind = 'guidance' if '调整' in current else 'chat'
        return {'kind':kind, 'actorIds':[], 'confidence':.99, 'reason':'合成测试'}

    monkeypatch.setattr(coordinator.expression, 'complete', decide)
    discussed = client.post('/api/messages/send', json={'conversationId':team_room,
        'content':'这个目录的分类思路怎么样？', 'requestId':uuid4().hex})
    assert discussed.status_code == 200, discussed.text
    assert discussed.json()['runId'] != team_id
    assert coordinator.store.get(team_id)['steering'] == []
    guided = client.post('/api/messages/send', json={'conversationId':team_room,
        'content':'请调整当前任务，先看子目录', 'requestId':uuid4().hex})
    assert guided.status_code == 200, guided.text
    assert guided.json()['runId'] == team_id
    assert guided.json()['route']['kind'] == 'guidance'
    assert len(coordinator.store.get(team_id)['steering']) == 1

    coordinator.store.update(team_id, status='completed', result={'summary':'已结束'})

    async def new_task(_messages, _settings):
        return {'kind':'task', 'actorIds':[ids[0]], 'confidence':.98, 'reason':'新的明确委托'}

    monkeypatch.setattr(coordinator.expression, 'complete', new_task)
    next_task = client.post('/api/messages/send', json={'conversationId':team_room,
        'content':'请读取工作区里另一份资料', 'requestId':uuid4().hex})
    assert next_task.status_code == 200, next_task.text
    assert next_task.json()['runId'] != team_id
    assert coordinator.store.get(next_task.json()['runId'])['mode'] == 'task'


def test_selected_book_evidence_reaches_expression_without_bleeding_into_smalltalk(world, monkeypatch):
    coordinator, client, ids = world
    actor = next(a for a in client.get('/api/workspace/load').json()['workspace']['data']['agents']
        if a['id'] == ids[0])
    read, _ = coordinator.store.create({'actorId':ids[0], 'actors':[actor],
        'mode':'task', 'collaborative':False, 'conversationId':'dm-'+ids[0],
        'prompt':'概括电子书', 'history':[], 'workspace':''}, uuid4().hex)
    coordinator.store.put_call(uuid4().hex, read['id'], 'read_document',
        {'path':'电子书.pdf', 'offset':0, 'limit':10}, 'completed', {'pages':10}, phase='main')
    coordinator.store.update(read['id'], status='completed',
        assignments=[{'id':'main','actorId':ids[0], 'status':'completed'}],
        result={'summary':'读到前十页，讨论港区通讯与朋友关系。'})
    observed = []
    sourced_answer = '我只看过前十页，那里主要谈朋友之间怎样联络。' + '我觉得它把人与人的距离写得挺细，不过这只是已读部分的印象。' * 8
    assert 180 < len(sourced_answer) < 900

    async def generate(messages, _settings):
        observed.append(messages)
        return {'segments':[sourced_answer if len(observed)==1 else '嗯，我在。']}

    monkeypatch.setattr(coordinator.expression, 'generate', generate)
    followup, _ = coordinator.store.create({'actorId':ids[0], 'actors':[actor],
        'mode':'chat', 'collaborative':False, 'conversationId':'dm-'+ids[0],
        'prompt':'你觉得这本书怎么样？', 'history':[], 'workspace':'',
        'taskEvidenceHint':True}, uuid4().hex)
    assert asyncio.run(coordinator.expression.speak(followup, actor, 'chat', followup['prompt'])) == sourced_answer
    relevant = json.dumps(observed[-1], ensure_ascii=False)
    assert '电子书.pdf' in relevant and '前十页' in relevant
    assert read['id'] in relevant
    assert '上限900字' in relevant

    casual, _ = coordinator.store.create({'actorId':ids[0], 'actors':[actor],
        'mode':'chat', 'collaborative':False, 'conversationId':'port-hub',
        'prompt':'今晚想聊什么？', 'history':[], 'workspace':''}, uuid4().hex)
    asyncio.run(coordinator.expression.speak(casual, actor, 'chat', casual['prompt']))
    assert read['id'] not in json.dumps(observed[-1], ensure_ascii=False)

    unclear, _ = coordinator.store.create({'actorId':ids[0], 'actors':[actor],
        'mode':'chat', 'collaborative':False, 'conversationId':'port-hub',
        'prompt':'能帮我看看吗？', 'history':[], 'workspace':'',
        'routeKind':'clarify'}, uuid4().hex)
    asyncio.run(coordinator.expression.speak(unclear, actor, 'chat', unclear['prompt']))
    assert '尚未接单' in json.dumps(observed[-1], ensure_ascii=False)

    unknown, _ = coordinator.store.create({'actorId':ids[0], 'actors':[actor],
        'mode':'chat', 'collaborative':False, 'conversationId':'port-hub',
        'prompt':'你觉得另一册.pdf怎么样？', 'history':[], 'workspace':'',
        'routeKind':'followup', 'taskEvidenceHint':True}, uuid4().hex)
    asyncio.run(coordinator.expression.speak(unknown, actor, 'chat', unknown['prompt']))
    missing = json.dumps(observed[-1], ensure_ascii=False)
    assert '没有匹配的可核验材料' in missing
    assert read['id'] not in missing


def test_completed_read_api_followup_in_same_and_other_channel(world, monkeypatch, tmp_path):
    coordinator, client, ids = world
    monkeypatch.setattr(coordinator, 'launch', lambda _run_id: None)
    authorize_workspace(client, tmp_path)

    async def route(messages, _settings):
        prompt = json.loads(messages[-1]['content'])
        current = prompt['current']
        # The model lacks old work evidence and misclassifies this as clarify;
        # the host can correct the private chat only for a verified reader.
        kind = 'clarify' if '重新' in current else 'task' if '概括' in current else 'clarify' if prompt['channelKind']=='dm' else 'followup'
        return {'kind':kind, 'actorIds':[ids[0]],
            'confidence':.98, 'reason':'合成测试'}

    monkeypatch.setattr(coordinator.expression, 'complete', route)
    task = client.post('/api/messages/send', json={'conversationId':'dm-'+ids[0],
        'content':'请概括工作区里的电子书.pdf', 'requestId':uuid4().hex})
    assert task.status_code == 200, task.text
    task_id = task.json()['runId']
    coordinator.store.put_call(uuid4().hex, task_id, 'read_document',
        {'path':'电子书.pdf', 'offset':0, 'limit':10}, 'completed', {'pages':10}, phase='main')
    summary = '已读前十页，主题是港区远程通讯。'
    coordinator.store.update(task_id, status='completed', result={'summary':summary},
        assignments=[{'id':'main','actorId':ids[0], 'status':'completed',
            'brief':'概括电子书', 'dependsOn':[], 'result':{'summary':summary}}])
    asyncio.run(coordinator.finish_callback(task_id, summary))
    with session_scope() as session:
        saved = session.query(MemoryChunk).filter_by(actor_id=ids[0], source_kind='verified_task').one()
        assert saved.metadata_json['readSources'][0]['name'] == '电子书.pdf'
    for cid in ('dm-'+ids[0], 'port-hub'):
        followup = client.post('/api/messages/send', json={'conversationId':cid,
            'content':'你觉得这本书写得怎么样？', 'requestId':uuid4().hex})
        assert followup.status_code == 200, followup.text
        run = coordinator.store.get(followup.json()['runId'])
        assert run['mode'] == 'chat' and run['taskEvidenceHint']
        assert followup.json()['route']['kind'] == 'followup'
        evidence = resolve_task_reference(coordinator.store, ids[0], run['prompt'], run['history'],
            conversation_id=cid, public=cid=='port-hub')
        if cid == 'port-hub':
            assert evidence is None  # Asking in a group is not permission to share private reading.
        else:
            assert evidence['runId'] == task_id
    assert resolve_task_reference(coordinator.store, ids[1], '你觉得这本书写得怎么样？') is None
    fresh_request=client.post('/api/messages/send', json={'conversationId':'dm-'+ids[0],
        'content':'请重新读取这本书并概括', 'requestId':uuid4().hex})
    assert fresh_request.status_code == 200, fresh_request.text
    assert fresh_request.json()['route']['kind'] == 'clarify'
    assert not coordinator.store.get(fresh_request.json()['runId'])['taskEvidenceHint']
