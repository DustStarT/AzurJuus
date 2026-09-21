"""Opt-in cloud social protocol check; only reads production credentials."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from backend.credentials import reveal
    with sqlite3.connect((ROOT/'.azurjuus/azurjuus.db').as_uri()+'?mode=ro',uri=True) as db:
        base, model, secret = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    home=ROOT/'.azurjuus/social-live-acceptance'/uuid4().hex
    (home/'work').mkdir(parents=True)
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'),AZURJUUS_WORKSPACE_ROOT=str(home/'work'),
        AZURJUUS_SOCIAL_ENGINE_ENABLED='1',AZURJUUS_SOCIAL_ENABLED='0',AZURJUUS_REFLECTION_ENABLED='0',
        AZURJUUS_SKILL_TRIALS_ENABLED='0',AZURJUUS_EXPRESSION_ENABLED='1',AZURJUUS_EXECUTION_BACKEND='hermes')
    from desktop import start_local_server, stop_local_server, wait_for_server
    server,thread,url,_=start_local_server(port=0)
    report={'model':model,'scope':'真实模型社会行动协议；非自主开场或自然度人工验收','passed':False,
        'protocolHash':hashlib.sha256((ROOT/'backend/social_engine.py').read_bytes()).hexdigest()}
    def get(path):
        response=requests.get(url+path,timeout=15);response.raise_for_status();return response.json()
    def post(path,data):
        response=requests.post(url+path,json=data,timeout=15);response.raise_for_status();return response.json()
    try:
        assert wait_for_server(url)
        post('/api/workspace/save',{'workspace':{'settings':{'llmBaseUrl':base,'llmModel':model,'llmApiKey':reveal(secret),
            'authorizedWorkspaceRoot':str(home/'work'),'runTimeoutSeconds':180}}})
        started=time.monotonic()
        rid=post('/api/messages/send',{'conversationId':'port-hub','content':
            '@埃佛森 我想养一盆好照顾的植物。能请你邀请豪另建一个植物交流群吗？新群只聊植物，不用带过去这里的其他聊天。你们可以商量一个适合新手的选择，不用每个人都发言。',
            'mode':'chat','requestId':uuid4().hex})['runId']
        while time.monotonic()-started<240:
            run=next(r for r in get('/api/runs')['runs'] if r['id']==rid)
            if run.get('resultMessageId') or run['status'] in {'failed','paused','cancelled'}: break
            time.sleep(.5)
        else: raise TimeoutError('社会协议验收超过240秒')
        data=get('/api/workspace/load')['workspace']['data']
        state=get('/api/social/state')
        topics=[t for t in state['topics'] if t['conversationId'] in {c['id'] for c in data['conversations']}]
        actions=[a for t in topics for a in get('/api/social/topics/'+t['id']+'/actions')['actions'] if a.get('runId')==rid]
        messages=[{'conversationId':cid,'speakerId':m['speakerId'],'body':m['body'],'metadata':m.get('metadata',{})}
            for cid,rows in data['messages'].items() for m in rows if m.get('metadata',{}).get('runId')==rid and m['speakerId']!='commander']
        created=[a for a in actions if a.get('createdConversationId')]
        events=get('/api/runtime/events?runId='+rid)['events']
        errors=[ev['payload'] for ev in events if ev['type']=='social.error']
        report.update(status=run['status'],error=run.get('error'),seconds=round(time.monotonic()-started,2),
            actions=actions,messages=messages,socialErrors=errors,createdGroups=[a['createdConversationId'] for a in created])
        report['passed']=run['status']=='completed' and not errors and bool(created) and bool(messages) and all(
            'commander' in next(c['memberIds'] for c in data['conversations'] if c['id']==a['createdConversationId']) for a in created)
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        stop_local_server(server,thread)
        out=ROOT/'validation/social-live';out.mkdir(parents=True,exist_ok=True)
        (out/(home.name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'actions','messages'}},ensure_ascii=False),flush=True)
    if not report['passed']: raise SystemExit(1)


if __name__=='__main__': main()
