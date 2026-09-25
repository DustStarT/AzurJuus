"""Durable admission, checkpoints and ordered events. Transactions never await an LLM."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from backend.platform.sqlite_policy import journal_mode

TERMINAL = {"completed", "failed", "cancelled"}


class RunStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        if path.exists():
            with sqlite3.connect(path) as source:
                tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'runs' in tables and 'message_routes' not in tables:
                    backup = path.parent / 'migration-backups' / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-runs-route.db')
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(backup) as target:
                        source.backup(target)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=" + journal_mode())
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES(1);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, request_key TEXT UNIQUE, status TEXT, data TEXT NOT NULL, updated REAL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, type TEXT, payload TEXT NOT NULL, at REAL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
                CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, run_id TEXT, name TEXT, args TEXT, status TEXT, result TEXT, approval TEXT, updated REAL);
                CREATE INDEX IF NOT EXISTS calls_run ON calls(run_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(run_id UNINDEXED, actor_id UNINDEXED, content, tokenize='unicode61');
                CREATE TABLE IF NOT EXISTS message_routes(request_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL);
            ''')
            columns = {r[1] for r in db.execute("PRAGMA table_info(calls)")}
            if "phase" not in columns:
                db.execute("ALTER TABLE calls ADD COLUMN phase TEXT")
            if "deleted_at" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN deleted_at REAL")

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def create(self, data: dict, request_key: str):
        with self.connect() as db:
            existing = db.execute("SELECT data FROM runs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                prior = json.loads(existing[0])
                if any((prior.get("originConversationId", prior.get(key)) if key == "conversationId" else prior.get(key)) != data.get(key) for key in ("prompt", "conversationId", "mode", "collaborative", "workspace")):
                    raise ValueError("重复请求标识对应不同任务，请重新提交。")
                return prior, False
            run = {**data, "id": "run-" + uuid4().hex, "status": "queued", "createdAt": time.time(), "updatedAt": time.time(), "result": None, "error": None, "steering": [], "artifacts": [], "assignments": []}
            db.execute("INSERT INTO runs(id,request_key,status,data,updated) VALUES(?,?,?,?,?)", (run["id"], request_key, run["status"], json.dumps(run, ensure_ascii=False), time.time()))
            self._event(db, run["id"], "run.created", run)
            return run, True

    def get(self, run_id: str):
        with self.connect() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row[0])

    def list(self, all_rows=False):
        with self.connect() as db:
            query = "SELECT data FROM runs WHERE deleted_at IS NULL ORDER BY updated DESC" + ("" if all_rows else " LIMIT 200")
            return [json.loads(r[0]) for r in db.execute(query)]

    def state(self):
        with self.connect() as db:
            db.execute("BEGIN")
            cursor = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            runs = [json.loads(r[0]) for r in db.execute("SELECT data FROM runs WHERE deleted_at IS NULL ORDER BY updated DESC LIMIT 200")]
            return {"runs": runs, "cursor": cursor}

    def update(self, run_id: str, **patch):
        with self.connect() as db:
            run = self.get(run_id)
            run.update(patch, updatedAt=time.time())
            db.execute("UPDATE runs SET status=?,data=?,updated=? WHERE id=?", (run["status"], json.dumps(run, ensure_ascii=False), time.time(), run_id))
            self._event(db, run_id, "run.updated", run)
        return run

    def remove(self, run_id):
        with self.connect() as db:
            run = self.get(run_id)
            if run["status"] not in {"completed", "failed", "cancelled"}:
                raise ValueError("请先停止任务，再删除工作记录。")
            if run.get("deletedAt"):
                return
            run["deletedAt"] = time.time()
            db.execute("UPDATE runs SET deleted_at=?,data=? WHERE id=?", (run["deletedAt"], json.dumps(run, ensure_ascii=False), run_id))
            self._event(db, run_id, "run.removed", {"id": run_id})

    def _event(self, db, run_id, kind, payload):
        at = time.time()
        cursor = db.execute("INSERT INTO events(run_id,type,payload,at) VALUES(?,?,?,?)", (run_id, kind, json.dumps(payload, ensure_ascii=False, default=str), at))
        return {"seq": cursor.lastrowid, "runId": run_id, "type": kind, "payload": payload, "at": at}

    def event(self, run_id: str | None, kind: str, payload: dict):
        with self.connect() as db:
            return self._event(db, run_id, kind, payload)

    def events(self, after=0, run_id=None, limit=500):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE seq>? AND (? IS NULL OR run_id=?) ORDER BY seq LIMIT ?", (after, run_id, run_id, limit)).fetchall()
        return [{"seq": r["seq"], "runId": r["run_id"], "type": r["type"], "payload": json.loads(r["payload"]), "at": r["at"]} for r in rows]

    def cognition_events(self, after=0):
        """Project through a stable event watermark without parsing tool stream deltas."""
        with self.connect() as db:
            db.execute('BEGIN')
            through=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            rows=db.execute("SELECT * FROM events WHERE seq>? AND seq<=? AND type IN ('run.created','run.updated','message.complete','mind.observation') ORDER BY seq",(after,through)).fetchall()
        return through,[{"seq":r["seq"],"runId":r["run_id"],"type":r["type"],
            "payload":json.loads(r["payload"]),"at":r["at"]} for r in rows]

    def call(self, call_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "args": json.loads(row["args"]), "result": json.loads(row["result"]) if row["result"] else None}

    def calls(self, run_id: str):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM calls WHERE run_id=? ORDER BY updated", (run_id,))]
        return [self.call(i) for i in ids]

    def put_call(self, call_id, run_id, name, args, status, result=None, approval=None, phase=None):
        with self.connect() as db:
            db.execute("INSERT INTO calls(id,run_id,name,args,status,result,approval,updated,phase) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,result=excluded.result,approval=COALESCE(excluded.approval,calls.approval),phase=COALESCE(excluded.phase,calls.phase),updated=excluded.updated", (call_id, run_id, name, json.dumps(args, ensure_ascii=False), status, json.dumps(result, ensure_ascii=False, default=str) if result is not None else None, approval, time.time(), phase))
            stored_phase = db.execute("SELECT phase FROM calls WHERE id=?", (call_id,)).fetchone()[0]
            self._event(db, run_id, "tool." + status, {"callId": call_id, "name": name, "args": args, "result": result, "assignmentId": stored_phase})

    def recover(self):
        with self.connect() as db:
            pending = [json.loads(r[0]) for r in db.execute("SELECT data FROM runs WHERE deleted_at IS NULL AND status NOT IN ('completed','failed','cancelled')")]
        for run in pending:
            if run["status"] not in TERMINAL:
                for call in self.calls(run["id"]):
                    if call["status"] in {"waiting_approval", "approved"}:
                        self.put_call(call["id"], run["id"], call["name"], call["args"], "expired", {"error": "审批会话已结束；操作未执行，需要执行者重新提交。"})
                uncertain = [c["id"] for c in self.calls(run["id"]) if c["status"] == "running"]
                discussions = run.get("discussions", [])
                for discussion in discussions:
                    if discussion["status"] in {"queued", "running"}:
                        discussion.update(status="interrupted", error="程序重启，讨论未完成。")
                self.update(run["id"], status="paused", error="程序已重启，请核验上次操作后继续。", uncertainCalls=uncertain, discussions=discussions)

    def remember(self, run_id, actor_id, content):
        with self.connect() as db:
            db.execute("INSERT INTO memory_fts VALUES(?,?,?)", (run_id, actor_id, content))

    def recall(self, text, actor_id, limit=5):
        words = [w for w in text.replace('"', '').split() if w][:8]
        if not words:
            return []
        query = " OR ".join('"' + w + '"' for w in words)
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT content FROM memory_fts WHERE memory_fts MATCH ? AND actor_id=? ORDER BY rank LIMIT ?", (query, actor_id, limit))]
