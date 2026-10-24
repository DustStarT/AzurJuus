"""Event-driven group decisions. Model calls never hold a database transaction."""
import asyncio
import hashlib
import json
import os
import re
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from sqlalchemy import select, update
from backend.database import session_scope
from backend.models import Actor, Conversation, ConversationMember, Message
from backend.social.social_models import SocialTopic, SocialAction, SocialDeferredReply
from backend.characters.terminal_characters import TERMINAL, context, background, lore, worldbook
from backend.chat.expression import validate
from backend.social.idle_social import _generate, SocialPreempted
from backend.social.social_activity import SocialActivity
from backend.chat.message_order import message_order
from backend.social.conversation_policy import closing_context, closing_violation, repeats_own_speech


def stable(*parts):
    return 'social-' + hashlib.sha256(':'.join(parts).encode()).hexdigest()[:32]


SOCIAL_ACTIONS={'speak','wait','end','invite','create_group'}
USER_WAIT=re.compile(r'(?:等|待|候|没回|回复|有空|方便).{0,15}指挥官|'
    r'指挥官.{0,20}(?:回复|回来|有空|方便|决定|没回|没答)')

def normalized_social_action(value):
    """Accept the model's unambiguous JSON variants; still validate effects."""
    if not isinstance(value,dict):return value
    if isinstance(value.get('action'),str) and value['action'] in SOCIAL_ACTIONS:return value
    raw=value.get('type')
    if isinstance(raw,str) and raw in SOCIAL_ACTIONS:return {**value,'action':raw}
    if isinstance(value.get('segments'),list) and value['segments']:
        return {**value,'action':'speak'}
    if value.get('targetId') and value.get('reason') and value.get('summary'):
        return {**value,'action':'create_group' if value.get('title') else 'invite'}
    return value


