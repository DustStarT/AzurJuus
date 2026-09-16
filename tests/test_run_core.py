import asyncio
from pathlib import Path
import sys

import pytest

from backend.run_store import RunStore
from backend.run_coordinator import RunCoordinator
from backend.capabilities import CapabilityError


@pytest.fixture
def runtime(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    store = RunStore(tmp_path / "state" / "runs.db")
    async def finished(*args):
        pass
    coordinator = RunCoordinator(store, lambda: {"llmApiKey": "test"}, finished, "http://127.0.0.1/tool")
    run, _ = store.create({"prompt": "Write a report", "mode": "task", "collaborative": False, "conversationId": "test", "workspace": str(work), "actorId": "a", "actors": [{"id": "a", "name": "A"}], "grants": []}, "request")
    store.update(run["id"], status="running", assignments=[{"id": "main", "actorId": "a", "status": "running", "brief": "Write", "dependsOn": [], "acceptance": ["read back"]}])
    coordinator.tokens["token"] = (run["id"], "a", "main")
    return coordinator, store, run["id"], work


async def call(c, name, args, cid):
    return await c.tool("token", {"callId": cid, "name": name, "args": args})


@pytest.mark.asyncio
async def test_delivery_requires_own_unchanged_readback(runtime):
    c, store, rid, work = runtime
    await call(c, "write_file", {"path": "report.txt", "content": "verified"}, "write")
    result = {"summary": "done", "artifacts": ["report.txt"], "checks": [{"callId": "write"}], "unresolved": []}
    assert (await call(c, "deliver", result, "bad"))["status"] == "failed"
    await call(c, "read_file", {"path": "report.txt"}, "read")
    result["checks"] = [{"callId": "read"}]
    (work / "report.txt").write_text("changed")
    assert (await call(c, "deliver", result, "stale"))["status"] == "failed"
    await call(c, "read_file", {"path": "report.txt"}, "read2")
    result["checks"] = [{"callId": "read2"}]
    assert (await call(c, "deliver", result, "good"))["status"] == "completed"
    c.tokens["token"] = (rid, "a", "reviewer")
    assert (await call(c, "deliver", result, "borrowed"))["status"] == "failed"
    assert (await call(c, "read_file", {"path": "report.txt"}, "own"))["status"] == "completed"
    result["checks"] = [{"callId": "own"}]
    assert (await call(c, "deliver", result, "review"))["status"] == "completed"


@pytest.mark.asyncio
async def test_approval_resumes_exact_call_once(runtime):
    c, store, rid, work = runtime
    args = {"argv": [sys.executable, "-c", "from pathlib import Path; p=Path('counter'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]}
    task = asyncio.create_task(call(c, "command", args, "execute"))
    for _ in range(100):
        if store.call("execute"):
            break
        await asyncio.sleep(.01)
    assert store.get(rid)["status"] == "waiting_approval"
    assert not (work / "counter").exists()
    await c.resolve(rid, "execute", "approved")
    assert (await task)["status"] == "completed"
    assert (await call(c, "command", args, "execute"))["status"] == "completed"
    assert (work / "counter").read_text() == "x"


def test_restart_expires_approval_and_preserves_unknown_effect(runtime):
    c, store, rid, _ = runtime
    store.put_call("pending", rid, "command", {}, "waiting_approval", phase="main")
    store.put_call("unknown", rid, "write_file", {}, "running", phase="main")
    RunStore(store.path).recover()
    assert store.get(rid)["status"] == "paused"
    assert store.call("pending")["status"] == "expired"
    assert store.get(rid)["uncertainCalls"] == ["unknown"]


def test_plan_rejects_cycles_paths_and_reserved_ids(runtime):
    c, _, rid, _ = runtime
    for aid, dependencies in [("../escape", []), ("reviewer", []), ("task", ["task"])]:
        with pytest.raises(ValueError):
            c.plan(rid, {"tasks": [{"id": aid, "actorId": "a", "brief": "work", "acceptance": ["done"], "dependsOn": dependencies}]})


@pytest.mark.asyncio
async def test_seven_tool_rounds_and_document_pagination(runtime):
    c, _, _, _ = runtime
    for i in range(7):
        assert (await call(c, "write_file", {"path": f"{i}.txt", "content": str(i)}, f"round{i}"))["status"] == "completed"
    await call(c, "write_document", {"path": "book.xlsx", "sheets": {"data": [[i, f"row {i}"] for i in range(620)]}}, "book")
    first = await call(c, "read_document", {"path": "book.xlsx", "limit": 500}, "page1")
    second = await call(c, "read_document", {"path": "book.xlsx", "offset": 500, "limit": 500}, "page2")
    assert first["result"]["nextOffset"] == 500
    assert len(second["result"]["items"]) == 120
    assert first["result"]["sha256"] == second["result"]["sha256"]


def test_paths_and_idempotency(runtime):
    c, store, rid, work = runtime
    with pytest.raises(CapabilityError):
        c.tools.path(str(work), "../state/runs.db")
    with pytest.raises(ValueError):
        store.create({"prompt": "different"}, "request")


@pytest.mark.asyncio
async def test_background_scheduler_rejects_progress_only(runtime):
    c, store, rid, _ = runtime
    class Bridge:
        session_id = None
        def __init__(self, *args): pass
        async def start(self): pass
        async def prompt(self, *args): return "Working on it!"
        async def close(self): pass
    c.bridge_factory = Bridge
    c.launch(rid)
    await c.tasks[rid]
    assert store.get(rid)["status"] == "failed"
    assert "未提交" in store.get(rid)["error"]


@pytest.mark.asyncio
async def test_thread_cancellation_retains_write_lock(runtime):
    import threading
    c, store, rid, _ = runtime
    entered, release = threading.Event(), threading.Event()
    def blocking(*args):
        entered.set()
        release.wait(3)
    c.tools.file_tool = blocking
    task = asyncio.create_task(c.tools.execute(store.get(rid), "write_file", {}, "blocking", lambda *a: None))
    while not entered.is_set():
        await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(.02)
    assert c.tools.write_lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not c.tools.write_lock.locked()


@pytest.mark.asyncio
async def test_pause_cancels_approval_without_executing(runtime):
    c,store,rid,work=runtime
    task=asyncio.create_task(call(c,'command',{'argv':[sys.executable,'-c',"from pathlib import Path;Path('forbidden').touch()"]},'waiting'))
    while store.call('waiting') is None:
        await asyncio.sleep(.01)
    await c.control(rid,'pause')
    with pytest.raises(asyncio.CancelledError): await task
    assert not (work/'forbidden').exists()
    assert store.get(rid)['status']=='paused'
    with pytest.raises(ValueError): await c.resolve(rid,'waiting','approved')


@pytest.mark.asyncio
async def test_budget_exhaustion_is_resumable(runtime):
    c,store,rid,_=runtime
    class BudgetBridge:
        session_id=None
        budget_exhausted=True
        def __init__(self,*args):pass
        async def start(self):pass
        async def prompt(self,*args):return 'Need more time'
        async def close(self):pass
    c.bridge_factory=BudgetBridge
    c.launch(rid)
    await c.tasks[rid]
    assert store.get(rid)['status']=='paused'
    assert '预算' in store.get(rid)['error']


@pytest.mark.asyncio
async def test_failed_tool_requires_documented_correction(runtime):
    c,store,rid,work=runtime
    await call(c,'read_file',{'path':'missing.txt'},'missing')
    await call(c,'write_file',{'path':'report.txt','content':'actual report'},'write')
    await call(c,'read_file',{'path':'report.txt'},'check')
    args={'summary':'done','artifacts':['report.txt'],'checks':[{'callId':'check'}],'unresolved':[]}
    assert (await call(c,'deliver',args,'premature'))['status']=='failed'
    args['resolvedErrors']=[{'callId':'missing','resolution':'Created the requested report instead of reading a nonexistent input','checkCallId':'check'}]
    assert (await call(c,'deliver',args,'corrected'))['status']=='completed'


@pytest.mark.asyncio
async def test_late_instruction_is_saved_and_consumed_on_resume(runtime):
    c, store, rid, _ = runtime
    store.update(rid, mode="chat", assignments=[])
    entered, finish = asyncio.Event(), asyncio.Event()
    histories = []
    class ChatBridge:
        session_id = "chat-session"
        def __init__(self, *args): pass
        async def start(self): pass
        async def close(self): pass
        async def steer(self, text): return {"queued": True}
        async def prompt(self, prompt, workspace, history):
            histories.append(history)
            entered.set()
            await finish.wait()
            return "Reply"
    c.bridge_factory = ChatBridge
    c.launch(rid)
    task = c.tasks[rid]
    await entered.wait()
    await c.control(rid, "steer", "Include the new requirement")
    finish.set()
    await task
    assert store.get(rid)["status"] == "paused"
    assert not store.get(rid)["steering"][0]["delivered"]
    await c.control(rid, "resume")
    await c.tasks[rid]
    assert store.get(rid)["status"] == "completed"
    assert store.get(rid)["steering"][0]["consumedBy"] == ["chat"]
    assert "Include the new requirement" in str(histories[-1])
    await c.close()


@pytest.mark.asyncio
async def test_directory_link_cannot_escape_task_workspace(runtime, tmp_path):
    import os
    c, store, rid, work = runtime
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("unchanged")
    link = work / "linked-directory"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        result = await call(c, "write_file", {"path":"linked-directory/keep.txt","content":"forbidden"}, "escape")
        assert result["status"] == "failed"
        assert (outside / "keep.txt").read_text() == "unchanged"
        with pytest.raises(CapabilityError):
            c.tools.path(str(work), "linked-directory/new.txt")
    finally:
        # Remove only the link itself, never recurse into its target.
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.asyncio
async def test_invalid_approval_arguments_are_journaled(runtime):
    c, store, rid, _ = runtime
    result = await call(c, "move", {"src":"missing-destination.txt"}, "invalidargs")
    assert result["status"] == "failed"
    assert store.call("invalidargs")["phase"] == "main"
    assert store.call("invalidargs")["status"] == "failed"
    assert store.get(rid)["status"] == "running"


@pytest.mark.asyncio
async def test_stopped_task_cannot_send_desktop_input(runtime):
    c, store, rid, _ = runtime
    await c.control(rid, "cancel")
    with pytest.raises(PermissionError, match="没有执行权限"):
        await call(c, "desktop", {"op":"shortcut","handle":123,"action":"paste"}, "afterstop")
    assert store.call("afterstop") is None
