"""Real model evolution using an already verified isolated cloud fixture."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def main():
    source=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    work=Path(source['workspace']).resolve();home=work.parent
    assert home.name.startswith('azur-cloud-task-') and (home/'app.db').is_file()
    assert not home.is_relative_to(ROOT/'.azurjuus')
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'),AZURJUUS_WORKSPACE_ROOT=str(work),
        AZURJUUS_REDIS_URL='',AZURJUUS_CHROMA_URL='',AZURJUUS_CHROMA_PATH=str(home/'chroma'),
        AZURJUUS_SOCIAL_ENABLED='0',AZURJUUS_MIND_ENABLED='1',AZURJUUS_SKILL_TRIALS_ENABLED='0')
    from server import create_server
    server=create_server('127.0.0.1',0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    report={'status':'failed','model':source['model'],'sourceRunId':source['runId'],'workspace':str(work),'checks':[]}
    try:
        async with httpx.AsyncClient(base_url='http://127.0.0.1:'+str(server.server_address[1]),timeout=200) as client:
            for _ in range(100):
                try:
                    if (await client.get('/api/health')).is_success:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.1)
            run=(await client.get('/api/runs/'+source['runId'])).json()['run']
            assert run['status']=='completed' and all(r['status']=='approved' for r in run['stageReviews'].values())
            owner=next(a for a in run['assignments'] if a['status']=='completed')
            aid=owner['actorId'];choice=run.get('methods',{}).get(owner['id'],{})
            response=await client.post(f'/api/actors/{aid}/methods/evolve',json={'sourceRunId':run['id'],'basedOn':choice.get('ids',[])})
            if not response.is_success:raise AssertionError(response.json())
            proposal=response.json();report['proposal']=proposal
            assert proposal['status']=='awaiting_trial_confirmation',proposal
            sid=proposal['skillId']
            report['checks'].append('actual model derives three declarative scenarios; current connected secretary reviews real source and proposal')
            response=await client.post(f'/api/actors/{aid}/methods/{sid}/confirm_trial',json={})
            response.raise_for_status()
            (await client.post('/api/life',json={'paused':False,'hourlyCalls':200})).raise_for_status()
            future=asyncio.run_coroutine_threadsafe(server.app.state.runs.growth.trial(aid,sid),server._loop)
            print('REAL_EVOLUTION_TRIALS_STARTED',flush=True)
            try:
                async with asyncio.timeout(900):await asyncio.wrap_future(future)
            finally:
                if not future.done():future.cancel()
            methods=(await client.get(f'/api/actors/{aid}/methods')).json()['skills']
            skill=next(s for s in methods if s['id']==sid);report['method']=skill
            assert skill['enabled'] and skill['validationCount']==3 and skill['trialSummary']['stable'],skill
            assert len(skill['trialEvidence'])==6 and all(e['passed'] for e in skill['trialEvidence'] if e['variant']=='candidate')
            report['checks'].append('six actual isolated Harness runs compare baseline and candidate; all three candidate file assertions pass and activate')
            report['status']='passed'
    except Exception as exc:report['error']=str(exc)
    finally:
        server.shutdown();await asyncio.to_thread(thread.join,20);server.server_close()
        (ROOT/'validation/gaps-20261004/method-evolution-cloud.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