class SocialEngine:
    def __init__(self, coordinator, service):
        self.c, self.service = coordinator, service
        # The previous default silently selected the fixed-speaker fallback in the desktop launcher.
        # Preserve an explicit off switch for rollback.
        self.enabled = os.getenv('AZURJUUS_SOCIAL_ENGINE_ENABLED', '1') == '1'
        self.gate = asyncio.Semaphore(1)
        self.generate = coordinator.expression.complete
        self.activity = SocialActivity(self)
        self.background = ContextVar('social_background', default=None)
        self.inflight = {}

    def actor_busy(self, actor_id):
        return actor_id in self.c.busy_actor_ids()

    def dialogue_preset(self):
        value=str(self.c.settings_loader().get('dialoguePreset') or 'balanced')
        return value if value in {'compact','balanced','rich'} else 'balanced'

    def dialogue_history_limit(self):
        return {'compact': 6, 'balanced': 12, 'rich': 18}[self.dialogue_preset()]

    def topic_limit(self, topic):
        """Return a safety budget; novelty checks usually end topics earlier."""
        limit=max(1, int(topic.get('maxSpoken', 6) or 6))
        if not topic.get('proactive'):
            return limit
        return min(limit, {'compact':4,'balanced':6,'rich':8}[self.dialogue_preset()])

    @staticmethod
    def topic_novelty(text, previous):
        """Estimate whether a reply adds content to the current topic."""
        import re
        current=set(re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9_]+', str(text or '').lower()))
        if not current or not previous:
            return 1.0
        from difflib import SequenceMatcher
        best_overlap=0.0
        best_similarity=0.0
        for item in previous:
            terms=set(re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9_]+', str(item or '').lower()))
            if terms:
                best_overlap=max(best_overlap, len(current & terms)/max(1,len(current)))
            best_similarity=max(best_similarity, SequenceMatcher(None,
                re.sub(r'\W+','',text or ''), re.sub(r'\W+','',item or '')).ratio())
        if best_overlap >= .60 and best_similarity >= .55:
            return 0.0
        return max(0.0, 1.0-best_overlap)

    def preempt_actor(self, actor_id):
        for task in list(self.inflight.get(actor_id, ())):
            if not task.done():task.cancel()

    async def track(self, actor_id, awaitable):
        task=asyncio.create_task(awaitable)
        self.inflight.setdefault(actor_id,set()).add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():raise
            raise SocialPreempted()
        finally:
            self.inflight[actor_id].discard(task)

    async def call(self, messages, topic):
        from backend.platform.local_clock import with_time_context
        messages=with_time_context(messages)
        background_busy = self.background.get()
        def stale():
            current = self.topic(topic['id'])
            return (current['status']!='active' or current['version']!=topic['version']
                    or self.actor_busy(topic.get('_actorId',''))
                    or bool(background_busy and background_busy()))
        if stale(): raise SocialPreempted()
        runtime=getattr(self.c.cognition,'runtime',None)
        if runtime and runtime.enabled:
            return await _generate(runtime.call(topic.get('_actorId',''), 'express',
                {'contextTraceId': topic.get('_contextTraceId', '')},
                background=bool(background_busy),revision=runtime.revision,messages=messages,generator=self.generate),stale)
        if background_busy and not self.activity.reserve(): raise SocialPreempted()
        return await _generate(asyncio.wait_for(self.generate(messages,self.c.settings_loader()),15),stale)

    def snapshot(self, cid, actor_id=None):
        with session_scope() as session:
            room = session.get(Conversation, cid)
            if not room or (room.extra_json or {}).get('archived'):
                raise ValueError('频道不存在或已归档。')
            config = (room.extra_json or {}).get('social', {})
            members = session.scalars(select(ConversationMember).where(ConversationMember.conversation_id==cid,
                ConversationMember.is_active.is_(True))).all()
            ids = {m.actor_id for m in members}
            if actor_id and actor_id not in ids:
                raise ValueError('人物未加入频道。')
            from backend.characters.character_identity import roster_actors
            actors = roster_actors(session)
            roster = [{'id':a.id,'name':a.name,'sourceName':a.source_character or a.name,'faction':a.faction} for a in actors]
            messages = session.scalars(select(Message).where(Message.conversation_id==cid).order_by(
                *message_order(session,newest_first=True)).limit(40)).all()[::-1]
            joined = config.get('joinedAt', {}).get(actor_id) if actor_id else None
            excluded = set(config.get('joinBoundaryIds', {}).get(actor_id, []))
            allowed_ids={'commander',*(a['id'] for a in roster)}
            visible = [m for m in messages if m.speaker_id in allowed_ids and (not joined or m.created_at.replace(tzinfo=UTC).timestamp() >= joined) and m.id not in excluded]
            from backend.chat.conversation_scope import asks_about_work
            latest_user=next((m for m in reversed(visible) if m.speaker_id=='commander'),None)
            if not latest_user or not asks_about_work(latest_user.body):
                visible=[m for m in visible if m.type not in {'task','task_progress','task_notice'}
                    and not (m.metadata_json or {}).get('guidance')]
            # Never expose another member's invitation summary or admission metadata.
            public_config = {key: config.get(key, default) for key, default in [('muted', False), ('allowInvites', True)]}
            return {'id':cid,'kind':room.kind,'title':room.title,'config':public_config,
                'members':[a for a in roster if a['id'] in ids], 'directory':roster,
                'history':[{'id':m.id,'speakerId':m.speaker_id,'speakerName':next((a['name'] for a in roster if a['id']==m.speaker_id),'指挥官'),'text':m.body,'at':m.created_at.replace(tzinfo=m.created_at.tzinfo or UTC).timestamp(),
                    'createdAt':m.created_at.replace(tzinfo=m.created_at.tzinfo or UTC).astimezone().isoformat(timespec='seconds'),
                    'replyTo':(m.metadata_json or {}).get('replyTo'),'mentions':(m.metadata_json or {}).get('mentions',[]),'topicId':(m.metadata_json or {}).get('topicId')} for m in visible],
                'invitationSummary':config.get('summaries',{}).get(actor_id,''),
                'limitedHistory': bool(joined)}

    def begin(self, run):
        cid, tid = run['conversationId'], stable(run['id'],'topic')
        closing = closing_context([], {'prompt': run['prompt']}, [a['id'] for a in run.get('actors', [])])
        with session_scope() as session:
            if session.get(SocialTopic, tid):
                return tid
            session.execute(update(SocialTopic).where(SocialTopic.conversation_id==cid,SocialTopic.status=='active')
                .values(status='superseded',version=SocialTopic.version+1))
            session.add(SocialTopic(id=tid,conversation_id=cid,version=0,status='active',
                data={'runId':run['id'],'prompt':run['prompt'],'spoken':0,'invites':0,'recent':[],
                    'routeKind':run.get('routeKind',''), 'attachments':run.get('attachments',[]),
                    **({'maxSpoken': max(6, len(closing['pendingTargets']))}
                        if closing['closing'] and closing['pendingTargets'] else {})}))
        return tid

    def interrupt(self, cid):
        with session_scope() as session:
            session.execute(update(SocialTopic).where(SocialTopic.conversation_id==cid,SocialTopic.status=='active')
                .values(status='superseded',version=SocialTopic.version+1))

    def recover(self):
        """Deliver saved notifications, never re-enact an offline conversation."""
        self.activity.initialize()
        with session_scope() as session:
            session.execute(update(SocialTopic).where(SocialTopic.status=='active')
                .values(status='interrupted',version=SocialTopic.version+1))
            for action in session.scalars(select(SocialAction).where(SocialAction.status=='pending')).all():
                action.status='expired'
                action.data={**action.data,'notified':False}
            session.execute(update(SocialDeferredReply).where(SocialDeferredReply.status=='pending')
                .values(status='expired'))
        self.flush()

    def defer_busy_mentions(self, room, topic):
        latest=room['history'][-1] if room['history'] else None
        if not latest or latest['speakerId']!='commander':return
        busy=self.c.busy_actor_ids()
        with session_scope() as session:
            for actor in room['members']:
                if actor['id'] not in busy or not (actor['id'] in latest.get('mentions',[])
                        or '@'+actor['name'] in latest['text']):continue
                rid=stable('deferred',actor['id'],latest['id'])
                if session.get(SocialDeferredReply,rid):continue
                session.add(SocialDeferredReply(id=rid,conversation_id=room['id'],actor_id=actor['id'],
                    source_message_id=latest['id'],status='pending',data={'topicId':topic['id']}))
                self.c.store.event(topic.get('runId'),'social.reply_deferred',{'actorId':actor['id'],
                    'conversationId':room['id'],'sourceMessageId':latest['id']})

    async def resume_deferred(self):
        if self.c.shutting_down:return
        with session_scope() as session:
            pending=session.scalars(select(SocialDeferredReply).where(SocialDeferredReply.status=='pending')).all()
            rows=[{'id':r.id,'actorId':r.actor_id,'conversationId':r.conversation_id,
                'sourceMessageId':r.source_message_id} for r in pending]
        for item in rows:
            if self.actor_busy(item['actorId']):continue
            try:room=self.snapshot(item['conversationId'],item['actorId'])
            except ValueError:room=None
            history=room['history'] if room else []
            latest_user=next((m for m in reversed(history) if m['speakerId']=='commander'),None)
            source_index=next((i for i,m in enumerate(history) if m['id']==item['sourceMessageId']),-1)
            newer=history[source_index+1:] if source_index>=0 else []
            # Other members may answer the original question, but an unrelated
            # turn ends the pending reply. A task finishing must not revive it.
            same_topic=bool(source_index>=0 and latest_user and latest_user['id']==item['sourceMessageId']
                and all(m['speakerId']!='commander' and (
                    m.get('replyTo')==item['sourceMessageId'] or
                    m.get('topicId')==history[source_index].get('topicId') and m.get('topicId'))
                    for m in newer))
            relevant=same_topic
            with session_scope() as session:
                row=session.get(SocialDeferredReply,item['id'])
                if not row or row.status!='pending':continue
                row.status='ready' if relevant else 'expired'
            if not relevant:continue
            tid=stable(item['id'],'resume')
            with session_scope() as session:
                if not session.get(SocialTopic,tid):
                    session.add(SocialTopic(id=tid,conversation_id=item['conversationId'],version=0,status='active',
                        data={'runId':None,'prompt':latest_user['text'],'spoken':0,'invites':0,
                            'recent':[],'deferredActorId':item['actorId'],'maxSpoken':1}))
            try:await self.exchange(tid)
            finally:
                with session_scope() as session:
                    row=session.get(SocialDeferredReply,item['id'])
                    if row:row.status='completed' if row.status=='ready' else row.status

    def topic(self, tid):
        with session_scope() as session:
            t=session.get(SocialTopic,tid)
            return {'id':t.id,'conversationId':t.conversation_id,'version':t.version,'status':t.status,**t.data}

    @staticmethod
    def topic_room(room, topic):
        """An autonomous exchange starts at its own opening, not an old user turn.

        This narrows attention; it does not change channel visibility or history.
        """
        opening=topic.get('openingMessageId') if topic.get('proactive') else None
        if not opening:
            from backend.chat.conversation_scope import dialogue_history, asks_about_work
            from backend.chat.task_reference import DEICTIC, FOLLOWUP
            prompt=topic.get('prompt','')
            return {**room,'history':dialogue_history(room['history'],prompt,
                preserve_context=bool(topic.get('routeKind')=='followup' or asks_about_work(prompt)
                    or DEICTIC.search(prompt) or FOLLOWUP.search(prompt)))}
        start=next((i for i,m in enumerate(room['history']) if m['id']==opening),None)
        return {**room,'history':room['history'][start:]} if start is not None else room

    def candidates(self, room, topic):
        last=room['history'][-1] if room['history'] else {'text':topic['prompt'],'speakerId':'commander'}
        recent=topic.get('recent',[])
        book=worldbook()
        interests={a['name']:a['topics'] for a in book['identities']}
        last_actor=next((a for a in room['members'] if a['id']==last['speakerId']),{})
        last_name=last_actor.get('sourceName',last_actor.get('name'))
        signals={a['id']:self.participation(a, room) for a in room['members'] if a['id']!=last['speakerId']}
        from backend.context.service import topic_ledger
        pending_targets = {target for question in topic_ledger(room['history'])['openQuestions'] for target in question['targets']}
        closing = closing_context(room['history'], topic, [a['id'] for a in room['members']])
        if closing['closing']:
            pending_targets.update(closing['pendingTargets'])
        from backend.chat.task_reference import resolve_task_reference
        evidence = ({a['id']: resolve_task_reference(self.c.store, a['id'], last['text'], room['history'][-6:],
            conversation_id=room.get('id', ''), public=True)
            for a in room['members'] if a['id'] != last['speakerId']}
            if last['speakerId'] == 'commander' else {})
        # Stable within a turn, varied across topics; retries retain attribution.
        import random
        lottery=random.Random(topic['id']+':'+str(topic.get('spoken',0)))
        jitter={a['id']:lottery.random() for a in sorted(room['members'],key=lambda a:a['id'])}
        def score(a):
            explicit=a['id'] in pending_targets or a['id'] in last.get('mentions',[]) or a['name'] in last['text'] or a.get('sourceName',a['name']) in last['text']
            name=a.get('sourceName',a['name'])
            # After the first reply, current speech and explicit mentions drive
            # participation. Re-ranking by the old user prompt makes everyone
            # revisit it even when the group has moved on.
            current=last['text'] if last['speakerId']=='commander' or topic.get('spoken') else topic['prompt']
            interest=sum(word in current for word in interests.get(name,[]))
            # Canonical familiarity influences willingness, not tool trust.
            known=any(r['from']==name and r['to']==last_name for r in book['relationships'])
            signal=signals[a['id']]
            continuity=signal['familiarity']+int(bool(signal['commitments']))
            own_evidence = evidence.get(a['id'])
            return (int(explicit), interest*2+int(known)+continuity+
                (5 if own_evidence and not own_evidence.get('ambiguous') else 0)
                -3*recent.count(a['id'])+jitter[a['id']], -recent.count(a['id']))
        def can_reenter(actor):
            # A peer mentioning someone is not an invitation to restart that
            # person's monologue.  A second turn is reserved for an explicit
            # pending question or a fresh user-directed mention; this stops
            # alternating "附和 -> 再附和" loops while keeping direct requests.
            # Autonomous and legacy callers retain their existing safety budget;
            # the stricter handoff applies to a user chat turn where this loop
            # was visible in the transcript.
            if topic.get('routeKind') != 'chat':
                return True
            if recent.count(actor['id']) < 1:
                return True
            if actor['id'] in pending_targets:
                return True
            return last['speakerId'] == 'commander' and actor['id'] in last.get('mentions', [])
        ordered=sorted([a for a in room['members'] if a['id']!=last['speakerId']
            and not self.actor_busy(a['id'])
            and can_reenter(a)
            and (not topic.get('deferredActorId') or a['id']==topic['deferredActorId'])
            and (a['id'] not in topic.get('withdrawn',[]) or score(a)[0])],key=score,reverse=True)
        if not room['history'] and topic.get('openingActorId'):
            ordered.sort(key=lambda a:a['id']!=topic['openingActorId'])
        return ordered[:3]

    @staticmethod
    def actor_room(room, actor):
        """Project speech ownership for this actor without changing the archive."""
        roster={a['id']:a for a in [*room.get('directory',[]),*room['members']]}
        names={aid:a.get('name') or a.get('sourceName') or '未知发言者' for aid,a in roster.items()}
        names['commander']='指挥官'
        return {**room,'history':[{**m,
            'speakerName':names.get(m.get('speakerId'),m.get('speakerName') or '未知发言者'),
            'speakerSourceName':roster.get(m.get('speakerId'),{}).get('sourceName'),
            'speakerRole':('self' if m.get('speakerId')==actor['id'] else
                'commander' if m.get('speakerId')=='commander' else 'peer'),
            'ownSpeech':m.get('speakerId')==actor['id']} for m in room['history']]}

    @staticmethod
    def history_messages(room, actor):
        # Only this actor's actual utterances belong to the assistant role.  A
        # peer turn must not use the generic user role: chat models often treat
        # every user-role turn as the same speaker even when a name is prefixed
        # in the text.  Keep peers and the commander as explicit transcript
        # records instead.
        rendered=[]
        for m in room['history']:
            sid=str(m.get('speakerId') or 'unknown')
            if sid==actor['id']:
                rendered.append({'role':'assistant','content':m['text']})
                continue
            name=m.get('speakerName') or '未知发言者'
            source=m.get('speakerSourceName') or ''
            role=m.get('speakerRole') or ('commander' if sid=='commander' else 'peer')
            rendered.append({'role':'system','content':
                f'【历史转录 role={role} speakerId={sid} speakerName={name}'
                +(f' sourceName={source}' if source else '')+f'】\n{m["text"]}'})
        return rendered

    @staticmethod
    def identity_claim_mismatch(text, actor, room):
        """Return the peer name when a reply explicitly claims that identity."""
        if not isinstance(text,str):
            return None
        own={str(actor.get('name') or ''),str(actor.get('sourceName') or '')}
        own.discard('')
        for peer in room.get('directory',[]):
            if peer.get('id') in {actor.get('id'),'commander'}:
                continue
            names={str(peer.get('name') or ''),str(peer.get('sourceName') or '')}
            for name in names-own-{''}:
                if re.search(r'(?:我(?:是|叫|就是)|这(?:里|边)是)\s*'+re.escape(name)+r'(?=$|[，。！？、!?~～啦呀哦喔呢啊\s])',text):
                    return name
        return None

    @staticmethod
    def conversation_focus(room, topic, actor_id=None):
        """Current conversational handoff, without repeatedly resetting the topic."""
        history=room['history']
        latest_user=next((m for m in reversed(history) if m['speakerId']=='commander'),None)
        latest=history[-1] if history else None
        question=latest if latest and any(mark in latest['text'] for mark in ('？','?')) else None
        from backend.context.service import topic_ledger
        ledger = topic_ledger(history)
        requested = next((q for q in reversed(ledger['openQuestions']) if actor_id in q['targets']), None)
        # Utterance text already appears once in the attributed transcript.
        ledger = {**ledger, 'viewpoints': [{k: v for k, v in h.items() if k != 'text'} for h in ledger['viewpoints']],
            'openQuestions': [{k: v for k, v in h.items() if k != 'text'} for h in ledger['openQuestions']]}
        return {'ledger': ledger, 'closing': closing_context(history, topic, [a['id'] for a in room['members']]),
            'responseTarget': {'id': requested['id'], 'speakerId': requested['speakerId']} if requested else None,
            'request':topic['prompt'] if not topic.get('spoken') else None,
            'latestUser':{'id':latest_user['id'],'text':latest_user['text']} if latest_user and (not topic.get('spoken') or latest is latest_user) else None,
            'current':{'id':latest['id'],'speakerId':latest['speakerId'],'text':latest['text']} if latest else None,
            'openQuestion':{'id':question['id'],'speakerId':question['speakerId'],
                'text':question['text']} if question else None}

    def participation(self, actor, room):
        if not self.c.cognition:
            return {'familiarity':0,'commitments':[]}
        # Admission filtering applies before scoring as well as before expression.
        visible=self.snapshot(room['id'],actor['id'])
        last=visible['history'][-1] if visible['history'] else {}
        return self.c.cognition.social_participation(actor['id'],room['id'],last.get('speakerId'),
            {m['text'] for m in visible['history']} & {m['text'] for m in room['history']})

    @staticmethod
    def turn_contract(actor, room, topic):
        latest=room['history'][-1] if room['history'] else None
        from backend.context.service import topic_ledger
        pending = topic_ledger(room['history'])['openQuestions']
        own_request = next((q for q in reversed(pending) if actor['id'] in q['targets']), None)
        direct=bool(own_request or not topic.get('spoken') and latest and latest['speakerId']=='commander' and (
            actor['id'] in latest.get('mentions',[]) or '@'+actor['name'] in latest['text'] or
            '@'+actor.get('sourceName',actor['name']) in latest['text']))
        opening=bool(not room['history'] and topic.get('openingActorId')==actor['id'])
        new_group=bool(direct and re.search(r'(?:另建|新建|建立|创建|建|另开|新开|拉).{0,12}群',latest['text']))
        actions=(['speak'] if opening or topic.get('routeKind')=='clarify' else
            ['speak','create_group'] if new_group else
            ['speak','invite','create_group']+([] if direct else ['wait','end']))
        guidance=('用户要另建群，create_group 发起新群邀请，invite 只将群外人物加入当前群。'
            'create_group 会由系统先询问目标人物是否接受，接受后才建立新群；不必用 speak 先重复口头征求同意。'
            '目标必须是用户请求的真实目录成员；若不愿或暂不能建群，选择 speak 说明，不用将来会执行的承诺替代行动。' if new_group else
            '你刚发起新群，请选择 speak 自然开场，不再次邀请。' if opening else
            '用户点名请求，须用行动或 speak 回应，也可以拒绝。' if direct else '')
        closing = closing_context(room['history'], topic, [a['id'] for a in room['members']])
        if closing['closing']:
            guidance += '用户正在道别、准备离开。简短自然道别或沉默；不要追问明早安排，不把问候变成工作承诺或新的同伴接力。用户同时提出的具体请求仍须回应。'
            if not closing['hasUserRequest']:
                actions = ['speak'] if direct else ['speak', 'wait', 'end']
        return direct,opening,new_group,actions,guidance

    async def prepare(self, actor, room, topic, source, context_bundle=None):
        if self.actor_busy(actor['id']):raise SocialPreempted()
        room=self.actor_room(self.topic_room(room,topic),actor)
        runtime=getattr(self.c.cognition,'runtime',None)
        if not runtime or not runtime.active(actor['id']):return None
        persona,_=background(actor['id'])
        from backend.chat.conversation_scope import asks_about_work
        if room.get('limitedHistory'):
            topic={**topic,'prompt':room['invitationSummary']}
        visible_room={**room,'history':room['history'][-self.dialogue_history_limit():]}
        latest=room['history'][-1] if room['history'] else None
        _,_,_,actions,turn_guidance=self.turn_contract(actor,room,topic)
        latest_user=next((m for m in reversed(room['history']) if m['speakerId']=='commander'),None)
        current_text=((latest if topic.get('spoken') else latest_user or latest) or {}).get('text',topic['prompt'])
        from backend.chat.task_reference import resolve_task_reference, DEICTIC
        related_work=resolve_task_reference(self.c.store,actor['id'],
            current_text,room['history'][-6:], conversation_id=room['id'], public=True)
        if context_bundle is None and getattr(self.c, 'context', None):
            from backend.context.contracts import ContextRequest
            context_bundle = self.c.context.collect(ContextRequest(actor['id'], room['id'], 'social',
                current_text, source, topic.get('runId') or '', self.dialogue_preset(), True),
                history=room['history'], persona={'background': persona})
        decision = await runtime.think(actor['id'],source,json.dumps({'actorId':actor['id'],'actorName':actor['name'],'room':visible_room,
            'focus':self.conversation_focus(room,topic,actor['id']),'turn':topic.get('spoken',0),
            'relatedWork':related_work,
            'guidance':('这是成员自己发起的交流；用户没有提出本轮问题。优先接同伴刚才的具体话或自己的相关兴趣，'
                '不要转成等待用户回来、回复或批准。' if topic.get('proactive') else
                '跟随最近消息；已结束的内容无需复述。')+
                '可以自然转题或沉默，不用轮流表态。'+turn_guidance},ensure_ascii=False),
            mode='social',conversation_id=room['id'],public=True,background=bool(self.background.get()),actions=actions,
            include_task_memory='outcomes' if (related_work and not related_work.get('ambiguous')
                or asks_about_work(current_text) and not DEICTIC.search(current_text)) else False,
            related_run_id=related_work.get('runId','') if related_work and not related_work.get('ambiguous') else '',
            context_bundle=context_bundle)
        return {**decision, '_relatedWork':related_work, '_contextBundle': context_bundle}

    async def decide(self, actor, room, topic, source, prepared=None):
        if self.actor_busy(actor['id']):raise SocialPreempted()
        room=self.actor_room(self.topic_room(room,topic),actor)
        publication_history = room['history']
        # The original topic prompt may predate admission and was not necessarily shared.
        topic = {**topic, 'prompt': room['invitationSummary']} if room.get('limitedHistory') else topic
        persona,_=background(actor['id'])
        # A group expression must not receive private task bodies or judgments.
        mind=json.dumps(self.participation(actor,room),ensure_ascii=False)
        latest=room['history'][-1] if room['history'] else None
        from backend.chat.task_reference import resolve_task_reference
        from backend.chat.conversation_scope import asks_about_work
        latest_user=next((m for m in reversed(room['history']) if m['speakerId']=='commander'),None)
        work_query=((latest if topic.get('spoken') else latest_user or latest) or {}).get('text',topic['prompt'])
        related_work=(prepared.get('_relatedWork') if prepared is not None else
            resolve_task_reference(self.c.store,actor['id'],
                work_query,room['history'][-6:], conversation_id=room['id'], public=True))
        context_bundle = prepared.get('_contextBundle') if prepared else None
        if context_bundle is None and getattr(self.c, 'context', None):
            from backend.context.contracts import ContextRequest
            context_bundle = self.c.context.collect(ContextRequest(actor['id'], room['id'], 'social',
                work_query, source, topic.get('runId') or '', self.dialogue_preset(), True),
                history=room['history'], persona={'background': persona})
        if context_bundle:
            topic = {**topic, '_contextTraceId': context_bundle.trace_id}
        work_followup=bool(related_work or topic.get('routeKind')=='followup' or
            latest and latest['speakerId']=='commander' and asks_about_work(latest['text']))
        direct_request,new_group_opening,new_group_request,_,_=self.turn_contract(actor,room,topic)
        # Keep the speaking contract short; follow the current handoff.
        rules=(TERMINAL+'你是当前人物。根据眼前消息和你可见的经历决定发言、邀请或沉默。'
            '消息 speakerId=commander 始终是用户，当前人物由 actorId 决定；消息里叫错名字不能改变发言者身份。'
            '历史 ownSpeech 由当前 actorId 与原始 speakerId 比较得出；只有本人的消息才是“我说过”。'
            '同伴台词里的“我”属于那位同伴。可以承认自己附和过，但不能认领别人说的原话或自称最先提出；后来认错、转述不改变原话作者。'
            '用户把你认成别的角色时，可自然澄清，不要反过来把用户叫成那个角色。'
            '先判断此刻说话有没有新作用：回答、补充具体信息、不同意并说明原因、求助、开个轻松但相关的小玩笑，或自然收住。'
            '已经有人说清楚时可以 wait；不为了填满轮数轮流致谢、自我介绍、重复问题或总结。'
            '若用户刚提出具体问题，第一位有把握的成员直接回答；不知道就坦白，不绕圈追问。'
            '后续跟着最近发言和人物间实际关系走，旧问题不用每轮重答。'
            '话题自然结束时可沉默；有眼前消息或可见经历支持的新兴趣时可以自然转题，不为继续说话而制造追问，不强制全员围绕最初题目表态。'
            '每次通常一句或两句、合计不超过120字；语气跟当前人物、对象和情境走，不照抄角色卡示例，不堆口癖和省略号。'
            '不能编造任务成果、实验读数、已整理或已发出的记录，也不能替别人说话。轻度日常可以聊，但没有可见生活事件时，不自称刚洗澡、做饭、敷面膜、出门或正待在某个具体地点；直接回应消息即可。只能引用当前可见消息，replyTo 是被回应的消息编号。'
            '返回 JSON：action 为 speak/wait/end/invite/create_group；speak 含 segments（1至3条，普通短答默认一条，只有连续独立发言才拆分）、sourceIds（仅当前 sourceId）、replyTo（消息编号或 null）、mentions（人物编号数组）；'
            'invite 含 targetId、reason、summary；create_group 另含 title；wait/end 无台词。')
        from backend.mind.character_policy import CHARACTER_PERSPECTIVE
        rules += CHARACTER_PERSPECTIVE
        if self.dialogue_preset() == 'compact':
            rules += '当前采用节省 token 的表达：保留人物差异和对最近消息的有效回应，省略重复背景、客套铺垫和已经说清的结论；没有新内容优先 wait/end。'
        from backend.chat.sticker_catalog import expression_catalog
        rules+=expression_catalog(actor.get('sourceName',actor['name']))
        if not work_followup and topic.get('routeKind')!='clarify':
            rules+='这是日常聊天。回应眼前的情绪、话意或玩笑即可；认真不等于监督他人，没有已接委托时不安排核对、催促工作或索要指令。往事可以记得，但问候和新话题不自动重启旧约定。'
        if topic.get('proactive'):
            rules+='这是成员自行发起的群聊，不是用户提问。原作里对指挥官说的台词只反映与指挥官的关系，不能照搬成同伴对话的目标。接最近同伴的具体话，也可以聊自己有依据的兴趣；别把每轮变成等指挥官回来、回复或批准。若同伴已说清，直接收住。'
        rules+='若已经回答清楚且没有需要别人回应的新问题，speak 可以附 endAfterReply:true，自然结束本话题。不反复接力表示待命或重复已作出的分工。'
        rules+='focus.responseTarget 非空时，当前有同伴或用户在直接请求你回应；回答该问题，replyTo 使用该目标编号，不再重复回应旧问候。'
        closing = closing_context(room['history'], topic, [a['id'] for a in room['members']])
        if closing['closing']:
            rules += self.turn_contract(actor, room, topic)[4]
            rules += '没有待答的用户请求时，只需简短道别；不询问明早要准备什么、不创造早餐或杂务分工、不以“我还没睡、随时找我”重新开启话题。'
        if topic.get('spoken',0)>=6:
            rules+='这一话题已经接续多轮。只有确有新内容或仍需回答的具体问题才发言；可以一句话自然收住，别再发起新的追问或承诺。'
        if direct_request:
            rules+='用户正在点名向你提出请求。你可以接受、拒绝或说明条件，但要用行动或一句话回应，不能无声略过。'
        if new_group_request:
            rules+=self.turn_contract(actor,room,topic)[4]
        if new_group_opening:
            rules+='你刚发起这个新群。现在用一句自然的开场说明要聊什么，给同伴接话的空间；不要轮流自我介绍，也不要声称已经完成任何资料或工作。'
        if topic.get('routeKind')=='clarify':
            rules+='本轮委托范围不明确，尚未接单；只需一位成员问一个具体澄清问题。不要声称已开始处理，也不要邀请其他成员。发问后设置 endAfterReply:true。'
        if topic.get('routeKind')=='followup' and not related_work:
            rules+='用户在追问以前的工作，但本角色当前没有匹配的可核验来源。不要断言自己从未读过这份资料；请问清书名或已读范围，不能代替同伴声称亲自读取。'
        current_text=latest['text'] if latest else topic['prompt']
        if related_work and related_work.get('ambiguous'):
            rules+='当前“这本书”等指代对应多份资料，只询问具体是哪一本，不猜测读过哪份。'
        visible_room={**room,'history':room['history'][-self.dialogue_history_limit():]}
        runtime=getattr(self.c.cognition,'runtime',None)
        decision=None
        if runtime and runtime.active(actor['id']):
            topic={**topic,'_actorId':actor['id']}
            decision=prepared or await self.prepare(actor,room,topic,source,context_bundle=context_bundle)
            if decision['intent']['action'] in {'wait','end'}:
                return {'action':decision['intent']['action']}
            # Rendering gets the selected public action, not private appraisals or
            # the short tradeoff recorded for diagnostics.
            from backend.characters.dialogue_persona import personality_for_dialogue, public_dialogue_intent
            mind=json.dumps({'intent':public_dialogue_intent(decision['intent'])},ensure_ascii=False)
            persona=json.dumps({'name':actor['name'],'background':persona,
                'personality':personality_for_dialogue(runtime.profile(actor['id'])['interpretation'])},ensure_ascii=False)
            rules+='表达遵守意图中的 action 和 targetId，并承接 reply 中愿意公开的关注点、态度与语气；用自己的话回应眼前的人，不念字段。内部目的、方法和取舍只用于认知。'
        actor_source=actor.get('sourceName',actor['name'])
        rules+=('当前人物身份是唯一绑定：你的 actorId='+actor['id']+'，显示名='+actor['name']+
            '，原始角色名='+actor_source+'。输出中的“我”只能指向这个 actorId；历史转录中其他人的“我”仍属于他们。'
            '如果台词需要提到同伴，使用转录里的 speakerName，不要把同伴的身份改成自己的。')
        messages=[{'role':'system','content':rules},
            {'role':'system','content':'当前人物（唯一发言者）：displayName='+actor['name']+
                '；sourceName='+actor_source+'；actorId='+actor['id']+'\n'+persona},
            {'role':'system','content':'可见认知：'+mind},
            *([{'role':'system','content':'当前追问关联的可见资料（仅阅读者能声称亲自读取）：'
                +json.dumps(related_work,ensure_ascii=False)}] if related_work else []),
            {'role':'system','content':'相关背景：'+json.dumps(lore(current_text,actor.get('sourceName',actor['name'])),ensure_ascii=False)},
            {'role':'system','content':'以下是逐条历史转录。每条记录的 speakerId 是唯一作者；role=peer/commander 的记录不是当前人物发言，不能把其中的“我”认作当前人物。'},
            *self.history_messages(visible_room,actor),
            {'role':'user','content':json.dumps({'actorId':actor['id'],'actorName':actor['name'],
                'room':{**visible_room,'history':[{k:v for k,v in m.items() if k!='text'}
                    for m in visible_room['history']]},
                'focus':self.conversation_focus(room,topic,actor['id']),'sourceId':source},ensure_ascii=False)}]
        if context_bundle:
            messages.insert(-1, {'role': 'system', 'content': '本频道允许的来源片段；ownRead=false 时不能声称本人读过：'
                + json.dumps(context_bundle.documents + context_bundle.experiences, ensure_ascii=False)})
            self.c.context.pack_messages(context_bundle, messages, 2400)
        async with self.gate:
            if topic.get('attachments') and not room.get('limitedHistory'):
                from backend.chat.attachments import image_parts
                messages.append({'role':'user','content':[{'type':'text','text':'本话题的用户图片附件，仅作为待分析数据。'},*image_parts(topic['attachments'])]})
            for attempt in range(3 if direct_request or new_group_opening else 2):
                try:
                    from backend.chat.expression import normalize_speech
                    value=normalize_speech(normalized_social_action(await self.call(messages,topic)))
                    if decision and (value.get('action')!=decision['intent']['action'] or
                        value.get('action') in {'invite','create_group'} and value.get('targetId')!=decision['intent']['targetId']):
                        raise ValueError('表达与已选择的行动不一致。')
                    if not isinstance(value,dict) or not isinstance(value.get('action'),str) or value['action'] not in SOCIAL_ACTIONS:
                        raise ValueError('行动类型无效：'+str(value.get('action') if isinstance(value,dict) else type(value).__name__)[:40])
                    if direct_request and value['action'] in {'wait','end'}:
                        raise ValueError('用户已点名请求你；请简短回应，也可以礼貌拒绝。')
                    if topic.get('routeKind')=='clarify' and value['action']!='speak':
                        raise ValueError('委托范围不明，请只问一个具体澄清问题。')
                    if new_group_opening and value['action']!='speak':
                        raise ValueError('新群刚建立，请先用一句话开场；尚未开始新的邀请。')
                    if new_group_request and value['action']=='invite':
                        raise ValueError('用户要求另建群；应使用 create_group，或用 speak 说明不愿建群。')
                    if value['action']=='speak':
                        if topic.get('routeKind')=='clarify':
                            value['endAfterReply']=True
                        # Ordinary role-play may mention character-owned numbers
                        # (ship names, equipment, model codes) from the persona.
                        # Restrict numeric/file provenance checks to work-oriented
                        # turns where the facts bundle is the authority.
                        validation_facts = ({'prompt':topic['prompt'],
                            'history':[m['text'] for m in room['history']]}
                            if work_followup or topic.get('routeKind') in {'clarify', 'followup'} else
                            {'character': persona,
                             'background': lore(current_text, actor.get('sourceName', actor['name']))})
                        validate(value,source,facts=validation_facts)
                        speech=' '.join(value['segments'])
                        own_previous = [m['text'] for m in publication_history
                            if m['speakerId'] == actor['id'] and m.get('topicId') == topic['id']]
                        if repeats_own_speech(speech, own_previous,
                                publication_history[-1]['text'] if publication_history else ''):
                            raise ValueError('这段台词重复了本人在本话题的旧发言。请直接回答眼前的问题，给出新增信息或简短收住。')
                        response_target = self.conversation_focus(room, topic, actor['id'])['responseTarget']
                        if response_target and response_target['speakerId'] != 'commander' and value.get('replyTo') != response_target['id']:
                            raise ValueError('当前同伴正在直接问你，replyTo 必须为 '+response_target['id']+'；不要继续回复旧的用户问候。')
                        if closing['closing'] and not closing['hasUserRequest'] and closing_violation(speech):
                            raise ValueError('用户已明确道别；请简短道别，不新增问题、任务安排或待命承诺。')
                        claimed=self.identity_claim_mismatch(speech,actor,room)
                        if claimed:
                            raise ValueError('台词把当前人物误认成同伴 '+claimed+'；请按当前 actorId 重新表达')
                        if len(speech)>120 or len(value['segments'])>2:
                            raise ValueError('群聊接话请缩成一至两段、合计不超过120字。')
                        if (topic.get('proactive') and latest and latest['speakerId']!='commander'
                                and USER_WAIT.search(speech)):
                            raise ValueError('这是成员自行发起的交流，用户没有提出本轮问题；请回应同伴的新内容，或选择 wait/end，不要转成等待用户。')
                        if re.search(r'(?:记录|文件|照片|表格|链接).{0,12}(?:发你了|传给你了|已发送|整理好了)',speech):
                            raise ValueError('尚未实际发送材料，不能声称记录或文件已经发出。')
                        if value.get('replyTo') and value['replyTo'] not in {m['id'] for m in room['history']}:
                            raise ValueError('只能回复已看见的消息')
                        if not isinstance(value.get('mentions',[]),list) or not set(value.get('mentions',[])) <= {a['id'] for a in room['directory']}:
                            raise ValueError('提及人物无效')
                    if value['action'] in {'invite','create_group'}:
                        if value.get('targetId') not in {a['id'] for a in room['directory']} or not value.get('reason') or not value.get('summary'):
                            raise ValueError('邀请需要真实人物、原因和摘要')
                        if value['action']=='invite' and value['targetId'] in {a['id'] for a in room['members']}:
                            raise ValueError('对方已在当前群聊，无需再次邀请；直接回应其消息或暂不发言。')
                        if value['targetId']==actor['id']:
                            raise ValueError('不能邀请自己。')
                        if len(str(value['summary']))>600 or len(str(value['reason']))>300:
                            raise ValueError('邀请摘要过长')
                        if value['action']=='create_group' and (not isinstance(value.get('title'),str) or not 1<=len(value['title'].strip())<=40):
                            raise ValueError('新群名称应为1至40字')
                    if context_bundle:
                        value['contextTraceId'] = context_bundle.trace_id
                    return value
                except ValueError as exc:
                    if attempt==(2 if direct_request or new_group_opening else 1): raise
                    messages.append({'role':'user','content':json.dumps({'validationError':str(exc),
                        'requiredSourceIds':[source],'instruction':'修正结构后重新返回完整行动。sourceIds 必须与 requiredSourceIds 完全相同，历史消息编号只能用于 replyTo。'},ensure_ascii=False)})

    def commit(self, topic, actor, aid, value):
        if self.actor_busy(actor['id']):return False
        with session_scope() as session:
            if session.get(SocialAction,aid): return False
            room=session.get(Conversation,topic['conversationId'])
            member=session.scalar(select(ConversationMember).where(ConversationMember.conversation_id==room.id,
                ConversationMember.actor_id==actor['id'],ConversationMember.is_active.is_(True))) if room else None
            cfg=(room.extra_json or {}).get('social',{}) if room else {}
            if not room or not member or (room.extra_json or {}).get('archived') or cfg.get('muted'):
                return False
            changed=session.execute(update(SocialTopic).where(SocialTopic.id==topic['id'],SocialTopic.version==topic['version'],
                SocialTopic.status=='active').values(version=SocialTopic.version+1))
            if changed.rowcount!=1: return False
            if getattr(self.c, 'context', None):
                self.c.context.validate_trace(value.get('contextTraceId'))
            row=session.get(SocialTopic,topic['id'])
            data=dict(row.data)
            kind=value['action']
            status='committed'
            if kind in {'invite','create_group'}:
                if self.actor_busy(value['targetId']):raise ValueError('对方正在处理已接任务，稍后再邀请。')
                target=session.get(Actor,value['targetId'])
                members=session.scalars(select(ConversationMember).where(ConversationMember.conversation_id==room.id,ConversationMember.is_active.is_(True))).all()
                if room.kind!='group' or cfg.get('allowInvites',True) is False or data['invites']>=2 or (kind=='invite' and len([m for m in members if m.actor_id!='commander'])>=8):
                    raise ValueError('频道不允许继续邀请。')
                if not target or not target.is_active or target.kind!='agent' or target.id==actor['id'] or (kind=='invite' and target.id in {m.actor_id for m in members}):
                    raise ValueError('目标已加入或不可邀请。')
                if kind=='create_group' and (not isinstance(value.get('title'),str) or not 1<=len(value['title'].strip())<=40):
                    raise ValueError('新群名称应为1至40字')
                data['invites']+=1
                status='pending'
            elif kind=='speak':
                if data['spoken']>=self.topic_limit(data):
                    session.rollback()
                    return False
                body='\n\n'.join(value['segments'])
                recent=session.scalars(select(Message).where(Message.conversation_id==room.id).order_by(
                    *message_order(session,newest_first=True)).limit(40)).all()
                from backend.context.service import topic_ledger
                ledger_history = [{'id': m.id, 'text': m.body, 'speakerId': m.speaker_id,
                    'replyTo': (m.metadata_json or {}).get('replyTo'),
                    'mentions': (m.metadata_json or {}).get('mentions', [])} for m in reversed(recent)]
                ledger = topic_ledger(ledger_history)
                closing = closing_context(ledger_history, data, [m.actor_id for m in session.scalars(
                    select(ConversationMember).where(ConversationMember.conversation_id==room.id,
                        ConversationMember.is_active.is_(True))).all() if m.actor_id != 'commander'])
                own_previous = [m.body for m in recent if m.speaker_id == actor['id']
                    and (m.metadata_json or {}).get('topicId') == topic['id']]
                latest_text = ledger_history[-1]['text'] if ledger_history else ''
                if repeats_own_speech(body, own_previous, latest_text):
                    session.rollback()
                    self.c.store.event(data.get('runId'), 'social.reply_suppressed',
                        {'topicId': topic['id'], 'actorId': actor['id'], 'reason': 'repeated-own-speech'})
                    return False
                if closing['closing'] and not closing['hasUserRequest'] and closing_violation(body):
                    session.rollback()
                    self.c.store.event(data.get('runId'), 'social.reply_suppressed',
                        {'topicId': topic['id'], 'actorId': actor['id'], 'reason': 'farewell-reopened'})
                    return False
                obligated = any(actor['id'] in q['targets'] or value.get('replyTo') == q['id']
                    for q in ledger['openQuestions']) or actor['id'] in closing['pendingTargets']
                correction = bool(re.search(r'不(?:是|对|同意)|纠正|更正|但是|不过|实际|补充|相反|应该是', body))
                topic_recent=[m.body for m in recent if (m.metadata_json or {}).get('topicId')==topic['id']]
                if not obligated and any(re.sub(r'\W+', '', prior)==re.sub(r'\W+', '', body) for prior in topic_recent):
                    session.rollback()
                    return False
                if data['spoken']:
                    from difflib import SequenceMatcher
                    normalized=re.sub(r'\W+','',body)
                    novelty=self.topic_novelty(body,topic_recent)
                    data['lastNovelty']=novelty
                    assent = bool(re.match(r'^(?:我也|嗯|是啊|没错|同意|赞同|确实|说得对)', body))
                    residual = re.sub(r'^(?:我也(?:同意|觉得|认为)?|嗯|是啊|没错|同意|赞同|确实|说得对)[，,。！!\s]*', '', body)
                    residual = re.sub(r'\W+', '', residual)
                    pure_repeat = bool(residual and any(residual in re.sub(r'\W+', '', prior) for prior in topic_recent))
                    if novelty == 0.0 and assent and pure_repeat and not obligated and not correction:
                        # Reject this candidate without consuming the CAS version;
                        # the exchange must still evaluate the other candidates.
                        session.rollback()
                        return False
                    data['staleTurns']=0
                data['spoken']+=1
                remaining = [q for q in ledger['openQuestions'] if q['id'] != value.get('replyTo')
                    and actor['id'] not in q['targets']]
                required_closings = set(closing['pendingTargets']) - {actor['id']} if closing['closing'] else set()
                if value.get('endAfterReply') is True and not remaining and not required_closings:row.status='ended'
                if closing['closing'] and not closing['hasUserRequest']:
                    if not (set(closing['pendingTargets']) - {actor['id']}):
                        row.status = 'ended'
                data['ledger'] = {**ledger, 'openQuestions': remaining}
                self.service._append_message(session,room,actor['id'],'text',body,message_id=aid,
                    metadata={'expression':True,'segments':value['segments'],'topicId':topic['id'],
                        'socialActionId':aid,'replyTo':value.get('replyTo'),'mentions':value.get('mentions',[]),
                        'runId':data['runId'], 'contextTraceId':value.get('contextTraceId')})
            elif kind=='end': data['withdrawn']=list(dict.fromkeys([*data.get('withdrawn',[]),actor['id']]))
            data['recent']=(data.get('recent',[])+[actor['id']])[-8:]
            row.data=data
            audience=list(session.scalars(select(ConversationMember.actor_id).where(
                ConversationMember.conversation_id==room.id,ConversationMember.is_active.is_(True))).all())
            session.add(SocialAction(id=aid,topic_id=topic['id'],actor_id=actor['id'],status=status,
                data={'action':value,'conversationId':room.id,'runId':data['runId'],'audience':audience,'notified':False}))
        return True

    def flush(self):
        with session_scope() as session:
            pending=session.scalars(select(SocialAction).where(SocialAction.status.in_(['committed','accepted','rejected','pending','expired']))).all()
            for action in pending:
                if action.data.get('notified'): continue
                data=action.data; value=data['action']
                self.c.store.event(data['runId'],'social.action',{'actionId':action.id,'actorId':action.actor_id,'status':action.status,**data})
                if value['action']=='speak':
                    self.c.store.event(data['runId'],'social.message',{'messageId':action.id,'actorId':action.actor_id,
                        'conversationId':data['conversationId'],'text':'\n\n'.join(value['segments']),'expression':True})
                    # Freeze visibility at the time of speech, not at notification
                    # replay. A subsequently admitted member must not inherit it.
                    for viewer in data.get('audience',[]):
                        if viewer=='commander': continue
                        self.c.store.event(data['runId'],'mind.observation',{
                            'key':action.id+':'+viewer,'actorIds':[viewer],'kind':'speech',
                            'text':'\n\n'.join(value['segments']),
                            'data':{'ownSpeech':viewer==action.actor_id,'speakerId':action.actor_id,
                                'scope':'team_public','conversationId':data['conversationId'],
                                'peers':[aid for aid in data['audience'] if aid not in {viewer,'commander'}]}})
                self.c.store.event(data['runId'],'workspace.changed',{})
                action.data={**data,'notified':True}

    async def invitation(self, aid):
        with session_scope() as session:
            row=session.get(SocialAction,aid)
            if not row or row.status!='pending': return
            invitation=dict(row.data['action']); topic=self.topic(row.topic_id)
        persona,_=context(invitation['targetId'])
        if self.actor_busy(invitation['targetId']):return
        async with self.gate:
            try:
                runtime=getattr(self.c.cognition,'runtime',None)
                if runtime and runtime.active(invitation['targetId']):
                    decision=await runtime.think(invitation['targetId'],aid+':accept',json.dumps(invitation,ensure_ascii=False),
                        mode='social-invitation',background=bool(self.background.get()),actions=['accept','decline'])
                    value={'accept':decision['intent']['action']=='accept'}
                else:
                    value=await self.call([{'role':'system','content':TERMINAL+persona+'你收到邀请，只依据邀请原因与摘要决定接受或拒绝。返回 JSON：accept 为布尔值。'},
                        {'role':'user','content':json.dumps(invitation,ensure_ascii=False)}],topic)
            except SocialPreempted:
                return
        if not isinstance(value,dict) or not isinstance(value.get('accept'),bool): raise ValueError('邀请决定无效')
        with session_scope() as session:
            row=session.get(SocialAction,aid)
            if not row or row.status!='pending': return
            t=session.get(SocialTopic,row.topic_id)
            room=session.get(Conversation,row.data['conversationId'])
            if not room or not t: return
            cfg=dict((room.extra_json or {}).get('social',{}))
            inviter=session.scalar(select(ConversationMember).where(ConversationMember.conversation_id==room.id,
                ConversationMember.actor_id==row.actor_id,ConversationMember.is_active.is_(True)))
            if not inviter or t.version!=topic['version'] or t.status!='active' or (room.extra_json or {}).get('archived') or cfg.get('muted') or not cfg.get('allowInvites',True): return
            target=session.get(Actor,invitation['targetId'])
            if not target or not target.is_active: return
            existing=session.scalar(select(ConversationMember).where(ConversationMember.conversation_id==room.id,ConversationMember.actor_id==target.id))
            count=len(session.scalars(select(ConversationMember).where(ConversationMember.conversation_id==room.id,ConversationMember.is_active.is_(True),ConversationMember.actor_id!='commander')).all())
            creating=invitation['action']=='create_group'
            accept=value['accept'] and (creating or count<8)
            # Reserve the topic version before changing membership or evaluating
            # the shared daily quota; SQLite serializes writers at this boundary.
            changed=session.execute(update(SocialTopic).where(SocialTopic.id==t.id,SocialTopic.version==topic['version'],
                SocialTopic.status=='active').values(version=SocialTopic.version+1))
            if changed.rowcount!=1: return
            day=datetime.now().astimezone().date().isoformat()
            if accept and creating:
                created=session.scalars(select(SocialAction).where(SocialAction.status=='accepted')).all()
                if sum(a.data.get('createdDay')==day for a in created)>=2:
                    accept=False
                    row.data={**row.data,'reason':'今日自主建群数量已达上限'}
                else:
                    cid=stable(aid,'group')
                    # A tombstoned channel is never recreated by action replay.
                    if session.get(Conversation,cid): return
                    group=Conversation(id=cid,kind='group',title=invitation['title'].strip(),faction='社交频道',
                        announcement=invitation['summary'],extra_json={'social':{'originActionId':aid}})
                    session.add(group); session.flush()
                    session.add_all([ConversationMember(conversation_id=cid,actor_id='commander',role='owner'),
                        ConversationMember(conversation_id=cid,actor_id=row.actor_id,role='member'),
                        ConversationMember(conversation_id=cid,actor_id=target.id,role='member')])
                    child_id=stable(aid,'topic')
                    session.add(SocialTopic(id=child_id,conversation_id=cid,version=0,status='active',
                        data={'runId':row.data['runId'],'prompt':invitation['summary'],'spoken':t.data['spoken'],
                            'invites':t.data['invites'],'recent':[],'parentTopicId':t.id,'openingActorId':row.actor_id}))
                    row.data={**row.data,'createdDay':day,'createdConversationId':cid,'nextTopicId':child_id}
                    t.status='moved'
            elif accept:
                if existing: existing.is_active=True
                else: session.add(ConversationMember(conversation_id=room.id,actor_id=target.id,role='member'))
                # SQLite timestamps have second precision. Record existing IDs at the
                # boundary so messages sent later in the same second remain visible.
                boundary=datetime.now(UTC).replace(microsecond=0)
                boundary_ids=list(session.scalars(select(Message.id).where(Message.conversation_id==room.id,
                    Message.created_at>boundary-timedelta(seconds=1))).all())
                cfg['joinedAt']={**cfg.get('joinedAt',{}),target.id:boundary.timestamp()}
                cfg['joinBoundaryIds']={**cfg.get('joinBoundaryIds',{}),target.id:boundary_ids}
                cfg['summaries']={**cfg.get('summaries',{}),target.id:invitation['summary']}
                room.extra_json={**(room.extra_json or {}),'social':cfg}
            row.status='accepted' if accept else 'rejected'
            row.data={**row.data,'notified':False}

    async def chat(self, run):
        tid=self.begin(run)
        return await self.exchange(tid)

    async def exchange(self, tid, *, is_busy=None, background=False):
        token=self.background.set(is_busy if background else None)
        try:
            return await self._exchange(tid)
        finally:
            self.background.reset(token)
            # Cancelled/failed exchanges must not leave invitations apparently
            # pending until the next application restart.
            with session_scope() as session:
                queue=[tid]
                seen=set()
                while queue:
                    current=queue.pop()
                    if current in seen: continue
                    seen.add(current)
                    row=session.get(SocialTopic,current)
                    if row and row.status=='active':
                        row.status='interrupted'; row.version+=1
                    for action in session.scalars(select(SocialAction).where(SocialAction.topic_id==current)).all():
                        if action.data.get('nextTopicId'): queue.append(action.data['nextTopicId'])
                        if action.status=='pending':
                            action.status='expired'; action.data={**action.data,'notified':False}
            self.flush()

    async def _exchange(self, tid):
        for turn in range(max(8, self.topic_limit(self.topic(tid)))):
            topic=self.topic(tid)
            if topic.get('spoken',0)>=self.topic_limit(topic):break
            try:
                room=self.topic_room(self.snapshot(topic['conversationId']),topic)
            except ValueError:
                break
            if topic['status']!='active' or room['config'].get('muted'): break
            self.defer_busy_mentions(room,topic)
            progressed=False
            candidates=self.candidates(room,topic)
            expected_version=topic['version']
            last_id=room['history'][-1]['id'] if room['history'] else None
            last=room['history'][-1] if room['history'] else {}
            direct=sum(a['id'] in last.get('mentions',[]) or '@'+a['name'] in last.get('text','') or
                '@'+a.get('sourceName',a['name']) in last.get('text','') for a in candidates)
            prefetch_count=3 if direct>=3 else 2
            runtime=getattr(self.c.cognition,'runtime',None)
            prepared={}
            # A normal user chat publishes one response at a time.  Preparing
            # two extra model decisions there only burns tokens and makes the
            # UI feel busy; retain parallel preparation for legacy/autonomous
            # exchanges and genuinely multi-addressed user turns.
            prefetch_allowed = (os.getenv('AZURJUUS_GROUP_PREFETCH','1') != '0'
                and (topic.get('routeKind') != 'chat' or direct >= 2))
            if runtime and runtime.enabled and prefetch_allowed:
                frozen_ids={message['id'] for message in room['history']}
                for actor in candidates[:prefetch_count]:
                    aid=stable(tid,str(turn),actor['id'])
                    visible=self.snapshot(room['id'],actor['id'])
                    visible['history']=[message for message in visible['history'] if message['id'] in frozen_ids]
                    prepared[actor['id']]=asyncio.create_task(
                        self.track(actor['id'],self.prepare(actor,visible,topic,aid)))
            self.c.store.event(topic.get('runId'),'mind.group_candidates',{'topicId':tid,
                'candidates':len(candidates),'prepared':len(prepared)})
            stale=False
            try:
                for actor in candidates:
                    topic=self.topic(tid)
                    try:latest=self.snapshot(room['id'])
                    except ValueError:
                        stale=True
                        break
                    if (topic['status']!='active' or topic['version']!=expected_version or
                            latest['config'].get('muted') or
                            (latest['history'][-1]['id'] if latest['history'] else None)!=last_id):
                        stale=True
                        break
                    aid=stable(tid,str(turn),actor['id'])
                    with session_scope() as session:
                        if session.get(SocialAction,aid): continue
                    try:
                        decision=await prepared[actor['id']] if actor['id'] in prepared else None
                        refreshed=self.topic(tid)
                        try:refreshed_room=self.snapshot(room['id'])
                        except ValueError:
                            stale=True
                            break
                        if (refreshed['status']!='active' or refreshed['version']!=expected_version or
                                refreshed_room['config'].get('muted') or
                                (refreshed_room['history'][-1]['id'] if refreshed_room['history'] else None)!=last_id):
                            stale=True
                            break
                        visible=self.snapshot(room['id'],actor['id'])
                        evaluation=(self.decide(actor,visible,topic,aid,prepared=decision) if decision is not None
                            else self.decide(actor,visible,topic,aid))
                        value=await self.track(actor['id'],evaluation)
                        if self.commit(topic,actor,aid,value):
                            expected_version+=1
                            if runtime and runtime.enabled:
                                from backend.mind.mind_runtime import key as mind_key
                                runtime.mark_social_publication(mind_key(actor['id'],aid,'social'),value['action'])
                            self.flush()
                            if value['action'] in {'invite','create_group'}:
                                await self.invitation(aid); self.flush()
                                with session_scope() as session:
                                    committed=session.get(SocialAction,aid)
                                    tid=committed.data.get('nextTopicId',tid)
                            if value['action'] in {'speak','invite','create_group'}: progressed=True; break
                    except SocialPreempted:
                        break
                    except (ValueError,RuntimeError,asyncio.TimeoutError) as exc:
                        self.c.store.event(topic['runId'],'social.error',{'actorId':actor['id'],'reason':str(exc)[:180]})
            finally:
                for task in prepared.values():
                    if not task.done():task.cancel()
                if prepared:await asyncio.gather(*prepared.values(),return_exceptions=True)
                if stale and runtime:
                    from backend.mind.mind_runtime import key as mind_key
                    for actor_id,task in prepared.items():
                        if not task.cancelled() and task.exception() is None and task.result():
                            aid=stable(tid,str(turn),actor_id)
                            runtime.mark_social_publication(mind_key(actor_id,aid,'social'),'stale')
            if not progressed: break
        with session_scope() as session:
            topic=session.get(SocialTopic,tid)
            if topic.status=='active':
                room = self.topic_room(self.snapshot(topic.conversation_id), {'id': tid, **topic.data})
                from backend.context.service import topic_ledger
                ledger = topic_ledger(room['history'])
                topic.data = {**topic.data, 'ledger': ledger}
                topic.status = 'waiting' if ledger['openQuestions'] else 'ended'
        return '本轮交流已结束。'


