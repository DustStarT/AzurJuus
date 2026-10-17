"""Real Harness PDF -> chat acceptance in an isolated workspace using saved credentials.

Only synthetic PDF data is sent. Does not change the user's conversations/settings.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def synthetic_pdf(path):
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 40 740 Td (Synthetic reading record: Lighthouse.) Tj '
        b'0 -20 Td (Sailors exchange letters every Friday to stay connected.) Tj '
        b'0 -20 Td (The document code is AZUR-CONTEXT-73.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as f:
        writer.write(f)


async def main():
    from backend.credentials import reveal
    with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
        base, model, protected = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    if not protected:
        raise SystemExit('没有已配置的模型凭据，真实模型验收未运行。')
    key = reveal(protected)
    home = Path(tempfile.mkdtemp(prefix='azur-context-'))
    work = home / 'work'
    work.mkdir()
    synthetic_pdf(work / 'Lighthouse Notes.pdf')
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home / 'business.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home / 'state' / 'workspace.json'),
        AZURJUUS_WORKSPACE_ROOT=str(work), AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='',
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='1', AZURJUUS_TASK_REVIEWS_ENABLED='0',
        AZURJUUS_REFLECTION_ENABLED='0', AZURJUUS_SKILL_TRIALS_ENABLED='0')
    from backend.config import get_settings
    get_settings.cache_clear()
    from server import create_server
    server = create_server(host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {'model': model, 'kind': 'real-Harness-PDF-to-chat', 'status': 'failed', 'checks': []}
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{server.server_address[1]}', timeout=120) as client:
            for _ in range(100):
                try:
                    boot = await client.get('/api/bootstrap')
                    if boot.is_success:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.1)
            snapshot = boot.json()['workspace']['data']
            actor = snapshot['agents'][0]
            cid = next(r['id'] for r in snapshot['conversations'] if r['kind'] == 'dm' and actor['id'] in r['memberIds'])
            (await client.post('/api/life', json={'paused': True})).raise_for_status()
            (await client.post('/api/workspace/save', json={'workspace': {'settings': {
                'llmBaseUrl': base, 'llmModel': model, 'llmApiKey': key, 'authorizedWorkspaceRoot': str(work),
                'connectedAgentIds': [actor['id']], 'secretaryAgentId': actor['id'], 'dialoguePreset': 'balanced'}}})).raise_for_status()
            (await client.post('/api/context/settings', json={'provider': 'local',
                'localModelPath': str(ROOT / '.azurjuus/embedding-model')})).raise_for_status()
            async def send(mode, content):
                response = await client.post('/api/runs', json={'conversationId': cid, 'mode': mode,
                    'prompt': content, 'requestId': uuid4().hex})
                response.raise_for_status()
                rid = response.json()['runId']
                previous = None
                async with asyncio.timeout(300):
                    while True:
                        response = await client.get('/api/runs/' + rid)
                        response.raise_for_status()
                        detail = response.json()
                        run = detail['run']
                        state = {'mode': mode, 'status': run['status'], 'calls': len(detail['calls'])}
                        if state != previous:
                            print(json.dumps(state), flush=True)
                            previous = state
                        if run['status'] in {'failed', 'paused', 'cancelled'}:
                            raise RuntimeError('隔离验收任务失败：' + str(run.get('error')))
                        if run['status'] == 'completed' and (mode == 'chat' or run.get('resultMessageId')):
                            return detail
                        await asyncio.sleep(1)
            task = await send('task', '先使用 method_search 和 method_select 选用已有方法，再用 read_document 读取 "Lighthouse Notes.pdf"。'
                '告诉我它讲什么以及文档编号。只读这个文件，不写文件、不执行命令、不联网，依据成功读取记录 deliver。')
            calls = [c for c in task['calls'] if c['name'] == 'read_document' and c['status'] == 'completed']
            assert calls, '没有真实成功的 read_document 调用'
            report['checks'].append('official Harness performed a successful PDF read')
            sources = (await client.get('/api/context/runs/' + task['run']['id'] + '/sources')).json()['sources']
            assert sources and sources[0]['data']['location']['page'] == 1
            report['checks'].append('actual returned PDF page indexed with reader and version')
            chat = await send('chat', '这个 PDF 的内容你觉得怎么样？请说一个具体看法，并再次告诉我那份文件的文档编号。')
            data = (await client.get('/api/workspace/load')).json()['workspace']['data']
            messages = [m for m in data['messages'][cid] if m['speakerId'] == actor['id']
                        and (m.get('metadata') or {}).get('runId') == chat['run']['id']]
            answer = '\n'.join(m['body'] for m in messages)
            assert 'AZUR-CONTEXT-73' in answer, '闲聊未恢复 PDF 中的准确编号'
            report['answer'] = answer
            report['checks'].append('later chat recovered the exact PDF code and gave an opinion')
            if messages:
                source_detail = (await client.get('/api/context/messages/' + messages[-1]['id'] + '/sources')).json()
                report['traceIds'] = source_detail.get('traces', [])
                assert source_detail.get('sources'), '消息没有绑定来源'
                report['checks'].append('answer exposes its permitted sources')
            (await client.post('/api/workspace/save', json={'workspace': {'settings': {
                'dialoguePreset': 'compact'}}})).raise_for_status()
            compact = await send('chat', '这个 PDF 的编号还记得吗？只需再告诉我编号。')
            data = (await client.get('/api/workspace/load')).json()['workspace']['data']
            compact_messages = [m for m in data['messages'][cid] if m['speakerId'] == actor['id']
                and (m.get('metadata') or {}).get('runId') == compact['run']['id']]
            compact_answer = '\n'.join(m['body'] for m in compact_messages)
            assert 'AZUR-CONTEXT-73' in compact_answer, '省流模式未保持准确证据'
            report['compactAnswer'] = compact_answer
            if compact_messages:
                linked = (await client.get('/api/context/messages/' + compact_messages[-1]['id'] + '/sources')).json()
                traces = [(await client.get('/api/context/traces/' + tid)).json() for tid in linked['traces']]
                stages = [call['stage'] for trace in traces for call in trace.get('providerCalls', [])]
                assert 'compact' in stages, stages
                report['compactStages'] = stages
                report['compactReadFollowupUsedFullFallback'] = 'understand' in stages
            report['checks'].append('real compact mode preserves exact reading evidence, with safe fallback allowed')
            greeting = await send('chat', '谢谢你，今天聊得很开心。')
            data = (await client.get('/api/workspace/load')).json()['workspace']['data']
            greetings = [m for m in data['messages'][cid] if m['speakerId'] == actor['id']
                and (m.get('metadata') or {}).get('runId') == greeting['run']['id']]
            assert greetings
            linked = (await client.get('/api/context/messages/' + greetings[-1]['id'] + '/sources')).json()
            traces = [(await client.get('/api/context/traces/' + tid)).json() for tid in linked['traces']]
            stages = [call['stage'] for trace in traces for call in trace.get('providerCalls', [])]
            assert 'compact' in stages and 'understand' not in stages and 'decide' not in stages, stages
            report['compactGreetingStages'] = stages
            report['checks'].append('ordinary real chat uses merged thought then expression')
            report['status'] = 'passed'
    except Exception as exc:
        report['error'] = (type(exc).__name__ + ': ' + str(exc)).replace(key, '[redacted]')
    finally:
        server.shutdown()
        await asyncio.to_thread(thread.join, 30)
        server.server_close()
        output = ROOT / 'validation/context-live-harness.json'
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
