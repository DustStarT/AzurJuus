"""Unified context with live business permissions; no derived index grants access."""
import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from threading import RLock
from uuid import uuid4

from sqlalchemy import select
from backend.database import session_scope
from backend.models import Actor, Conversation, ConversationMember, MemoryChunk, WorkspaceSetting
from backend.mind.cognition_models import Experience
from backend.context.models import (ContextSource, ContextGrant, ContextJob, ContextTombstone,
                                    ContextReference, ContextTrace, ContextSetting)
from backend.context.contracts import ContextRequest, ContextBundle
from backend.context.embedding import Embeddings
from backend.context.retrieval import RetrievalService, terms

log = logging.getLogger(__name__)
DEFAULTS = {'provider': 'local', 'localModelPath': '', 'baseUrl': '', 'model': '',
            'dimensions': 384, 'modelVersion': '', 'apiKey': '',
            'chatTokenizerPath': '', 'chatTokenizerModel': '',
            'contextLimit': 64000}
TARGETS = {'compact': 4000, 'balanced': 8000, 'rich': 12000}
DOCUMENT_QUERY = re.compile(r'pdf|docx?|xlsx?|这(?:本|篇|份|个|部|张)?(?:书|文档|文件|报告|资料|表格|工作表)|那(?:本|份|个|张)(?:书|文件|文档|表格)|读过|看过', re.I)


def stable(*parts):
    return hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()


