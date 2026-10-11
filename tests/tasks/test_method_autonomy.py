import asyncio
import copy
import json
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import select
from backend.database import session_scope
from backend.models import ActorSkill,SkillRun
from backend.tasks.method_trials import validate_cases,evaluate,create_files


def cases():
    return [{'prompt':'读取 input.txt 转为大写写入 output.txt，读回后交付。',
        'files':{'input.txt':text},'expected':{'output.txt':{'text':text.upper()}}}
        for text in ['hello','multiple\nlines','中文 abc 123']]


async def source(world,tmp_path):
    c,client,actors=world
    work=tmp_path.parent/(tmp_path.name+'-method-work');work.mkdir()
    (work/'input.txt').write_text('hello',encoding='utf-8')
    client.post('/api/workspace/save',json={'workspace':{'settings':{'secretaryAgentId':actors[0],
        'connectedAgentIds':actors[:2],'authorizedWorkspaceRoot':str(work),'llmApiKey':'synthetic'}}}).raise_for_status()
    run,_=c.store.create({'prompt':'处理文字','mode':'task','collaborative':False,'conversationId':'port-hub',
        'workspace':str(work),'actorId':actors[0],'actors':[{'id':actors[0],'name':'Fixture'}],'grants':[]},uuid4().hex)
    rid=run['id'];c.store.update(rid,status='running',assignments=[{'id':'main','actorId':actors[0],
        'brief':'处理文字','dependsOn':[],'acceptance':['实际读取'],'status':'running'}])
    c.tokens['methodtoken']=(rid,actors[0],'main')
    async def tool(name,args):return await c.tool('methodtoken',{'callId':uuid4().hex,'name':name,'args':args})
    found=await tool('method_search',{'query':'处理','limit':20})
    chosen=next(m for m in found['result']['methods'] if m['visibility']=='private')
    assert (await tool('method_select',{'ids':[chosen['id']]}))['status']=='completed'
    read=await tool('read_file',{'path':'input.txt'})
    assert (await tool('deliver',{'summary':'hello','checks':[{'callId':read['callId']}],'artifacts':[],'unresolved':[]}))['status']=='completed'
    c.store.update(rid,status='completed')
    with session_scope() as session:
        session.add(SkillRun(id='verified-'+uuid4().hex,actor_id=actors[0],primary_skill_id=chosen['id'],
            primary_skill_name=chosen['name'],selected_skill_ids=[chosen['id']],selected_skill_names=[chosen['name']],
            mode='task',source_kind='deepseek-harness',conversation_id='port-hub',input_summary='处理文字',output_summary='hello',
            used_tool_names=['read_file'],extra_json={'verified':True,'runId':rid,'phase':'main'}))
    async def approve(*_):return {'approved':True,'reason':'三种输入边界明确，无越权。'}
    c.reviews.generate=approve
    return c,rid,actors,chosen,tool


@pytest.mark.asyncio
async def test_model_chooses_and_combines_only_own_enabled_methods_without_grants(world,tmp_path):
    c,rid,actors,chosen,tool=await source(world,tmp_path)
    c.store.update(rid,status='running')
    found=(await tool('method_search',{'query':'','limit':20}))['result']['methods']
    private={m['id'] for m in found if m['visibility']=='private'}
    with session_scope() as session:
        other=session.scalars(select(ActorSkill).where(ActorSkill.actor_id==actors[1])).all()
        assert not private&{s.skill_id for s in other}
    ids=[chosen['id'],next(m['id'] for m in found if m['visibility']=='public')]
    result=await tool('method_select',{'ids':ids})
    assert result['status']=='completed' and result['result']['method']
    assert c.store.get(rid)['methods']['main']['selectedBy']=='model' and c.store.get(rid)['grants']==[]
    with pytest.raises(ValueError):c.method_runtime.select(rid,actors[0],'main',{'ids':['unconfigured']})
    with pytest.raises(ValueError):c.method_runtime.select(rid,actors[0],'main',{'ids':[ids[0],ids[0]]})
    proof=await tool('method_search',{'query':'','limit':5})
    forged=await tool('deliver',{'summary':'not evidence','checks':[{'callId':proof['callId']}],'artifacts':[],'unresolved':[]})
    assert forged['status']=='failed'
    c.tokens.pop('methodtoken')


@pytest.mark.asyncio
async def test_method_revocation_requires_fresh_selection(world,tmp_path):
    c,rid,actors,chosen,tool=await source(world,tmp_path)
    c.store.update(rid,status='running')
    c.growth.control(actors[0],chosen['id'],'disable')
    assert (await tool('read_file',{'path':'input.txt'}))['status']=='failed'
    found=(await tool('method_search',{'query':''}))['result']['methods']
    assert chosen['id'] not in {m['id'] for m in found}
    assert (await tool('method_select',{'ids':[next(m['id'] for m in found if m['visibility']=='public')]}))['status']=='completed'
    assert (await tool('read_file',{'path':'input.txt'}))['status']=='completed'
    c.tokens.pop('methodtoken')


