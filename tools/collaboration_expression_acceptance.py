"""Three synthetic collaboration utterances, real model, isolated data.

This checks expression only, not file execution or human-rated naturalness.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    from backend.credentials import reveal
    with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
        base, model, secret = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    home = ROOT / '.azurjuus/collaboration-expression' / uuid4().hex
    home.mkdir(parents=True)
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(home),
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_REFLECTION_ENABLED='0', AZURJUUS_SKILL_TRIALS_ENABLED='0',
        AZURJUUS_EXECUTION_BACKEND='hermes', AZURJUUS_EXPRESSION_ENABLED='1')
    from backend.app import create_app
    from backend.terminal_characters import activate
    from fastapi.testclient import TestClient
    app = create_app()
    report = {'model':model, 'scope':'synthetic expression only; no actual file task', 'humanReview':'pending', 'scenes':[]}
    with TestClient(app) as client:
        client.post('/api/workspace/save', json={'workspace':{'settings':{'llmBaseUrl':base,'llmModel':model,'llmApiKey':reveal(secret)}}}).raise_for_status()
        agents = client.get('/api/workspace/load').json()['workspace']['data']['agents']
        a, b = agents[:2]
        for actor in (a, b):
            activate(actor['id'], 'restore')
        c = app.state.runs
        run, _ = c.store.create({'actors':[a,b], 'actorId':a['id'], 'mode':'task', 'collaborative':True,
            'conversationId':'synthetic', 'prompt':'整理三份文件', 'history':[]}, uuid4().hex)
        scenes = [
            (a, 'discussion-question', '直接向同伴提出分类建议。', {'statement':'不要只按扩展名分类，应先检查文档内容。'},
                {'kind':'peer','id':b['id'],'name':b['name']}),
            (b, 'discussion-reply', '回应同伴的建议，说明自己的具体理由。', {'statement':'其中一份扫描件暂时读不到正文，建议单列待检查，其余两份按内容分类。'},
                {'kind':'peer','id':a['id'],'name':a['name']}),
            (a, 'result', '在当前协作群收尾，说清结果和仍未解决的一件事。',
                {'status':'completed','summary':'两份文档已按内容分类，一份扫描件单列待检查；没有删除文件。'},
                {'kind':'team','name':'当前协作群','includesUser':True,'members':[{'id':x['id'],'name':x['name']} for x in (a,b)]}),
        ]
        for actor, phase, intent, facts, audience in scenes:
            started = time.monotonic()
            text = await c.expression.speak(run, actor, phase, intent, facts, audience=audience)
            report['scenes'].append({'actor':actor['name'],'phase':phase,'text':text,
                'characters':len(text),'seconds':round(time.monotonic()-started,2),'passed':bool(text) and len(text)<=180})
            run['history'].append({'role':'assistant','content':actor['name']+'：'+text})
            if not text:
                break
    report['status'] = 'passed' if len(report['scenes'])==3 and all(s['passed'] for s in report['scenes']) else 'failed'
    out = ROOT/'validation/collaboration-expression'
    out.mkdir(parents=True, exist_ok=True)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
