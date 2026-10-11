import asyncio

import pytest

from backend.tasks.run_coordinator import RunCoordinator
from backend.tasks.run_store import RunStore


def coordinator(tmp_path):
    async def finished(*_):
        pass
    return RunCoordinator(RunStore(tmp_path / 'runs.db'),
                          lambda: {'connectedAgentIds':['secretary','member','helper']}, finished, '')


@pytest.mark.asyncio
async def test_approved_helper_starts_while_original_member_is_still_running(tmp_path):
    c = coordinator(tmp_path)
    run, _ = c.store.create({'prompt':'synthetic','conversationId':'x','actorId':'secretary',
                            'actors':[{'id':aid,'name':aid} for aid in ['secretary','member','helper']]}, 'dynamic')
    c.store.update(run['id'], status='running', assignments=[
        {'id':'main','actorId':'member','brief':'prepare report','acceptance':['read it'],
         'status':'pending','dependsOn':[]}])
    original_started, helper_finished = asyncio.Event(), asyncio.Event()

    async def assignment(rid, aid, _settings):
        c.patch_assignment(rid, aid, status='running')
        if aid == 'main':
            original_started.set()
            await helper_finished.wait()
        else:
            helper_finished.set()
        c.patch_assignment(rid, aid, status='completed', result={'summary':aid, 'artifacts':[]})
    c.assignment = assignment
    scheduler = asyncio.create_task(c.execute_assignments(run['id'], {}))
    try:
        await asyncio.wait_for(original_started.wait(), 2)
        request = c.request_help(run['id'], requester_id='member', assignment_id='main',
                                 brief='verify upstream', candidate_ids=['helper'])
        c.resolve_help(run['id'], request['id'], decision='approved', actor={'id':'helper','name':'helper'})
        await asyncio.wait_for(scheduler, 2)
        saved = c.store.get(run['id'])
        assert len(saved['actors']) == 3
        assert all(item['status'] == 'completed' for item in saved['assignments'])
        assert not c.plan_changes
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)


@pytest.mark.asyncio
async def test_one_actor_never_runs_two_assignments_at_once(tmp_path):
    c = coordinator(tmp_path)
    run, _ = c.store.create({'prompt':'synthetic','conversationId':'x'}, 'same-actor')
    c.store.update(run['id'], assignments=[{'id':aid,'actorId':'member','status':'pending','dependsOn':[]} for aid in ['a','b']])
    active, peak = 0, 0
    async def assignment(rid, aid, _settings):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.02)
        active -= 1
        c.patch_assignment(rid, aid, status='completed')
    c.assignment = assignment
    await asyncio.wait_for(c.execute_assignments(run['id'], {}), 2)
    assert peak == 1


@pytest.mark.asyncio
async def test_worker_help_tool_cannot_spoof_another_assignment_or_replan(tmp_path):
    c = coordinator(tmp_path)
    run, _ = c.store.create({'prompt':'synthetic','conversationId':'x','actorId':'secretary','workspace':str(tmp_path),
                            'actors':[{'id':'member','name':'member'}]}, 'help-tool')
    c.store.update(run['id'], status='running', assignments=[{'id':'own','actorId':'member',
        'status':'running','brief':'own work','acceptance':['verify'],'dependsOn':[]}])
    c.tokens['test'] = (run['id'], 'member', 'own')
    reply = await c.tool('test', {'callId':'help1','name':'request_help',
                                 'args':{'assignmentId':'other','brief':'verify','candidateIds':['helper']}})
    assert reply['status'] == 'completed'
    assert reply['result']['assignmentId'] == 'own'
    with pytest.raises(PermissionError, match='秘书'):
        await c.tool('test', {'callId':'replan1','name':'replan','args':{'tasks':[]}})
    with pytest.raises(ValueError, match='负责人'):
        c.request_help(run['id'], requester_id='helper', assignment_id='own', brief='invalid')
    with pytest.raises(ValueError, match='实际工具调用'):
        c.deliver(run['id'], 'own', {'summary':'not execution', 'artifacts':[], 'checks':[{'callId':'help1'}]})


def test_final_review_cannot_receive_a_late_plan_or_helper(tmp_path):
    c = coordinator(tmp_path)
    run, _ = c.store.create({'prompt':'synthetic','conversationId':'x','actorId':'secretary',
                            'actors':[{'id':'member','name':'member'}]}, 'late-helper')
    task = {'id':'own','actorId':'member','brief':'work','acceptance':['verify'],'status':'running','dependsOn':[]}
    c.store.update(run['id'], status='running', assignments=[task])
    request = c.request_help(run['id'], requester_id='member', assignment_id='own', brief='verify', candidate_ids=['helper'])
    # A launched run outside execute_assignments is planning or final review.
    c.tasks[run['id']] = object()
    with pytest.raises(ValueError, match='先暂停'):
        c.resolve_help(run['id'], request['id'], decision='approved', actor={'id':'helper'})
    with pytest.raises(ValueError, match='先暂停'):
        c.replan(run['id'], [task])
    assert c.store.get(run['id'])['helpRequests'][0]['status'] == 'pending'
