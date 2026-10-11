"""Real provider through the production HTTP admission and artifact APIs."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import tempfile
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.credentials import reveal
from tools.acceptance_reviews import approve_async


async def main():
    with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
        base, model, protected, connected_raw, secretary = db.execute('SELECT llm_base_url,llm_model,llm_api_key,connected_agent_ids,secretary_agent_id FROM workspace_settings LIMIT 1').fetchone()
    connected=json.loads(connected_raw)
    key = reveal(protected)
    collaborative='--collaborative' in sys.argv
    progress_check='--progress-check' in sys.argv
    home = Path(tempfile.mkdtemp(prefix='azur-cloud-task-'))
    work = home / 'work'
    work.mkdir(parents=True)
    (work / 'input.txt').write_text('Synthetic HTTP acceptance: red=13, blue=8.\n', encoding='utf-8')
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home / 'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home / 'state' / 'workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(work),
        AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='', AZURJUUS_CHROMA_PATH=str(home/'chroma'),
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='1', AZURJUUS_EXECUTION_BACKEND='deepseek-harness',
        AZURJUUS_SKILL_TRIALS_ENABLED='0')
    from server import create_server
    server = create_server(host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:' + str(server.server_address[1])
    report = {'model': model, 'collaborative':collaborative, 'status': 'failed', 'checks': [], 'workspace': str(work)}
    try:
        async with httpx.AsyncClient(base_url=url, timeout=30) as client:
            for _ in range(80):
                try:
                    response = await client.get('/api/health')
                    if response.is_success:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.1)
            response = await client.post('/api/workspace/save', json={'workspace': {'settings': {
                'llmBaseUrl': base, 'llmModel': model, 'llmApiKey': key, 'authorizedWorkspaceRoot': str(work),
                'connectedAgentIds':connected,'secretaryAgentId':secretary}}})
            response.raise_for_status()
            (await client.post('/api/life',json={'paused':True})).raise_for_status()
            boot = (await client.get('/api/bootstrap')).json()
            assert key not in json.dumps(boot)
            assert boot['workspace']['settings']['llmApiKeyConfigured']
            report['checks'].append('saved credentials are hidden from bootstrap')
            assert set(connected)<={a['id'] for a in boot['snapshot']['agents']}
            cid = next(c['id'] for c in boot['snapshot']['conversations'] if c['kind'] == 'dm' and secretary in c['memberIds'])
            payload = {'conversationId': cid, 'mode': 'task', 'collaborative':collaborative, 'requestId': uuid4().hex,
                'content': '先用 method_search 查看可见方法，自主选用或组合 method_select。读取 input.txt，将 red 与 blue 两个数量相加，将两个来源数量、算式和总数写入 api-report.txt。读取输出核验再 deliver。只处理这两个文件，不执行命令、不联网。'}
            accepted = await client.post('/api/messages/send', json=payload)
            accepted.raise_for_status()
            rid = accepted.json()['runId']
            duplicate = await client.post('/api/messages/send', json=payload)
            duplicate.raise_for_status()
            assert duplicate.json()['runId'] == rid
            report['checks'].append('duplicate HTTP admission returns the same task')
            report['runId'] = rid
            async with asyncio.timeout(360):
                previous = None
                while True:
                    response = await client.get('/api/runs/' + rid)
                    response.raise_for_status()
                    state = response.json()
                    run = state['run']
                    progress = {'status': run['status'], 'calls': len(state['calls'])}
                    if progress != previous:
                        print(json.dumps(progress), flush=True)
                        previous = progress
                    if run['status'] in {'failed', 'paused', 'cancelled'} or run['status']=='completed' and run.get('resultMessageId'):
                        break
                    for call in state['calls']:
                        if call['status'] == 'waiting_approval':
                            raise RuntimeError('Unexpected approval in the file-only HTTP fixture')
                    report.setdefault('userStageApprovals',[]).extend(await approve_async(client,run,work))
                    await asyncio.sleep(2)
            assert run['status'] == 'completed', run.get('error')
            report['checks'].append('production background scheduler completes real cloud task')
            assert report.get('userStageApprovals') and all(r['status']=='approved' for r in run.get('stageReviews',{}).values())
            assert any(c['name']=='method_search' and c['status']=='completed' for c in state['calls'])
            assert any(c['name']=='method_select' and c['status']=='completed' for c in state['calls'])
            report['checks'].append('model independently retrieves and selects methods; secretary and synthetic user approve plan, stages and final result')
            index = next(i for i, artifact in enumerate(run['artifacts']) if artifact['path'] == 'api-report.txt')
            downloaded = await client.get(f'/api/runs/{rid}/artifacts/{index}')
            downloaded.raise_for_status()
            assert downloaded.content == (work / 'api-report.txt').read_bytes()
            assert '21' in downloaded.content.decode('utf-8')
            report['checks'].append('artifact download matches actual file and total 21')
            final = (await client.get('/api/bootstrap')).json()
            messages = final['snapshot']['messages'][run['conversationId']]
            assert len([m for m in messages if m['id'] == rid + '-user']) == 1
            assert len([m for m in messages if m['id'] == run['resultMessageId'] and m.get('text',m.get('body',''))]) == 1
            result_message=next(m for m in messages if m['id']==run['resultMessageId'])
            assert result_message['metadata'].get('expression') is True, 'Must pass role expression, not fallback delivery'
            assert not result_message['metadata'].get('expressionFallback') and not run.get('expressionError')
            report['checks'].append('role expression succeeds without verified-result fallback')
            if progress_check:
                phases=[m.get('metadata',{}).get('phase','') for m in messages
                    if m.get('metadata',{}).get('runId')==rid and m.get('metadata',{}).get('expression')]
                assert 'task_start' in phases, phases
                assert any(p.startswith('work_update_') for p in phases), phases
                if collaborative:assert 'plan_notice' in phases, phases
                report['progressPhases']=phases
                report['checks'].append('character progress and actual team division are visible')
            if collaborative:
                assert any(m['id']==rid+'-origin-result' for m in final['snapshot']['messages'][cid])
            events = (await client.get('/api/runtime/events')).json()['events']
            assert any(e['type'] == 'conversation.changed' and e['runId'] == rid for e in events)
            report['checks'].append('one user message, one delivered result, persisted conversation events')
            if progress_check:
                casual=await client.post('/api/messages/send',json={'conversationId':cid,'mode':'chat',
                    'content':'今天想聊点轻松的：你最近喜欢什么？','requestId':uuid4().hex})
                casual.raise_for_status()
                chat_id=casual.json()['runId']
                async with asyncio.timeout(150):
                    while True:
                        chat_run=(await client.get('/api/runs/'+chat_id)).json()['run']
                        if chat_run['status'] in {'completed','failed'}:break
                        await asyncio.sleep(2)
                assert chat_run['status']=='completed',chat_run.get('error')
                answer=(chat_run.get('result') or {}).get('summary','')
                assert answer and not any(item in answer for item in ('api-report.txt','red=13','blue=8')),answer
                report['casualReply']=answer
                report['checks'].append('later casual reply does not repeat task evidence')
            report['status'] = 'passed'
            report['artifacts'] = run['artifacts']
            if '--evolution' in sys.argv:
                owner=next(a for a in run['assignments'] if a['status']=='completed')
                choice=run.get('methods',{}).get(owner['id'],{})
                response=await client.post(f"/api/actors/{owner['actorId']}/methods/evolve",
                    json={'sourceRunId':rid,'basedOn':choice.get('ids',[])},timeout=200)
                response.raise_for_status();proposal=response.json()
                report['evolutionProposal']=proposal
                assert proposal['status']=='awaiting_trial_confirmation',proposal
                sid=proposal['skillId']
                response=await client.post(f"/api/actors/{owner['actorId']}/methods/{sid}/confirm_trial",json={})
                response.raise_for_status()
                report['checks'].append('real model proposes three scenarios from its verified execution; actual secretary and synthetic user approve bound specification')
                (await client.post('/api/life',json={'paused':False,'hourlyCalls':200})).raise_for_status()
                future=asyncio.run_coroutine_threadsafe(server.app.state.runs.growth.trial(owner['actorId'],sid),server._loop)
                async with asyncio.timeout(900):
                    await asyncio.wrap_future(future)
                methods=(await client.get(f"/api/actors/{owner['actorId']}/methods")).json()['skills']
                skill=next(s for s in methods if s['id']==sid)
                report['evolutionResult']=skill
                assert skill['enabled'] and skill['validationCount']==3 and skill['trialSummary']['stable'],skill
                assert len(skill['trialEvidence'])==6 and all(e['passed'] for e in skill['trialEvidence'] if e['variant']=='candidate')
                report['checks'].append('six actual Harness runs compare original and evolved method against real files; three candidate scenarios pass and activate')
    except Exception as exc:
        report['status']='failed'
        report['error'] = str(exc).replace(key, '[redacted]')
    finally:
        server.shutdown()
        await asyncio.to_thread(thread.join, 20)
        server.server_close()
        path=('validation/cloud-api-progress-collaborative.json' if collaborative else 'validation/cloud-api-progress-single.json') if progress_check else ('validation/cloud-api-collaborative-report.json' if collaborative else 'validation/cloud-api-report.json')
        (ROOT / path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
