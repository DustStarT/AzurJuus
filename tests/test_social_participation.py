import asyncio
import json
from sqlalchemy import select
from test_cognition import world, observe
from test_social_engine import setup
from backend.database import session_scope
from backend.cognition_models import Experience, MindState


def remember(c, actor, peer, text='我会再核对一次', **data):
    observe(c, actor, text, scope='team_public', conversationId='port-hub',
            speakerId=peer, peers=[peer], **data)
    with session_scope() as s:
        row=s.scalars(select(Experience).where(Experience.actor_id==actor)
                      .order_by(Experience.at.desc())).first()
        state=s.get(MindState,actor)
        state.data={**state.data,'commitments':[{'text':text,'sourceId':row.id}]}
        return row.id


def test_public_commitment_requires_visible_source_and_forget_removes_it(world):
    c,_,(a,b,*_)=world
    eid=remember(c,a,a)
    view=lambda texts:c.cognition.social_participation(a,'port-hub',b,texts)
    assert view({'我会再核对一次'})['commitments']==['我会再核对一次']
    assert not view(set())['commitments']
    assert not c.cognition.social_participation(b,'port-hub',a,{'我会再核对一次'})['commitments']
    c.cognition.revise(a,eid,forget=True)
    assert not view({'我会再核对一次'})['commitments']


def test_private_and_other_room_evidence_cannot_enter_social_view(world):
    c,_,(a,b,*_)=world
    observe(c,a,'秘密任务口令',peers=[b])
    with session_scope() as s:
        state=s.get(MindState,a)
        state.data={**state.data,'appraisals':[{'peerId':b,'interpretation':'私人负面判断'}]}
    result=c.cognition.social_participation(a,'port-hub',b,{'秘密任务口令'})
    assert result['familiarity']==0 and result['commitments']==[]
    assert '秘密任务口令' not in str(result) and '私人负面判断' not in str(result)
    remember(c,a,b,'公共经历')
    result=c.cognition.social_participation(a,'different-room',b,{'公共经历'})
    assert result['familiarity']==0 and result['commitments']==[] and '公共经历' not in str(result)


def test_experience_changes_selection_but_direct_mention_stays_first(world,monkeypatch):
    e,_,t,_=setup(world)
    room=e.snapshot('port-hub')
    # Isolate application history from canonical relationships and topics.
    monkeypatch.setattr('backend.social_engine.worldbook',lambda:{'identities':[],'relationships':[]})
    initial=e.candidates(room,t)
    chosen=initial[-1]
    last=room['history'][-1]['speakerId']
    remember(e.c,chosen['id'],last,'一起讨论过的事情')
    assert e.candidates(room,t)[0]['id']==chosen['id']
    room['history'][-1]['mentions']=[initial[0]['id']]
    assert e.candidates(room,t)[0]['id']==initial[0]['id']
    e.c.cognition.configure(chosen['id'],enabled=False)
    assert e.participation(chosen,room)=={'familiarity':0,'commitments':[]}


def test_group_model_never_receives_private_cognitive_payload(world):
    e,_,t,a=setup(world)
    observe(e.c,a['id'],'秘密口令不要公开')
    requests=[]
    async def capture(messages,settings):
        requests.extend(messages)
        return {'action':'wait'}
    e.generate=capture
    asyncio.run(e.decide(a,e.snapshot('port-hub',a['id']),t,'source-private-check'))
    assert requests and '秘密口令不要公开' not in json.dumps(requests,ensure_ascii=False)
