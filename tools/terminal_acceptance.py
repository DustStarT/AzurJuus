"""Twelve synthetic scenes, real configured model; never writes live user data."""
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

SCENES = [
    ('chat','你好，在忙吗？',None),
    ('chat','今天有点累，不想说工作。',None),
    ('chat','谢谢，刚才帮上忙了。',None),
    ('chat','你们在港区平时会聊什么？',None),
    ('result','告诉用户整理结果。',{'status':'completed','summary':'3份文件已经分类，逐项核验通过。'}),
    ('result','告诉用户还有什么需要确认。',{'status':'waiting_approval','summary':'发现两份内容相同的文件，尚未删除，需要用户决定是否保留。'}),
    ('result','承认刚才遗漏并说明已修正。',{'status':'completed','summary':'刚才漏了一份附件，现在已补入清单并核验。'}),
    ('discussion_a','向同伴提出不同意见。',{'statement':'不应只按扩展名分类，需要先检查文档内容。'}),
    ('discussion_b','向同伴求助，不替同伴回答。',{'statement':'这份文档没有可提取的正文，请帮忙判断下一步。'}),
    ('discussion_c','回应同伴的质疑，承认具体问题。',{'statement':'同伴指出清单少了一项，经核实确实遗漏，准备补齐。'}),
    ('chat','解释一下为什么保留重复文件，简短一点。',None),
    ('chat','豪和乔治五世是什么关系？',None),
]

async def main():
    from backend.credentials import reveal
    with sqlite3.connect((ROOT/'.azurjuus/azurjuus.db').as_uri()+'?mode=ro',uri=True) as db:
        base, model, secret = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
        rows = db.execute("SELECT length(body),body FROM messages WHERE type='task_progress' ORDER BY created_at DESC LIMIT 100").fetchall()
        baseline = {'sampleCount':len(rows), 'meanCharacters':sum(r[0] for r in rows)/max(1,len(rows)),
            'over180':sum(r[0]>180 for r in rows)}
    home = ROOT/'.azurjuus/terminal-acceptance'/uuid4().hex
    home.mkdir(parents=True)
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'),AZURJUUS_WORKSPACE_ROOT=str(home),
        AZURJUUS_SOCIAL_ENABLED='0',AZURJUUS_REFLECTION_ENABLED='0',AZURJUUS_SKILL_TRIALS_ENABLED='0',
        AZURJUUS_EXECUTION_BACKEND='hermes',AZURJUUS_EXPRESSION_ENABLED='1')
    from backend.app import create_app
    from backend.characters.terminal_characters import activate
    from fastapi.testclient import TestClient
    app = create_app()
    out = ROOT/'validation/terminal'
    out.mkdir(parents=True,exist_ok=True)
    report = {'model':model,'historicalAggregate':baseline,'scenes':[], 'humanReview':'pending',
        'comparison':'Synthetic scenes against reconstructed old prompt; not a replay of private conversations.'}
    with TestClient(app) as client:
        client.post('/api/workspace/save',json={'workspace':{'settings':{'llmBaseUrl':base,'llmModel':model,'llmApiKey':reveal(secret)}}}).raise_for_status()
        agents = client.get('/api/workspace/load').json()['workspace']['data']['agents']
        c = app.state.runs
        for i,(phase,intent,facts) in enumerate(SCENES):
            actor = agents[i%len(agents)]
            old = actor.get('systemPrompt','')
            activate(actor['id'],'restore')
            run,_=c.store.create({'actors':agents,'actorId':actor['id'],'mode':'chat' if phase=='chat' else 'task',
                'prompt':intent,'history':[],'collaborative':False,'conversationId':'synthetic'},uuid4().hex)
            source=run['id']+':'+phase
            started=time.monotonic()
            sample={'scene':i+1,'actor':actor['name'],'intent':intent,'facts':facts}
            try:
                before=await c.expression.complete([{'role':'system','content':old},
                    {'role':'user','content':json.dumps({'intent':intent,'facts':facts},ensure_ascii=False)+'\n返回 JSON，正文放在 text 字段。'}],c.settings_loader())
                sample['before']=before.get('text',str(before))
                sample['after']=await c.expression.speak(run,actor,phase,intent,facts)
                sample['passed']=bool(sample['after']) and len(sample['after'])<=180
            except Exception as exc:
                sample.update(passed=False,error=type(exc).__name__ + ': ' + str(exc)[:160])
                # Stop on a provider-wide failure instead of repeating paid requests.
                if 'HTTP ' in str(exc):
                    report['providerError']=str(exc)
                    report['scenes'].append(sample)
                    break
            sample['seconds']=round(time.monotonic()-started,2)
            report['scenes'].append(sample)
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(f"SCENE {i+1}/12 {'PASS' if sample['passed'] else 'FAIL'}",flush=True)
        report['under120Rate']=sum(len(s.get('after',''))<=120 and s['passed'] for s in report['scenes'])/12
        report['status']='passed' if all(s['passed'] for s in report['scenes']) and report['under120Rate']>=.9 else 'needs_revision'
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'status':report['status'],'under120Rate':report['under120Rate']}),flush=True)

if __name__=='__main__': asyncio.run(main())
