import asyncio
from sqlalchemy import select
from test_cognition import world
from backend.database import session_scope
from backend.models import Actor, Message, SocialComment, SocialPost
from backend.character_identity import default_relationship
from backend.relationship_research import allowed,story_sources,validate,OFFICIAL_INDEX

def test_default_faction_relationship_and_graph(world):
    c,client,ids=world
    with session_scope() as s:
        a,b,d=[s.get(Actor,i) for i in ids[:3]]
        a.faction=b.faction='重樱';d.faction='皇家'
        assert default_relationship(a,b)['familiarity']=='familiar'
        assert default_relationship(a,d)['familiarity']=='known'
    rows=c.cognition.relationships(ids[0])
    same=next(r for r in rows if r['peerId']==ids[1])
    assert '认识且熟悉' in same['summary'] and not same['sharedSources']
    c.cognition.inspect(ids[0])
    signal=c.cognition.social_participation(ids[0],'port-hub',ids[1],[])
    assert signal['defaultRelationship']['familiarity']=='familiar'
    assert not signal['commitments']
    assert client.get('/api/relationships/graph').json()['edges']
    # Cross-faction default acquaintance stays in cognition but does not clutter the graph.
    graph=client.get('/api/relationships/graph').json()
    assert not any(e['from']==ids[0] and e['to']==ids[2] for e in graph['edges'])

def test_hidden_author_content_and_world_entries(world):
    c,client,ids=world
    with session_scope() as s:
        a=s.get(Actor,ids[0]);name=a.source_character or a.name
        s.add(Message(id='hidden-test-msg',conversation_id='port-hub',speaker_id=ids[1],type='text',body='应隐藏的旧发言'))
        c.social_engine.service.apply_personas(s,[{'sourceName':name}],[name],1)
    snapshot=client.get('/api/bootstrap').json()['snapshot']
    assert len(snapshot['agents'])==1
    assert all(m['speakerId'] in {ids[0],'commander'} for messages in snapshot['messages'].values() for m in messages)
    entries=client.get('/api/terminal/settings').json()['world']
    assert not any(e.get('from') or e.get('to') for e in entries)
    assert all(e.get('name',name)==name for e in entries)

def test_official_story_discovery_and_bounded_evidence(monkeypatch):
    visited=[]
    async def page(url):
        visited.append(url)
        if url==OFFICIAL_INDEX:return {'url':url,'text':'目录','links':['https://1st.azurlane-bisoku.jp/story/02/']}
        return {'url':url,'text':'ラフィーとジャベリンが相談します。','links':[]}
    monkeypatch.setattr('backend.relationship_research.page',page)
    sources,errors,checked=asyncio.run(story_sources('拉菲II',[],[],[]))
    assert any('/story/02/' in u for u in checked) and not errors
    assert len(visited)<=18 and len(sources)<=6
    source=next(s for s in sources if '/story/02/' in s['url'])
    value={'relationships':[{'from':'a','to':'b','source':source['url'],'quote':source['text'],'description':'交流背景'}]}
    assert validate(value,sources,{'a':'拉菲II','b':'标枪'},'a')
    assert allowed(source['url']) and not allowed('https://1st.azurlane-bisoku.jp.evil.test/story/02/')

def test_example_relationships_are_concise_and_cross_default_hidden(world):
    c,client,_=world
    specs=[{'sourceName':'标枪','displayName':'标枪','faction':'皇家'},
        {'sourceName':'Z23','displayName':'Z23','faction':'铁血'},
        {'sourceName':'雅努斯','displayName':'雅努斯','faction':'皇家'},
        {'sourceName':'七省','displayName':'七省','faction':'郁金王国'}]
    with session_scope() as s:c.social_engine.service.apply_personas(s,specs,[v['sourceName'] for v in specs],4)
    graph=client.get('/api/relationships/graph').json()
    ids={n['name']:n['id'] for n in graph['nodes']}
    edge=lambda a,b:next((e for e in graph['edges'] if e['from']==ids[a] and e['to']==ids[b]),None)
    assert '经常一同行动' in edge('标枪','Z23')['summary']
    assert edge('标枪','雅努斯')['summary']=='同属皇家，彼此认识且熟悉。'
    assert edge('标枪','七省') is None
    assert all(len(e['summary'])<160 for e in graph['edges'])
