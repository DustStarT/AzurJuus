import asyncio
import time
from unittest.mock import AsyncMock

from conftest import fixture_run


def enable_moments(mind, monkeypatch):
    runtime, coordinator, client, _, _ = mind
    monkeypatch.setattr(runtime.life, 'quiet', lambda: False)
    runtime.control({'paused':False,'momentsEnabled':True})
    return coordinator, client


def test_chat_blocks_background_calls_but_is_reported_as_replying(mind, monkeypatch):
    coordinator, client = enable_moments(mind, monkeypatch)
    runtime, _, _, ids, stages = mind
    run, _ = fixture_run(coordinator, ids[0])
    post = AsyncMock(return_value='wait')
    monkeypatch.setattr(coordinator.moments, '_post', post)
    coordinator.moments._change(lambda data:data.update(startAt=time.time()-12*3600))
    async def scenario():
        job = asyncio.get_running_loop().create_future()
        coordinator.tasks[run['id']] = job
        try:
            status = client.get('/api/posts/status').json()
            assert status['reason']=='responding'
            assert coordinator.foreground_activity()=='chat'
            assert runtime.background_blocked()
            await coordinator.moments.tick()
            assert post.await_count==0 and not stages
        finally:
            job.set_result(None)
            coordinator.tasks.pop(run['id'], None)
        assert client.get('/api/posts/status').json()['reason']=='waiting'
        await coordinator.moments.tick()
        assert post.await_count==1 and not stages
    asyncio.run(scenario())


def test_real_work_takes_priority_over_chat_in_status(mind, monkeypatch):
    coordinator, client = enable_moments(mind, monkeypatch)
    _, _, _, ids, stages = mind
    chat, _ = fixture_run(coordinator, ids[0])
    work, _ = fixture_run(coordinator, ids[1])
    coordinator.store.update(work['id'], mode='task')
    async def scenario():
        loop = asyncio.get_running_loop()
        jobs = {chat['id']:loop.create_future(), work['id']:loop.create_future()}
        coordinator.tasks.update(jobs)
        try:
            assert coordinator.foreground_activity()=='task'
            assert client.get('/api/posts/status').json()['reason']=='task_busy'
            jobs[work['id']].set_result(None)
            coordinator.tasks.pop(work['id'])
            assert client.get('/api/posts/status').json()['reason']=='responding'
            assert not stages
        finally:
            for rid, job in jobs.items():
                if not job.done():job.set_result(None)
                coordinator.tasks.pop(rid, None)
    asyncio.run(scenario())


def test_untracked_foreground_job_is_not_claimed_to_be_a_work_task(mind, monkeypatch):
    coordinator, client = enable_moments(mind, monkeypatch)
    async def scenario():
        job = asyncio.get_running_loop().create_future()
        coordinator.tasks['unknown-fixture-job'] = job
        try:
            assert coordinator.foreground_activity()=='other'
            assert client.get('/api/posts/status').json()['reason']=='busy'
        finally:
            job.set_result(None)
            coordinator.tasks.clear()
    asyncio.run(scenario())


def test_completed_job_does_not_override_a_live_chat(mind, monkeypatch):
    coordinator, client = enable_moments(mind, monkeypatch)
    _, _, _, ids, _ = mind
    chat, _ = fixture_run(coordinator, ids[0])
    work, _ = fixture_run(coordinator, ids[1])
    coordinator.store.update(work['id'], mode='task')
    async def scenario():
        loop = asyncio.get_running_loop()
        done, live = loop.create_future(), loop.create_future()
        done.set_result(None)
        coordinator.tasks.update({work['id']:done, chat['id']:live})
        try:
            assert coordinator.foreground_activity()=='chat'
            assert client.get('/api/posts/status').json()['reason']=='responding'
        finally:
            live.set_result(None)
            coordinator.tasks.clear()
    asyncio.run(scenario())


def test_foreground_exit_notifies_after_terminal_record_is_already_saved(mind, monkeypatch):
    coordinator, client = enable_moments(mind, monkeypatch)
    _, _, _, ids, _ = mind
    run, _ = fixture_run(coordinator, ids[0])
    async def scenario():
        terminal, release = asyncio.Event(), asyncio.Event()
        async def hold(run_id):
            coordinator.store.update(run_id, status='completed')
            terminal.set()
            await release.wait()
        monkeypatch.setattr(coordinator, 'run', hold)
        coordinator.launch(run['id'])
        job = coordinator.tasks[run['id']]
        try:
            await terminal.wait()
            assert client.get('/api/posts/status').json()['reason']=='responding'
        finally:
            release.set()
            await job
            await asyncio.sleep(0)
        assert run['id'] not in coordinator.tasks
        assert client.get('/api/posts/status').json()['reason']=='waiting'
        assert any(e['type']=='workspace.changed' and
            e['payload'].get('event')=='foreground.finished' and
            e['payload'].get('runId')==run['id'] for e in coordinator.store.events())
    asyncio.run(scenario())
