import pytest
from sqlalchemy import select
from test_cognition import world
from backend.database import session_scope
from backend.models import Actor
from backend.character_identity import source_names
from backend.relationship_research import validate
from backend.terminal_characters import context

def test_only_roster_members_appear(world):
    c,client,ids=world
    with session_scope() as s:
        original=s.get(Actor,ids[0])
        name=original.source_character or original.name
        c.social_engine.service.apply_personas(s,[{'sourceName':name}],[name],1)
        # Even an old active row outside the authoritative roster must stay hidden.
        s.get(Actor,ids[1]).is_active=True
    assert [a['id'] for a in client.get('/api/social/actors').json()['actors']]==[ids[0]]
    assert [a['id'] for a in client.get('/api/terminal/settings').json()['members']]==[ids[0]]
    graph=client.get('/api/relationships/graph').json()
    assert [n['id'] for n in graph['nodes']]==[ids[0]]
    assert not graph['edges']
    with session_scope() as s:
        c.social_engine.service.ensure_seed(s)
        assert not s.get(Actor,ids[1]).is_active
        assert s.get(Actor,ids[1]) is not None  # History stays recoverable.

@pytest.mark.parametrize('name',['拉菲II','约克城II'])
def test_two_source_editions_one_actor(world,monkeypatch,name):
    from tools import blhx_character_import as importer
    c,client,_=world
    calls=[]
    def resolve(n,refresh=False):
        calls.append(n)
        return importer.CharacterProfile(name=n,url=importer.normalize_url(n),prompt_seed=n+'的独立来源资料')
    monkeypatch.setattr(importer,'resolve_profile',resolve)
    specs,missing=importer.resolve_personas([name])
    assert not missing and len(specs)==1
    assert set(calls)==set(source_names(name))
    with session_scope() as s:
        c.social_engine.service.apply_personas(s,specs,[name],1)
        actor=s.scalar(select(Actor).where(Actor.source_character==name))
        aid=actor.id
        assert s.scalar(select(Actor).where(Actor.source_character==source_names(name)[0])) is None
    prompt=context(aid)[0]
    assert all(n+'的独立来源资料' in prompt for n in source_names(name))
    assert [a['id'] for a in client.get('/api/relationships/graph').json()['nodes']]==[aid]
    source={'url':'https://wiki.biligame.com/blhx/example','text':source_names(name)[0]+'与能代的原版背景记录'}
    edge={'from':'a','to':'b','quote':source['text'],'source':source['url'],'description':'原版背景'}
    assert validate({'relationships':[edge]},[source],{'a':name,'b':'能代'},'a')

def test_other_suffixes_are_not_guessed():
    assert source_names('用户II')==['用户II']

def test_wiki_utf8_is_not_guessed_as_other_encoding(monkeypatch):
    import requests
    from tools import blhx_character_import as importer
    response=requests.Response()
    response.status_code=200
    response.url='https://wiki.biligame.com/blhx/example'
    response._content='<html><title>拉菲</title><p>白鹰所属</p></html>'.encode('utf-8')
    response.encoding='windows-1251'
    monkeypatch.setattr(importer.requests,'get',lambda *args,**kwargs:response)
    soup,_=importer.fetch_page(response.url)
    assert soup.title.text=='拉菲' and '白鹰所属' in soup.text
