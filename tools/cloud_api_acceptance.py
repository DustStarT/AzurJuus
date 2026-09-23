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


async def main():
    with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
        base, model, protected = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    key = reveal(protected)
    collaborative='--collaborative' in sys.argv
    home = Path(tempfile.mkdtemp(prefix='azur-cloud-task-'))
    work = home / 'work'
    work.mkdir(parents=True)
    (work / 'input.txt').write_text('Synthetic HTTP acceptance: red=13, blue=8.\n', encoding='utf-8')
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home / 'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home / 'state' / 'workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(work),
        AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='', AZURJUUS_CHROMA_PATH=str(home/'chroma'),
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='1', AZURJUUS_EXECUTION_BACKEND='hermes')
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
                'llmBaseUrl': base, 'llmModel': model, 'llmApiKey': key, 'authorizedWorkspaceRoot': str(work)}}})
            response.raise_for_status()
            (await client.post('/api/life',json={'paused':True})).raise_for_status()
            boot = (await client.get('/api/bootstrap')).json()
            assert key not in json.dumps(boot)
            assert boot['workspace']['settings']['llmApiKeyConfigured']
            report['checks'].append('saved credentials are hidden from bootstrap')
            cid = next(c['id'] for c in boot['snapshot']['conversations'] if c['kind'] == 'dm')
            payload = {'conversationId': cid, 'mode': 'task', 'collaborative':collaborative, 'requestId': uuid4().hex,
                'content': '读取 input.txt，将 red 与 blue 两个数量相加，将两个来源数量、算式和总数写入 api-report.txt。读取输出核验再 deliver。只处理这两个文件，不执行命令、不联网。'}
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
                    await asyncio.sleep(2)
            assert run['status'] == 'completed', run.get('error')
            report['checks'].append('production background scheduler completes real cloud task')
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
            if collaborative:
                assert any(m['id']==rid+'-origin-result' for m in final['snapshot']['messages'][cid])
            events = (await client.get('/api/runtime/events')).json()['events']
            assert any(e['type'] == 'conversation.changed' and e['runId'] == rid for e in events)
            report['checks'].append('one user message, one delivered result, persisted conversation events')
            report['status'] = 'passed'
            report['artifacts'] = run['artifacts']
    except Exception as exc:
        report['error'] = str(exc).replace(key, '[redacted]')
    finally:
        server.shutdown()
        await asyncio.to_thread(thread.join, 20)
        server.server_close()
        (ROOT / ('validation/cloud-api-collaborative-report.json' if collaborative else 'validation/cloud-api-report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
