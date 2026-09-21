"""Event-driven group decisions. Model calls never hold a database transaction."""
import asyncio
import hashlib
import json
import os
import re
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from sqlalchemy import select, update
from .database import session_scope
from .models import Actor, Conversation, ConversationMember, Message
from .social_models import SocialTopic, SocialAction
from .terminal_characters import TERMINAL, context, lore, worldbook
from .expression import validate
from .idle_social import _generate, SocialPreempted
from .social_activity import SocialActivity
from .message_order import message_order


def stable(*parts):
    return 'social-' + hashlib.sha256(':'.join(parts).encode()).hexdigest()[:32]


SOCIAL_ACTIONS={'speak','wait','end','invite','create_group'}

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

    async def call(self, messages, topic):
        background_busy = self.background.get()
        def stale():
            current = self.topic(topic['id'])
            return current['status']!='active' or current['version']!=topic['version'] or bool(background_busy and background_busy())
        if stale(): raise SocialPreempted()
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
            from .character_identity import roster_actors
            actors = roster_actors(session)
            roster = [{'id':a.id,'name':a.name,'sourceName':a.source_character or a.name,'faction':a.faction} for a in actors]
            messages = session.scalars(select(Message).where(Message.conversation_id==cid).order_by(
                *message_order(session,newest_first=True)).limit(40)).all()[::-1]
            joined = config.get('joinedAt', {}).get(actor_id) if actor_id else None
            excluded = set(config.get('joinBoundaryIds', {}).get(actor_id, []))
            allowed_ids={'commander',*(a['id'] for a in roster)}
            visible = [m for m in messages if m.speaker_id in allowed_ids and (not joined or m.created_at.replace(tzinfo=UTC).timestamp() >= joined) and m.id not in excluded]
            # Never expose another member's invitation summary or admission metadata.
            public_config = {key: config.get(key, default) for key, default in [('muted', False), ('allowInvites', True)]}
            return {'id':cid,'kind':room.kind,'title':room.title,'config':public_config,
                'members':[a for a in roster if a['id'] in ids], 'directory':roster,
                'history':[{'id':m.id,'speakerId':m.speaker_id,'speakerName':next((a['name'] for a in roster if a['id']==m.speaker_id),'指挥官'),'text':m.body,'replyTo':(m.metadata_json or {}).get('replyTo'),'mentions':(m.metadata_json or {}).get('mentions',[]),'topicId':(m.metadata_json or {}).get('topicId')} for m in visible],
                'invitationSummary':config.get('summaries',{}).get(actor_id,''),
                'limitedHistory': bool(joined)}

    def begin(self, run):
        cid, tid = run['conversationId'], stable(run['id'],'topic')
        with session_scope() as session:
            if session.get(SocialTopic, tid):
                return tid
            session.execute(update(SocialTopic).where(SocialTopic.conversation_id==cid,SocialTopic.status=='active')
                .values(status='superseded',version=SocialTopic.version+1))
            session.add(SocialTopic(id=tid,conversation_id=cid,version=0,status='active',
                data={'runId':run['id'],'prompt':run['prompt'],'spoken':0,'invites':0,'recent':[], 'attachments':run.get('attachments',[])}))
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
        self.flush()

    def topic(self, tid):
        with session_scope() as session:
            t=session.get(SocialTopic,tid)
            return {'id':t.id,'conversationId':t.conversation_id,'version':t.version,'status':t.status,**t.data}

    def candidates(self, room, topic):
        last=room['history'][-1] if room['history'] else {'text':topic['prompt'],'speakerId':'commander'}
        recent=topic.get('recent',[])
        book=worldbook()
        interests={a['name']:a['topics'] for a in book['identities']}
        last_actor=next((a for a in room['members'] if a['id']==last['speakerId']),{})
        last_name=last_actor.get('sourceName',last_actor.get('name'))
        signals={a['id']:self.participation(a, room) for a in room['members'] if a['id']!=last['speakerId']}
        def score(a):
            explicit=a['id'] in last.get('mentions',[]) or a['name'] in last['text'] or a.get('sourceName',a['name']) in last['text']
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
            return (int(explicit), interest*2+int(known)+continuity-2*recent.count(a['id']), -recent.count(a['id']), a['id'])
        ordered=sorted([a for a in room['members'] if a['id']!=last['speakerId'] and (a['id'] not in topic.get('withdrawn',[]) or score(a)[0])],key=score,reverse=True)
        if not room['history'] and topic.get('openingActorId'):
            ordered.sort(key=lambda a:a['id']!=topic['openingActorId'])
        return ordered[:3]

    @staticmethod
    def conversation_focus(room, topic):
        """Current conversational handoff, without repeatedly resetting the topic."""
        history=room['history']
        latest_user=next((m for m in reversed(history) if m['speakerId']=='commander'),None)
        latest=history[-1] if history else None
        question=latest if latest and any(mark in latest['text'] for mark in ('？','?')) else None
        return {'request':topic['prompt'] if not topic.get('spoken') else None,
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
            {m['text'] for m in visible['history']})

    async def decide(self, actor, room, topic, source):
        # The original topic prompt may predate admission and was not necessarily shared.
        topic = {**topic, 'prompt': room['invitationSummary']} if room.get('limitedHistory') else topic
        persona,_=context(actor['id'])
        # A group expression must not receive private task bodies or judgments.
        mind=json.dumps(self.participation(actor,room),ensure_ascii=False)
        latest=room['history'][-1] if room['history'] else None
        direct_request=bool(not topic.get('spoken') and latest and latest['speakerId']=='commander' and (
            actor['id'] in latest.get('mentions',[]) or '@'+actor['name'] in latest['text'] or
            '@'+actor.get('sourceName',actor['name']) in latest['text']))
        new_group_opening=bool(not room['history'] and topic.get('openingActorId')==actor['id'])
        new_group_request=bool(direct_request and any(word in latest['text'] for word in ('建群','新群','另开群','拉个群')))
        # Keep the speaking contract short; follow the current handoff.
        rules=(TERMINAL+'你只扮演当前人物。根据眼前消息和你可见的经历决定发言、邀请或沉默。'
            '先判断此刻说话有没有新作用：回答、补充具体信息、不同意并说明原因、求助、开个轻松但相关的小玩笑，或自然收住。'
            '已经有人说清楚时可以 wait；不为了填满轮数轮流致谢、自我介绍、重复问题或总结。'
            '若用户刚提出具体问题，第一位有把握的成员直接回答；不知道就坦白，不绕圈追问。'
            '后续跟着最近发言和人物间实际关系走，旧问题不用每轮重答。'
            '每次通常一句或两句、合计不超过120字；语气跟当前人物、对象和情境走，不照抄角色卡示例，不堆口癖和省略号。'
            '不能编造任务成果、实验读数、已整理或已发出的记录，也不能替别人说话。轻度日常可以聊，但不要把即兴设想说成可核验的数据。只能引用当前可见消息，replyTo 是被回应的消息编号。'
            '返回 JSON：action 为 speak/wait/end/invite/create_group；speak 含 segments（1至3条）、sourceIds（仅当前 sourceId）、replyTo（消息编号或 null）、mentions（人物编号数组）；'
            'invite 含 targetId、reason、summary；create_group 另含 title；wait/end 无台词。')
        if topic.get('spoken',0)>=6:
            rules+='这一话题已经接续多轮。只有确有新内容或仍需回答的具体问题才发言；可以一句话自然收住，别再发起新的追问或承诺。'
        if direct_request:
            rules+='用户正在点名向你提出请求。你可以接受、拒绝或说明条件，但要用行动或一句话回应，不能无声略过。'
        if new_group_request:
            rules+='用户要另建一个群：请用 create_group，目标人物即使已在当前群也可以加入新群；invite 只用于把群外人物加进当前群。若不愿建群，用 speak 简短说明。'
        if new_group_opening:
            rules+='你刚发起这个新群。现在用一句自然的开场说明要聊什么，给同伴接话的空间；不要轮流自我介绍，也不要声称已经完成任何资料或工作。'
        current_text=' '.join(m['text'] for m in room['history'][-4:]) or topic['prompt']
        visible_room={**room,'history':room['history'][-12:]}
        messages=[{'role':'system','content':rules},{'role':'system','content':'当前人物：'+persona},
            {'role':'system','content':'可见认知：'+mind},
            {'role':'system','content':'相关背景：'+json.dumps(lore(current_text,actor.get('sourceName',actor['name'])),ensure_ascii=False)},
            {'role':'user','content':json.dumps({'room':visible_room,'focus':self.conversation_focus(room,topic),'sourceId':source},ensure_ascii=False)}]
        async with self.gate:
            if topic.get('attachments') and not room.get('limitedHistory'):
                from .attachments import image_parts
                messages.append({'role':'user','content':[{'type':'text','text':'本话题的用户图片附件，仅作为待分析数据。'},*image_parts(topic['attachments'])]})
            for attempt in range(3 if direct_request or new_group_opening else 2):
                try:
                    value=normalized_social_action(await self.call(messages,topic))
                    if not isinstance(value,dict) or not isinstance(value.get('action'),str) or value['action'] not in SOCIAL_ACTIONS:
                        raise ValueError('行动类型无效：'+str(value.get('action') if isinstance(value,dict) else type(value).__name__)[:40])
                    if direct_request and value['action'] in {'wait','end'}:
                        raise ValueError('用户已点名请求你；请简短回应，也可以礼貌拒绝。')
                    if new_group_opening and value['action']!='speak':
                        raise ValueError('新群刚建立，请先用一句话开场；尚未开始新的邀请。')
                    if new_group_request and value['action']=='invite':
                        raise ValueError('用户要求另建群；应使用 create_group，或用 speak 说明不愿建群。')
                    if value['action']=='speak':
                        validate(value,source,facts={'prompt':topic['prompt'],
                            'history':[m['text'] for m in room['history']]})
                        speech=' '.join(value['segments'])
                        if len(speech)>120 or len(value['segments'])>2:
                            raise ValueError('群聊接话请缩成一至两段、合计不超过120字。')
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
                    return value
                except ValueError as exc:
                    if attempt==(2 if direct_request or new_group_opening else 1): raise
                    messages.append({'role':'user','content':json.dumps({'validationError':str(exc),
                        'requiredSourceIds':[source],'instruction':'修正结构后重新返回完整行动。sourceIds 必须与 requiredSourceIds 完全相同，历史消息编号只能用于 replyTo。'},ensure_ascii=False)})

    def commit(self, topic, actor, aid, value):
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
            row=session.get(SocialTopic,topic['id'])
            data=dict(row.data)
            kind=value['action']
            status='committed'
            if kind in {'invite','create_group'}:
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
                if data['spoken']>=8: return False
                body='\n\n'.join(value['segments'])
                recent=session.scalars(select(Message).where(Message.conversation_id==room.id).order_by(
                    *message_order(session,newest_first=True)).limit(8)).all()
                if any(m.body.strip()==body.strip() for m in recent):return False
                data['spoken']+=1
                self.service._append_message(session,room,actor['id'],'text',body,message_id=aid,
                    metadata={'expression':True,'segments':value['segments'],'topicId':topic['id'],
                        'socialActionId':aid,'replyTo':value.get('replyTo'),'mentions':value.get('mentions',[]),'runId':data['runId']})
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
        async with self.gate:
            try:
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
        for turn in range(8):
            topic=self.topic(tid)
            try:
                room=self.snapshot(topic['conversationId'])
            except ValueError:
                break
            if topic['status']!='active' or room['config'].get('muted'): break
            progressed=False
            for actor in self.candidates(room,topic):
                topic=self.topic(tid)
                if topic['status']!='active': break
                aid=stable(tid,str(turn),actor['id'])
                with session_scope() as session:
                    if session.get(SocialAction,aid): continue
                try:
                    value=await self.decide(actor,self.snapshot(room['id'],actor['id']),topic,aid)
                    if self.commit(topic,actor,aid,value):
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
            if not progressed: break
        with session_scope() as session:
            topic=session.get(SocialTopic,tid)
            if topic.status=='active': topic.status='ended'
        return '本轮交流已结束。'


def install_social_api(app, engine):
    from fastapi import HTTPException
    from .social_roster import install_roster_api
    install_roster_api(app,engine)

    @app.post('/api/conversations/groups')
    def create_group(payload:dict):
        from uuid import uuid4
        from .character_identity import roster_actors
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
        from .character_identity import roster_actors
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
        from .character_identity import roster_actors
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
