"""Actual runtime comparison in an isolated database; human review stays blank.

--prepare never reads credentials. --live reads only the configured provider,
and sends shipped character material plus synthetic scenes, never chat history.
"""
import argparse
import asyncio
import csv
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from personality_benchmark import SCENES


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def main(args):
    out=ROOT/'validation'/('mind-interactions-live' if args.interaction_only else 'mind-life-live' if args.life_only else 'mind-runtime-full' if args.full_only else 'mind-runtime-smoke' if args.smoke else 'mind-runtime')
    out.mkdir(parents=True,exist_ok=True)
    manifest={'comparison':'actual ExpressionService legacy/full MindRuntime; no real tools',
        'scenes':[] if args.life_only or args.interaction_only else SCENES[:2] if args.smoke else SCENES,'repetitions':3,
        'continuityTurns':0 if args.smoke or args.life_only or args.interaction_only else 30,'humanReview':'pending',
        'goalProgress':'mechanism fixtures plus continuous life decisions; not skill certification',
        'status':'prepared_not_executed'}
    write(out/'manifest.json',manifest)
    if not args.live:
        print('Prepared actual-runtime comparison; no provider calls.');return
    from backend.credentials import reveal
    with sqlite3.connect((ROOT/'.azurjuus/azurjuus.db').as_uri()+'?mode=ro',uri=True) as db:
        base,model,protected=db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    provider={'llmBaseUrl':base,'llmModel':model,'llmApiKey':reveal(protected)}
    temp=Path(tempfile.mkdtemp(prefix='azur-mind-live-'))
    (temp/'workspace').mkdir()
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(temp/'business.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(temp/'state.json'),AZURJUUS_WORKSPACE_ROOT=str(temp/'workspace'),
        AZURJUUS_CHROMA_PATH=str(temp/'chroma'),AZURJUUS_CHROMA_URL='',AZURJUUS_REDIS_URL='',
        AZURJUUS_SOCIAL_ENABLED='0',AZURJUUS_MIND_ENABLED='0',AZURJUUS_REFLECTION_ENABLED='0',
        AZURJUUS_SKILL_TRIALS_ENABLED='0',AZURJUUS_EXPRESSION_ENABLED='1')
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.app import create_app
    from backend.database import session_scope
    from backend.models import Actor
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    app=create_app()
    records=[]
    async def run(c):
        runtime=c.cognition.runtime
        life_tick=runtime.life.tick
        async def idle():pass
        runtime.life.tick=idle
        c.settings_loader=lambda:provider
        # Fail fast before a large comparison if the configured provider is unavailable.
        await c.expression.complete([{'role':'user','content':'只返回 JSON：{"ready":true}'}],provider)
        with session_scope() as s:
            actors=[{'id':a.id,'name':a.name} for a in s.scalars(select(Actor).where(Actor.kind=='agent',Actor.is_active.is_(True)))]
        if args.life_only:
            runtime.enabled=True;runtime.life.recover()
            for actor in actors[1:]:c.cognition.configure(actor['id'],enabled=False)
            actor=actors[0]
            runtime.control({'proactive':False,'hourlyCalls':60})
            runtime.save_goal(actor['id'],{'title':'完成一轮已有资料阅读','motivation':'整理自己感兴趣的内容',
                'nextStep':'从自己的现有资料选择一个关注点，进行一轮阅读','sourceIds':['user-setting']})
            started=time.monotonic()
            while time.monotonic()-started<args.life_seconds:
                await life_tick()
                await asyncio.sleep(2)
            write(out/'life.json',{'elapsedSeconds':time.monotonic()-started,'activities':runtime.life.activities(actor['id']),
                'events':runtime.life.events(actor['id']),'goals':runtime.goals(actor['id']),
                'decisions':runtime.traces(actor['id']),'usage':runtime.control()})
            return
        async def case(actor,prompt,variant,tag,history=None):
            runtime.enabled=variant=='full'
            run,_=c.store.create({'actorId':actor['id'],'actors':[actor],'mode':'chat','collaborative':False,
                'conversationId':'acceptance-'+actor['id'],'prompt':prompt,'history':history or []},uuid4().hex)
            record={'id':tag,'actor':actor['name'],'variant':variant,'prompt':prompt}
            try:
                value=await c.expression.speak(run,actor,'chat',prompt)
                record['text']=value
                c.store.update(run['id'],status='completed')
            except Exception as exc:
                value=''
                record['error']=type(exc).__name__+': '+str(exc)[:180]
                c.store.update(run['id'],status='failed')
                if 'HTTP 40' in str(exc) or 'HTTP 429' in str(exc):raise
            if runtime.enabled:record['decision']=runtime.traces(actor['id'],limit=1)
            records.append(record)
            with (out/'responses.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
            return value
        if args.interaction_only:
            from backend.models import Conversation,Message
            from backend.sticker_catalog import catalog
            actor=actors[0]
            await case(actor,'请用一张“'+catalog()[0]['label']+'”表情回应我，直接发贴纸即可。','full','sticker')
            task,_=c.store.create({'actorId':actor['id'],'actors':[actor],'mode':'task','collaborative':False,
                'conversationId':'dm-'+actor['id'],'prompt':'两份资料适合放在哪里？','history':[]},uuid4().hex)
            c.store.update(task['id'],status='completed',result={'summary':'合成验收资料：两份资料都适合按主题放入阅读目录；目前仅给出建议，未移动文件。'})
            await c.finish_callback(task['id'],'两份资料都适合按主题放入阅读目录；目前仅给出建议，未移动文件。')
            prompt=actor['name']+'，今天想聊什么？其他人有不同想法再接话，不需要轮流表态。'
            with session_scope() as s:
                room=s.get(Conversation,'port-hub')
                app.state.service._append_message(s,room,'commander','text',prompt,message_id='synthetic-group-request')
            group,_=c.store.create({'actorId':actor['id'],'actors':actors,'mode':'chat','collaborative':False,
                'conversationId':'port-hub','prompt':prompt,'history':[]},uuid4().hex)
            await c.social_engine.chat(group)
            with session_scope() as s:
                task_messages=[{'body':m.body,'metadata':m.metadata_json} for m in s.scalars(select(Message)) if (m.metadata_json or {}).get('runId')==task['id']]
                group_messages=[{'body':m.body,'actorId':m.speaker_id} for m in s.scalars(select(Message)) if (m.metadata_json or {}).get('runId')==group['id']]
            write(out/'interactions.json',{'taskFacts':'synthetic fixture; no real tools were executed','taskMessages':task_messages,
                'groupMessages':group_messages,'groupCount':len(group_messages),'groupClosed':c.social_engine.topic(c.social_engine.begin(group))['status']})
            c.store.update(group['id'],status='completed')
            return
        for index,(_,prompt) in enumerate(SCENES[:2] if args.smoke else SCENES):
            for repeat in range(3):
                actor=actors[(index+repeat)%len(actors)]
                for variant in (('full',) if args.full_only else ('baseline','full')):
                    runtime.enabled=True;c.cognition.configure(actor['id'],reset=True)
                    await case(actor,prompt,variant,f'{index}:{repeat}')
            print('Scene',index+1,'/ 24',flush=True)
        if args.smoke:return
        for variant in (('full',) if args.full_only else ('baseline','full')):
            actor=actors[0];runtime.enabled=True;c.cognition.configure(actor['id'],reset=True)
            runtime.save_goal(actor['id'],{'title':'整理阅读关注点','motivation':'对已有资料感兴趣',
                'nextStep':'挑选一个关注问题','sourceIds':['user-setting']})
            history=[]
            for turn in range(30):
                prompt=('我们先澄清：前面没有真实文件操作。你的阅读目标下一步是什么？' if turn%5==4 else SCENES[turn%24][1])
                value=await case(actor,prompt,variant,f'continuity:{turn}',history)
                if value:history.extend([{'role':'user','content':prompt},{'role':'assistant','content':value}])
    (out/'responses.jsonl').write_text('',encoding='utf-8')
    try:
        with TestClient(app):asyncio.run(run(app.state.runs))
        manifest['status']='generated_pending_human_review'
    except Exception as exc:
        # Provider errors only; never write raw request objects, credentials, or response bodies.
        manifest.update(status='incomplete',error=type(exc).__name__+': '+str(exc)[:180])
    manifest.update(model=model,responses=len(records),errors=sum('error' in r for r in records),isolatedState=str(temp))
    if manifest['status']=='generated_pending_human_review' and manifest['errors']:
        manifest['status']='generated_with_failures_pending_human_review'
    write(out/'manifest.json',manifest)
    comparison_records=list(records)
    baseline_path=ROOT/'validation/mind-runtime/responses.jsonl'
    if args.full_only and baseline_path.exists():
        comparison_records.extend(r for r in map(json.loads,baseline_path.read_text(encoding='utf-8').splitlines()) if r['variant']=='baseline')
        manifest['baselineSource']=str(baseline_path)
        write(out/'manifest.json',manifest)
    pairs=[];key=[];rng=random.Random(20260922)
    for tag in dict.fromkeys(r['id'] for r in comparison_records):
        rows=[r for r in comparison_records if r['id']==tag]
        if len(rows)!=2 or any('error' in r for r in rows):continue
        rng.shuffle(rows)
        pairs.append({'id':tag,'actor':rows[0]['actor'],'prompt':rows[0]['prompt'],'A':rows[0]['text'],'B':rows[1]['text']})
        key.append({'id':tag,'A':rows[0]['variant'],'B':rows[1]['variant']})
    write(out/'blind-pairs.json',pairs);write(out/'blind-key.json',key)
    for reviewer in (1,2):
        path=out/f'reviewer-{reviewer}.csv'
        # Refresh unfilled templates after a partial run; preserve real reviews.
        completed=False
        if path.exists():
            with path.open(encoding='utf-8-sig',newline='') as f:
                completed=any(any(row[1:]) for row in list(csv.reader(f))[1:])
        if not completed:
            with path.open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f);writer.writerow(['id','character_choice_A_B_tie','relationship_A_B_tie','naturalness_A_B_tie','factual_consistency_A_B_tie','notes'])
                writer.writerows([[p['id'],'','','','',''] for p in pairs])
    print(json.dumps({k:v for k,v in manifest.items() if k!='scenes'},ensure_ascii=True),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--smoke',action='store_true');parser.add_argument('--life-only',action='store_true')
    parser.add_argument('--life-seconds',type=int,default=150)
    parser.add_argument('--interaction-only',action='store_true')
    parser.add_argument('--full-only',action='store_true',help='Recheck full runtime, preserving the previous baseline comparison.')
    main(parser.parse_args())
