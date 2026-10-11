import asyncio
from pathlib import Path
from uuid import uuid4
import pytest
from backend.tasks.task_reviews import ReviewRejected


def setup(world,tmp_path):
    c,client,actors=world
    work=tmp_path.parent/(tmp_path.name+'-review-work');work.mkdir()
    client.post('/api/workspace/save',json={'workspace':{'settings':{
        'secretaryAgentId':actors[0],'connectedAgentIds':actors[:2],
        'authorizedWorkspaceRoot':str(work),'llmApiKey':'synthetic'}}}).raise_for_status()
    c.reviews.enabled=True
    async def approve(*_):return {'approved':True,'reason':'实际证据满足本阶段要求。'}
    c.reviews.generate=approve
    run,_=c.store.create({'prompt':'生成并核验两个依赖文件','mode':'task','collaborative':False,
        'conversationId':'port-hub','workspace':str(work),'actorId':actors[0],
        'actors':[{'id':actors[0],'name':'Synthetic'}],'grants':[]},uuid4().hex)
    rid=run['id']
    c.store.update(rid,status='running',assignments=[
        {'id':i,'actorId':actors[0],'brief':i,'dependsOn':[] if i=='first' else ['first'],
         'acceptance':['实际写入并读取'],'status':'pending'} for i in ['first','second']])
    calls=[]
    async def execute(run,actor,phase,*_):
        calls.append(phase)
        token=uuid4().hex;c.tokens[token]=(rid,actor['id'],phase)
        async def tool(name,args):
            result=await c.tool(token,{'callId':uuid4().hex,'name':name,'args':args})
            assert result['status']=='completed',result
            return result
        try:
            paths=['first.txt','second.txt'] if phase=='reviewer' else [phase+'.txt']
            if phase!='reviewer':await tool('write_file',{'path':paths[0],'content':phase})
            checks=[{'callId':(await tool('read_file',{'path':p}))['callId']} for p in paths]
            await tool('deliver',{'summary':'核验实际文件','artifacts':paths,'checks':checks,'unresolved':[]})
            return '已核验'
        finally:c.tokens.pop(token,None)
    c.execute_actor=execute
    return c,rid,work,calls


async def waiting(c,rid,key):
    async with asyncio.timeout(5):
        while True:
            run=c.store.get(rid)
            assert run['status'] not in {'failed','paused'},run.get('error')
            value=next((r for r in c.store.get(rid).get('stageReviews',{}).values()
                if r['key']==key and r['status']=='waiting_user'),None)
            if value:return value
            await asyncio.sleep(.01)


async def stop(job):
    if not job.done():job.cancel()
    await asyncio.gather(job,return_exceptions=True)


