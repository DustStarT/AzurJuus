import asyncio
import json
from sqlalchemy import select
from test_cognition import world
from backend.database import session_scope
from backend.models import Actor,AgentRelationship
from backend.relationship_research import validate,allowed


def test_refresh_all_queues_only_roster_and_preserves_custom_sources(world):
    _,client,ids=world
    source='https://wiki.biligame.com/blhx/聊天'
    with session_scope() as session:
        actor=session.get(Actor,ids[0])
        actor.extra_json={**(actor.extra_json or {}),'wikiResearch':{'sourceUrls':[source],'status':'completed'}}
    response=client.post('/api/relationships/refresh-all').json()
    graph=client.get('/api/relationships/graph').json()
    assert response['queued']==len(ids)
    assert graph['batch']['total']==len(ids)
    assert graph['batch']['done']==0
    assert next(node for node in graph['nodes'] if node['id']==ids[0])['research']['sourceUrls']==[source]


def test_research_recovers_truncated_json_in_source_batches():
    from backend.relationship_research import research_complete
    calls=[]
    async def generate(messages,settings):
        sources=json.loads(messages[-1]['content'])['sources']
        calls.append((len(sources),settings['_structuredOutputTokens']))
        if len(sources)>1:raise ValueError('结构化回复超出输出预算')
        return {'relationships':[{'source':sources[0]['url']}],'mindNotes':[]}
    messages=[{'role':'system','content':'research'},
        {'role':'user','content':json.dumps({'sources':[{'url':str(i)} for i in range(4)]})}]
    result=asyncio.run(research_complete(generate,lambda:True,messages,{}))
    assert calls==[(4,3200),(1,3200),(1,3200),(1,3200),(1,3200)]
    assert len(result['relationships'])==4


def test_story_mind_note_requires_nearby_character_and_exact_quote():
    from backend.relationship_research import validate_mind_notes
    source={'url':'https://wiki.biligame.com/blhx/JUUs动态/第一期',
        'text':'标枪向雅努斯问起今天的训练，雅努斯答应稍后再聊。'}
    note={'text':'与雅努斯聊天时会先问近况','quote':'标枪向雅努斯问起今天的训练',
        'source':source['url']}
    result=validate_mind_notes({'mindNotes':[note]},[source],{'a':'标枪'},'a')
    assert result[0]['sourceKind']=='moments'
    assert result[0]['origin']=='original-background-interpretation'
    assert not validate_mind_notes({'mindNotes':[{**note,'quote':'并不存在的台词'}]},[source],{'a':'标枪'},'a')


def test_activity_directory_exposes_scene_links_for_followup():
    from backend.relationship_research import prompt_sources
    activity='蝶海梦花'
    chapter='https://wiki.biligame.com/blhx/碧蓝回忆录/蝶海梦花/序'
    index={'url':'https://wiki.biligame.com/blhx/碧蓝回忆录/蝶海梦花',
        'text':'蝶海梦花章节目录','links':[chapter]}
    prepared=prompt_sources([index],['信浓'],[activity],['信浓'])
    assert chapter in prepared[0]['links']


def test_public_wiki_source_cache_preserves_provenance(monkeypatch,tmp_path):
    from backend import relationship_research as research
    monkeypatch.setattr(research,'cached_page_path',lambda url:tmp_path/'source.json')
    url='https://wiki.biligame.com/blhx/聊天'
    record={'url':url,'text':'可核对的原文','links':[],'_fetchedAt':123.0}
    research.save_cached_page(url,record)
    assert research.load_cached_page(url)==record
    assert research.load_cached_page(url+'/other') is None

def test_related_activity_and_censored_name_connect_to_story():
    from bs4 import BeautifulSoup
    from backend.relationship_research import activity_titles,known_name_aliases,story_matches,NAMES_INDEX
    role=BeautifulSoup('<table><tr><td><b>相关 活动</b></td><td><a href="/blhx/活动页">蝶海梦花</a></td></tr>'
        '<tr><td>普通掉落点</td><td><a href="/blhx/无关页">别的活动</a></td></tr></table>','html.parser')
    names=BeautifulSoup('<table><tr><td><ruby><rb>鵗</rb><rt>xī</rt></ruby></td>'
        '<td><a title="信浓" href="/blhx/信浓">信浓</a></td></tr></table>','html.parser')
    assert activity_titles(role)==['蝶海梦花']
    assert known_name_aliases(names)['信浓']==['鵗']
    story='https://wiki.biligame.com/blhx/碧蓝回忆录文字版/蝶海梦花'
    assert story_matches({'蝶海梦花':story},activity_titles(role))==[story]
    quote='鵗向能代提起了港区的梦。'
    edge={'from':'s','to':'n','source':story,'quote':quote,'description':'就港区梦境交谈。'}
    extracted=validate({'relationships':[edge]},[{'url':story,'text':quote}],{'s':'信浓','n':'能代'},'s',{'信浓':['鵗']})
    assert extracted and extracted[0]['aliasSource']==NAMES_INDEX
    assert not validate({'relationships':[edge]},[{'url':story,'text':'两位舰船交谈。'}],{'s':'信浓','n':'能代'},'s',{'信浓':['鵗']})

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
    far={'url':source['url'],'text':'七省正在整理花园。'+'海上航行。'*150+'埃佛森独自研究电报。'}
    unsupported={**edge,'quote':'七省正在整理花园。'}
    assert not validate({'relationships':[unsupported]},[far],members,'a')

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
    async def generate(messages,settings):return {'relationships':[{'from':a,'to':b,'source':source['url'],'quote':source['text'],'description':'有请教经验的背景。'}],
        'mindNotes':[{'text':'遇到不确定时先向熟悉的人请教','source':source['url'],'quote':source['text']}]}
    monkeypatch.setattr('backend.relationship_research.page',fetch)
    monkeypatch.setattr(c.expression,'complete',generate)
    monkeypatch.setattr(c.social_engine.service,'background_budget',lambda:True)
    client.post(f'/api/actors/{a}/relationships/{b}',json={'description':'用户保留的关系'}).raise_for_status()
    client.post(f'/api/actors/{a}/research-relationships',json={}).raise_for_status()
    asyncio.run(c.relationship_research.tick())
    graph=client.get('/api/relationships/graph').json()
    edge=next(e for e in graph['edges'] if e['from']==a and e['to']==b)
    assert 'userDefined' not in edge and '用户保留的关系' not in edge['summary']
    assert next(r for r in c.cognition.relationships(a) if r['peerId']==b)['userDefined']=='用户保留的关系'
    assert any(e.get('origin')=='wiki-model-interpretation' for e in edge['background'])
    with session_scope() as session:
        notes=session.get(Actor,a).extra_json['originalMindNotes']
    assert notes[0]['origin']=='original-background-interpretation'
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
    assert research['status']=='partial' and research['sourceErrors']
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
