"""Real official-story/model relationship extraction; production credentials read-only."""
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    subject = sys.argv[1] if len(sys.argv)>1 else '标枪'
    if subject not in {'标枪','信浓'}:raise SystemExit('只支持标枪或信浓的隔离验收')
    from backend.credentials import reveal
    with sqlite3.connect((ROOT/'.azurjuus/azurjuus.db').as_uri()+'?mode=ro', uri=True) as db:
        base, model, secret = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    home = ROOT/'validation/relationship-live'/uuid4().hex
    home.mkdir(parents=True)
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(home/'work'),
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_REFLECTION_ENABLED='0', AZURJUUS_SKILL_TRIALS_ENABLED='0')
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.database import session_scope
    from backend.models import Actor
    from sqlalchemy import select
    app = create_app()
    started = time.monotonic()
    report = {'model':model, 'passed':False}
    try:
        with TestClient(app) as client:
            client.post('/api/workspace/save',json={'workspace':{'settings':{
                'llmBaseUrl':base,'llmModel':model,'llmApiKey':reveal(secret)}}}).raise_for_status()
            with session_scope() as s:
                specs=[{'sourceName':'标枪','displayName':'标枪','faction':'皇家'},
                    {'sourceName':'Z23','displayName':'Z23','faction':'铁血'},
                    {'sourceName':'雅努斯','displayName':'雅努斯','faction':'皇家'},
                    {'sourceName':'七省','displayName':'七省','faction':'郁金王国'},
                    {'sourceName':'贾维斯','displayName':'贾维斯','faction':'皇家'},
                    {'sourceName':'信浓','displayName':'信浓','faction':'重樱'},
                    {'sourceName':'能代','displayName':'能代','faction':'重樱'},
                    {'sourceName':'武藏','displayName':'武藏','faction':'重樱'}]
                app.state.runs.social_engine.service.apply_personas(s,specs,[v['sourceName'] for v in specs],len(specs))
                for a in s.scalars(select(Actor).where(Actor.kind=='agent')).all():
                    a.extra_json={**a.extra_json,'wikiResearch':{'status':'completed'}}
                aid=s.scalar(select(Actor).where(Actor.source_character==subject)).id
            client.post(f'/api/actors/{aid}/research-relationships',json={}).raise_for_status()
            original=app.state.runs.expression.complete
            async def capture(messages,settings):
                value=await original(messages,settings)
                report.setdefault('modelOutputs',[]).append(value)
                return value
            app.state.runs.expression.complete=capture
            asyncio.run(app.state.runs.relationship_research.tick())
            graph=client.get('/api/relationships/graph').json()
            report['research']=next(n['research'] for n in graph['nodes'] if n['id']==aid)
            report['evidence']=[b for e in graph['edges'] for b in e['background']
                if b.get('subject')==aid and b.get('origin')=='wiki-model-interpretation']
            report['subject']=subject
            report['passed']=report['research']['status']=='completed' and report['research']['progress']==100 and (
                any(e['source']=='https://2nd.azurlane-bisoku.jp/api/resource/story' for e in report['evidence']) if subject=='标枪' else
                any('蝶海梦花' in e['source'] or '%E8%9D%B6%E6%B5%B7%E6%A2%A6%E8%8A%B1' in e['source'] for e in report['evidence']))
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
    report['seconds']=round(time.monotonic()-started,2)
    (home/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'seconds':report['seconds'],'report':str(home/'report.json')},ensure_ascii=False))
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':main()
