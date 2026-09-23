"""Online-only lightweight life; no tools and no fabricated offline completions."""
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, update, delete

from .database import session_scope
from .models import Actor, Conversation, ConversationMember, Message
from .mind_models import MindControl, LifeActivity, LifeRecord, PersonalGoal, MindProfile
from .cognition_models import Experience, MindState, MindOutbox
from .mind_contracts import LifeEvent


class LifeRuntime:
    ACTIONS={'read':'阅读已有资料','practice':'兴趣练习','rest':'休息','reflect':'独处整理','invite':'共同交流'}

    def __init__(self, runtime):
        self.r=runtime
        self.last_tick=None

    def repair_contact_attribution(self):
        """Revoke old group-contact projections that impersonated the speaker."""
        from .database import get_engine
        from .mind_runtime import key

        with session_scope() as s:
            contacts={row.id:row.data for row in s.scalars(select(LifeRecord))
                if row.data.get('kind')=='contact' and row.data.get('actorId')}
            if not contacts:return
            malformed={eid for eid,data in contacts.items()
                if data.get('participants')!=[data['actorId']]}
            wrong=[row.id for row in s.scalars(select(Experience).where(Experience.kind=='simulation',
                Experience.forgotten.is_(False)))
                if row.data.get('eventId') in contacts
                and row.actor_id!=contacts[row.data['eventId']]['actorId']]
            if not malformed and not wrong:return

        engine=get_engine()
        backup=''
        if engine.dialect.name=='sqlite' and engine.url.database!=':memory:':
            source=Path(engine.url.database).resolve()
            folder=source.parent/'migration-backups'/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-contact-attribution-v1')
            folder.mkdir(parents=True,exist_ok=True)
            target=folder/'business.db'
            with closing(sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True)) as original, closing(sqlite3.connect(target)) as saved:
                original.backup(saved)
            backup=str(target)

        corrected=invalidated=0
        with session_scope() as s:
            contacts={row.id:row for row in s.scalars(select(LifeRecord))
                if row.data.get('kind')=='contact' and row.data.get('actorId')}
            for record in contacts.values():
                speaker=record.data['actorId']
                if record.data.get('participants')!=[speaker]:
                    record.data={**record.data,'participants':[speaker]}
                    corrected+=1
            for row in s.scalars(select(Experience).where(Experience.kind=='simulation',
                    Experience.forgotten.is_(False))):
                event=contacts.get(row.data.get('eventId'))
                if not event or row.actor_id==event.data['actorId']:continue
                row.forgotten=True
                row.data={**row.data,'reflection':'invalidated','mindReflection':'historical',
                    'derived':[],'invalidatedBy':'contact-attribution-v1'}
                memories=s.scalars(select(Experience).where(Experience.actor_id==row.actor_id)).all()
                affected={row.id}
                while True:
                    children={item.id for item in memories if set(item.data.get('dependencies',[])) & affected}
                    if children<=affected:break
                    affected|=children
                for item in memories:
                    if item.id in affected and item.id!=row.id:
                        item.forgotten=True
                        item.data={**item.data,'reflection':'invalidated','mindReflection':'historical',
                            'invalidatedBy':row.id}
                state=self.r.mind.state(s,row.actor_id)
                revised={**state.data,**{field:[item for item in state.data.get(field,[])
                    if item.get('sourceId') not in affected] for field in ('focus','commitments','appraisals')},
                    'mood':'依据新证据重新理解'}
                revised.pop('mindMood',None)
                state.data=revised
                state.version+=1
                for source_id in affected:
                    self.r.invalidate_in_session(s,row.actor_id,source_id)
                outbox_id=key('contact-attribution-v1',row.id)
                if not s.get(MindOutbox,outbox_id):
                    s.add(MindOutbox(id=outbox_id,payload={'actorId':row.actor_id,
                        'sourceId':row.id,'version':state.version}))
                invalidated+=1
            marker=s.get(MindControl,'contact-attribution-v1')
            details={'at':time.time(),'correctedEvents':corrected,
                'invalidatedExperiences':invalidated,'backup':backup}
            if marker:marker.data=details
            else:s.add(MindControl(id='contact-attribution-v1',data=details))
        self.r.revision+=1
        self.r.mind.flush_outbox()

    def recover(self):
        self.r.control()
        self.r.decay()
        with session_scope() as s:
            marker=s.get(MindControl,'migration-v4')
            if marker is None:
                # Do not turn old speech into retrospective life events.
                for row in s.scalars(select(Experience)):
                    row.data={**row.data,'mindReflection':'historical','memoryOrigin':'legacy'}
                for state in s.scalars(select(MindState)):
                    state.data={**state.data,'legacyState':True}
                s.add(MindControl(id='migration-v4',data={'at':time.time()}))
            for goal in s.scalars(select(PersonalGoal).where(PersonalGoal.status=='active')):
                if goal.data.get('dueAt') and goal.data['dueAt']<time.time():
                    goal.data={**goal.data,'overdue':True}
            for row in s.scalars(select(LifeActivity).where(LifeActivity.status.in_(['active','invited']))):
                row.status='suspended';row.updated=time.time()
                row.data={**row.data,'reason':'应用停止后暂停；等待角色重新决定。'}
        self.last_tick=time.monotonic()

    def quiet(self, now=None):
        cfg=self.r.control()['settings']
        from .local_clock import snapshot
        hour=now.hour if now else snapshot()['hour']
        start,end=cfg['quietStart'],cfg['quietEnd']
        return start<=hour<end if start<end else hour>=start or hour<end if start>end else False

    def busy_actors(self):
        return {a for run in self.r.c.store.list(all_rows=True)
            if run.get('mode')!='chat' and run['status'] not in {'completed','cancelled','failed'}
            for a in {run['actorId'],*(t['actorId'] for t in run.get('assignments',[]))}}

    def suspend_busy(self):
        busy=self.busy_actors()
        with session_scope() as s:
            for row in s.scalars(select(LifeActivity).where(LifeActivity.status=='active')):
                if busy.intersection(row.data['participants']):
                    row.status='suspended';row.data={**row.data,'reason':'已承诺的任务优先。'}
                    row.updated=time.time()

    def activities(self, actor_id):
        with session_scope() as s:
            self.r.mind.state(s,actor_id)
            rows=s.scalars(select(LifeActivity).order_by(LifeActivity.updated.desc())).all()
            return [{'id':r.id,'actorId':r.actor_id,'status':r.status,**r.data}
                for r in rows if actor_id in r.data['participants']][:30]

    def events(self, actor_id, before=None, limit=20):
        with session_scope() as s:
            self.r.mind.state(s,actor_id)
            q=select(LifeRecord).order_by(LifeRecord.seq.desc())
            if before:q=q.where(LifeRecord.seq<int(before))
            # Filter before applying the visible page size; never return another actor's events.
            result=[]
            for row in s.scalars(q):
                visible=(row.data.get('actorId')==actor_id if row.data.get('kind')=='contact'
                    else actor_id in row.data.get('participants',[]))
                if visible:
                    result.append({'seq':row.seq,**row.data})
                    if len(result)>=min(50,max(1,limit)):break
            return result

    def _event(self, s, eid, actor_id, participants, kind, text, activity_id='', **extra):
        from .mind_runtime import key
        if s.scalar(select(LifeRecord).where(LifeRecord.id==eid)):return
        if kind=='contact' and participants!=[actor_id]:
            raise ValueError('频道发言只属于实际发言者。')
        event=LifeEvent(id=eid,actorId=actor_id,participants=participants,kind=kind,text=text,at=time.time(),activityId=activity_id)
        record=LifeRecord(id=eid,data={**event.model_dump(),**extra})
        s.add(record);s.flush()
        for aid in participants:
            state=self.r.mind.state(s,aid)
            if not state.enabled:continue
            experience_id=key('life',eid,aid)
            s.add(Experience(id=experience_id,actor_id=aid,source_seq=-record.seq,kind='simulation',text=text,
                at=event.at,data={'sourceKind':'simulation','eventId':eid,'activityId':activity_id,
                    'scope':'personal_observation','peers':[a for a in participants if a!=aid],
                    'conversationId':extra.get('conversationId',''),'shareable':extra.get('shareable',False),
                    'mindReflection':'pending','importance':2,'observation':'模拟世界中发生的活动，不是现实工具证据'}))
            state.version+=1
            s.add(MindOutbox(id=experience_id,payload={'actorId':aid,'sourceId':experience_id,'eventId':eid,'version':state.version}))

    def start(self, actor_id, intent, decision_id):
        from .mind_runtime import key
        action=intent['action']
        if action not in self.ACTIONS:raise ValueError('自主生活不能执行工具或未知行动。')
        target=intent.get('targetId') if action=='invite' else None
        participants=[actor_id]
        if target:
            if target==actor_id:raise ValueError('不能邀请自己。')
            participants.append(target)
        elif action=='invite':raise ValueError('邀请需要具体同伴。')
        if self.busy_actors().intersection(participants):raise ValueError('参与者有尚未完成的任务承诺。')
        activity_id=key('activity',decision_id)
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            prior=s.get(LifeActivity,activity_id)
            if prior:return activity_id
            for aid in participants:
                actor=s.get(Actor,aid)
                if not actor or not actor.is_active or actor.kind!='agent' or not self.r.mind.state(s,aid).enabled:
                    raise ValueError('参与者不可用。')
            for other in s.scalars(select(LifeActivity).where(LifeActivity.status.in_(['active','invited','suspended']))):
                if set(participants).intersection(other.data['participants']):raise ValueError('人物已有未结束的活动。')
            goal_id=intent.get('goalId','')
            if goal_id:
                goal=s.get(PersonalGoal,goal_id)
                if not goal or goal.actor_id!=actor_id or goal.status!='active':raise ValueError('目标不可用。')
            status='invited' if target else 'active'
            row=LifeActivity(id=activity_id,actor_id=actor_id,status=status,updated=time.time(),
                data={'kind':action,'title':self.ACTIONS[action], 'purpose':intent['purpose'],
                    'participants':participants,'accepted':[actor_id],'goalId':goal_id,
                    'onlineSeconds':0,'durationSeconds':120,'reason':'','decisionId':decision_id})
            s.add(row)
            self._event(s,key(activity_id,'start'),actor_id,[actor_id],status,
                ('提出共同交流邀请，等待对方决定。' if target else '开始'+self.ACTIONS[action]+'。'),activity_id)
        self.r.mind.flush_outbox()
        return activity_id

    def respond(self, activity_id, actor_id, accept, reason):
        from .mind_runtime import key
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            row=s.get(LifeActivity,activity_id)
            if not row or row.status!='invited' or actor_id not in row.data['participants'] or actor_id==row.actor_id:
                raise ValueError('邀请不存在或已经处理。')
            if self.busy_actors().intersection(row.data['participants']):raise ValueError('已有任务承诺，不能接受活动。')
            row.data={**row.data,'accepted':list(dict.fromkeys([*row.data['accepted'],actor_id])) if accept else row.data['accepted'],
                'response':reason[:400]}
            row.status='active' if accept else 'declined';row.updated=time.time()
            self._event(s,key(row.id,'response'),actor_id,row.data['participants'],'accepted' if accept else 'declined',
                '双方同意开始共同交流。' if accept else '同伴暂未接受共同交流邀请。',row.id)
        self.r.mind.flush_outbox()

    def transition(self, activity_id, actor_id, action, reason):
        from .mind_runtime import key
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            row=s.get(LifeActivity,activity_id)
            if not row or actor_id not in row.data['participants']:raise ValueError('活动不可见。')
            if row.status in {'completed','cancelled','declined'}:return
            if action=='continue':
                if self.busy_actors().intersection(row.data['participants']):raise ValueError('任务优先。')
                goal=s.get(PersonalGoal,row.data.get('goalId',''))
                if goal and goal.status!='active':raise ValueError('关联目标已暂停或结束。')
                row.status='active' if set(row.data['accepted'])==set(row.data['participants']) else 'invited'
            elif action=='finish':
                if row.status!='active' or row.data['onlineSeconds']<row.data['durationSeconds']:
                    raise ValueError('活动还没有足够在线进度。')
                if set(row.data['accepted'])!=set(row.data['participants']):raise ValueError('同伴尚未接受。')
                if row.data['kind']=='invite' and len({v['actorId'] for v in row.data.get('dialogue',[])})<2:
                    raise ValueError('共同交流还没有双方实际发言。')
                row.status='completed'
                eid=key(row.id,'complete')
                self._event(s,eid,actor_id,row.data['participants'],'completed','完成'+row.data['title']+'。',row.id)
                goal=s.get(PersonalGoal,row.data.get('goalId',''))
                if goal:
                    goal.data={**goal.data,'evidence':list(dict.fromkeys([*goal.data.get('evidence',[]),eid])),
                        'lastProgress':reason[:400],'updatedAt':time.time()}
            elif action=='pause':row.status='suspended'
            elif action=='end':row.status='cancelled'
            else:raise ValueError('活动操作无效。')
            row.updated=time.time();row.data={**row.data,'reason':reason[:400]}
        self.r.mind.flush_outbox()

    def shutdown(self):
        with session_scope() as s:
            for row in s.scalars(select(LifeActivity).where(LifeActivity.status.in_(['active','invited']))):
                row.status='suspended';row.updated=time.time()
                row.data={**row.data,'reason':'应用停止，保留在线进度。'}

    async def converse(self, activity, actor_id, decision):
        from .mind_runtime import key, MindInterrupted
        from .expression import validate
        source=key(activity['id'],actor_id,len(activity.get('dialogue',[])))
        revision=self.r.revision
        profile=self.r.profile(actor_id)
        value=await self.r.call(actor_id,'express',{'profile':profile['interpretation'],
            'intent':decision['intent'],'history':activity.get('dialogue',[]),
            'sourceId':source,'scene':'参与者通过港区终端交流，不替他人说话或行动。'},background=True,revision=revision)
        segments=validate(value,source)
        if self.r.background_blocked(revision):raise MindInterrupted('共同交流已被打断。')
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            row=s.get(LifeActivity,activity['id'])
            if row.status!='active' or len(row.data.get('dialogue',[]))!=len(activity.get('dialogue',[])):
                raise MindInterrupted('交流状态已变化。')
            text='\n\n'.join(segments)
            row.data={**row.data,'dialogue':[*row.data.get('dialogue',[]),{'actorId':actor_id,'text':text,'id':source}]}
            self._event(s,source,actor_id,row.data['participants'],'dialogue',text,row.id)
        self.r.mind.flush_outbox()

    def pulse(self):
        now=time.monotonic()
        elapsed=min(30,max(0,now-(self.last_tick or now)))
        self.last_tick=now
        self.suspend_busy()
        with session_scope() as s:
            wall_now=time.time()
            for goal in s.scalars(select(PersonalGoal).where(PersonalGoal.status=='active')):
                overdue=bool(goal.data.get('dueAt') and goal.data['dueAt']<wall_now)
                if bool(goal.data.get('overdue'))!=overdue:
                    goal.data={**goal.data,'overdue':overdue}
        if self.r.control()['settings']['paused']:return
        with session_scope() as s:
            for row in s.scalars(select(LifeActivity).where(LifeActivity.status=='active')):
                row.data={**row.data,'onlineSeconds':min(86400,row.data['onlineSeconds']+elapsed)}
                row.updated=time.time()

    def can_contact(self, actor_id, kind):
        if self.quiet():return False
        cfg=self.r.control()['settings']
        if not cfg['proactive'] or cfg['paused']:return False
        with session_scope() as s:
            row=s.get(MindControl,'contacts')
            contacts=[v for v in (row.data.get('items',[]) if row else []) if v['at']>time.time()-3600]
        if kind=='dm':
            return sum(v['kind']=='dm' for v in contacts)<cfg['dmHourly'] and sum(v['kind']=='dm' and v['actorId']==actor_id for v in contacts)<cfg['actorDmHourly']
        return sum(v['kind']=='group' for v in contacts)<cfg['groupHourly']

    async def contact(self, actor_id, decision):
        from .mind_runtime import key, MindInterrupted
        from .expression import validate
        cid=decision['intent'].get('targetId') or 'dm-'+actor_id
        with session_scope() as s:
            room=s.get(Conversation,cid)
            if not room or (room.extra_json or {}).get('archived') or (room.extra_json or {}).get('social',{}).get('muted'):
                return
            members=set(s.scalars(select(ConversationMember.actor_id).where(ConversationMember.conversation_id==cid,ConversationMember.is_active.is_(True))))
            if not {actor_id,'commander'}<=members:return
            kind=room.kind
        if not self.can_contact(actor_id,kind):return
        source=key('contact',decision['id'])
        # Only solo activities may seed unsolicited disclosure. No private peer dialogue is supplied.
        events=[e for e in self.events(actor_id) if e['participants']==[actor_id] and e['kind']=='completed'][:2]
        profile=self.r.profile(actor_id)
        revision=self.r.revision
        value=await self.r.call(actor_id,'express',{'profile':profile['interpretation'],
            'intent':'有相关内容才分享自己的近况，或简短关心对方；不催促回复，不讲其他人的私事。',
            'facts':events,'sourceId':source},background=True,revision=revision)
        segments=validate(value,source)
        if self.r.background_blocked(revision) or not self.can_contact(actor_id,kind):return
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            if s.get(Message,source):return
            room=s.get(Conversation,cid)
            if not room or (room.extra_json or {}).get('archived') or (room.extra_json or {}).get('social',{}).get('muted'):return
            active=set(s.scalars(select(ConversationMember.actor_id).where(ConversationMember.conversation_id==cid,ConversationMember.is_active.is_(True))))
            if not {actor_id,'commander'}<=active:return
            service=self.r.c.social_engine.service
            service._append_message(s,room,actor_id,'text','\n\n'.join(segments),
                metadata={'expression':True,'proactive':True,'sourceIds':[source]},message_id=source)
            control=s.get(MindControl,'contacts')
            if not control:control=MindControl(id='contacts',data={'items':[]});s.add(control)
            control.data={'items':[*[v for v in control.data['items'] if v['at']>time.time()-3600],
                {'actorId':actor_id,'kind':kind,'at':time.time(),'messageId':source}]}
            self._event(s,key(source,'said'),actor_id,[actor_id],'contact',
                '在频道发出文字：'+'\n\n'.join(segments),conversationId=cid)
        self.r.mind.flush_outbox()
        self.r.c.store.event(None,'workspace.changed',{'conversationId':cid})
        if kind=='group':
            from .social_models import SocialTopic
            tid=key(source,'topic')
            with session_scope() as s:
                if not s.get(SocialTopic,tid):
                    s.add(SocialTopic(id=tid,conversation_id=cid,status='active',version=0,
                        data={'runId':None,'proactive':True,'prompt':'自然承接刚才的话题；没有补充就不发言。',
                            'spoken':1,'invites':0,'recent':[actor_id],'maxSpoken':6}))
            await self.r.c.social_engine.exchange(tid,is_busy=lambda:self.r.background_blocked(revision),background=True)

    async def tick(self):
        from .mind_runtime import MindInterrupted
        if not self.r.enabled:return
        self.pulse()
        self.r.decay()
        if self.r.c.shutting_down or self.r.c.tasks:return
        async with self.r.background_gate:
            # User-queued research is independent of an actor's optional-life
            # cooldown, including when every actor has disabled personal growth.
            research=self.r.c.relationship_research
            if research.has_pending() and time.monotonic()-getattr(self,'last_research',-30)>=30:
                self.last_research=time.monotonic()
                token=self.r.background_context.set(self.r.revision)
                try:await research.tick()
                finally:self.r.background_context.reset(token)
                return
            if self.r.background_blocked():return
            with session_scope() as s:
                actors=s.scalars(select(Actor).where(Actor.kind=='agent',Actor.is_active.is_(True))).all()
                control=s.get(MindControl,'queue')
                if not control:control=MindControl(id='queue',data={'attempts':{},'turn':0});s.add(control)
                attempts=control.data.get('attempts',{})
                wake=control.data.get('wake',{})
                now=time.time()
                eligible=[a for a in actors if self.r.mind.state(s,a.id).enabled and a.id not in self.busy_actors()
                          and (now-attempts.get(a.id,0)>120 or now<attempts.get(a.id,0))
                          and self.ready(a.id,wake.get(a.id,{}),now)]
                if not eligible:return
                actor=min(eligible,key=lambda a:attempts.get(a.id,0))
                aid=actor.id
                turn=control.data.get('turn',0)+1
                control.data={**control.data,'attempts':{**attempts,aid:now},'turn':turn}
            try:
                if turn%7==0 and os.getenv('AZURJUUS_SKILL_TRIALS_ENABLED','1')=='1':
                    token=self.r.background_context.set(self.r.revision)
                    try:
                        await self.r.c.growth.tick(lambda:self.r.background_blocked())
                    finally:self.r.background_context.reset(token)
                    return
                if turn%3==0 and os.getenv('AZURJUUS_REFLECTION_ENABLED','1')=='1':
                    await self.r.reflect()
                    return
                activities=[a for a in self.activities(aid) if a['status'] in {'active','invited','suspended'}]
                activity=activities[0] if activities else None
                if activity:
                    if activity['status']=='invited':
                        if aid==activity['actorId']:return
                        actions=['accept','decline','wait']
                    elif activity['status']=='suspended':
                        actions=['end','wait']
                        goal=next((g for g in self.r.goals(activity['actorId']) if g['id']==activity.get('goalId')),None)
                        if not goal or goal['status']=='active':actions.insert(0,'continue')
                    else:
                        dialogue=activity.get('dialogue',[])
                        if activity['kind']=='invite' and dialogue and dialogue[-1]['actorId']==aid:return
                        complete=activity['onlineSeconds']>=activity['durationSeconds'] and (activity['kind']!='invite' or len(dialogue)>=2)
                        actions=['wait','pause','end']+(['finish'] if complete else [])
                        if activity['kind']=='invite' and len(dialogue)<6:actions.append('speak')
                    prompt='决定如何处理当前模拟活动：'+json.dumps(activity,ensure_ascii=False)
                else:
                    actions=['read','practice','rest','reflect','invite','wait']
                    with session_scope() as s:
                        directory=[{'id':a.id,'name':a.name} for a in s.scalars(select(Actor).where(Actor.kind=='agent',Actor.is_active.is_(True)))]
                        rooms=[{'id':room.id,'kind':room.kind,'title':room.title} for room in s.scalars(select(Conversation).join(ConversationMember,
                            ConversationMember.conversation_id==Conversation.id).where(ConversationMember.actor_id==aid,ConversationMember.is_active.is_(True)))
                            if not (room.extra_json or {}).get('archived')]
                    rooms=[room for room in rooms if self.can_contact(aid,room['kind'])]
                    if rooms:actions.append('contact')
                    prompt='根据自己的兴趣和目标选择下一项轻量生活活动。可以等待；没有合适目标时可提出一个有背景依据的长期目标。邀请只使用人物编号，主动联系 targetId 使用频道编号。模拟活动不授予现实能力。\n'+json.dumps({'directory':directory,'channels':rooms},ensure_ascii=False)
                # Keep unexecuted plans across quota failures and restarts. Re-evaluate
                # against current state, never backfill events from the missed interval.
                with session_scope() as s:
                    pending=s.get(MindControl,'life-work:'+aid)
                    activity_id=activity['id'] if activity else ''
                    if not pending:
                        pending=MindControl(id='life-work:'+aid,data={});s.add(pending)
                    if pending.data.get('activityId')!=activity_id or not pending.data.get('source'):
                        pending.data={'source':'life-tick:'+str(turn)+':'+aid,'activityId':activity_id}
                    source=pending.data['source']
                decision=await self.r.think(aid,source,prompt,mode='life',background=True,actions=actions,
                    public=bool(activity and activity['kind']=='invite'))
                if not decision:return
                intent=decision['intent'];action=intent['action']
                if action=='wait':pass
                elif activity:
                    if action=='speak':await self.converse(activity,aid,decision)
                    elif action in {'accept','decline'}:self.respond(activity['id'],aid,action=='accept',intent['purpose'])
                    else:self.transition(activity['id'],aid,action,intent['purpose'])
                elif action=='contact':await self.contact(aid,decision)
                else:self.start(aid,intent,decision['id'])
                self.defer(aid,action)
                with session_scope() as s:s.execute(delete(MindControl).where(MindControl.id=='life-work:'+aid))
            except MindInterrupted:
                return
            except Exception as exc:
                self.defer(aid,'failed')
                self.r.c.store.event(None,'mind.background_failed',{'actorId':aid,'error':type(exc).__name__})

    def stimulus(self, actor_id):
        # Decay and the actor's own cognition revisions are not new stimuli.
        with session_scope() as s:
            row=s.scalar(select(Experience).where(Experience.actor_id==actor_id,Experience.forgotten.is_(False))
                .order_by(Experience.at.desc(),Experience.id.desc()).limit(1))
            goals=s.scalars(select(PersonalGoal).where(PersonalGoal.actor_id==actor_id)).all()
            profile=s.get(MindProfile,actor_id)
            return [row.id if row else '', row.text if row else '', profile.version if profile else 0,
                sorted([g.id,g.status,g.data.get('updatedAt',0),bool(g.data.get('overdue'))] for g in goals)]

    def ready(self, actor_id, wake, now):
        if wake and 0<wake.get('at',0)-now<=3600 and wake.get('stimulus')==self.stimulus(actor_id):return False
        activities=[a for a in self.activities(actor_id) if a['status'] in {'active','invited','suspended'}]
        if activities:
            a=activities[0]
            if a['status']=='invited' and a['actorId']==actor_id:return False
            if a['status']=='active' and a['kind']!='invite' and a['onlineSeconds']<a['durationSeconds']:return False
        return True

    def defer(self, actor_id, action):
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='queue').values(id='queue'))
            row=s.get(MindControl,'queue')
            if not row:return
            old=row.data.get('wake',{}).get(actor_id,{})
            stalls=min(4,old.get('stalls',0)+1) if action in {'wait','failed'} else 0
            delay=min(3600,300*2**max(0,stalls-1)) if stalls else 120
            row.data={**row.data,'wake':{**row.data.get('wake',{}),actor_id:{
                'at':time.time()+delay,'stalls':stalls,'reason':action,'stimulus':self.stimulus(actor_id)}}}
