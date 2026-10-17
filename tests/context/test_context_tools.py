import asyncio
from uuid import uuid4

from conftest import choose_fixture_method
from test_context_service import room


def test_full_index_and_readonly_context_tools_use_the_authorized_gateway(world, tmp_path):
    c, client, ids = world
    cid = room(client, ids[0])
    work = tmp_path.parent / ('context-files-' + uuid4().hex)
    work.mkdir()
    path = work / 'long notes.txt'
    path.write_text('\n'.join(f'Synthetic row {i}: sailors exchange letters.' for i in range(250)), encoding='utf-8')
    run, _ = c.store.create({'actorId': ids[0], 'actors': [{'id': ids[0], 'name': 'reader'}],
        'mode': 'task', 'collaborative': False, 'conversationId': cid, 'workspace': str(work),
        'prompt': '请索引全文 long notes.txt'}, uuid4().hex)
    c.store.update(run['id'], status='running')
    token = uuid4().hex
    c.tokens[token] = (run['id'], ids[0], 'main')
    async def scenario():
        await choose_fixture_method(c, token)
        async def call(name, args):
            return await c.tool(token, {'callId': uuid4().hex, 'name': name, 'args': args})
        result = await call('index_document', {'path': path.name})
        assert result['status'] == 'completed', result
        assert len(result['result']['checks']) == 2
        reads = [v for v in c.store.calls(run['id']) if v['name'] == 'read_file']
        assert len(reads) == 2 and reads[-1]['result']['nextOffset'] is None
        found = await call('search_context', {'query': 'long notes.txt'})
        assert found['status'] == 'completed' and found['result']['sources']
        source = next(s for s in found['result']['sources'] if s.get('kind') == 'document')
        # Context reads use stored evidence, not a fresh, unjournalled disk read.
        path.write_text('CHANGED_AFTER_READING', encoding='utf-8')
        remembered = await call('read_context_source', {'sourceId': source['sourceId']})
        assert remembered['status'] == 'completed'
        assert 'CHANGED_AFTER_READING' not in remembered['result']['text']
        denied = await call('index_document', {'path': '../outside.txt'})
        assert denied['status'] == 'failed'
    try:
        asyncio.run(scenario())
    finally:
        c.tokens.pop(token, None)
        c.store.update(run['id'], status='completed', result={'summary': 'synthetic acceptance'})


def test_ordinary_read_cannot_silently_request_full_index(world, tmp_path):
    c, client, ids = world
    cid = room(client, ids[0])
    run, _ = c.store.create({'actorId': ids[0], 'actors': [{'id': ids[0], 'name': 'reader'}],
        'mode': 'task', 'collaborative': False, 'conversationId': cid, 'workspace': str(tmp_path),
        'prompt': '查看文件第一段'}, uuid4().hex)
    c.store.update(run['id'], status='running')
    token = uuid4().hex
    c.tokens[token] = (run['id'], ids[0], 'main')
    result = asyncio.run(c.tool(token, {'callId': uuid4().hex, 'name': 'index_document', 'args': {'path': 'notes.txt'}}))
    assert result['status'] == 'failed'
    assert not any(v['name'] == 'read_file' for v in c.store.calls(run['id']))
    c.tokens.pop(token, None)
    c.store.update(run['id'], status='failed')
