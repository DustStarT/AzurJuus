"""Link natural follow-ups to an actor's verified work, without importing task chat into small talk."""
from __future__ import annotations

import re
from datetime import UTC
from pathlib import PurePath

from sqlalchemy import select

from backend.database import session_scope
from backend.models import MemoryChunk
from backend.mind.cognition_models import Experience

from backend.chat.conversation_scope import asks_about_work

READ_TOOLS = {'read_file', 'read_document'}
DEICTIC = re.compile(r'这(?:本|篇|份|个|部|些)?(?:书|电子书|文档|文件|报告|资料)|那(?:本|篇|份|个|部)?(?:书|文档|文件|报告)|刚才(?:看|读|查)|你(?:看|读)过')
FOLLOWUP = re.compile(r'(?:它|里面|其中|内容|作者).{0,16}(?:写|讲|说|怎么样|如何|评价|觉得)|(?:你觉得|你怎么看).{0,12}(?:它|里面|其中)')
FILE_NAME = re.compile(r'[\w\u4e00-\u9fff.-]+\.(?:pdf|epub|txt|docx?|md)(?![A-Za-z0-9])',re.I)


def read_sources(store, run: dict, actor_id: str) -> list[dict]:
    """Only successful calls by this actor prove that the actor read a source."""
    owners = {'planner': run.get('actorId'), 'reviewer': run.get('actorId'),
              'main': run.get('actorId')}
    owners.update({item['id']: item['actorId'] for item in run.get('assignments', [])})
    result = []
    for call in store.calls(run['id']):
        if call['name'] not in READ_TOOLS or call['status'] != 'completed':
            continue
        if owners.get(call.get('phase')) != actor_id:
            continue
        path = str((call.get('args') or {}).get('path') or '').strip()
        if not path:
            continue
        evidence = call.get('result') or {}
        result.append({'path': path, 'name': PurePath(path.replace('\\', '/')).name,
                       'callId': call['id'], 'tool': call['name'],
                       'offset': (call.get('args') or {}).get('offset'),
                       'limit': (call.get('args') or {}).get('limit'),
                       'truncated': evidence.get('truncated') if isinstance(evidence, dict) else None})
    return result


def resolve_task_reference(store, actor_id: str, query: str, history: list | None = None,
                           *, force: bool = False) -> dict | None:
    """A result may be mentioned in any joined room, but never attributed to a peer."""
    query = str(query or '').strip()
    if not query:
        return None
    context = ' '.join(str(item.get('text') or item.get('content') or '')
        for item in (history or [])[-4:]) if FOLLOWUP.search(query) else ''
    anchored = bool(context and (DEICTIC.search(context) or FILE_NAME.search(context)))
    explicit = force or asks_about_work(query) or bool(DEICTIC.search(query)) or anchored
    if not explicit and not FILE_NAME.search(query):
        return None
    runs = [run for run in store.list(all_rows=True) if run.get('mode') != 'chat'
            and run.get('status') == 'completed' and run.get('result')
            and actor_id in {run.get('actorId'), *(a.get('actorId') for a in run.get('assignments', []))}]
    # Read evidence is actor-specific. The durable business memory also covers
    # records removed from the work drawer; a forgotten outcome suppresses both.
    with session_scope() as session:
        memories = session.scalars(select(MemoryChunk).where(MemoryChunk.actor_id == actor_id,
            MemoryChunk.source_kind == 'verified_task').order_by(MemoryChunk.created_at.desc()).limit(100)).all()
        forgotten = {row.run_id for row in session.scalars(select(Experience).where(
            Experience.actor_id == actor_id, Experience.kind == 'outcome')).all()
            if row.run_id and (row.forgotten or row.data.get('reflection') in {'corrected','invalidated'})}
        saved = [{'runId':m.metadata_json.get('runId'), 'summary':m.metadata_json.get('taskEvidence',{}).get('summary') or m.summary,
            'sources':m.metadata_json.get('readSources',[]),
            'at':m.created_at.replace(tzinfo=m.created_at.tzinfo or UTC).timestamp()
                if m.created_at else 0} for m in memories]
    candidates = []
    for run in runs:
        if run['id'] not in forgotten:
            candidates.append({'runId':run['id'], 'summary':str((run.get('result') or {}).get('summary') or ''),
                'sources':read_sources(store, run, actor_id), 'at':run.get('updatedAt',0)})
    known = {item['runId'] for item in candidates}
    candidates.extend(item for item in saved if item['runId'] and item['runId'] not in known
        and item['runId'] not in forgotten)
    matches = []
    reference = query + ' ' + context if anchored else query
    requested_names = FILE_NAME.findall(query)
    for item in candidates:
        sources = item['sources']
        names = [source.get('name','') for source in sources]
        named = [name for name in names if name and (name.lower() in reference.lower()
            or len(name.rsplit('.',1)[0]) >= 3 and name.rsplit('.',1)[0].lower() in reference.lower())]
        if not explicit and not named:
            continue
        if requested_names and not named:
            continue
        if DEICTIC.search(query) and not sources:
            continue
        source_hint = bool(DEICTIC.search(query) or FILE_NAME.search(query))
        matches.append(((10 if named else 0) + (2 if sources and source_hint else 0),
            item['at'], item, named))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    score, _, selected, named = matches[0]
    sources = selected['sources']
    # A deictic reference with several recent books is ambiguous even if the
    # older read happened more than a minute earlier.
    if (DEICTIC.search(query) or anchored or force) and not named:
        alternatives = [item for item in matches if item[0] == score
            and abs(item[1] - matches[0][1]) < 7 * 86400]
        source_names = {source.get('name','') for item in alternatives for source in item[2]['sources']}
        source_names.discard('')
        if len(source_names) > 1:
            return {'ambiguous': True, 'candidateNames': sorted(source_names)[:5]}
    summary = str(selected['summary'] or '')
    if not summary and not sources:
        return None
    return {'runId': selected['runId'], 'verified': True, 'actorId': actor_id,
            'summary': summary[:3200], 'readSources': sources[:12],
            'readScope': '只确认列出的成功读取调用；不能据此声称通读整本书。'}
