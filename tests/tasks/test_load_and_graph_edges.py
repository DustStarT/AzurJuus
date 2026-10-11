from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.tasks.run_store import RunStore
from conftest import fixture_run


def test_event_pages_and_idempotent_admission_under_parallel_writers(tmp_path):
    store = RunStore(tmp_path / 'runs.db')
    data = {'prompt': 'synthetic', 'conversationId': 'synthetic', 'mode': 'task', 'collaborative': False}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.create(data, 'one-request'), range(32)))
        list(pool.map(lambda i: store.event(None, 'synthetic.event', {'index': i}), range(1200)))
    assert sum(created for _, created in results) == 1
    assert len({run['id'] for run, _ in results}) == 1
    events, cursor = [], 0
    while page := store.events(after=cursor, limit=73):
        events.extend(page)
        cursor = page[-1]['seq']
    assert len(events) == 1201
    assert [event['seq'] for event in events] == list(range(1, 1202))
    assert len({event['payload']['index'] for event in events if event['type'] == 'synthetic.event'}) == 1200
    with pytest.raises(ValueError):
        store.create({**data, 'prompt': 'changed'}, 'one-request')
    assert len(store.list(all_rows=True)) == 1


def test_record_cleanup_reaches_beyond_the_default_list_limit(tmp_path):
    store = RunStore(tmp_path / 'runs.db')
    created = []
    for i in range(260):
        run, _ = store.create({'prompt': 'synthetic', 'mode': 'chat'}, 'request-' + str(i))
        store.update(run['id'], status='completed')
        created.append(run['id'])
    assert len(store.list()) == 200 and len(store.list(all_rows=True)) == 260
    for rid in created:
        store.remove(rid)
    assert store.list() == [] and store.list(all_rows=True) == []
    # Tombstones retain admission identities and cannot silently repeat work.
    for i in [0, 199, 259]:
        prior, new = store.create({'prompt': 'synthetic', 'mode': 'chat'}, 'request-' + str(i))
        assert not new and prior['id'] == created[i] and prior['deletedAt']


def test_maximum_plan_and_malformed_revisions_preserve_accepted_state(world):
    coordinator, _, actors = world
    run, _ = fixture_run(coordinator, actors[0])
    rid = run['id']
    tasks = [{'id': 't' + str(i), 'actorId': actors[0], 'brief': 'synthetic', 'acceptance': 'verified',
              'dependsOn': ['t' + str(i - 1)] if i else []} for i in range(24)]
    assert coordinator.plan(rid, {'tasks': tasks})['count'] == 24
    coordinator.store.update(rid, status='paused')
    before = coordinator.store.get(rid)['assignments']
    bad = [None, 'tasks', [None], [{'id': 'x', 'actorId': ['wrong']}],
           [{**tasks[0], 'brief': []}], [{**tasks[0], 'dependsOn': [['wrong']]}], tasks + [tasks[0]],
           [{**tasks[0], 'dependsOn': ['t0']}], [tasks[0], tasks[0]]]
    for revision in bad:
        with pytest.raises(ValueError):
            coordinator.replan(rid, revision)
        assert coordinator.store.get(rid)['assignments'] == before
    for plan in bad:
        with pytest.raises(ValueError):
            coordinator.plan(rid, {'tasks': plan})
        assert coordinator.store.get(rid)['assignments'] == before
