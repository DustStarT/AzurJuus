import asyncio
from sqlalchemy import select
from test_cognition import world
from backend.database import session_scope
from backend.models import Actor,AgentRelationship
from backend.relationship_research import validate,allowed

def test_sources_and_quotes_are_checked():
    members={'a':'七省','b':'埃佛森'}
    source={'url':'https://wiki.biligame.com/blhx/聊天/游园邀请','text':'七省请埃佛森教她使用啾信。'}
    edge={'from':'a','to':'b','source':source['url'],'quote':source['text'],'description':'七省向埃佛森请教通讯软件。'}
    assert len(validate({'relationships':[edge]},[source],members,'a'))==1
    assert len(validate({'relationships':[edge,edge]},[source],members,'a'))==1
    assert not validate({'relationships':[{**edge,'quote':'原文没有的话'}]},[source],members,'a')
    assert not validate({'relationships':[{**edge,'to':'c'}]},[source],members,'a')
    assert not allowed('https://wiki.biligame.com.evil.test/blhx/x')
    assert not allowed('http://127.0.0.1/blhx/x')

def test_research_graph_and_user_override_survive(world,monkeypatch):
    c,client,ids=world
    a,b=ids[:2]
    with session_scope() as s:
        actors=s.scalars(select(Actor).where(Actor.kind=='agent')).all()
        names={v.id:v.source_character or v.name for v in actors}
        for v in actors:v.extra_json={**v.extra_json,'wikiResearch':{'status':'completed'}}
    client.post('/api/workspace/save',json={'workspace':{'settings':{'llmApiKey':'synthetic'}}}).raise_for_status()
    source={'url':'https://wiki.biligame.com/blhx/test','text':names[a]+'向'+names[b]+'请教经验。','links':[]}
    async def fetch(url):return source
    async def generate(messages,settings):return {'relationships':[{'from':a,'to':b,'source':source['url'],'quote':source['text'],'description':'有请教经验的背景。'}]}
    monkeypatch.setattr('backend.relationship_research.page',fetch)
    monkeypatch.setattr(c.expression,'complete',generate)
    monkeypatch.setattr(c.social_engine.service,'background_budget',lambda:True)
    client.post(f'/api/actors/{a}/relationships/{b}',json={'description':'用户保留的关系'}).raise_for_status()
    client.post(f'/api/actors/{a}/research-relationships',json={}).raise_for_status()
    asyncio.run(c.relationship_research.tick())
    graph=client.get('/api/relationships/graph').json()
    edge=next(e for e in graph['edges'] if e['from']==a and e['to']==b)
    assert edge['userDefined']=='用户保留的关系'
    assert any(e.get('origin')=='wiki-model-interpretation' for e in edge['background'])
    node=next(n for n in graph['nodes'] if n['id']==a)
    assert node['research']['progress']==100 and '已更新' in node['research']['message']
    c.cognition.inspect(a)
    signal=c.cognition.social_participation(a,'room',b,[])
    assert signal['originalBackgroundInterpretation']['description']=='有请教经验的背景。'
    assert 'wiki-model-interpretation' in c.cognition.context(a,peers=[b],social=True)
    assert next(n for n in graph['nodes'] if n['id']==a)['research']['status']=='completed'
    asyncio.run(c.relationship_research.tick())
    assert client.post(f'/api/actors/{a}/research-relationships',json={'sourceUrls':['https://example.com']}).status_code==400

    # A blocked character page must not erase previously supported relations.
    import httpx
    async def partial_fetch(url):
        if 'wiki.biligame.com' in url:
            response=httpx.Response(567,request=httpx.Request('GET',url))
            raise httpx.HTTPStatusError('blocked',request=response.request,response=response)
        return {'url':url,'text':names[a]+' has no new evidence.','links':[]}
    async def no_new_edges(messages,settings):return {'relationships':[]}
    monkeypatch.setattr('backend.relationship_research.page',partial_fetch)
    monkeypatch.setattr(c.expression,'complete',no_new_edges)
    stages=[]
    original_progress=c.relationship_research.progress
    def progress(*args,**kwargs):
        stages.append((args[2],args[3]))
        return original_progress(*args,**kwargs)
    monkeypatch.setattr(c.relationship_research,'progress',progress)
    client.post(f'/api/actors/{a}/research-relationships',json={}).raise_for_status()
    asyncio.run(c.relationship_research.tick())
    assert ('fetching',15) in stages and ('analyzing',65) in stages and ('saving',90) in stages
    graph=client.get('/api/relationships/graph').json()
    research=next(n['research'] for n in graph['nodes'] if n['id']==a)
    assert research['status']=='completed' and research['sourceErrors']
    edge=next(e for e in graph['edges'] if e['from']==a and e['to']==b)
    assert any(e.get('origin')=='wiki-model-interpretation' for e in edge['background'])

def test_paused_moments_never_access_service():
    from backend.idle_social import run_idle_social
    assert asyncio.run(run_idle_social(None)) is None

def test_new_member_queues_both_directions(world):
    c,client,ids=world
    aid=client.post('/api/social/characters/七省/enable',json={}).json()['actorId']
    with session_scope() as s:
        assert s.get(Actor,aid).extra_json['wikiResearch']['status']=='pending'
        assert s.get(Actor,ids[0]).extra_json['wikiResearch']['status']=='pending'
        rows=s.scalars(select(AgentRelationship).where(AgentRelationship.agent_id==aid)).all()
        assert {r.peer_agent_id for r in rows}>=set(ids)

def test_replaced_job_does_not_commit_old_result(world,monkeypatch):
    c,client,ids=world
    a=ids[0]
    with session_scope() as s:
        for actor in s.scalars(select(Actor).where(Actor.kind=='agent')).all():
            actor.extra_json={**actor.extra_json,'wikiResearch':{'status':'completed'}}
    client.post('/api/workspace/save',json={'workspace':{'settings':{'llmApiKey':'synthetic'}}}).raise_for_status()
    async def fetch(url):return {'url':url,'text':'测试材料','links':[]}
    async def generate(messages,settings):
        with session_scope() as s:
            actor=s.get(Actor,a)
            actor.extra_json={**actor.extra_json,'wikiResearch':{'status':'pending','queuedAt':999}}
        return {'relationships':[]}
    monkeypatch.setattr('backend.relationship_research.page',fetch)
    monkeypatch.setattr(c.expression,'complete',generate)
    monkeypatch.setattr(c.social_engine.service,'background_budget',lambda:True)
    client.post(f'/api/actors/{a}/research-relationships',json={}).raise_for_status()
    asyncio.run(c.relationship_research.tick())
    with session_scope() as s:
        assert s.get(Actor,a).extra_json['wikiResearch']=={'status':'pending','queuedAt':999}

def test_old_http_error_is_sanitized(world):
    _,client,ids=world
    with session_scope() as s:
        actor=s.get(Actor,ids[0])
        actor.extra_json={**actor.extra_json,'wikiResearch':{'status':'failed','error':
            "HTTPStatusError: Server error '567 Unknown Status' for url 'https://wiki.biligame.com/blhx/x'"}}
    node=next(n for n in client.get('/api/relationships/graph').json()['nodes'] if n['id']==ids[0])
    assert node['research']['error']=='资料站暂时不可用，请稍后重试。'
