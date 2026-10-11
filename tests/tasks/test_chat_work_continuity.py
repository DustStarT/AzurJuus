import asyncio
from uuid import uuid4

from sqlalchemy import select

from backend.chat.task_reference import resolve_task_reference
from backend.database import session_scope
from backend.models import Message, MemoryChunk
from backend.models import Conversation
from backend.mind.cognition_models import Experience
from backend.social.social_models import SocialDeferredReply


def _finished_read(coordinator, actor_id, conversation_id, title, summary):
    run, _ = coordinator.store.create({
        'actorId': actor_id, 'actors': [{'id': actor_id, 'name': 'reader'}],
        'mode': 'task', 'collaborative': False, 'conversationId': conversation_id,
        'prompt': f'看看{title}', 'history': [], 'workspace': '.',
    }, uuid4().hex)
    coordinator.store.put_call(uuid4().hex, run['id'], 'read_document', {'path': title},
        'completed', {'pages': 12}, phase='main')
    coordinator.store.update(run['id'], status='completed',
        assignments=[{'id': 'main', 'actorId': actor_id, 'status': 'completed'}],
        result={'summary': summary})
    return run


def test_read_book_followup_uses_own_verified_task_across_rooms(world):
    coordinator, _, ids = world
    book = _finished_read(coordinator, ids[0], 'dm-' + ids[0],
        '港区随笔.pdf', '这本书写港区成员的日常与彼此照应。')
    linked = resolve_task_reference(coordinator.store, ids[0], '你觉得这本书写得怎么样？')
    assert linked['runId'] == book['id']
    assert linked['readSources'][0]['name'] == '港区随笔.pdf'
    assert linked['verified']
    assert resolve_task_reference(coordinator.store, ids[0], '今天想聊什么？') is None
    assert resolve_task_reference(coordinator.store, ids[1], '你觉得这本书写得怎么样？') is None
    assert resolve_task_reference(coordinator.store, ids[1], '刚才那本书你读过吗？') is None
    assert resolve_task_reference(coordinator.store, ids[0], '你觉得另一册.pdf怎么样？', force=True) is None
    linked_again=resolve_task_reference(coordinator.store,ids[0],'你觉得它写得如何？',
        [{'speakerId':'commander','content':'刚看的港区随笔.pdf，是这本书。'}])
    assert linked_again['runId']==book['id']


def test_two_recent_books_require_clarification(world):
    coordinator,_,ids=world
    first=_finished_read(coordinator,ids[0],'dm-'+ids[0],'第一本书.pdf','第一本书讨论港区生活。')
    second=_finished_read(coordinator,ids[0],'port-hub','第二本书.pdf','第二本书讨论航海。')
    ambiguous=resolve_task_reference(coordinator.store,ids[0],'你觉得这本书怎么样？')
    assert ambiguous['ambiguous']
    assert set(ambiguous['candidateNames'])=={'第一本书.pdf','第二本书.pdf'}
    named=resolve_task_reference(coordinator.store,ids[0],'你觉得第二本书.pdf怎么样？')
    assert named['runId']==second['id']


def test_group_followup_prioritizes_member_who_read_the_book(world):
    coordinator, _, ids = world
    reader = ids[0]
    _finished_read(coordinator, reader, 'dm-' + reader,
        '航海故事.pdf', '前十二页讲的是两位朋友保持联系。')
    with session_scope() as session:
        coordinator.social_engine.service._append_message(session,
            session.get(Conversation, 'port-hub'), 'commander', 'text',
            '你觉得这本书写得怎么样？', message_id='group-book-followup')
    room = coordinator.social_engine.snapshot('port-hub')
    assert coordinator.social_engine.candidates(room, {'id':'book-topic','prompt':'你觉得这本书写得怎么样？',
        'spoken':0, 'recent':[]})[0]['id'] == reader


def test_verified_read_survives_work_drawer_removal_until_actor_forgets(world):
    coordinator, _, ids = world
    read = _finished_read(coordinator, ids[0], 'dm-' + ids[0],
        '航海札记.pdf', '已读的前十二页讨论航线与日常。')
    sources = __import__('backend.chat.task_reference', fromlist=['read_sources']).read_sources(
        coordinator.store, read, ids[0])
    with session_scope() as session:
        session.add(MemoryChunk(id='verified-'+read['id'], actor_id=ids[0],
            conversation_id='dm-'+ids[0], source_kind='verified_task',
            summary='已读的前十二页讨论航线与日常。',
            metadata_json={'runId':read['id'], 'verified':True,
                'taskEvidence':{'summary':'已读的前十二页讨论航线与日常。'},
                'readSources':sources}))
    coordinator.cognition.pump()
    coordinator.store.remove(read['id'])
    linked = resolve_task_reference(coordinator.store, ids[0], '你觉得这本书怎么样？')
    assert linked and linked['runId'] == read['id']
    assert linked['readSources'][0]['name'] == '航海札记.pdf'
    with session_scope() as session:
        outcome = session.scalar(select(Experience).where(Experience.actor_id == ids[0],
            Experience.run_id == read['id'], Experience.kind == 'outcome'))
        assert outcome
        experience_id = outcome.id
    coordinator.cognition.revise(ids[0], experience_id, forget=True)
    assert resolve_task_reference(coordinator.store, ids[0], '你觉得这本书怎么样？') is None
    assert resolve_task_reference(coordinator.store, ids[1], '你觉得这本书怎么样？') is None


