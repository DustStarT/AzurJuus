import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.app import create_app
from backend.config import get_settings
from backend.database import session_scope
from backend.models import Actor, ActorSkill
from backend.cognition_models import MindCursor, Experience
from test_backend_flows import configure_test_env


@pytest.fixture
def world(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv('AZURJUUS_EXECUTION_BACKEND', 'hermes')
    monkeypatch.setenv('AZURJUUS_REFLECTION_ENABLED', '0')
    monkeypatch.setenv('AZURJUUS_SKILL_TRIALS_ENABLED', '0')
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        with session_scope() as session:
            actors = [a.id for a in session.scalars(select(Actor).where(Actor.kind == 'agent'))]
        yield app.state.runs, client, actors


def observe(c, aid, text='我会下次提醒你检查附件', key=None, **data):
    c.store.event(None, 'mind.observation', {'key': key or uuid4().hex, 'actorIds': [aid],
        'kind': 'speech', 'text': text, 'data': {'ownSpeech': True, **data}})
    c.cognition.pump()


def test_private_visibility_idempotence_and_recovery(world):
    c, client, (a, b, *_) = world
    observe(c, a, '我会保管秘密口令：檀木灯', key='same')
    assert '檀木灯' in c.cognition.context(a)
    assert '檀木灯' not in c.cognition.context(b)
    version = c.cognition.inspect(a)['version']
    observe(c, a, '我会保管秘密口令：檀木灯', key='same')
    assert c.cognition.inspect(a)['version'] == version
    with session_scope() as session:
        session.get(MindCursor, 'runtime').seq = 0
    c.cognition.pump()
    assert c.cognition.inspect(a)['version'] == version
    assert len(c.cognition.experiences(a)) == 1


def test_stale_reflection_cannot_overwrite_new_state(world):
    c, _, (a, b, *_) = world
    observe(c, a, peers=[b])
    candidate = c.cognition.reflection_candidate()
    observe(c, a, '我会先处理新的任务')
    assert not c.cognition.apply_reflection(candidate, {'mood':'过期', 'commitments':['我会下次提醒你检查附件']})
    assert c.cognition.inspect(a)['data']['mood'] != '过期'


def test_evidence_bound_relationship_and_forgetting(world):
    c, client, (a, b, other, *_) = world
    observe(c, a, peers=[b])
    candidate = c.cognition.reflection_candidate()
    assert c.cognition.apply_reflection(candidate, {'mood': '愿意帮助', 'commitments': ['我会下次提醒你检查附件', '伪造承诺'],
        'judgments': [{'peerId': b, 'interpretation':'愿意一起检查附件', 'confidence': .5},
                      {'peerId': other, 'interpretation':'无依据的读心'}]})
    state = c.cognition.inspect(a)
    assert len(state['data']['commitments']) == 1
    assert len(state['data']['appraisals']) == 1
    assert not c.cognition.inspect(b)['data']['appraisals']
    assert client.post(f'/api/actors/{b}/experiences/{candidate["id"]}/forget', json={}).status_code == 404
    assert client.post(f'/api/actors/{a}/experiences/{candidate["id"]}/forget', json={}).status_code == 200
    assert not c.cognition.inspect(a)['data']['commitments']
    assert '愿意一起检查附件' not in c.cognition.context(a)
    assert not c.cognition.apply_reflection(candidate, {'mood':'不应复活'})


def test_correction_and_disabled_state(world):
    c, client, (a, *_) = world
    observe(c, a)
    eid = c.cognition.experiences(a)[0]['id']
    response = client.post(f'/api/actors/{a}/experiences/{eid}/correct', json={'interpretation':'是共同核验，并非能力不足'})
    assert response.is_success
    assert '共同核验' in c.cognition.context(a)
    client.post(f'/api/actors/{a}/mind', json={'enabled':False}).raise_for_status()
    observe(c, a, '停用期间不能记录')
    assert c.cognition.context(a) == ''
    client.post(f'/api/actors/{a}/mind', json={'enabled':True}).raise_for_status()
    assert '停用期间' not in c.cognition.context(a)


def test_social_context_does_not_export_task_contents(world):
    c, _, (a, *_) = world
    observe(c, a, '我会检查客户机密文件 private-account.csv')
    assert 'private-account' in c.cognition.context(a)
    assert 'private-account' not in c.cognition.context(a, social=True)


def test_failed_consume_does_not_commit_cursor(world, monkeypatch):
    c, _, (a, *_) = world
    c.store.event(None, 'mind.observation', {'key':'failure', 'actorIds':[a], 'text':'恢复后的观察'})
    original = c.cognition.state
    def fail(*args):
        raise RuntimeError('injected database failure')
    monkeypatch.setattr(c.cognition, 'state', fail)
    c.cognition.pump()
    assert c.cognition.last_error
    monkeypatch.setattr(c.cognition, 'state', original)
    c.cognition.pump()
    assert '恢复后的观察' in c.cognition.context(a)


@pytest.mark.asyncio
async def test_reflection_retry_and_preemption(world):
    c, _, (a, *_) = world
    observe(c, a)
    calls = []
    async def fail(candidate):
        calls.append(candidate['id'])
        raise ValueError('invalid model response')
    await c.cognition.reflect_once(fail, lambda:False)
    assert len(calls) == 2
    assert c.cognition.experiences(a)[0]['data']['reflection'] == 'failed'
    observe(c, a, '我会处理另一条承诺')
    async def hang(candidate):
        await asyncio.Event().wait()
    task = asyncio.create_task(c.cognition.reflect_once(hang, lambda:False))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


def test_no_invented_historical_memories(world):
    c, _, (a, *_) = world
    # Initialization never ingests business chat history or invents a past mood.
    assert c.cognition.experiences(a) == []
    assert c.cognition.inspect(a)['data']['mood'] == '尚无足够经历'


@pytest.mark.asyncio
async def test_actual_trial_files_promote_only_after_all_scenarios(world):
    c, _, (a, *_) = world
    with session_scope() as session:
        source = session.scalar(select(ActorSkill).where(ActorSkill.actor_id == a))
        source.is_enabled = False
        source.extra_json = {**(source.extra_json or {}), 'lifecycle':'candidate', 'trialFamily':'inventory'}
        sid = source.skill_id
    class Bridge:
        session_id = None
        def __init__(self, home, settings, endpoint, token, event):
            self.token, self.phase, self.event = token, home.name, event
        async def start(self): pass
        async def close(self): pass
        async def prompt(self, prompt, workspace, history=None):
            async def tool(name, args):
                result = await c.tool(self.token, {'callId':uuid4().hex,'name':name,'args':args})
                assert result['status'] == 'completed', result
                return result
            if self.phase == 'main':
                await tool('list_dir', {'path':'.'})
                names = sorted(p.relative_to(workspace).as_posix() for p in Path(workspace).rglob('*') if p.is_file())
                await tool('write_file', {'path':'result.json','content':json.dumps(names)})
            checked = await tool('read_file', {'path':'result.json'})
            await tool('deliver', {'summary':'清单已核验','artifacts':['result.json'],'checks':[{'callId':checked['callId']}],'unresolved':[]})
            return '清单已核验'
    c.bridge_factory = Bridge
    c.settings_loader = lambda: {'llmApiKey':'synthetic'}
    report = await c.growth.trial(a, sid)
    assert len(report) == 6 and all(r['passed'] for r in report), json.dumps(report, ensure_ascii=True)
    with session_scope() as session:
        row = c.growth.row(session, a, sid)
        assert row.is_enabled and row.extra_json['validationCount'] == 3
    c.growth.control(a, sid, 'rollback')
    with session_scope() as session:
        assert not c.growth.row(session, a, sid).is_enabled
    assert not c.trial_runners


def test_unknown_family_remains_candidate(world):
    c, client, (a, *_) = world
    with session_scope() as session:
        row = session.scalar(select(ActorSkill).where(ActorSkill.actor_id == a))
        row.is_enabled = False
        sid = row.skill_id
    assert client.post(f'/api/actors/{a}/methods/{sid}/trial', json={}).status_code == 409


def test_forgetting_invalidates_derived_judgments(world):
    c, _, (a,b,*_) = world
    observe(c,a,peers=[b])
    first = c.cognition.reflection_candidate()
    c.cognition.apply_reflection(first, {'judgments':[{'peerId':b,'interpretation':'初次印象'}]})
    observe(c,a,'我会再次核验附件',peers=[b])
    second = c.cognition.reflection_candidate()
    c.cognition.apply_reflection(second, {'judgments':[{'peerId':b,'interpretation':'基于先前印象的推断'}]})
    c.cognition.revise(a,first['id'],forget=True)
    assert not c.cognition.inspect(a)['data']['appraisals']
    assert c.cognition.experiences(a)[0]['data']['reflection'] == 'invalidated'


@pytest.mark.asyncio
async def test_public_method_requires_review_and_explicit_confirmation(world, monkeypatch):
    from backend.models import SkillProposal
    c, client, (a,*_) = world
    with session_scope() as session:
        row=session.scalar(select(ActorSkill).where(ActorSkill.actor_id==a))
        row.extra_json={**row.extra_json,'trialFamily':'inventory','validationCount':3,'lifecycle':'active','revision':0}
        row.is_enabled=True
        sid=row.skill_id
    assert client.post(f'/api/actors/{a}/methods/{sid}/confirm_publish',json={}).status_code == 409
    c.growth.control(a,sid,'publish')
    async def review(**kwargs):
        from backend.database import get_engine
        assert get_engine().pool.checkedout() == 0
        return '{"approved":true,"reason":"已核对三个合成场景"}',[],{}
    monkeypatch.setattr(c.growth.service.bundle.runtime,'_complete_chat',review)
    await c.growth.review_public(a,sid)
    with session_scope() as session:
        assert not list(session.scalars(select(SkillProposal)))
    assert client.post(f'/api/actors/{a}/methods/{sid}/confirm_publish',json={}).is_success
    assert client.post(f'/api/actors/{a}/methods/{sid}/confirm_publish',json={}).is_success
    with session_scope() as session:
        proposals=list(session.scalars(select(SkillProposal)))
        assert len(proposals)==1 and proposals[0].status=='approved'
    c.growth.control(a,sid,'rollback')
    with session_scope() as session:
        assert session.scalar(select(SkillProposal)).status=='rejected'


def test_interrupted_trial_is_recoverable(world):
    c,_,(a,*_)=world
    with session_scope() as session:
        row=session.scalar(select(ActorSkill).where(ActorSkill.actor_id==a))
        row.extra_json={**row.extra_json,'lifecycle':'testing','trialFamily':'inventory'}
        sid=row.skill_id
    c.growth.recover()
    assert c.growth.next_candidate()==(a,sid)
    with session_scope() as session:
        assert not c.growth.row(session,a,sid).is_enabled


def test_configuration_changes_publish_versioned_events(world):
    c, _, (a,*_) = world
    cursor = c.store.state()['cursor']
    before = c.cognition.inspect(a)['version']
    state = c.cognition.configure(a, enabled=False)
    events = [e for e in c.store.events(cursor) if e['type'] == 'mind.changed']
    assert state['version'] == before + 1
    assert events[-1]['payload']['actorId'] == a
    assert events[-1]['payload']['version'] == state['version']


@pytest.mark.asyncio
async def test_discussion_round_limit_and_resolution(world, monkeypatch):
    c, _, (a,b,*_) = world
    run,_ = c.store.create({'prompt':'合成讨论','actorId':a,'collaborative':True,
        'actors':[{'id':a,'name':'A'},{'id':b,'name':'B'}]}, 'discussion-round-test')
    c.store.update(run['id'], assignments=[{'actorId':a},{'actorId':b}])
    async def no_reply(*args): pass
    monkeypatch.setattr(c.dialogue,'reply',no_reply)
    c.message_callback = None
    first = await c.dialogue.send(run['id'],a,{'actorId':b,'text':'检查哪些附件？'})
    second = await c.dialogue.send(run['id'],a,{'actorId':b,'text':'还有一个例外','threadId':first['discussionId']})
    with pytest.raises(ValueError, match='两轮'):
        await c.dialogue.send(run['id'],a,{'actorId':b,'text':'继续','threadId':second['discussionId']})
    result = await c.dialogue.send(run['id'],a,{'actorId':b,'text':'按清单核验即可','kind':'resolved','threadId':first['discussionId']})
    assert result['status']=='resolved'
    assert all(d.get('resolution') == '按清单核验即可' for d in c.store.get(run['id'])['discussions'])
    await c.dialogue.drain(run['id'])
