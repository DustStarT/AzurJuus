"""Event-driven character memory with actor-local observations and CAS reflection.

No model call is made inside a database transaction. Receipts and projections
commit together; outbox events may be replayed and carry stable identifiers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from sqlalchemy import select, update

from .database import session_scope
from .models import Actor, AgentRelationship
from .cognition_models import MindState, Experience, MindCursor, MindReceipt, MindOutbox

log = logging.getLogger(__name__)


def identity(*parts):
    return hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()


def empty_state():
    return {'focus': [], 'commitments': [], 'appraisals': [], 'mood': '尚无足够经历', 'socialAfter': 0}


class Cognition:
    def __init__(self, store):
        self.store = store
        self.enabled = os.getenv('AZURJUUS_COGNITION_ENABLED', '1') == '1'
        self.last_error = None
        self.latencies = []

    def initialize(self):
        # Existing events must not become invented psychological history.
        with session_scope() as session:
            if session.get(MindCursor, 'runtime') is None:
                session.add(MindCursor(id='runtime', seq=self.store.state()['cursor']))

    def state(self, session, actor_id):
        actor = session.get(Actor, actor_id)
        if actor is None or actor.kind != 'agent':
            raise ValueError('角色不存在。')
        state = session.get(MindState, actor_id)
        if state is None:
            state = MindState(actor_id=actor_id, version=0, enabled=True, data=empty_state())
            session.add(state)
            session.flush()
        return state

    def pump(self):
        if not self.enabled:
            return
        started = time.perf_counter()
        try:
            with session_scope() as session:
                cursor = session.get(MindCursor, 'runtime')
                if cursor is None:
                    session.add(MindCursor(id='runtime', seq=self.store.state()['cursor']))
                    return
                batch = self.store.events(cursor.seq, limit=100)
                for event in batch:
                    for item in self.observations(event):
                        aid, kind, text, metadata, key = item
                        receipt = identity(aid, key)
                        if session.get(MindReceipt, receipt):
                            continue
                        actor = session.get(Actor, aid)
                        if actor is None or actor.kind != 'agent':
                            continue
                        state = self.state(session, aid)
                        session.add(MindReceipt(id=receipt, source_seq=event['seq']))
                        if not state.enabled:
                            continue
                        eid = identity('experience', receipt)
                        session.add(Experience(id=eid, actor_id=aid, source_seq=event['seq'],
                            run_id=event.get('runId'), kind=kind, text=text[:2400], at=event['at'],
                            data={**metadata, 'reflection': 'pending' if kind in {'outcome', 'social'} or
                                kind == 'speech' and (metadata.get('ownSpeech') and any(w in text for w in ('我会', '我来', '答应', '我负责', '下次', '提醒', '疏漏', '不同意'))) else 'not_needed'}))
                        data = dict(state.data)
                        if kind == 'request':
                            data['focus'] = [*data.get('focus', []), {'text': text[:180], 'sourceId': eid, 'runId': event.get('runId')}][-6:]
                        if kind == 'outcome':
                            data['focus'] = [f for f in data.get('focus', []) if f.get('runId') != event.get('runId')]
                        state.data, state.version = data, state.version + 1
                        session.add(MindOutbox(id=receipt, payload={'eventId': receipt, 'actorId': aid,
                            'sourceSeq': event['seq'], 'sourceId': eid, 'runId': event.get('runId'), 'version': state.version}))
                        session.flush()
                    cursor.seq = event['seq']
            self.flush_outbox()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            log.exception('Cognitive projection failed; execution remains available')
        finally:
            self.latencies.append((time.perf_counter() - started) * 1000)
            self.latencies = self.latencies[-500:]

    def observations(self, event):
        payload, kind = event['payload'], event['type']
        if kind == 'mind.observation':
            for aid in payload.get('actorIds', []):
                metadata = dict(payload.get('data', {}))
                if metadata.get('speakerId'):
                    metadata['ownSpeech'] = aid == metadata['speakerId']
                metadata['peers'] = [peer for peer in metadata.get('peers', []) if peer != aid]
                yield aid, payload.get('kind', 'observation'), str(payload.get('text', '')), metadata, payload['key']
        elif kind == 'run.created':
            # On collaborative admission only the coordinator has observed the request.
            aid = payload['actorId']
            yield aid, 'request', payload['prompt'], {'scope': 'personal_observation'}, payload['id'] + ':request'
        elif kind == 'message.complete' and payload.get('text'):
            run = self.store.get(event['runId'])
            speaker = payload.get('actorId')
            participants = {run['actorId'], *(a['actorId'] for a in run.get('assignments', []))}
            # Group messages are public observations; personal thoughts never enter this stream.
            for aid in participants:
                yield aid, 'speech', payload['text'], {'scope': 'team_public' if run['collaborative'] else 'personal_observation',
                    'speakerId': speaker, 'ownSpeech': aid == speaker, 'peers': sorted(participants - {aid})}, payload['messageId']
        elif kind == 'run.updated' and payload.get('status') in {'completed', 'failed', 'cancelled'}:
            if payload.get('mode') == 'chat':
                return
            participants = {payload['actorId'], *(a['actorId'] for a in payload.get('assignments', []))}
            summary = (payload.get('result') or {}).get('summary') or payload.get('error') or '任务已取消'
            for aid in participants:
                yield aid, 'outcome', f"任务状态：{payload['status']}。{summary}", {
                    'scope': 'team_public', 'verified': payload['status'] == 'completed',
                    'peers': sorted(participants - {aid})}, payload['id'] + ':outcome:' + payload['status']

    def flush_outbox(self):
        with session_scope() as session:
            for row in session.scalars(select(MindOutbox).where(MindOutbox.sent.is_(False)).limit(100)):
                self.store.event(row.payload.get('runId'), 'mind.changed', row.payload)
                row.sent = True

    def experiences(self, actor_id, before=None, limit=30):
        with session_scope() as session:
            self.state(session, actor_id)
            query = select(Experience).where(Experience.actor_id == actor_id, Experience.forgotten.is_(False))
            if before is not None:
                query = query.where(Experience.source_seq < before)
            rows = session.scalars(query.order_by(Experience.source_seq.desc()).limit(min(max(limit, 1), 100))).all()
            return [{'id': r.id, 'sourceSeq': r.source_seq, 'runId': r.run_id, 'kind': r.kind,
                'text': r.text, 'at': r.at, 'data': r.data} for r in rows]

    def inspect(self, actor_id):
        with session_scope() as session:
            state = self.state(session, actor_id)
            actor = session.get(Actor, actor_id)
            return {'actorId': actor_id, 'version': state.version, 'enabled': state.enabled and self.enabled,
                'data': state.data, 'anchor': {'text': actor.system_prompt or actor.persona or '',
                    'source': actor.character_url, 'note': '导入设定；未经逐项核实的内容属于项目解释，非自动确认的原作事实'},
                'originalBackground':(actor.extra_json or {}).get('originalMindNotes',[]),
                'error': self.last_error}

    def relationships(self, actor_id):
        from .terminal_characters import world_entries
        backgrounds=world_entries()
        with session_scope() as session:
            state = self.state(session, actor_id)
            owner=session.get(Actor,actor_id)
            baseline = session.scalars(select(AgentRelationship).where(AgentRelationship.agent_id == actor_id)).all()
            result = []
            shared = session.scalars(select(Experience).where(Experience.actor_id == actor_id,
                Experience.forgotten.is_(False)).order_by(Experience.at.desc()).limit(100)).all()
            for row in baseline:
                peer = session.get(Actor, row.peer_agent_id)
                if not peer or not peer.is_active:continue
                evidence = [v for v in state.data.get('appraisals', []) if v.get('peerId') == row.peer_agent_id]
                encounters = [e for e in shared if row.peer_agent_id in e.data.get('peers', [])]
                outcomes = list({e.run_id: e for e in encounters if e.kind == 'outcome' and e.run_id}.values())
                from .character_identity import default_relationship
                default=default_relationship(owner,peer)
                familiarity = default['text'] if not encounters else '已有应用内交流，可承接共同话题。'
                if outcomes:
                    familiarity = f'有 {len(outcomes)} 次可追溯的共同任务经历，具体能力信任仍以领域证据为准'
                defined = state.data.get('relationshipNotes', {}).get(row.peer_agent_id, '')
                from .character_identity import source_names
                canonical=[e for e in backgrounds if e.get('from') in source_names(owner.source_character or owner.name)
                    and e.get('to') in source_names(peer.source_character or peer.name)]
                researched=(row.notes_json or {}).get('wiki')
                if researched:
                    canonical.append({**researched,'text':researched['description']})
                meaningful=[e for e in canonical if e.get('origin')!='user-world-setting']
                if defined:
                    summary=defined
                elif meaningful:
                    summary='；'.join(dict.fromkeys(e['text'].strip('。') for e in meaningful))+'。'
                elif encounters:
                    summary='已有实际交流，可承接共同话题。'
                elif default['familiarity']=='familiar':
                    summary=f'同属{owner.faction}，彼此认识且熟悉。'
                else:
                    summary='彼此认识，暂无更多直接交集。'
                if outcomes:summary+=f' 有 {len(outcomes)} 次可追溯的共同任务经历。'
                result.append({'peerId': row.peer_agent_id, 'name': peer.name if peer else row.peer_agent_id,
                    'baseline': {'affinity': row.affinity_score, 'trust': row.trust_score, 'confidence': 'low'},
                    'observations': evidence, 'familiarity': familiarity,
                    'userDefined': defined,
                    'defaultRelationship':default,
                    'background': meaningful,
                    'sharedSources': [e.id for e in encounters[:3]],
                    'summary':summary})
            return result

    def set_relationship(self, actor_id, peer_id, description):
        with session_scope() as session:
            state = self.state(session, actor_id)
            peer = session.get(Actor, peer_id)
            if not peer or peer.kind != 'agent' or peer_id == actor_id:
                raise ValueError('请选择另一位已存在的成员。')
            notes = dict(state.data.get('relationshipNotes', {}))
            if description.strip():
                notes[peer_id] = description.strip()[:400]
            else:
                notes.pop(peer_id, None)
            state.data = {**state.data, 'relationshipNotes': notes}
            state.version += 1
            session.add(MindOutbox(id=identity(actor_id, 'relationship', state.version),
                payload={'actorId':actor_id, 'peerId':peer_id, 'version':state.version, 'origin':'user'}))
        self.flush_outbox()
        return self.relationships(actor_id)

    def social_participation(self, actor_id, conversation_id, peer_id, visible_texts):
        """Actor-local, bounded social signals; never return private judgments.

        Only utterances still visible in this room can supply commitment text.
        Familiarity uses observed public encounters, not legacy affinity scores.
        """
        result = {'familiarity': 0, 'commitments': []}
        if not self.enabled:
            return result
        try:
            with session_scope() as session:
                state = session.get(MindState, actor_id)
                if not state or not state.enabled:
                    return result
                defined=state.data.get('relationshipNotes',{}).get(peer_id)
                if defined:
                    result['userDefinedRelationship']={'peerId':peer_id,'description':defined,'origin':'user'}
                from .character_identity import default_relationship
                owner,peer=session.get(Actor,actor_id),session.get(Actor,peer_id)
                if owner and peer and peer.kind=='agent':
                    result['defaultRelationship']=default_relationship(owner,peer)
                relationship = session.scalar(select(AgentRelationship).where(
                    AgentRelationship.agent_id == actor_id,
                    AgentRelationship.peer_agent_id == peer_id))
                researched = (relationship.notes_json or {}).get('wiki') if relationship else None
                if researched:
                    result['originalBackgroundInterpretation'] = {
                        'peerId': peer_id, 'description': researched['description'],
                        'source': researched['source'], 'origin': 'wiki-model-interpretation',
                        'boundary': '原作资料解释，不是应用内共同经历；用户设定优先。',
                    }
                rows = session.scalars(select(Experience).where(
                    Experience.actor_id == actor_id, Experience.forgotten.is_(False))
                    .order_by(Experience.at.desc()).limit(80)).all()
                public = {r.id: r for r in rows if
                    r.data.get('scope') == 'team_public' and
                    r.data.get('conversationId') == conversation_id and
                    r.data.get('reflection') != 'invalidated'}
                result['familiarity'] = min(2, sum(
                    r.data.get('speakerId') == peer_id and not r.data.get('ownSpeech')
                    for r in public.values()))
                for item in state.data.get('commitments', []):
                    row = public.get(item.get('sourceId'))
                    text = item.get('text', '')
                    if (row and row.data.get('ownSpeech') and text and text in row.text
                            and row.text in visible_texts and item.get('status', 'open') == 'open'):
                        if text not in result['commitments']:
                            result['commitments'].append(text)
                result['commitments'] = result['commitments'][-3:]
        except Exception:
            log.exception('Social cognition unavailable; participation uses public topic only')
        return result

    def context(self, actor_id, query='', peers=None, social=False):
        if not self.enabled:
            return ''
        try:
            state = self.inspect(actor_id)
            if not state['enabled']:
                return ''
            rows = self.experiences(actor_id, limit=100)
            # Never export task bodies into the social channel without explicit sharing.
            if social:
                rows = [r for r in rows if r['data'].get('shareable')]
            terms = set(re.findall(r'[\u4e00-\u9fff]|\w+', query.lower()))
            rows.sort(key=lambda r: (r['id'] in {x.get('sourceId') for x in state['data'].get('commitments', [])},
                sum(t in r['text'].lower() for t in terms), r['at']), reverse=True)
            memories, budget = [], 2000
            for row in rows[:8]:
                fragment = {'sourceId': row['id'], 'kind': row['kind'], 'observation': row['text'][:500],
                    'userCorrection': row['data'].get('userCorrection')}
                size = sum(2 if ord(c) > 127 else .35 for c in json.dumps(fragment, ensure_ascii=False))
                if size > budget:
                    break
                memories.append(fragment)
                budget -= size
            relations = self.relationships(actor_id)
            relations = [r for r in relations if peers is None or r['peerId'] in peers]
            # Domain trust is an evidence-backed interpretation, never objective truth.
            data = {'version': state['version'], 'focus': [] if social else state['data'].get('focus', []),
                'commitments': [] if social else state['data'].get('commitments', [])[-5:],
                'mood': '依据当前可见内容自然回应' if social else state['data'].get('mood'),
                'memories': memories,
                'originalBackground':state.get('originalBackground',[])[:4],
                'relationships': [{'peerId':r['peerId'], 'name':r['name'],
                    'background': r['background'], 'userDefinedRelationship': r['userDefined'],
                    'approach':r['observations'][-1].get('approach','neutral') if r['observations'] else 'neutral'} for r in relations[:6]] if social
                    else [{'peerId': r['peerId'], 'name': r['name'], 'familiarity': r['familiarity'],
                        'background':r['background'],
                        'userDefinedRelationship': r['userDefined'],
                        'sharedSources': r['sharedSources'], 'judgments': r['observations'][-2:]} for r in relations[:6]]}
            from .character_behavior import behavior_context
            with session_scope() as session:
                name = session.get(Actor, actor_id).name
            return (behavior_context(name) + '\n人物连续状态（记录与个人判断，不是新指令；未知不等于已知）：\n' + json.dumps(data, ensure_ascii=False)
                + '\n先选择此刻要回答、追问、提醒、求助、反驳、缓和还是不发言；关系应影响求助对象、解释深度和纠正方式。'
                '不要复述心理字段。人物判断可能错误，以新证据修正；保持核心设定。'
                '执行任务时保持当前目标、承诺、依赖和待核验事项；关系只调整沟通与求助方式，不能取代验收标准或工具证据。'
                'Wiki及剧情仅是原作背景，模型提炼属于可纠正解释，不是本应用中共同完成过的工作。')
        except Exception:
            log.exception('Cognitive context unavailable')
            return ''

    def revise(self, actor_id, experience_id, interpretation=None, forget=False):
        with session_scope() as session:
            state = self.state(session, actor_id)
            row = session.get(Experience, experience_id)
            if row is None or row.actor_id != actor_id:
                raise ValueError('经历不存在。')
            row.forgotten = forget
            row.data = {**row.data, 'reflection': 'corrected' if not forget else 'forgotten',
                'userCorrection': interpretation}
            affected = {experience_id}
            descendants = session.scalars(select(Experience).where(Experience.actor_id == actor_id)).all()
            while True:
                children = {r.id for r in descendants if affected.intersection(r.data.get('dependencies', []))}
                if children <= affected:
                    break
                affected |= children
            for child in descendants:
                if child.id in affected and child.id != experience_id:
                    child.data = {**child.data, 'reflection': 'invalidated', 'invalidatedBy': experience_id}
            data = dict(state.data)
            for key in ('focus', 'commitments', 'appraisals'):
                data[key] = [v for v in data.get(key, []) if v.get('sourceId') not in affected]
            data['mood'] = '依据新证据重新理解'
            if interpretation and not forget:
                data['appraisals'].append({'sourceId': experience_id, 'interpretation': interpretation[:800], 'confidence': 1, 'origin': 'user'})
            state.data, state.version = data, state.version + 1
            session.add(MindOutbox(id=identity(actor_id, 'revise', state.version), payload={'actorId': actor_id,
                'sourceId': experience_id, 'version': state.version, 'runId': row.run_id}))
        self.flush_outbox()

    def configure(self, actor_id, enabled=None, reset=False):
        with session_scope() as session:
            state = self.state(session, actor_id)
            if enabled is not None:
                state.enabled = enabled
            if reset:
                session.execute(update(Experience).where(Experience.actor_id == actor_id).values(forgotten=True))
                state.data = empty_state()
            state.version += 1
            session.add(MindOutbox(id=identity(actor_id, 'configure', state.version),
                payload={'actorId':actor_id, 'version':state.version, 'reset':reset}))
        self.flush_outbox()
        return self.inspect(actor_id)

    def reflection_candidate(self):
        with session_scope() as session:
            row = session.scalars(select(Experience).where(Experience.forgotten.is_(False)).order_by(Experience.at.desc()).limit(100)).all()
            for experience in row:
                if experience.data.get('reflection') != 'pending':
                    continue
                state = self.state(session, experience.actor_id)
                if not state.enabled:
                    continue
                return {'id': experience.id, 'actorId': experience.actor_id, 'version': state.version,
                    'text': experience.text, 'data': experience.data, 'state': state.data}

    def apply_reflection(self, candidate, result):
        if not isinstance(result, dict):
            raise ValueError('反思必须是对象。')
        judgments = result.get('judgments', [])
        commitments = result.get('commitments', [])
        if not isinstance(judgments, list) or not isinstance(commitments, list) or len(judgments) > 3 or len(commitments) > 3:
            raise ValueError('反思超出条目限制。')
        with session_scope() as session:
            row = session.get(Experience, candidate['id'])
            state = self.state(session, candidate['actorId'])
            if not row or row.forgotten or not state.enabled or row.data.get('reflection') != 'pending':
                return False
            if state.version != candidate['version']:
                # A new observation wins; a later job may reflect on a fresh snapshot.
                return False
            data = dict(state.data)
            additions = []
            for judgment in judgments:
                if not isinstance(judgment, dict) or judgment.get('peerId') not in row.data.get('peers', []):
                    continue
                text = str(judgment.get('interpretation', ''))[:400]
                if text:
                    additions.append({'sourceId': row.id, 'peerId': judgment['peerId'], 'interpretation': text,
                        'domain': str(judgment.get('domain', '协作'))[:40],
                        'approach': judgment.get('approach') if judgment.get('approach') in {'seek_help','verify','neutral'} else 'neutral',
                        'confidence': min(.7, max(0, float(judgment.get('confidence', .3))))})
            data['appraisals'] = [*data.get('appraisals', []), *additions][-30:]
            # Only own utterances can create commitments, with a verbatim source quote.
            if row.data.get('ownSpeech'):
                for item in commitments:
                    if isinstance(item, str) and item.strip() and item in row.text:
                        data['commitments'] = [*data.get('commitments', []), {'text': item[:200], 'sourceId': row.id}][-12:]
            data['mood'] = str(result.get('mood', data.get('mood', '平静')))[:80]
            changed = session.execute(update(MindState).where(MindState.actor_id == state.actor_id,
                MindState.version == candidate['version']).values(data=data, version=candidate['version'] + 1))
            if changed.rowcount != 1:
                return False
            dependencies = {v.get('sourceId') for key in ('focus', 'commitments', 'appraisals')
                for v in candidate.get('state', {}).get(key, []) if v.get('sourceId') and v.get('sourceId') != row.id}
            row.data = {**row.data, 'reflection': 'done', 'dependencies': sorted(dependencies)}
            session.add(MindOutbox(id=identity(row.id, 'reflection'), payload={'actorId': state.actor_id,
                'sourceId': row.id, 'sourceSeq': row.source_seq, 'version': candidate['version'] + 1, 'runId': row.run_id}))
        self.flush_outbox()
        return True

    async def reflect_once(self, generate, is_busy):
        if not self.enabled or is_busy():
            return
        candidate = self.reflection_candidate()
        if not candidate:
            return
        from .idle_social import _generate, SocialPreempted
        for attempt in range(2):
            try:
                async with asyncio.timeout(15):
                    result = await _generate(generate(candidate), is_busy)
                self.apply_reflection(candidate, result)
                return
            except (asyncio.CancelledError, SocialPreempted):
                raise
            except Exception as exc:
                if attempt:
                    with session_scope() as session:
                        row = session.get(Experience, candidate['id'])
                        if row and row.data.get('reflection') == 'pending':
                            row.data = {**row.data, 'reflection': 'failed', 'error': type(exc).__name__}
                    log.warning('Reflection failed after retry: %s', type(exc).__name__)