def install_social_api(app, engine):
    from fastapi import HTTPException
    from backend.social.social_roster import install_roster_api
    install_roster_api(app,engine)

    @app.post('/api/conversations/groups')
    def create_group(payload:dict):
        from uuid import uuid4
        from backend.characters.character_identity import roster_actors
        title=payload.get('title','')
        members=payload.get('memberIds',[])
        if not isinstance(title,str) or not 1<=len(title.strip())<=40:
            raise HTTPException(400,'群名称应为1至40字')
        if not isinstance(members,list) or not all(isinstance(a,str) for a in members) or not 1<=len(set(members))<=24:
            raise HTTPException(400,'请选择1至24名名单成员')
        cid='group-'+uuid4().hex
        with session_scope() as session:
            allowed={a.id for a in roster_actors(session)}
            if not set(members)<=allowed:raise HTTPException(400,'只能邀请当前角色名单中的成员')
            session.add(Conversation(id=cid,kind='group',title=title.strip(),faction='自建频道',extra_json={'social':{'createdBy':'commander'}}))
            session.flush()
            session.add_all([ConversationMember(conversation_id=cid,actor_id=a,role='owner' if a=='commander' else 'member') for a in ['commander',*dict.fromkeys(members)]])
        engine.c.store.event(None,'workspace.changed',{'conversationId':cid})
        return {'conversationId':cid}

    @app.get('/api/social/state')
    def state():
        with session_scope() as session:
            topics=session.scalars(select(SocialTopic).where(SocialTopic.status!='control').limit(100)).all()
            return {'enabled':engine.enabled,**engine.activity.inspect(),'topics':[{'id':t.id,'conversationId':t.conversation_id,'version':t.version,'status':t.status} for t in topics]}

    @app.post('/api/social/settings')
    async def configure_social(payload:dict):
        try: return engine.activity.change(payload)
        except ValueError as exc: raise HTTPException(400,str(exc))

    @app.get('/api/social/actors')
    def actors():
        from backend.characters.character_identity import roster_actors
        with session_scope() as session:
            rows=roster_actors(session)
            actual=[{'id':a.id,'name':a.name,'faction':a.faction,'availability':'active' if a.is_active else 'background'} for a in rows]
            return {'actors':actual}

    @app.get('/api/conversations/{cid}/social')
    def inspect_room(cid:str):
        try: return engine.snapshot(cid)
        except ValueError as exc: raise HTTPException(404,str(exc))

    @app.post('/api/conversations/{cid}/social')
    def configure_room(cid:str,payload:dict):
        with session_scope() as session:
            room=session.get(Conversation,cid)
            if not room: raise HTTPException(404,'频道不存在')
            cfg=dict((room.extra_json or {}).get('social',{}))
            for key in ('muted','allowInvites'):
                if key in payload:
                    if not isinstance(payload[key],bool): raise HTTPException(400,'需要布尔值')
                    cfg[key]=payload[key]
            room.extra_json={**(room.extra_json or {}),'social':cfg}
            session.execute(update(SocialTopic).where(SocialTopic.conversation_id==cid,SocialTopic.status=='active')
                .values(status='superseded',version=SocialTopic.version+1))
        engine.c.store.event(None,'workspace.changed',{})
        return cfg

    @app.post('/api/conversations/{cid}/members/{actor_id}/remove')
    def remove_member(cid:str,actor_id:str):
        if actor_id=='commander': raise HTTPException(400,'用户保留频道管理权')
        with session_scope() as session:
            room=session.get(Conversation,cid)
            if not room or room.kind!='group': raise HTTPException(400,'只能管理群成员')
            member=session.scalar(select(ConversationMember).where(ConversationMember.conversation_id==cid,ConversationMember.actor_id==actor_id))
            if not member: raise HTTPException(404,'成员不存在')
            member.is_active=False
            session.execute(update(SocialTopic).where(SocialTopic.conversation_id==cid,SocialTopic.status=='active')
                .values(status='superseded',version=SocialTopic.version+1))
        engine.c.store.event(None,'workspace.changed',{})
        return {'removed':True}

    @app.post('/api/conversations/{cid}/members/{actor_id}/add')
    def add_member(cid:str,actor_id:str):
        from backend.characters.character_identity import roster_actors
        with session_scope() as session:
            room=session.get(Conversation,cid)
            if not room or room.kind!='group' or (room.extra_json or {}).get('archived'):raise HTTPException(404,'群聊不存在')
            if actor_id not in {a.id for a in roster_actors(session)}:raise HTTPException(400,'只能加入名单中的人物')
            existing=session.scalar(select(ConversationMember).where(ConversationMember.conversation_id==cid,ConversationMember.actor_id==actor_id))
            if existing and existing.is_active:return {'added':True}
            count=len(session.scalars(select(ConversationMember).where(ConversationMember.conversation_id==cid,ConversationMember.is_active.is_(True),ConversationMember.actor_id!='commander')).all())
            if count>=24:raise HTTPException(400,'群成员已达24人上限')
            if existing:existing.is_active=True
            else:session.add(ConversationMember(conversation_id=cid,actor_id=actor_id,role='member'))
            cfg=dict((room.extra_json or {}).get('social',{}))
            boundary=datetime.now(UTC).replace(microsecond=0)
            cfg['joinedAt']={**cfg.get('joinedAt',{}),actor_id:boundary.timestamp()}
            cfg['joinBoundaryIds']={**cfg.get('joinBoundaryIds',{}),actor_id:list(session.scalars(select(Message.id).where(Message.conversation_id==cid)).all())}
            cfg['summaries']={**cfg.get('summaries',{}),actor_id:'用户邀请你加入本群。此前的对话未分享；从新消息开始参与。'}
            room.extra_json={**(room.extra_json or {}),'social':cfg}
            session.execute(update(SocialTopic).where(SocialTopic.conversation_id==cid,SocialTopic.status=='active').values(version=SocialTopic.version+1))
        engine.c.store.event(None,'workspace.changed',{'conversationId':cid})
        return {'added':True}

    @app.get('/api/social/topics/{tid}/actions')
    def actions(tid:str):
        with session_scope() as session:
            return {'actions':[{'id':a.id,'actorId':a.actor_id,'status':a.status,**a.data}
                for a in session.scalars(select(SocialAction).where(SocialAction.topic_id==tid)).all()]}
