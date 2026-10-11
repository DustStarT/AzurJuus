"""The four-character seed is limited to a new or explicitly reset workspace."""
import pytest
from sqlalchemy import func, select

from backend.database import session_scope
from backend.models import Actor, SocialPost
from backend.mind.mind_models import LifeRecord
from backend.platform.constants import DEFAULT_CHARACTERS
from conftest import open_client


def assert_four_character_seed(client):
    result = client.get('/api/bootstrap').json()
    snapshot, settings = result['snapshot'], result['workspace']['settings']
    assert settings['characterRosterText'].splitlines() == DEFAULT_CHARACTERS
    assert settings['maxConnectedAgents'] == 4
    assert {agent['sourceCharacter'] for agent in snapshot['agents']} == set(DEFAULT_CHARACTERS)
    assert all(not agent['socialOnly'] for agent in snapshot['agents'])
    secretary = next(agent for agent in snapshot['agents'] if agent['id'] == settings['secretaryAgentId'])
    assert secretary['sourceCharacter'] == '标枪'
    assert snapshot['posts'] == []
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(SocialPost)) == 0
    assert client.get('/api/posts').json()['posts'] == []
    assert client.post('/api/posts/publish',json={}).status_code == 400
    for action in ('like', 'comment'):
        assert client.post('/api/posts/' + action, json={}).status_code == 404


def test_first_bootstrap_has_four_task_capable_actors_and_no_posts(monkeypatch, tmp_path):
    with open_client(monkeypatch, tmp_path) as client:
        assert_four_character_seed(client)


@pytest.mark.parametrize('preserve', [False, True])
def test_reset_restores_four_actors_and_empty_moments(monkeypatch, tmp_path, preserve):
    with open_client(monkeypatch, tmp_path) as client:
        client.post('/api/personas/compose', json={'names': ['信浓'], 'participantCount': 1}).raise_for_status()
        with session_scope() as session:
            owner = session.scalar(select(Actor).where(Actor.source_character == '信浓'))
            session.add(SocialPost(id='synthetic-before-reset', author_id=owner.id,
                excerpt='合成历史动态', likes=0, liked_by_user=False, art_palette=[], following=False))
            session.add(LifeRecord(id='synthetic-before-reset', data={'actorId': owner.id,
                'participants': [owner.id], 'kind': 'rest', 'text': '合成活动'}))
        response = client.post('/api/system/reset', json={'preserveKeysAndProfile': preserve})
        response.raise_for_status()
        assert_four_character_seed(client)
        with session_scope() as session:
            assert session.scalar(select(func.count()).select_from(LifeRecord)) == 0


def test_existing_workspace_roster_survives_restart(monkeypatch, tmp_path):
    with open_client(monkeypatch, tmp_path) as client:
        client.post('/api/personas/compose', json={'names': ['信浓', '能代'], 'participantCount': 2}).raise_for_status()
    with open_client(monkeypatch, tmp_path) as client:
        result = client.get('/api/bootstrap').json()['workspace']
        assert result['settings']['characterRosterText'].splitlines() == ['信浓', '能代']
        assert {agent['sourceCharacter'] for agent in result['data']['agents']} == {'信浓', '能代'}
