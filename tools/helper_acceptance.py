"""Short cloud acceptance using only saved connected members and isolated synthetic data."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    from backend.credentials import reveal
    with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
        base, model, protected, connected, secretary = db.execute(
            'SELECT llm_base_url,llm_model,llm_api_key,connected_agent_ids,secretary_agent_id FROM workspace_settings LIMIT 1').fetchone()
    connected = json.loads(connected)
    if secretary not in connected or len(connected) < 2:
        raise ValueError('短期外援验收需要当前配置的秘书和至少另一名已连接成员。')
    helper_id = next(aid for aid in connected if aid != secretary)
    key = reveal(protected)
    home = Path(tempfile.mkdtemp(prefix='azur-helper-'))
    work = home / 'work'
    work.mkdir()
    (work / 'source.txt').write_text('SOURCE_OK=42\n', encoding='utf-8')
    (work / 'helper.txt').write_text('HELPER_OK=7\n', encoding='utf-8')
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home / 'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home / 'state/workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(work),
        AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='', AZURJUUS_CHROMA_PATH=str(home / 'chroma'),
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='0', AZURJUUS_EXPRESSION_ENABLED='0')
    from server import create_server
    from backend.database import session_scope
    from backend.models import Actor, Conversation, ConversationMember
    server = create_server(host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {'model':model, 'members':[secretary, helper_id], 'status':'failed', 'checks':[]}
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{server.server_address[1]}', timeout=30) as client:
            for _ in range(100):
                try:
                    if (await client.get('/api/health')).is_success:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.1)
            (await client.post('/api/workspace/save', json={'workspace':{'settings':{
                'llmBaseUrl':base, 'llmModel':model, 'llmApiKey':key, 'connectedAgentIds':connected,
                'secretaryAgentId':secretary, 'authorizedWorkspaceRoot':str(work)}}})).raise_for_status()
            c = server.app.state.runs

            async def setup():
                with session_scope() as session:
                    rows = [session.get(Actor, aid) for aid in [secretary, helper_id]]
                    if any(row is None or not row.is_active for row in rows):
                        raise ValueError('隔离测试人物名单不包含当前已连接成员；不能换成配置外人物。')
                    actors = [server.app.state.service.serialize_actor(row) for row in rows]
                    session.add(Conversation(id='helper-cloud-room', kind='group', title='合成外援验收'))
                    session.flush()
                    session.add_all([ConversationMember(conversation_id='helper-cloud-room', actor_id=aid, role='member')
                                     for aid in ['commander', secretary]])
                run = await c.admit({'conversationId':'helper-cloud-room','mode':'task','collaborative':True,
                    'prompt':'读取 source.txt 报告其数值。若外援加入，请在最终复核时同时读取 helper.txt 并汇总两项核验结论。只读，不修改文件，不运行命令。'},
                    actors, c.settings_loader(), [], start_now=False)
                c.store.update(run['id'], status='running', teamConversationId='helper-cloud-room')
                c.plan(run['id'], {'tasks':[{'id':'main','actorId':secretary,'brief':'读取 source.txt 并核验 SOURCE_OK=42。',
                                           'acceptance':['引用本人成功读取的 callId 并交付文字结果'], 'dependsOn':[]}]})
                c.patch_assignment(run['id'], 'main', brief='先调用 request_help 申请当前已连接成员 ' + helper_id +
                    '（candidateIds=[' + json.dumps(helper_id, ensure_ascii=False) + ']），请求其读取 helper.txt 核验 HELPER_OK=7，并交付本人读取证据；理由是独立核验。'
                    '随后你自己读取 source.txt 并核验 SOURCE_OK=42，交付文字结论。不要自行批准入群。')
                c.launch(run['id'])
                return run['id']

            rid = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(setup(), server._loop))
            report['runId'] = rid
            async with asyncio.timeout(240):
                while True:
                    state = (await client.get('/api/runs/' + rid)).json()
                    requests = state['run'].get('helpRequests', [])
                    from tools.acceptance_reviews import approve_async
                    report.setdefault('userStageApprovals',[]).extend(await approve_async(client,state['run'],work))
                    if requests:
                        request_id = requests[0]['id']
                        assert requests[0]['candidateIds'] == [helper_id]
                        break
                    if state['run']['status'] in {'paused','failed','completed'}:
                        raise RuntimeError('原成员未进入可加入外援的执行阶段。')
                    await asyncio.sleep(.1)
                accepted = await client.post(f'/api/workflows/help-request/{rid}/{request_id}',
                                            json={'decision':'approved','actorId':helper_id})
                accepted.raise_for_status()
                helper_assignment = accepted.json()['result']['assignment']['id']
                report['checks'].append('configured helper admitted while original member was running')
                while True:
                    state = (await client.get('/api/runs/' + rid)).json()
                    run = state['run']
                    report.setdefault('userStageApprovals',[]).extend(await approve_async(client,run,work))
                    if run['status'] in {'completed','failed','paused','cancelled'}:
                        break
                    await asyncio.sleep(1)
            assert run['status'] == 'completed', run.get('error')
            assert any(call['name'] == 'request_help' and call['status'] == 'completed' for call in state['calls'])
            report['checks'].append('original native model requested only the configured helper through the audited tool')
            helper = next(item for item in run['assignments'] if item['id'] == helper_assignment)
            assert helper['status'] == 'completed' and helper['actorId'] == helper_id
            checks = {item['callId'] for item in helper['result']['checks']}
            assert any(call['id'] in checks and call['status'] == 'completed' and call['name'] == 'read_file'
                       and call['args']['path'] == 'helper.txt' for call in state['calls'])
            report['checks'].append('native helper read synthetic file and submitted genuine evidence')
            room = next(room for room in (await client.get('/api/bootstrap')).json()['snapshot']['conversations']
                        if room['id'] == 'helper-cloud-room')
            assert helper_id in room['memberIds']
            assert '42' in run['result']['summary'] and '7' in run['result']['summary']
            report['checks'].append('membership sync and final read-only review include both results')
            report['status'] = 'passed'
    except Exception as exc:
        report['error'] = str(exc).replace(key, '[redacted]')
    finally:
        server.shutdown()
        thread.join(timeout=15)
        server.server_close()
        report['shutdownClean'] = not thread.is_alive()
    output = ROOT / 'validation/helper-cloud'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report['status'] == 'passed' and report['shutdownClean'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
