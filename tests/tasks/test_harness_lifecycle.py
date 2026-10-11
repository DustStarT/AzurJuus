"""Cancellation must settle the synchronous SDK, not just its async awaiter."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from deepseek_harness.models import Notification

from backend.tasks.deepseek_bridge import DeepSeekHarnessBridge, DeepSeekHarnessUnavailable


@pytest.fixture
def sdk(monkeypatch):
    class Harness:
        def __init__(self, config):
            self.started = threading.Event()
            self.start_release = threading.Event()
            self.running = threading.Event()
            self.run_release = threading.Event()
            self.joined = threading.Event()
            self.starts = self.closes = 0
            self.closed = False
            self.notify = None
            self.client = self
            self.config = config

        def start(self):
            self.starts += 1
            self.started.set()
            assert self.start_release.wait(5)

        def start_session(self, session_id):
            self.session_id = session_id
            return self

        def event(self, kind, data, session_id=None):
            self.notify(Notification(method='session.event', payload={
                'sessionId': session_id or self.session_id, 'event': {'type': kind, 'data': data}}))

        def run(self, content, on_notification):
            self.notify = on_notification
            self.running.set()
            try:
                assert self.run_release.wait(5)
                if self.closed:
                    raise RuntimeError('transport closed')
                return SimpleNamespace(final_response='Finished.', finish_reason='stop')
            finally:
                self.joined.set()

        def session_prompt(self, session_id, content):
            self.steered_content = content
            self.event('agent/inbox/spliced', {'inserted': [{'id': 'steering-message'}]})
            return 'steering-message'

        def close(self):
            self.closes += 1
            self.closed = True
            self.run_release.set()

    instances = []

    def factory(config):
        instance = Harness(config)
        instances.append(instance)
        return instance

    monkeypatch.setattr('backend.tasks.deepseek_bridge.DeepSeekHarness', factory)
    return instances


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


def bridge_at(tmp_path, events):
    async def on_event(kind, payload):
        events.append((kind, payload))
    return DeepSeekHarnessBridge(tmp_path / 'home', {
        'llmBaseUrl': 'http://127.0.0.1', 'llmModel': 'synthetic',
    }, 'http://127.0.0.1/tool', 'synthetic-token', on_event)


@pytest.mark.asyncio
async def test_cancelled_start_waits_for_startup_then_reaps_once(tmp_path, sdk):
    events = []
    bridge = bridge_at(tmp_path, events)
    task = asyncio.create_task(bridge.start())
    await until(lambda: sdk and sdk[0].started.is_set())
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    sdk[0].start_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(bridge.close(), bridge.close())
    assert sdk[0].starts == sdk[0].closes == 1
    assert sum(payload.get('stage') == 'closed' for _, payload in events) == 1
    with pytest.raises(DeepSeekHarnessUnavailable):
        await bridge.start()


@pytest.mark.asyncio
async def test_concurrent_start_and_close_cannot_publish_a_late_runtime(tmp_path, sdk):
    bridge = bridge_at(tmp_path, [])
    starts = [asyncio.create_task(bridge.start()) for _ in range(3)]
    await until(lambda: sdk and sdk[0].started.is_set())
    closes = [asyncio.create_task(bridge.close()) for _ in range(3)]
    await asyncio.sleep(.01)
    sdk[0].start_release.set()
    results = await asyncio.gather(*starts, *closes, return_exceptions=True)
    assert all(isinstance(result, DeepSeekHarnessUnavailable) for result in results[:3])
    assert results[3:] == [None] * 3
    assert bridge.harness is None
    assert sdk[0].starts == sdk[0].closes == 1


async def running_bridge(tmp_path, sdk, events):
    bridge = bridge_at(tmp_path, events)
    task = asyncio.create_task(bridge.prompt('Work.', str(tmp_path)))
    await until(lambda: sdk and sdk[0].started.is_set())
    sdk[0].start_release.set()
    await until(lambda: sdk[0].running.is_set())
    return bridge, task


@pytest.mark.asyncio
async def test_cancelled_prompt_joins_sdk_call_before_return(tmp_path, sdk):
    events = []
    bridge, task = await running_bridge(tmp_path, sdk, events)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk[0].joined.is_set() and sdk[0].closes == 1
    assert bridge.harness is None
    assert not any(kind == 'message.complete' for kind, _ in events)


@pytest.mark.asyncio
async def test_waiting_prompt_and_late_notifications_cannot_revive_closed_bridge(tmp_path, sdk):
    events = []
    bridge, task = await running_bridge(tmp_path, sdk, events)
    queued = asyncio.create_task(bridge.prompt('Next.', str(tmp_path)))
    await bridge.close()
    sdk[0].event('assistant/message', {'message': {'content': [{'type': 'text', 'text': 'Late.'}]}})
    with pytest.raises(RuntimeError, match='transport closed'):
        await task
    with pytest.raises(DeepSeekHarnessUnavailable):
        await queued
    assert sdk[0].starts == sdk[0].closes == 1
    assert not any(kind in {'message.delta', 'message.complete'} for kind, _ in events)


@pytest.mark.asyncio
async def test_callback_failure_shuts_down_and_joins_the_sdk(tmp_path, sdk):
    bridge, task = await running_bridge(tmp_path, sdk, [])

    async def fail(kind, payload):
        if kind == 'message.delta':
            raise ValueError('projection unavailable')

    bridge.on_event = fail
    sdk[0].event('assistant/message', {'message': {'content': [{'type': 'text', 'text': 'Partial.'}]}})
    with pytest.raises(ValueError, match='projection unavailable'):
        await task
    assert sdk[0].joined.is_set() and sdk[0].closes == 1


@pytest.mark.asyncio
async def test_native_steering_requires_root_inbox_receipt(tmp_path, sdk):
    events = []
    bridge, task = await running_bridge(tmp_path, sdk, events)
    sdk[0].event('agent/inbox/spliced', {'inserted': [{'id': 'child-message'}]}, 'child-session')
    sdk[0].event('assistant/message', {'message': {'content': [{'type': 'text', 'text': 'Child reply.'}]}}, 'child-session')
    result = await bridge.steer('Additional requirement.', 'instruction-1')
    assert result == {'queued': True, 'messageId': 'steering-message'}
    await until(lambda: any(kind == 'runtime.steering' for kind, _ in events))
    assert [(kind, payload) for kind, payload in events if kind == 'runtime.steering'] == [
        ('runtime.steering', {'instructionId': 'instruction-1', 'messageId': 'steering-message'})]
    assert sdk[0].steered_content[0]['text'].endswith('Additional requirement.')
    assert not any(kind == 'message.delta' for kind, _ in events)
    sdk[0].run_release.set()
    assert await task == 'Finished.'
    assert await bridge.steer('Too late.', 'instruction-2') == {'queued': False}
    await bridge.close()
