import sys
import json
from pathlib import Path
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import create_app
from backend.config import get_settings
from backend.database import session_scope
from backend.models import Actor


@pytest.fixture(autouse=True)
def isolated_review_policy(monkeypatch):
    """Existing domain tests use noninteractive gates; gate tests opt in."""
    monkeypatch.setenv('AZURJUUS_TASK_REVIEWS_ENABLED','0')


def configure_test_env(monkeypatch, tmp_path: Path, *, social_enabled: bool = False) -> None:
    """Isolated database, workspace and provider settings for one test."""
    monkeypatch.setenv("AZURJUUS_EXPRESSION_ENABLED", "0")
    monkeypatch.setenv("AZURJUUS_MIND_ENABLED", "0")
    monkeypatch.setenv("AZURJUUS_EXECUTION_BACKEND", "deepseek-harness")
    # Focused review tests explicitly enable the new user gates; unrelated
    # domain tests must not depend on an interactive user or a cloud model.
    monkeypatch.setenv("AZURJUUS_TASK_REVIEWS_ENABLED", "0")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZURJUUS_DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'azurjuus.db').as_posix()}")
    monkeypatch.setenv("AZURJUUS_REDIS_URL", "")
    monkeypatch.setenv("AZURJUUS_CHROMA_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AZURJUUS_CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("AZURJUUS_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("AZURJUUS_WORKSPACE_STATE_PATH", str(tmp_path / "workspace-state.json"))
    monkeypatch.setenv("AZURJUUS_SOCIAL_ENABLED", "1" if social_enabled else "0")
    monkeypatch.setenv("AZURJUUS_SOCIAL_TICK_SECONDS", "9999")
    get_settings.cache_clear()


def open_client(monkeypatch, tmp_path: Path, *, social_enabled: bool = False):
    from fastapi.testclient import TestClient

    configure_test_env(monkeypatch, tmp_path, social_enabled=social_enabled)
    return TestClient(create_app())


@pytest.fixture
def world(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv('AZURJUUS_REFLECTION_ENABLED', '0')
    monkeypatch.setenv('AZURJUUS_SKILL_TRIALS_ENABLED', '0')
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        with session_scope() as session:
            actors = [a.id for a in session.scalars(select(Actor).where(Actor.kind == 'agent'))]
        yield client.app.state.runs, client, actors


def observe(coordinator, actor_id, text='我会下次提醒你检查附件', key=None, **data):
    coordinator.store.event(None, 'mind.observation', {'key': key or uuid4().hex,
        'actorIds': [actor_id], 'kind':'speech', 'text':text,
        'data': {'ownSpeech': True, **data}})
    coordinator.cognition.pump()


@pytest.fixture
def mind(world, monkeypatch):
    coordinator, client, actor_ids = world
    runtime = coordinator.cognition.runtime
    runtime.enabled = True
    runtime.life.recover()
    monkeypatch.setattr(runtime.life, 'tick', AsyncMock())
    old = coordinator.settings_loader
    monkeypatch.setattr(coordinator, 'settings_loader',
        lambda: {**old(), 'llmApiKey': 'synthetic-test-only'})
    stages = []

    async def generate(messages, cfg):
        stage = cfg['_mindStage']
        stages.append(stage)
        data = json.loads(messages[-1]['content'])
        if stage == 'profile':
            return {'values':['不辜负约定'], 'interests':['阅读'], 'exceptions':['紧急时先行动']}
        if stage == 'understand':
            return {'interpretation':'先理解当前问题。', 'sourceIds':[data['current']['id']],
                'emotion':{'name':'关切','target':'当前问题','cause':'希望做好','intensity':.4}}
        if stage == 'decide':
            return {'action':data['allowedActions'][0], 'purpose':'先处理关键问题。',
                'sourceIds':data['frame']['sourceIds']}
        if stage == 'reflect':
            return {'beliefs':[]}
        return {'segments':['我在。'], 'sourceIds':[data['sourceId']]}

    runtime.generate = generate
    return runtime, coordinator, client, actor_ids, stages


def fixture_run(c, aid):
    from backend.database import session_scope
    from backend.models import Actor
    with session_scope() as session:
        actor = session.get(Actor, aid)
        payload = {'id':aid, 'name':actor.name}
    run, _ = c.store.create({'actorId':aid, 'actors':[payload], 'mode':'chat',
        'collaborative':False, 'conversationId':'missing-test-conversation', 'prompt':'你好', 'history':[]},uuid4().hex)
    return run, payload


def setup_social_topic(world):
    c,client,ids=world
    engine=c.social_engine
    room=engine.snapshot('port-hub')
    run,_=c.store.create({'actorId':ids[0],'actors':room['members'],'mode':'chat','collaborative':False,
        'conversationId':'port-hub','prompt':'聊聊最近的兴趣','history':[]},uuid4().hex)
    tid=engine.begin(run)
    return engine,run,engine.topic(tid),room['members'][0]


async def choose_fixture_method(coordinator, token, *, private=False):
    """Scripted model explicitly uses the production native method tools."""
    from uuid import uuid4
    found=await coordinator.tool(token,{'callId':uuid4().hex,'name':'method_search','args':{'query':'','limit':20}})
    assert found['status']=='completed',found
    methods=found['result']['methods']
    choice=next((m for m in methods if m['visibility']=='private'),methods[0]) if private else methods[0]
    selected=await coordinator.tool(token,{'callId':uuid4().hex,'name':'method_select','args':{'ids':[choice['id']]}})
    assert selected['status']=='completed',selected
