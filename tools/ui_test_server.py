"""Isolated, real backend for UI acceptance; never opens the user's database."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_ROOT = ROOT / ".azurjuus" / os.getenv("AZURJUUS_UI_TEST_DIRECTORY", "ui-acceptance")
TEST_ROOT.mkdir(parents=True, exist_ok=True)
(TEST_ROOT / "workspace").mkdir(exist_ok=True)
os.environ.update(
    AZURJUUS_DATABASE_URL="sqlite+pysqlite:///" + (TEST_ROOT / "ui.db").as_posix(),
    AZURJUUS_WORKSPACE_STATE_PATH=str(TEST_ROOT / "state" / "workspace-state.json"),
    AZURJUUS_WORKSPACE_ROOT=str(TEST_ROOT / "workspace"),
    AZURJUUS_REDIS_URL="", AZURJUUS_CHROMA_URL="", AZURJUUS_SOCIAL_ENABLED="0",
    AZURJUUS_EXECUTION_BACKEND="hermes", AZURJUUS_PORT="8879",
    AZURJUUS_REFLECTION_ENABLED="0", AZURJUUS_SKILL_TRIALS_ENABLED="0",
)
from server import create_server, serve


def build_server(port=8879):
    server = create_server(host="127.0.0.1", port=port)
    @server.app.post('/__acceptance/mind')
    async def mind_fixture(payload: dict):
        c = server.app.state.runs
        actor_id = payload['actorId']
        c.store.event(None,'mind.observation',{'key':'ui-mind-promise','actorIds':[actor_id],
            'kind':'speech','text':'我会在提交前提醒检查合成附件。','data':{'ownSpeech':True}})
        c.cognition.pump()
        return {'status':'ok'}
    @server.app.post("/__acceptance/history_stream")
    async def history_stream():
        import asyncio
        from backend.database import session_scope
        from backend.models import Conversation, Message
        c, service = server.app.state.runs, server.app.state.service
        with session_scope() as session:
            snapshot = service.build_snapshot(session)
            conversation = next(v for v in snapshot["conversations"] if v["kind"] == "dm")
            cid = conversation["id"]
            actor = next(a for a in snapshot["agents"] if a["id"] in conversation["memberIds"])
            row = session.get(Conversation, cid)
            for index in range(350):
                mid = f"ui-history-{index}"
                if session.get(Message, mid) is None:
                    service._append_message(session, row, actor["id"], "text", f"长列表合成消息 {index:03d}", message_id=mid)
        run, _ = c.store.create({"prompt":"流式合成界面验收", "conversationId":cid,"workspace":str(TEST_ROOT / "workspace"),"actors":[actor],"actorId":actor["id"],"mode":"chat","collaborative":False,"grants":[]},"ui-stream-fixture")
        rid = run["id"]
        c.store.update(rid, status="running")
        c.store.event(None, "workspace.changed", {})
        async def stream():
            await asyncio.sleep(1)
            envelope = {"actorId":actor["id"], "assignmentId":"chat", "messageId":rid + "-result"}
            c.store.event(rid, "message.start", envelope)
            text = ""
            for index in range(60):
                delta = f"流{index:02d} " + ("。" if index % 5 == 4 else "") + ("\n\n" if index in {15, 35} else "")
                text += delta
                c.store.event(rid, "message.delta", {**envelope,"delta":delta})
                if index % 6 == 0:
                    c.store.event(None, "workspace.changed", {})
                await asyncio.sleep(.05)
            await asyncio.sleep(5)
            await c.message_callback(rid, {**envelope, "text":text})
            c.store.event(rid, "message.complete", {**envelope, "text":text})
            c.store.update(rid, status="completed", result={"summary":text,"artifacts":[],"checks":[],"unresolved":[]})
            await c.finish_callback(rid, text)
        task = asyncio.create_task(stream())
        # Keep and close this synthetic event source with the normal app lifecycle.
        c.tasks[rid] = task
        task.add_done_callback(lambda _: c.tasks.pop(rid, None))
        return {"conversationTitle":conversation["title"], "conversationId":cid, "runId":rid, "messageId":rid + "-result"}

    @server.app.post("/__acceptance/seed_run")
    async def seed_run():
        from backend.database import session_scope
        c = server.app.state.runs
        service = server.app.state.service
        with session_scope() as session:
            snapshot = service.build_snapshot(session)
        actor = snapshot["agents"][0]
        cid = next(v["id"] for v in snapshot["conversations"] if v["kind"] == "group")
        run, created = c.store.create({"prompt":"界面验收：生成并核验测试报告", "conversationId":cid,"workspace":str(TEST_ROOT / "workspace"),"actors":[actor],"actorId":actor["id"],"mode":"task","collaborative":False,"grants":[]},"ui-evidence-v1")
        if not created:
            return {"runId":run["id"]}
        rid=run["id"]
        c.store.update(rid,status="running",assignments=[{"id":"main","actorId":actor["id"],"brief":"写入合成验收报告并读取核验","dependsOn":[],"acceptance":["文件内容正确"],"status":"running"}])
        token="ui-fixture-token"
        from uuid import uuid4
        async def call(name,args):
            result=await c.tool(token,{"callId":uuid4().hex,"name":name,"args":args})
            if result["status"]!='completed':raise RuntimeError(str(result))
            return result["callId"]
        try:
            c.tokens[token]=(rid,actor['id'],'main')
            await call('write_file',{'path':'ui-report.txt','content':'Synthetic UI acceptance report. File tools executed; no cloud model was used.\n'})
            check=await call('read_file',{'path':'ui-report.txt'})
            delivery={'summary':'测试报告已写入并核验。此条用于界面验收，工具真实执行，未调用云端模型。','artifacts':['ui-report.txt'],'checks':[{'callId':check,'description':'读取并检查当前文件'}],'unresolved':[]}
            await call('deliver',delivery)
            c.tokens[token]=(rid,actor['id'],'reviewer')
            delivery['checks']=[{'callId':await call('read_file',{'path':'ui-report.txt'}),'description':'独立复核读取'}]
            await call('deliver',delivery)
            result=c.store.get(rid)['reviewResult']
            c.store.update(rid,status='completed',result=result,artifacts=result['artifacts'])
            await c.finish_callback(rid,result['summary'])
        finally:
            c.tokens.pop(token,None)
        return {'runId':rid}

    @server.app.post("/__acceptance/seed_group")
    async def seed_group():
        from backend.database import session_scope
        from backend.models import Conversation, ConversationMember
        from uuid import uuid4
        cid = 'ui-group-' + uuid4().hex
        with session_scope() as session:
            service = server.app.state.service
            snapshot = service.build_snapshot(session)
            dm = next(c for c in snapshot['conversations'] if c['kind'] == 'dm')
            foreign = next(a for a in snapshot['agents'] if a['id'] not in dm['memberIds'])
            service._append_message(session, session.get(Conversation, dm['id']), foreign['id'], 'task', '旧版秘书回传内容', message_id=cid + '-foreign')
            session.add(Conversation(id=cid, kind='group', title='可删除的合成协作群'))
            session.flush()
            session.add_all([ConversationMember(conversation_id=cid, actor_id=aid, role='member') for aid in dm['memberIds']])
        server.app.state.runs.store.event(None, 'workspace.changed', {})
        return {'conversationId':cid, 'originId':dm['id'], 'foreignMessageId':cid + '-foreign'}
    return server

if __name__ == "__main__":
    serve(build_server())