def estimate(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Explicitly an estimate, never reported as provider usage.
    return int(sum(1.5 if ord(c) > 127 else .3 for c in text)) + 1


def basename(path):
    return PurePosixPath(str(path).replace('\\', '/')).name


class ContextService:
    def __init__(self, root, store, settings_loader):
        self.root, self.store, self.settings_loader = Path(root), store, settings_loader
        self.embedding = Embeddings(root, self.settings)
        self.retrieval = RetrievalService(self.root / 'retrieval.db', self.embedding)
        self.lock = RLock()
        self.worker = None
        self.foreground_busy = lambda: False
        self.backfill_complete = False
        self.last_error = ''
        self._seen_calls = set()
        self._wiki_mtimes = {}
        self._sync_version = None
        self._chat_tokenizer = None
        self._chat_tokenizer_signature = None

    def settings(self):
        with session_scope() as s:
            row = s.get(ContextSetting, 1)
            return {**DEFAULTS, **(row.data if row else {})}

    def configure(self, patch):
        from backend.credentials import protect
        cfg = self.settings()
        cfg.update({k: v for k, v in patch.items() if k in DEFAULTS and k != 'apiKey'})
        if patch.get('apiKey'):
            cfg['apiKey'] = protect(patch['apiKey'])
        if patch.get('clearApiKey'):
            cfg['apiKey'] = ''
        if cfg['provider'] not in {'local', 'lexical', 'cloud'}:
            raise ValueError('检索模式无效')
        if not 1 <= int(cfg['dimensions']) <= 4096 or not 4096 <= int(cfg['contextLimit']) <= 2000000:
            raise ValueError('维度或上下文上限无效')
        if cfg['provider'] == 'cloud':
            from urllib.parse import urlparse
            url = urlparse(cfg['baseUrl'])
            if not cfg['model'] or not cfg['apiKey'] or url.scheme not in {'http', 'https'} or not url.hostname:
                raise ValueError('云端 embedding 需要独立的接口、模型和密钥')
        with session_scope() as s:
            row = s.get(ContextSetting, 1)
            if row:
                row.data = cfg
            else:
                s.add(ContextSetting(id=1, data=cfg))
        self.embedding.model = None
        self.embedding.tokenizer = None
        self._tokenized = False
        return self.public_settings()

    def public_settings(self):
        cfg = self.settings()
        return {k: v for k, v in cfg.items() if k != 'apiKey'} | {'apiKeyConfigured': bool(cfg['apiKey'])}

    def register(self, sid, kind, title, text, data, *, actor_id='', conversation_id='', grant_data=None, at=None):
        with self.lock:
            return self._register(sid, kind, title, text, data, actor_id=actor_id,
                conversation_id=conversation_id, grant_data=grant_data, at=at)

    def _register(self, sid, kind, title, text, data, *, actor_id='', conversation_id='', grant_data=None, at=None):
        if not text.strip():
            return None
        version = stable(text, json.dumps(data, sort_keys=True, ensure_ascii=False, default=str))
        changed = False
        with session_scope() as s:
            if s.get(ContextTombstone, sid) or s.get(ContextTombstone, 'all:' + sid):
                return None
            row = s.get(ContextSource, sid)
            if not row:
                row = ContextSource(id=sid, kind=kind, title=title, text=text,
                    version=version, data=data, at=at or time.time(), active=True)
                s.add(row)
                changed = True
            elif row.version != version:
                row.version, row.text, row.data, row.title = version, text, data, title
                changed = True
            if actor_id:
                gid = stable(sid, actor_id, conversation_id)
                if not s.get(ContextGrant, gid) and not s.get(ContextTombstone, gid):
                    s.add(ContextGrant(id=gid, source_id=sid, actor_id=actor_id,
                        conversation_id=conversation_id, active=True, data=grant_data or {}))
            if changed:
                jid = stable(sid, version)
                if not s.get(ContextJob, jid):
                    s.add(ContextJob(id=jid, status='pending', data={'sourceId': sid, 'version': version}))
        if changed:
            # Lexical fallback is ready before the vector worker or model download.
            self.retrieval.index(sid, version, text)
        return sid

    def ingest_read(self, run, actor_id, call):
        if call.get('status') != 'completed' or call.get('name') not in {'read_file', 'read_document'}:
            return []
        args, result = call.get('args') or {}, call.get('result') or {}
        if not isinstance(result, dict) or result.get('error'):
            return []
        path = str(args.get('path') or result.get('path') or '')
        if not path:
            return []
        parts = []
        if 'content' in result:
            parts.append((str(result['content']), {'lineStart': int(args.get('offset') or 0) + 1,
                'columnStart': int(args.get('column') or 0), 'nextOffset': result.get('nextOffset'),
                'nextColumn': result.get('nextColumn'), 'totalLines': result.get('totalLines')}))
        for item in result.get('items', []):
            text = item.get('text')
            if text is None:
                text = json.dumps(item.get('rows', item.get('cells', [])), ensure_ascii=False, default=str)
            parts.append((str(text), {k: v for k, v in item.items() if k not in {'text', 'rows', 'cells', 'lines'}}))
        if not parts:
            parts.append(('成功读取调用，历史正文未保留，不能据此评价正文。',
                {'offset': args.get('offset'), 'limit': args.get('limit'), 'bodyUnavailable': True}))
        ids = []
        for ordinal, (text, location) in enumerate(parts):
            sid = stable('read', call['id'], ordinal)
            data = {'runId': run['id'], 'callId': call['id'], 'path': path,
                'name': basename(path), 'fileHash': result.get('sha256'), 'location': location,
                'readerId': actor_id, 'provenance': 'personal_read', 'partial': True,
                'totalUnits': result.get('total', result.get('totalLines')),
                'unitOffset': int(args.get('offset') or 0) + ordinal,
                'wholeReturn': bool(int(args.get('offset') or 0) == 0 and not args.get('column') and
                    result.get('nextOffset') is None and not result.get('nextColumn') and
                    not location.get('needsOCR') and not location.get('bodyUnavailable') and
                    (call['name'] == 'read_file' or result.get('total') == 1) and
                    (result.get('total') is not None or result.get('totalLines') is not None)),
                'tool': call['name'], 'sourceVersion': result.get('sha256') or call['id'],
                'taskStatus': run.get('status'), 'conversationId': run.get('conversationId', ''),
                'workspace': run.get('workspace', '')}
            data['partial'] = not data['wholeReturn']
            if self.register(sid, 'document', basename(path), text, data, actor_id=actor_id,
                    conversation_id=run.get('conversationId', ''),
                    grant_data={'reader': True, 'callId': call['id']}, at=run.get('updatedAt')):
                ids.append(sid)
        self._seen_calls.add(call['id'])
        return ids

    def sync(self):
        """Only business records and successful returned tool data; never diagnostic logs."""
        with self.lock:
            from sqlalchemy import func
            with session_scope() as s:
                exp_version = tuple(s.execute(select(func.count(Experience.id), func.max(Experience.at))).one())
                mem_version = tuple(s.execute(select(func.count(MemoryChunk.id), func.max(MemoryChunk.created_at))).one())
            with self.store.connect() as db:
                event_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            # Changes to forget/grant state are checked live in visible(), not cached here.
            version = (exp_version, mem_version, event_seq, int(time.monotonic() // 5))
            if version == self._sync_version:
                return
            with session_scope() as s:
                experiences = [(r.id, r.actor_id, r.kind, r.text, r.run_id, r.at, dict(r.data))
                    for r in s.scalars(select(Experience).where(Experience.forgotten.is_(False))).all()
                    if r.data.get('reflection') != 'invalidated']
                memories = [(m.id, m.actor_id, m.summary, m.conversation_id, dict(m.metadata_json or {}))
                    for m in s.scalars(select(MemoryChunk).where(MemoryChunk.source_kind == 'verified_task')).all()]
            for eid, aid, kind, text, run_id, at, data in experiences:
                corrected = data.get('reflection') == 'corrected'
                if corrected:
                    if not data.get('userCorrection'):
                        continue
                    text, kind, run_id = str(data['userCorrection']), 'correction', None
                # Each actor projection retains its own grant and experience validity.
                event = data.get('eventId') or eid
                sid = stable('event', event, text)
                self.register(sid, 'correction' if corrected else 'experience', kind, text,
                    {'eventId': event, 'runId': run_id, 'provenance': 'simulation' if kind == 'simulation' else 'observation',
                     'kind': kind, 'mode': data.get('mode'), 'verified': bool(data.get('verified')),
                     'correction': corrected},
                    actor_id=aid, conversation_id=data.get('conversationId', ''), at=at,
                    grant_data={'experienceId': eid, 'shareable': bool(data.get('shareable')),
                        'speakerId': data.get('speakerId'), 'ownSpeech': data.get('ownSpeech'),
                        'sourceKind': data.get('sourceKind', kind)})
            for mid, aid, text, cid, data in memories:
                self.register(stable('memory', mid), 'task', '已验证任务',
                    data.get('taskEvidence', {}).get('summary') or text,
                    {'memoryId': mid, 'runId': data.get('runId'), 'verified': True, 'provenance': 'task_result'},
                    actor_id=aid, conversation_id=cid or '', grant_data={'memoryId': mid})
            for aid, data in [(e[1], e[6]) for e in experiences] + [(m[1], m[4]) for m in memories]:
                for evidence in data.get('readSources', []):
                    call = self.store.call(str(evidence.get('callId') or ''))
                    if call and call['id'] not in self._seen_calls and call.get('status') == 'completed':
                        try:
                            original = self.store.get(call['run_id'])
                        except KeyError:
                            continue
                        owners = {'main': original.get('actorId'), 'planner': original.get('actorId'),
                                  'reviewer': original.get('actorId')}
                        owners.update({a['id']: a['actorId'] for a in original.get('assignments', []) if a.get('id') and a.get('actorId')})
                        if owners.get(call.get('phase')) == aid:
                            self.ingest_read(original, aid, call)
            for run in self.store.list(all_rows=True):
                if run.get('mode') == 'chat':
                    continue
                owners = {'main': run.get('actorId'), 'planner': run.get('actorId'), 'reviewer': run.get('actorId')}
                owners.update({a['id']: a['actorId'] for a in run.get('assignments', []) if a.get('id') and a.get('actorId')})
                for call in self.store.calls(run['id']):
                    aid = owners.get(call.get('phase'))
                    if aid and call['id'] not in self._seen_calls:
                        self.ingest_read(run, aid, call)
                if run.get('status') == 'completed' and (run.get('result') or {}).get('summary'):
                    for aid in set(owners.values()) - {None}:
                        summaries = [a.get('result', {}).get('summary', '') for a in run.get('assignments', [])
                            if a['actorId'] == aid and a.get('result')]
                        if aid == run.get('actorId'):
                            summaries.append(run['result']['summary'])
                        if summaries:
                            self.register(stable('run-outcome', run['id'], aid), 'task', '已验收任务',
                                '\n'.join(dict.fromkeys(summaries)), {'runId': run['id'], 'verified': True,
                                    'provenance': 'task_result'}, actor_id=aid,
                                conversation_id=run.get('conversationId', ''), at=run.get('updatedAt'))
            folder = self.root / 'wiki-source-cache'
            for path in folder.glob('*.json'):
                mtime = path.stat().st_mtime_ns
                if self._wiki_mtimes.get(path) == mtime:
                    continue
                try:
                    page = json.loads(path.read_text(encoding='utf-8'))
                    if page.get('url') and isinstance(page.get('text'), str):
                        self.register(stable('wiki', page['url'], stable(page['text'])), 'wiki',
                            page.get('title') or page['url'], page['text'],
                            {'url': page['url'], 'provenance': 'original_background',
                             'fetchedAt': page.get('_fetchedAt'), 'stale': page.get('_stale', False),
                             'hash': stable(page['text'])}, at=page.get('_fetchedAt'))
                    self._wiki_mtimes[path] = mtime
                except (ValueError, OSError):
                    log.warning('Skipping invalid Wiki cache: %s', path.name)
            self.backfill_complete = True
            self._sync_version = version

    def visible(self, request):
        with session_scope() as s:
            actor = s.get(Actor, request.actor_id)
            if not actor or not actor.is_active:
                return {}
            members = s.scalars(select(ConversationMember).where(
                ConversationMember.actor_id == request.actor_id, ConversationMember.is_active.is_(True))).all()
            rooms = {m.conversation_id: s.get(Conversation, m.conversation_id) for m in members}
            if request.conversation_id and request.conversation_id not in rooms:
                return {}
            current = rooms.get(request.conversation_id)
            public = request.public or current is not None and current.kind != 'dm'
            tombstones = {r.id for r in s.scalars(select(ContextTombstone)).all()}
            blocked_runs = {r.run_id for r in s.scalars(select(Experience).where(
                Experience.actor_id == request.actor_id)).all() if r.run_id and
                (r.forgotten or r.data.get('reflection') in {'corrected', 'invalidated'})}
            grants = s.scalars(select(ContextGrant).where(ContextGrant.actor_id == request.actor_id,
                ContextGrant.active.is_(True))).all()
            result = {}
            for grant in grants:
                if grant.id in tombstones or grant.source_id in tombstones:
                    continue
                row = s.get(ContextSource, grant.source_id)
                if not row or not row.active or row.data.get('runId') in blocked_runs:
                    continue
                origin = rooms.get(grant.conversation_id)
                if grant.conversation_id:
                    known_origin = origin or s.get(Conversation, grant.conversation_id)
                    if known_origin and (not origin or (known_origin.extra_json or {}).get('archived')):
                        continue
                    if not known_origin and public:
                        continue
                if public and not (request.conversation_id and grant.conversation_id == request.conversation_id or grant.data.get('shareable')):
                    continue
                if origin:
                    cfg = (origin.extra_json or {}).get('social', {})
                    joined = cfg.get('joinedAt', {}).get(request.actor_id)
                    if joined and max(row.at, grant.data.get('sharedAt', 0)) < joined:
                        continue
                    if row.data.get('eventId') in cfg.get('joinBoundaryIds', {}).get(request.actor_id, []):
                        continue
                if eid := grant.data.get('experienceId'):
                    exp = s.get(Experience, eid)
                    if (not exp or exp.forgotten or exp.data.get('reflection') == 'invalidated' or
                            exp.data.get('reflection') == 'corrected' and row.kind != 'correction'):
                        continue
                if mid := grant.data.get('memoryId'):
                    if not s.get(MemoryChunk, mid):
                        continue
                result[row.id] = self.source_payload(row) | {'grant': {**grant.data,
                    'reader': bool(grant.data.get('reader') or row.data.get('readerId') == request.actor_id)}}
            for row in s.scalars(select(ContextSource).where(ContextSource.kind == 'wiki', ContextSource.active.is_(True))).all():
                if row.id not in tombstones:
                    result[row.id] = self.source_payload(row) | {'grant': {'reader': False}}
            return result

    @staticmethod
    def source_payload(row):
        data = dict(row.data)
        if row.kind == 'wiki':
            fetched = data.get('fetchedAt')
            data['stale'] = bool(data.get('stale') or not isinstance(fetched, (int, float))
                or time.time() - fetched > 7 * 86400)
            data['freshness'] = 'unknown' if not isinstance(fetched, (int, float)) else 'stale' if data['stale'] else 'cached'
        return {'sourceId': row.id, 'version': row.version, 'kind': row.kind, 'title': row.title,
                'text': row.text, 'at': row.at, 'data': data}

    def reference(self, request, allowed, history=()):
        docs = {k: v for k, v in allowed.items() if v['kind'] == 'document'}
        query = request.query
        # Match complete stored names first, including spaces and Unicode.
        named = {k: v for k, v in docs.items() if v['title'].lower() in query.lower() or
            len(Path(v['title']).stem) >= 3 and Path(v['title']).stem.lower() in query.lower() and
            not re.search(r'\.(?:pdf|docx?|xlsx?|txt|csv|md|epub)(?![A-Za-z0-9])', query, re.I)}
        explicit_file = bool(re.search(r'\.(?:pdf|docx?|xlsx?|txt|csv|md|epub)(?![A-Za-z0-9])', query, re.I))
        if explicit_file and not named:
            return {'unresolved': True, 'readSources': [], 'summary': '', 'verified': False}
        chosen = named
        reference_key = stable(request.actor_id, request.conversation_id)
        if not chosen and (DOCUMENT_QUERY.search(query) or re.search(r'它|里面|其中|内容', query)):
            with session_scope() as s:
                ref = s.get(ContextReference, reference_key)
                ids = ref.data.get('sources', {}) if ref else {}
            chosen = {k: docs[k] for k, version in ids.items() if k in docs and docs[k]['version'] == version}
            if not chosen:
                for item in reversed(history):
                    text = str(item.get('content') or item.get('text') or '')
                    matched = {k: v for k, v in docs.items() if v['title'].lower() in text.lower()}
                    if matched:
                        chosen = matched
                        break
            if not chosen:
                chosen = docs
        if not chosen:
            return None
        # Different workspaces with the same filename remain ambiguous.
        identities = {(v['data'].get('workspace'), v['data']['path']) for v in chosen.values()}
        if len(identities) > 1:
            return {'ambiguous': True, 'candidateNames': sorted({v['data']['path'] for v in chosen.values()})[:8]}
        # Latest observed version wins, keeping all actually read pieces of that version.
        latest = max(chosen.values(), key=lambda v: v['at'])
        chosen = {k: v for k, v in chosen.items() if v['data'].get('sourceVersion') == latest['data'].get('sourceVersion')}
        if named or request.source_id and DOCUMENT_QUERY.search(query):
            message_id = request.source_id
            try:
                run = self.store.get(request.run_id or request.source_id.split(':', 1)[0])
                message_id = run.get('userMessageId') or message_id
            except KeyError:
                pass
            with session_scope() as s:
                ref = s.get(ContextReference, reference_key)
                data = {'messageId': message_id, 'sources': {k: v['version'] for k, v in chosen.items()}}
                if ref:
                    ref.data = data
                else:
                    s.add(ContextReference(id=reference_key, actor_id=request.actor_id,
                        conversation_id=request.conversation_id, data=data))
        sources = [{'sourceId': k, 'version': v['version'], **v['data'],
                    'name': v['title'], 'ownRead': bool(v['grant'].get('reader'))} for k, v in chosen.items()]
        run_id = latest['data'].get('runId')
        outcomes = [v['text'] for v in allowed.values() if v['data'].get('runId') == run_id
                    and v['kind'] in {'task', 'experience'} and v['data'].get('verified')]
        totals = {v['data'].get('totalUnits') for v in chosen.values() if v['data'].get('totalUnits') is not None}
        units = {v['data'].get('unitOffset') for v in chosen.values() if not v['data']['location'].get('needsOCR')}
        complete = (any(v['data'].get('wholeReturn') for v in chosen.values()) or
            len(totals) == 1 and bool(totals) and totals.pop() == len(units) and min(units, default=-1) == 0)
        return {'runId': run_id, 'actorId': request.actor_id, 'verified': bool(outcomes),
            'summary': '\n'.join(dict.fromkeys(outcomes)), 'readSources': sources,
            'sourceIds': list(chosen), 'coversAllReturnedUnits': bool(complete),
            'readScope': ('成功读取返回覆盖该历史版本全部页段的可提取正文；不保证图片、图表或理解深度。' if complete else
                '仅确认列出的实际返回片段；索引与片段不能证明通读，内容为读取时的历史版本。')}

    def collect(self, request, history=None, persona=None):
        started = time.monotonic()
        self.sync()
        allowed = self.visible(request)
        reference = self.reference(request, allowed, history or [])
        pinned = list((reference or {}).get('sourceIds', []))
        # An explicit document cannot be replaced with a semantically similar one.
        candidates = allowed
        from backend.chat.conversation_scope import asks_about_work
        modes = {}
        def eligible(source):
            rid = source['data'].get('runId')
            prior = request.include_prior_task_history or asks_about_work(request.query) or bool(reference)
            if request.run_id and request.mode not in {'chat', 'social', 'moments', 'life-share'}:
                if source['data'].get('kind') in {'request', 'speech', 'simulation', 'outcome'} and rid != request.run_id:
                    return prior and bool(source['data'].get('verified'))
            if not rid:
                return True
            if rid not in modes:
                try:
                    modes[rid] = self.store.get(rid).get('mode')
                except KeyError:
                    modes[rid] = source['data'].get('mode')
            is_task = modes[rid] != 'chat'
            if not is_task:
                return True
            if request.run_id and request.mode == 'task':
                return rid == request.run_id or prior and (source['kind'] in {'document', 'task'} or source['data'].get('verified'))
            if not request.include_task_history:
                return source['kind'] == 'document' or prior and (source['kind'] == 'task' or source['data'].get('verified'))
            return True
        candidates = {k: v for k, v in candidates.items() if eligible(v)}
        if reference and (reference.get('unresolved') or reference.get('ambiguous')):
            candidates = {k: v for k, v in candidates.items() if v['kind'] not in {'document', 'task'}}
        elif pinned:
            run_ids = {allowed[k]['data'].get('runId') for k in pinned}
            candidates = {k: v for k, v in candidates.items() if v['kind'] != 'document' or k in pinned}
            candidates = {k: v for k, v in candidates.items() if v['kind'] != 'task' or v['data'].get('runId') in run_ids}
        hits = self.retrieval.search(request.query, candidates, pinned)
        required = [v for v in candidates.values() if request.run_id and
            request.mode not in {'chat', 'social', 'moments', 'life-share'} and
            v['data'].get('runId') == request.run_id and v['data'].get('kind') in {'request', 'speech'}]
        required = sorted(required, key=lambda v: (v['data'].get('kind') == 'request', v['at']), reverse=True)[:4]
        if not reference and (request.include_prior_task_history or asks_about_work(request.query)):
            outcomes = [v for v in candidates.values() if v['kind'] == 'task' and v['data'].get('verified')]
            if outcomes:
                required.append(max(outcomes, key=lambda v: v['at']))
        for value in required:
            pinned.append(value['sourceId'])
            hits.insert(0, {'source': value['sourceId'], 'text': value['text'], 'start': 0,
                'stop': len(value['text']), 'reasons': ['current_task_request' if value['data'].get('kind') == 'request' else 'explicit_task_followup']})
        if pinned:
            related_runs = {allowed[k]['data'].get('runId') for k in pinned}
            results = [v for v in candidates.values() if v['kind'] == 'task'
                and v['data'].get('verified') and v['data'].get('runId') in related_runs]
            if results:
                result = max(results, key=lambda v: len(v['text']))
                pinned.append(result['sourceId'])
                hits.insert(0, {'source': result['sourceId'], 'text': result['text'], 'start': 0,
                    'stop': len(result['text']), 'reasons': ['confirmed_reference_result']})
        if not request.query.strip():
            hits = [{'source': k, 'text': v['text'], 'start': 0, 'stop': len(v['text']),
                'reasons': ['recent_visible']} for k, v in sorted(candidates.items(),
                key=lambda item: item[1]['at'], reverse=True)[:12] if v['kind'] in {'experience', 'correction', 'task'}]
        bundle = ContextBundle(request=request, persona=persona or {}, reference=reference)
        target = TARGETS.get(request.preset, 8000)
        mandatory = estimate(request.query) + estimate(bundle.persona) + estimate(bundle.rules)
        used = mandatory
        omitted, seen, selected = [], set(), []
        uncertain = []
        # Current message and exact evidence are mandatory and may exceed the soft target.
        for hit in hits:
            source = allowed[hit['source']]
            if hit.get('needsClarification') and hit['source'] not in pinned:
                uncertain.append({'sourceId': source['sourceId'], 'title': source['title'],
                    'similarity': hit.get('similarity')})
                omitted.append({'sourceId': source['sourceId'], 'reason': 'low_semantic_confidence'})
                continue
            identity = stable(source['data'].get('eventId') or source['data'].get('callId') or source['sourceId'], hit['text'])
            result_run = source['data'].get('runId') if source['kind'] == 'task' or source['data'].get('kind') == 'outcome' else None
            if result_run and 'result:' + result_run in seen:
                continue
            if identity in seen:
                continue
            fragment = {'sourceId': source['sourceId'], 'version': source['version'], 'kind': source['kind'],
                'title': source['title'], 'text': hit['text'], 'at': source['at'], **source['data'],
                'ownRead': bool(source['grant'].get('reader')), 'span': [hit['start'], hit['stop']],
                'experienceId': source['grant'].get('experienceId'),
                'speakerId': source['grant'].get('speakerId'), 'ownSpeech': source['grant'].get('ownSpeech'),
                'reasons': hit['reasons']}
            cost = estimate(fragment)
            if used + cost > target * .65 and hit['source'] not in pinned:
                omitted.append({'sourceId': hit['source'], 'reason': 'budget'})
                continue
            seen.add(identity)
            if result_run:
                seen.add('result:' + result_run)
            used += cost
            selected.append(fragment)
        # Merge overlapping spans without duplicating their contents.
        merged = []
        for fragment in sorted(selected, key=lambda v: (v['sourceId'], v['span'][0])):
            prev = merged[-1] if merged else None
            if prev and prev['sourceId'] == fragment['sourceId'] and fragment['span'][0] <= prev['span'][1]:
                extra = max(0, prev['span'][1] - fragment['span'][0])
                prev['text'] += fragment['text'][extra:]
                prev['span'][1] = max(prev['span'][1], fragment['span'][1])
            else:
                merged.append(fragment)
        bundle.documents = [v for v in merged if v['kind'] in {'document', 'wiki'}]
        bundle.experiences = [v for v in merged if v['kind'] not in {'document', 'wiki'}]
        if uncertain and not merged and not reference:
            bundle.reference = {'ambiguous': True,
                'candidateNames': list(dict.fromkeys(v['title'] for v in uncertain))[:5],
                'reason': '仅有低置信度语义候选，请先确认来源。'}
        selected_texts = [v['text'] for v in merged]
        bundle.history = self.select_history(request.query, history or [], max(0, target - used), selected_texts)
        bundle.open_questions = topic_ledger(bundle.history)['openQuestions']
        bundle.allowed_sources = list(dict.fromkeys([request.source_id, *(v['sourceId'] for v in merged),
            *(v['experienceId'] for v in merged if v.get('experienceId'))]))
        bundle.trace_id = uuid4().hex
        bundle.budget = {'targetInputTokens': target, 'estimatedInputTokens': estimate({
            'query': request.query, 'persona': bundle.persona, 'sources': merged, 'history': bundle.history}),
            'tokenCountKind': 'estimate', 'actualUsage': None}
        with session_scope() as s:
            s.add(ContextTrace(id=bundle.trace_id, actor_id=request.actor_id,
                conversation_id=request.conversation_id, at=time.time(), data={
                    'preset': request.preset, 'mode': request.mode, 'sourceId': request.source_id,
                    'selected': [{'sourceId': v['sourceId'], 'version': v['version'], 'reasons': v['reasons']} for v in merged],
                    'omitted': omitted, 'retrievalMs': round((time.monotonic() - started) * 1000, 2), **bundle.budget}))
        return bundle

    @staticmethod
    def select_history(query, history, budget, source_texts=()):
        tokens = set(terms(query))
        ranked = []
        for i, item in enumerate(history):
            text = str(item.get('content') or item.get('text') or '')
            if any(text.strip() and text.strip() == t.strip() for t in source_texts):
                continue
            score = len(tokens & set(terms(text))) * 3 + (i + 1) / max(1, len(history))
            ranked.append((i, score, item))
        selected, used = [], 0
        for i, _, item in sorted(ranked, key=lambda v: v[1], reverse=True):
            cost = estimate(item)
            # Preserve the immediate conversational handoff even above a soft target.
            if used + cost <= budget or i >= len(history) - 2:
                selected.append((i, item)); used += cost
        return [v for _, v in sorted(selected)]

    def pack_messages(self, bundle, messages, output_tokens=2400):
        """Final wire-size accounting; mandatory messages are never silently truncated."""
        target = TARGETS.get(bundle.request.preset, 8000)
        # Remove optional history before allowing mandatory instructions/evidence
        # to expand the soft target. Keep the immediate handoff and open requests.
        protected = {v.get('id') for v in bundle.open_questions}
        optional = []
        for i, item in enumerate(bundle.history[:-2]):
            if item.get('id') and item.get('id') in protected:
                continue
            text = str(item.get('content') or item.get('text') or '')
            indexes = [j for j, msg in enumerate(messages[:-1]) if isinstance(msg.get('content'), str)
                and text and msg['content'].endswith(text)]
            relevance = len(set(terms(bundle.request.query)) & set(terms(text)))
            optional.extend((relevance, i, j) for j in indexes)
        removed = set()
        for _, _, index in sorted(optional):
            if self.count_tokens([m for j, m in enumerate(messages) if j not in removed])[0] <= target:
                break
            removed.add(index)
        messages = [m for j, m in enumerate(messages) if j not in removed]
        total, count_kind = self.count_tokens(messages)
        limit = int(self.settings()['contextLimit'])
        if total + output_tokens > limit:
            raise ValueError('必要上下文超过配置的模型上限，请缩小问题范围或提高正确的模型上下文上限。')
        with session_scope() as s:
            row = s.get(ContextTrace, bundle.trace_id)
            if row:
                row.data = {**row.data, 'estimatedInputTokens': total, 'outputReserve': output_tokens,
                            'tokenCountKind': count_kind,
                            'omittedHistoryItems': len(removed),
                            'softTargetExceeded': total > row.data['targetInputTokens']}
        return messages

    def record_call_input(self, trace_id, messages, stage, output_tokens):
        with session_scope() as s:
            trace = s.get(ContextTrace, trace_id)
            target = trace.data.get('targetInputTokens', 8000) if trace else 8000
        # Cognition receives a structured host transcript. Trim only that optional
        # history, never the current utterance, rules, persona or document facts.
        if stage in {'understand', 'decide', 'compact'} and messages:
            try:
                value = json.loads(messages[-1]['content'])
                snap = value.get('snapshot') or value
                contextual = json.loads(snap['current']['text'])
                host_chat = 'respondingActorId' in contextual and 'intent' in contextual
                host_group = 'actorId' in contextual and isinstance(contextual.get('focus'), dict)
                history = contextual.get('history') if host_chat else (contextual.get('room') or {}).get('history') if host_group else None
                if isinstance(history, list):
                    while len(history) > 2 and self.count_tokens(messages)[0] > target:
                        question_ids = {q.get('id') for q in topic_ledger(history)['openQuestions']}
                        removable = [i for i, h in enumerate(history[:-2]) if not h.get('id') or h.get('id') not in question_ids]
                        if not removable:
                            break
                        from backend.chat.conversation_scope import current_dialogue_text
                        query_terms = set(terms(current_dialogue_text(contextual)))
                        index = min(removable, key=lambda i: len(query_terms & set(terms(
                            history[i].get('text') or history[i].get('content') or ''))))
                        history.pop(index)
                        snap['current']['text'] = json.dumps(contextual, ensure_ascii=False)
                        messages[-1] = {**messages[-1], 'content': json.dumps(value, ensure_ascii=False)}
            except (ValueError, TypeError, KeyError):
                pass  # Direct, unstructured current utterances are mandatory data.
        total, count_kind = self.count_tokens(messages)
        if total + output_tokens > int(self.settings()['contextLimit']):
            raise ValueError('必要认知上下文超过配置的模型上限，请缩小问题范围。')
        with session_scope() as s:
            row = s.get(ContextTrace, trace_id)
            if row:
                row.data = {**row.data, 'calls': [*row.data.get('calls', []), {
                    'stage': stage, 'estimatedInputTokens': total, 'tokenCountKind': count_kind,
                    'outputReserve': output_tokens, 'at': time.time()}],
                    'softTargetExceeded': total > row.data['targetInputTokens']}

    def process_jobs(self):
        """Retry lexical materialization independently of successful task calls."""
        with session_scope() as s:
            jobs = [(r.id, dict(r.data)) for r in s.scalars(select(ContextJob).where(
                ContextJob.status == 'pending').limit(16)).all()]
        for jid, data in jobs:
            with session_scope() as s:
                row = s.get(ContextSource, data['sourceId'])
                source = self.source_payload(row) if row and row.active and row.version == data['version'] else None
            if source:
                with self.retrieval.db() as db:
                    exists = db.execute('SELECT 1 FROM chunks WHERE source=? AND version=? LIMIT 1',
                        (source['sourceId'], source['version'])).fetchone()
                if not exists:
                    self.retrieval.index(source['sourceId'], source['version'], source['text'])
            with session_scope() as s:
                row = s.get(ContextJob, jid)
                if row:
                    row.status = 'lexical' if source else 'obsolete'

    def count_tokens(self, value):
        cfg = self.settings()
        current_model = self.settings_loader().get('llmModel')
        path = cfg.get('chatTokenizerPath')
        signature = (path, cfg.get('chatTokenizerModel'), current_model)
        if path and cfg.get('chatTokenizerModel') == current_model:
            try:
                if self._chat_tokenizer_signature != signature:
                    from tokenizers import Tokenizer
                    self._chat_tokenizer = Tokenizer.from_file(path)
                    self._chat_tokenizer.no_truncation()
                    self._chat_tokenizer_signature = signature
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                return len(self._chat_tokenizer.encode(text).ids), 'local-tokenizer-excluding-provider-overhead'
            except Exception as exc:
                log.debug('Matching chat tokenizer unavailable: %s', type(exc).__name__)
        return estimate(value), 'estimate'

    def revoke(self, source_id, actor_id=None):
        with session_scope() as s:
            rows = s.scalars(select(ContextGrant).where(ContextGrant.source_id == source_id)).all()
            if actor_id is None:
                if not s.get(ContextTombstone, source_id):
                    s.add(ContextTombstone(id=source_id, data={'reason': 'forgotten', 'at': time.time()}))
                source = s.get(ContextSource, source_id)
                if source:
                    source.active = False
            for row in rows:
                if actor_id is None or row.actor_id == actor_id:
                    row.active = False
                    if not s.get(ContextTombstone, row.id):
                        s.add(ContextTombstone(id=row.id, data={'reason': 'revoked'}))

    def validate_trace(self, trace_id):
        """An in-flight answer cannot outlive revocation or a source correction."""
        if not trace_id:
            return
        with session_scope() as s:
            row = s.get(ContextTrace, trace_id)
            if not row:
                raise ValueError('上下文记录已失效，请重新生成回复。')
            request = ContextRequest(row.actor_id, row.conversation_id)
            selected = list(row.data.get('selected', []))
        allowed = self.visible(request)
        if any(v['sourceId'] not in allowed or allowed[v['sourceId']]['version'] != v['version'] for v in selected):
            raise ValueError('回复所用来源的权限或版本已改变，请依据当前可见资料重新生成。')

    def clear_memory(self):
        self.sync()
        with session_scope() as s:
            ids = [r.id for r in s.scalars(select(ContextSource).where(ContextSource.kind != 'wiki')).all()]
        for sid in ids:
            self.revoke(sid)
        self.retrieval.purge(self.active_source_ids())

    def active_source_ids(self):
        with session_scope() as s:
            return set(s.scalars(select(ContextSource.id).where(ContextSource.active.is_(True))).all())

    def prune(self):
        """Purge evidence with no remaining live readers; business tombstones win rebuilds."""
        with session_scope() as s:
            grants = s.scalars(select(ContextGrant).where(ContextGrant.active.is_(True))).all()
            for grant in grants:
                invalid = False
                if eid := grant.data.get('experienceId'):
                    exp = s.get(Experience, eid)
                    source = s.get(ContextSource, grant.source_id)
                    invalid = (not exp or exp.forgotten or exp.data.get('reflection') == 'invalidated' or
                        exp.data.get('reflection') == 'corrected' and (not source or source.kind != 'correction'))
                if mid := grant.data.get('memoryId'):
                    invalid = invalid or not s.get(MemoryChunk, mid)
                row = s.get(ContextSource, grant.source_id)
                if row and row.data.get('runId'):
                    invalid = invalid or any(e.forgotten or e.data.get('reflection') in {'corrected', 'invalidated'}
                        for e in s.scalars(select(Experience).where(Experience.actor_id == grant.actor_id,
                            Experience.run_id == row.data['runId'])).all())
                if invalid:
                    grant.active = False
                    if not s.get(ContextTombstone, grant.id):
                        s.add(ContextTombstone(id=grant.id, data={'reason': 'invalidated'}))
            s.flush()
            live = set(s.scalars(select(ContextGrant.source_id).where(ContextGrant.active.is_(True))).all())
            for row in s.scalars(select(ContextSource).where(ContextSource.active.is_(True), ContextSource.kind != 'wiki')).all():
                if row.id not in live:
                    row.active = False
                    if not s.get(ContextTombstone, row.id):
                        s.add(ContextTombstone(id=row.id, data={'reason': 'no_live_evidence'}))
        self.retrieval.purge(self.active_source_ids())

    def reindex(self):
        self.sync()
        self.prune()
        with session_scope() as s:
            rows = [self.source_payload(r) for r in s.scalars(select(ContextSource).where(ContextSource.active.is_(True))).all()
                    if not s.get(ContextTombstone, r.id)]
        for row in rows:
            self.retrieval.index(row['sourceId'], row['version'], row['text'])
        self.retrieval.purge({r['sourceId'] for r in rows})
        return {'status': 'queued', 'sources': len(rows)}

    def status(self):
        with session_scope() as s:
            sources = len(s.scalars(select(ContextSource.id).where(ContextSource.active.is_(True))).all())
            pending = len(s.scalars(select(ContextJob.id).where(ContextJob.status == 'pending')).all())
        cfg = self.settings()
        return {'sources': sources, 'pendingJobs': pending, **self.retrieval.status(),
                'backfillComplete': self.backfill_complete, 'provider': cfg['provider'],
                'localModelReady': self.embedding.model is not None,
                'degraded': cfg['provider'] == 'local' and self.embedding.model is None,
                'message': self.last_error or ('仅词法检索' if cfg['provider'] == 'lexical' else
                    self.embedding.error if cfg['provider'] == 'local' else '') or '混合检索已就绪'}

    async def start(self):
        if self.worker:
            return
        async def work():
            loaded = None
            while True:
                try:
                    if not self.foreground_busy():
                        cfg = self.settings()
                        signature = (cfg['provider'], cfg['localModelPath'])
                        if loaded != signature and cfg['provider'] == 'local':
                            await self.threaded(self.embedding.load)
                            loaded = signature
                        await self.threaded(self.sync)
                        await self.threaded(self.prune)
                        await self.threaded(self.process_jobs)
                        if self.embedding.tokenizer and not getattr(self, '_tokenized', False):
                            await self.threaded(self.reindex)
                            self._tokenized = True
                        more = await self.threaded(self.retrieval.vector_batch)
                        if not more:
                            with session_scope() as s:
                                for job in s.scalars(select(ContextJob).where(ContextJob.status.in_(['pending', 'lexical']))).all():
                                    job.status = 'lexical' if cfg['provider'] == 'lexical' or self.embedding.model is None and cfg['provider'] != 'cloud' else 'completed'
                        self.last_error = ''
                    await asyncio.sleep(.2 if not self.foreground_busy() and locals().get('more') else 2)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = '后台索引降级：' + type(exc).__name__
                    log.exception('Context index worker failed')
                    await asyncio.sleep(5)
        self.worker = asyncio.create_task(work(), name='context-index')

    @staticmethod
    async def threaded(function, *args):
        # Cancellation cannot terminate a native SQLite/ONNX thread. Join it
        # before the app closes or another workspace installs its session factory.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    @asynccontextmanager
    async def suspended(self):
        await self.close()
        try:
            yield
        finally:
            self._sync_version = None
            self._seen_calls.clear()
            self._wiki_mtimes.clear()
            self.retrieval.purge(self.active_source_ids())
            await self.start()


def topic_ledger(history):
    """Extract only recorded utterances and reply links, without another LLM call."""
    from backend.social.conversation_policy import solicits_reply
    answered = {h.get('replyTo') for h in history if h.get('replyTo')}
    questions, seen, viewpoints = [], set(), []
    boundary = max((i for i, h in enumerate(history) if h.get('speakerId') == 'commander'), default=0)
    for i, h in enumerate(history):
        text = str(h.get('text') or h.get('content') or '')
        normalized = re.sub(r'\W+', '', text.lower())
        mid = h.get('id')
        targets = h.get('mentions', [])
        later = history[i + 1:]
        # One member's replyTo must not discharge another named member's request.
        pending_targets = [t for t in targets if not any(v.get('speakerId') == t for v in later)]
        unresolved = bool(pending_targets) if targets else mid not in answered
        if (i >= boundary and (solicits_reply(text) or targets and h.get('speakerId') == 'commander') and unresolved and
                (normalized not in seen or h.get('speakerId') == 'commander')):
            questions.append({'id': mid, 'text': text, 'targets': pending_targets, 'speakerId': h.get('speakerId')})
        seen.add(normalized)
        if text:
            viewpoints.append({'id': mid, 'speakerId': h.get('speakerId'), 'text': text})
    return {'viewpoints': viewpoints, 'openQuestions': questions, 'replyTargets': sorted(answered - {None})}
