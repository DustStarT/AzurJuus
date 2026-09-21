import asyncio
from sqlalchemy import select
from test_cognition import world
from test_social_engine import setup
from backend.database import session_scope
from backend.models import Message


def test_manual_group_members_and_history_boundary(world):
    c,client,ids=world
    response=client.post('/api/conversations/groups',json={'title':'我的群聊','memberIds':ids[:2]})
    response.raise_for_status();cid=response.json()['conversationId']
    assert {a['id'] for a in c.social_engine.snapshot(cid)['members']}==set(ids[:2])
    assert client.post('/api/conversations/groups',json={'title':'非法','memberIds':['not-in-roster']}).status_code==400
    with session_scope() as s:
        s.add(Message(id='before-manual-join',conversation_id=cid,speaker_id=ids[0],type='text',body='不分享的旧话题'))
    client.post(f'/api/conversations/{cid}/members/{ids[2]}/add',json={}).raise_for_status()
    assert not c.social_engine.snapshot(cid,ids[2])['history']
    client.post(f'/api/conversations/{cid}/members/{ids[2]}/add',json={}).raise_for_status()
    assert len(c.social_engine.snapshot(cid)['members'])==3
    assert not c.tokens


def test_one_member_ending_does_not_end_other_members_question(world):
    e,run,t,a=setup(world)
    assert e.commit(t,a,'withdraw',{'action':'end'})
    topic=e.topic(t['id'])
    assert topic['status']=='active' and a['id'] in topic['withdrawn']
    other=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    with session_scope() as s:
        s.add(Message(id='question-for-withdrawn',conversation_id='port-hub',speaker_id=other['id'],type='text',body=a['name']+'，这个问题你怎么看？'))
    room=e.snapshot('port-hub')
    assert room['history'][-1]['speakerName']==other['name']
    assert e.candidates(room,topic)[0]['id']==a['id']


def test_world_settings_only_show_global_background(world):
    _,client,_=world
    entries=client.get('/api/terminal/settings').json()['world']
    assert len(entries)>=4
    assert all(e['id']=='port-terminal' or e['id'].startswith('world-') for e in entries)


def test_cross_document_identity_keeps_supporting_quotes():
    from backend.relationship_research import validate
    sources=[{'url':'scene','text':'标枪一行人在走廊遇见贾维斯。'},
        {'url':'cast','text':'这次同行的是标枪与Z23。'}]
    edge={'from':'z','to':'j','source':'scene','quote':sources[0]['text'],
        'description':'同行时遇见贾维斯。','evidence':[{'source':'cast','quote':sources[1]['text']}]}
    assert validate({'relationships':[edge]},sources,{'z':'Z23','j':'贾维斯'},'z')[0]['evidence']==edge['evidence']
    edge['evidence'][0]['quote']='原文不存在的共同经历'
    assert not validate({'relationships':[edge]},sources,{'z':'Z23','j':'贾维斯'},'z')


def test_research_follows_two_evidence_rounds_only(world,monkeypatch):
    from backend.models import Actor
    c,client,ids=world
    with session_scope() as s:
        for a in s.scalars(select(Actor).where(Actor.kind=='agent')):
            a.extra_json={**a.extra_json,'wikiResearch':{'status':'completed'}}
    client.post('/api/workspace/save',json={'workspace':{'settings':{'llmApiKey':'synthetic'}}}).raise_for_status()
    base='https://wiki.biligame.com/blhx/'
    calls=[];fetched=[]
    async def fetch(url):
        fetched.append(url)
        next_url=base+'step2' if url.endswith('step1') else base+'step3'
        return {'url':url,'text':'一行人的身份待核对','links':[next_url]}
    async def discover(*args):return ([{'url':base+'index','text':'剧情索引','links':[base+'step1']}],[],[])
    async def generate(messages,settings):
        calls.append(messages)
        return {'relationships':[],'followUpUrls':[base+'step'+str(len(calls))],'unresolved':'待核实同行者'}
    monkeypatch.setattr('backend.relationship_research.page',fetch)
    monkeypatch.setattr('backend.relationship_research.story_sources',discover)
    monkeypatch.setattr(c.expression,'complete',generate)
    monkeypatch.setattr(c.social_engine.service,'background_budget',lambda:True)
    client.post(f'/api/actors/{ids[0]}/research-relationships',json={}).raise_for_status()
    asyncio.run(c.relationship_research.tick())
    assert len(calls)==3
    assert base+'step1' in fetched and base+'step2' in fetched and base+'step3' not in fetched