@pytest.mark.asyncio
async def test_plan_stage_and_final_gates_block_dependencies_and_finish(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    job=asyncio.create_task(c.run(rid))
    try:
        review=await waiting(c,rid,'plan')
        assert calls==[] and not list(work.iterdir())
        c.reviews.resolve(rid,review['id'],'approved')
        first=await waiting(c,rid,'stage:first')
        assert calls==['first'] and (work/'first.txt').is_file() and not (work/'second.txt').exists()
        assert first['secretary']['actorId']==c.settings_loader()['secretaryAgentId']
        assert 'methodEvidence' in first['material']
        c.reviews.resolve(rid,first['id'],'approved')
        second=await waiting(c,rid,'stage:second')
        assert calls==['first','second']
        assert second['material']['upstream'][0]['status']=='completed'
        assert second['material']['upstream'][0]['checks'][0]['name']=='read_file'
        c.reviews.resolve(rid,second['id'],'approved')
        final=await waiting(c,rid,'final')
        assert c.store.get(rid)['status']=='waiting_approval' and not c.store.get(rid).get('resultMessageId')
        c.reviews.resolve(rid,final['id'],'approved')
        await asyncio.wait_for(job,5)
        assert c.store.get(rid)['status']=='completed'
    finally:await stop(job)


@pytest.mark.asyncio
async def test_pause_restart_reuses_user_gate_without_replaying_write(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    job=asyncio.create_task(c.run(rid))
    try:
        plan=await waiting(c,rid,'plan');c.reviews.resolve(rid,plan['id'],'approved')
        first=await waiting(c,rid,'stage:first')
        await stop(job)
        assert c.store.get(rid)['status']=='paused'
        c.store.recover()
        job=asyncio.create_task(c.run(rid))
        await asyncio.sleep(0)
        resumed=await waiting(c,rid,'stage:first')
        assert resumed['id']==first['id']
        c.reviews.resolve(rid,resumed['id'],'approved')
        second=await waiting(c,rid,'stage:second');c.reviews.resolve(rid,second['id'],'approved')
        final=await waiting(c,rid,'final');c.reviews.resolve(rid,final['id'],'approved')
        await job
        assert calls.count('first')==1
        assert len([x for x in c.store.calls(rid) if x['name']=='write_file' and x['args']['path']=='first.txt'])==1
    finally:await stop(job)


@pytest.mark.asyncio
async def test_stage_rejection_preserves_outputs_and_pauses_for_explicit_revision(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    job=asyncio.create_task(c.run(rid))
    try:
        plan=await waiting(c,rid,'plan');c.reviews.resolve(rid,plan['id'],'approved')
        first=await waiting(c,rid,'stage:first');c.reviews.resolve(rid,first['id'],'rejected','补充说明来源。')
        await job
        run=c.store.get(rid)
        assert run['status']=='paused' and run['assignments'][0]['status']=='needs_revision'
        assert (work/'first.txt').read_text()=='first' and calls==['first']
        assert run['assignments'][0]['revisionNotes']==['补充说明来源。']
    finally:await stop(job)


@pytest.mark.asyncio
async def test_changed_artifact_and_forged_secretary_cannot_approve(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    job=asyncio.create_task(c.run(rid))
    try:
        plan=await waiting(c,rid,'plan');c.reviews.resolve(rid,plan['id'],'approved')
        first=await waiting(c,rid,'stage:first')
        client=world[1]
        assert client.post(f"/api/runs/{rid}/reviews/{first['id']}",json={'decision':'approved','reviewer':'secretary'}).status_code==409
        url=f"/api/runs/{rid}/reviews/{first['id']}/artifacts/0"
        assert client.get(url).content==b'first'
        (work/'first.txt').write_text('changed')
        assert client.get(url).status_code==409
        with pytest.raises(ValueError,match='改变'):c.reviews.resolve(rid,first['id'],'approved')
        assert not (work/'second.txt').exists()
    finally:await stop(job)


@pytest.mark.asyncio
async def test_new_instruction_invalidates_waiting_gate(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    job=asyncio.create_task(c.run(rid))
    try:
        plan=await waiting(c,rid,'plan');c.reviews.resolve(rid,plan['id'],'approved')
        first=await waiting(c,rid,'stage:first')
        await c.control(rid,'steer','增加来源说明')
        await job
        assert c.store.get(rid)['status']=='paused'
        assert c.store.get(rid)['stageReviews'][first['id']]['status']=='stale'
        with pytest.raises(ValueError):c.reviews.resolve(rid,first['id'],'approved')
    finally:await stop(job)


@pytest.mark.parametrize('secretary_result',[{'approved':False,'reason':'不必要的副作用。'}, {'approved':'true','reason':'错误类型'}])
@pytest.mark.asyncio
async def test_high_risk_cannot_reach_user_or_execute_without_secretary(world,tmp_path,secretary_result):
    c,rid,work,_=setup(world,tmp_path)
    c.tokens['risk-token']=(rid,world[2][0],'first')
    async def reject(*_):return secretary_result
    c.reviews.generate=reject
    import sys
    result=await c.tool('risk-token',{'callId':'riskcall','name':'command','args':{
        'argv':[sys.executable,'-c',"from pathlib import Path;Path('must-not-exist').write_text('bad')"]}})
    assert result['status']=='failed' and not (work/'must-not-exist').exists()
    with pytest.raises(ValueError):await c.resolve(rid,'riskcall','approved')
    c.tokens.pop('risk-token')


@pytest.mark.asyncio
async def test_risk_double_approval_executes_once_and_binds_target(world,tmp_path):
    c,rid,work,_=setup(world,tmp_path)
    (work/'from.txt').write_text('new');(work/'to.txt').write_text('old')
    c.tokens['risk-token']=(rid,world[2][0],'first')
    args={'src':'from.txt','dest':'to.txt'}
    job=asyncio.create_task(c.tool('risk-token',{'callId':'riskcopy','name':'copy','args':args}))
    try:
        async with asyncio.timeout(5):
            while not c.store.call('riskcopy') or c.store.call('riskcopy')['status']!='waiting_approval':await asyncio.sleep(.01)
        assert (work/'to.txt').read_text()=='old'
        assert c.store.call('riskcopy')['result']['secretaryReview']['approved'] is True
        (work/'to.txt').write_text('changed')
        with pytest.raises(ValueError,match='改变'):await c.resolve(rid,'riskcopy','approved')
        (work/'to.txt').write_text('old')
        await c.resolve(rid,'riskcopy','approved')
        assert (await job)['status']=='completed'
        assert (work/'to.txt').read_text()=='new'
        replay=await c.tool('risk-token',{'callId':'riskcopy','name':'copy','args':args})
        assert replay['status']=='completed'
    finally:await stop(job);c.tokens.pop('risk-token',None)


@pytest.mark.asyncio
async def test_new_instruction_invalidates_pending_risk_authorization(world,tmp_path):
    c,rid,work,_=setup(world,tmp_path)
    (work/'target.txt').write_text('old')
    c.tokens['risk-token']=(rid,world[2][0],'first')
    job=asyncio.create_task(c.tool('risk-token',{'callId':'steerrisk','name':'write_file',
        'args':{'path':'target.txt','content':'new'}}))
    try:
        async with asyncio.timeout(5):
            while not c.store.call('steerrisk') or c.store.call('steerrisk')['status']!='waiting_approval':await asyncio.sleep(.01)
        await c.control(rid,'steer','不要修改原文件，改用新文件。')
        with pytest.raises(ValueError,match='改变'):await c.resolve(rid,'steerrisk','approved')
        await c.resolve(rid,'steerrisk','rejected')
        assert (await job)['status']=='failed' and (work/'target.txt').read_text()=='old'
    finally:await stop(job);c.tokens.pop('risk-token',None)


@pytest.mark.asyncio
async def test_preexecution_plan_is_reviewed_without_demanding_deliverables(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    async def phase_aware(messages,*_):
        return {'approved':'尚未执行任务' in messages[0]['content'],
                'reason':'规划审查不要求尚未产生的交付证据。'}
    c.reviews.generate=phase_aware
    job=asyncio.create_task(c.execute_assignments(rid,c.settings_loader()))
    try:
        review=await waiting(c,rid,'plan')
        assert review['secretary']['approved'] and calls==[] and not list(work.iterdir())
    finally:await stop(job)


@pytest.mark.asyncio
async def test_stage_review_includes_actual_method_selection_without_using_it_as_delivery_proof(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    c.tokens['method-proof']=(rid,world[2][0],'first')
    found=await c.tool('method-proof',{'callId':'methodlookup','name':'method_search','args':{}})
    sid=found['result']['methods'][0]['id']
    selected=await c.tool('method-proof',{'callId':'methodchoice','name':'method_select','args':{'ids':[sid]}})
    assert selected['status']=='completed'
    job=asyncio.create_task(c.run(rid))
    try:
        plan=await waiting(c,rid,'plan');c.reviews.resolve(rid,plan['id'],'approved')
        stage=await waiting(c,rid,'stage:first')
        assert {e['id'] for e in stage['material']['methodEvidence']}=={'methodlookup','methodchoice'}
        assert not {'methodlookup','methodchoice'}&{e['id'] for e in stage['material']['checks']}
    finally:await stop(job);c.tokens.pop('method-proof',None)


@pytest.mark.asyncio
async def test_replanning_invalidates_old_confirmation_and_blocks_new_work(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    job=asyncio.create_task(c.execute_assignments(rid,c.settings_loader()))
    try:
        old=await waiting(c,rid,'plan')
        plan=c.reviews.plan(c.store.get(rid));plan[0]['brief']='changed scope'
        c.workflow.replan(rid,plan)
        with pytest.raises(ValueError):c.reviews.resolve(rid,old['id'],'approved')
        new=await waiting(c,rid,'plan')
        assert new['id']!=old['id'] and new['material']['assignments'][0]['brief']=='changed scope'
        assert calls==[] and not list(work.iterdir())
    finally:await stop(job)


@pytest.mark.asyncio
async def test_downstream_cannot_use_changed_approved_artifact(world,tmp_path):
    c,rid,work,calls=setup(world,tmp_path)
    (work/'first.txt').write_text('changed')
    c.patch_assignment(rid,'first',status='completed',result={'artifacts':[
        {'path':'first.txt','sha256':'0'*64}]})
    from backend.tasks.run_coordinator import RunPaused
    with pytest.raises(RunPaused,match='上游'):await c.assignment(rid,'second',c.settings_loader())
    assert calls==[] and c.store.get(rid)['assignments'][0]['status']=='needs_revision'


@pytest.mark.parametrize('operation',['command','write_file'])
@pytest.mark.asyncio
async def test_every_command_and_file_overwrite_requires_fresh_double_review(world,tmp_path,operation):
    c,rid,work,_=setup(world,tmp_path)
    import sys
    (work/'target.txt').write_text('old')
    c.store.update(rid,grants=['command'])
    c.tokens['risk-token']=(rid,world[2][0],'first')
    args={'argv':[sys.executable,'-c','print(42)']} if operation=='command' else {'path':'target.txt','content':'new'}
    job=asyncio.create_task(c.tool('risk-token',{'callId':'freshrisk','name':operation,'args':args}))
    try:
        async with asyncio.timeout(5):
            while not c.store.call('freshrisk') or c.store.call('freshrisk')['status']!='waiting_approval':await asyncio.sleep(.01)
        assert (work/'target.txt').read_text()=='old'
        assert c.store.call('freshrisk')['result']['secretaryReview']['approved'] is True
        await c.resolve(rid,'freshrisk','approved')
        assert (await job)['status']=='completed'
    finally:await stop(job);c.tokens.pop('risk-token',None)