def test_busy_actor_mention_is_deferred_without_changing_task(world, monkeypatch):
    coordinator, _, ids = world
    engine = coordinator.social_engine
    coordinator.cognition.runtime.enabled = False
    task, _ = coordinator.store.create({
        'actorId': ids[0], 'actors': [{'id': ids[0], 'name': 'reader'}],
        'mode': 'task', 'collaborative': False, 'conversationId': 'dm-' + ids[0],
        'prompt': '读书', 'history': [], 'workspace': '.',
    }, uuid4().hex)
    coordinator.store.update(task['id'], status='running',
        assignments=[{'id': 'main', 'actorId': ids[0], 'status': 'running'}])
    with session_scope() as session:
        engine.service._append_message(session, session.get(__import__('backend.models', fromlist=['Conversation']).Conversation, 'port-hub'),
            'commander', 'text', '@标枪 来聊聊', metadata={'mentions': [ids[0]]})
    room = engine.snapshot('port-hub')
    assert ids[0] not in [actor['id'] for actor in engine.candidates(room, {'id': 'topic', 'prompt': '来聊聊', 'spoken': 0})]
    engine.defer_busy_mentions(room, {'id': 'topic', 'runId': None})
    with session_scope() as session:
        row = session.scalar(select(SocialDeferredReply).where(SocialDeferredReply.actor_id == ids[0]))
        assert row and row.status == 'pending'
        assert any(item['actorId']==ids[0] for item in engine.service.build_snapshot(session)['pendingReplies'])
    assert coordinator.store.get(task['id'])['status'] == 'running'
    coordinator.store.update(task['id'], status='completed', result={'summary': '读完了'})
    called = []
    async def exchange(topic_id):
        called.append(topic_id)
    monkeypatch.setattr(engine, 'exchange', exchange)
    asyncio.run(engine.resume_deferred())
    assert called
    with session_scope() as session:
        assert session.get(SocialDeferredReply, row.id).status == 'completed'


def test_social_preemption_cancels_only_chat_inference(world):
    coordinator,_,ids=world
    engine=coordinator.social_engine
    async def scenario():
        work=asyncio.create_task(asyncio.Event().wait())
        coordinator.tasks['work']=work
        group=asyncio.create_task(engine.track(ids[0],asyncio.sleep(30)))
        await asyncio.sleep(0)
        engine.preempt_actor(ids[0])
        from backend.social.idle_social import SocialPreempted
        try:await group
        except SocialPreempted:pass
        else:raise AssertionError('chat inference should yield')
        assert not work.done() and not work.cancelled()
        work.cancel()
        try:await work
        except asyncio.CancelledError:pass
        coordinator.tasks.pop('work')
    asyncio.run(scenario())


def test_new_peer_topic_expires_pending_reply(world):
    coordinator,_,ids=world
    engine=coordinator.social_engine
    with session_scope() as session:
        room=session.get(Conversation,'port-hub')
        first=engine.service._append_message(session,room,'commander','text','@标枪 来聊聊',metadata={'mentions':[ids[0]]})
        engine.service._append_message(session,room,ids[1],'text','换个话题聊航海吧',metadata={'topicId':'new-topic'})
        session.add(SocialDeferredReply(id='stale-topic',conversation_id='port-hub',actor_id=ids[0],
            source_message_id=first.id,status='pending',data={}))
    asyncio.run(engine.resume_deferred())
    with session_scope() as session:
        assert session.get(SocialDeferredReply,'stale-topic').status=='expired'


def test_new_user_message_expires_deferred_reply(world):
    coordinator, _, ids = world
    with session_scope() as session:
        session.add(SocialDeferredReply(id='old', conversation_id='port-hub', actor_id=ids[0],
            source_message_id='old-message', status='pending', data={}))
    coordinator.social_engine.recover()
    with session_scope() as session:
        assert session.get(SocialDeferredReply, 'old').status == 'expired'