@pytest.mark.asyncio
async def test_evolution_requires_verified_own_source_and_confirmed_immutable_cases(world,tmp_path):
    c,rid,actors,chosen,tool=await source(world,tmp_path)
    proposal={'name':'逐行转大写','summary':'UTF-8 文本转换','patch':'EVOLVE_FIX：逐行转换后精确读回。',
        'sourceRunId':rid,'basedOn':[chosen['id']],'cases':cases()}
    with pytest.raises(ValueError):await c.growth.propose(actors[1],proposal)
    c.store.update(rid,status='paused')
    with pytest.raises(ValueError):await c.growth.propose(actors[0],proposal)
    c.store.update(rid,status='completed')
    result=await c.growth.propose(actors[0],proposal);sid=result['skillId']
    assert result['status']=='awaiting_trial_confirmation'
    material=c.growth.source_material(c.store.get(rid),c.store.get(rid)['assignments'],c.store.calls(rid))
    assert {item['name'] for item in material['methodEvidence']}=={'method_search','method_select'}
    assert c.store.get(rid)['workspace'] not in json.dumps(material,ensure_ascii=False)
    with pytest.raises(ValueError):c.growth.control(actors[0],sid,'trial')
    with pytest.raises(ValueError):c.growth.control(actors[0],sid,'enable')
    c.growth.control(actors[0],sid,'confirm_trial')
    with session_scope() as session:
        row=c.growth.row(session,actors[0],sid)
        row.prompt_patch+=' changed'
    with pytest.raises(ValueError):c.growth.control(actors[0],sid,'trial')
    c.tokens.pop('methodtoken')


@pytest.mark.asyncio
async def test_general_evolution_trials_compare_real_files_then_activate_and_rollback(world,tmp_path):
    c,rid,actors,chosen,tool=await source(world,tmp_path)
    result=await c.growth.propose(actors[0],{'name':'文本转换','summary':'转为大写并核验','patch':'EVOLVE_FIX',
        'sourceRunId':rid,'basedOn':[chosen['id']],'cases':cases()})
    sid=result['skillId'];c.growth.control(actors[0],sid,'confirm_trial')
    class Bridge:
        session_id=None
        def __init__(self,home,settings,endpoint,token,event):self.phase,self.token=home.name,token
        async def start(self):pass
        async def close(self):pass
        async def prompt(self,prompt,workspace,history=None):
            async def call(name,args):
                output=await c.tool(self.token,{'callId':uuid4().hex,'name':name,'args':args})
                assert output['status']=='completed',output
                return output
            if self.phase!='reviewer':
                read=await call('read_file',{'path':'input.txt'})
                text=Path(workspace,'input.txt').read_text(encoding='utf-8')
                improved=any('EVOLVE_FIX' in m['content'] for m in history or [])
                await call('write_file',{'path':'output.txt','content':text.upper() if improved else text})
            verify=await call('read_file',{'path':'output.txt'})
            await call('deliver',{'summary':'实际核验','checks':[{'callId':verify['callId']}],'artifacts':['output.txt'],'unresolved':[]})
            return 'checked'
    c.bridge_factory=Bridge
    report=await c.growth.trial(actors[0],sid)
    assert len(report)==6
    assert all(r['passed'] for r in report if r['variant']=='candidate')
    assert not any(r['passed'] for r in report if r['variant']=='baseline')
    with session_scope() as session:
        row=c.growth.row(session,actors[0],sid)
        assert row.is_enabled and row.origin=='self_evolved' and row.extra_json['trialSummary']['stable']
    c.growth.control(actors[0],sid,'rollback')
    with session_scope() as session:assert not c.growth.row(session,actors[0],sid).is_enabled
    assert not c.trial_runners
    c.tokens.pop('methodtoken')


@pytest.mark.parametrize('change',['escape','unknown_assertion','duplicates','empty_contains','nonfinite'])
def test_general_trial_contract_rejects_false_or_unsafe_acceptance(change):
    spec=cases()
    if change=='escape':spec[0]['files']={'../real.txt':'bad'}
    elif change=='unknown_assertion':spec[0]['expected']['output.txt']={'eval':'code'}
    elif change=='duplicates':spec=[copy.deepcopy(spec[0]) for _ in range(3)]
    elif change=='empty_contains':spec[0]['expected']['output.txt']={'contains':''}
    else:spec[0]['expected']['output.txt']={'json':float('nan')}
    with pytest.raises(ValueError):validate_cases(spec)


def test_custom_document_assertions_validate_actual_docx_and_xlsx(tmp_path):
    spec={'files':{'source.docx':{'kind':'docx','content':'一行\n第二行'},
        'sheet.xlsx':{'kind':'xlsx','sheets':{'data':[['x',1],['y',2]]}}},
        'expected':{'source.docx':{'docx':'一行\n第二行'},'sheet.xlsx':{'xlsx':{'data':[['x',1],['y',2]]}}}}
    before=create_files(tmp_path,spec)
    assert evaluate(tmp_path,spec,before)
    spec['expected']['source.docx']={'docx':'wrong'}
    assert not evaluate(tmp_path,spec,before)


@pytest.mark.asyncio
async def test_restart_restores_method_usage_from_actual_completed_selection(world,tmp_path):
    c,rid,actors,chosen,tool=await source(world,tmp_path)
    c.method_runtime.selections.clear()
    await c.finish_callback(rid,'hello')
    import hashlib
    record_id='skillrun-'+hashlib.sha256((rid+':main').encode()).hexdigest()[:24]
    with session_scope() as session:
        record=session.get(SkillRun,record_id)
        assert record and record.selected_skill_ids==[chosen['id']] and record.extra_json['verified']
    assert not c.method_runtime.selections
    c.tokens.pop('methodtoken')


def test_json_assertions_do_not_accept_booleans_as_numbers(tmp_path):
    case={'files':{},'expected':{'result.json':{'json':{'values':[0,1]}}}}
    path=tmp_path/'result.json';path.write_text('{"values":[false,true]}')
    assert not evaluate(tmp_path,case,{})
    path.write_text('{"values":[0,1]}')
    assert evaluate(tmp_path,case,{})
