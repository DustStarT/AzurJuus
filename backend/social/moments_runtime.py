"""Online, event-driven Moments decisions sharing the life model budget."""
from __future__ import annotations

import re
import time
import hashlib
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, update

from backend.database import session_scope
from backend.models import Actor, SocialPost, SocialComment, WorkspaceSetting
from backend.mind.mind_models import MindControl, LifeRecord
from backend.platform.local_clock import snapshot as local_time
from backend.social.social_models import SocialPublication, SocialShareConsent


class PostDraft(BaseModel):
    kind: Literal['thought', 'event', 'new_activity'] = 'thought'
    body: str = Field(default='', max_length=4000)
    audienceKind: Literal['port', 'selected'] = 'port'
    selectedIds: list[str] = Field(default_factory=list, max_length=24)
    activityKind: Literal['read','practice','rest','reflect','invite','stroll','meal','hobby'] | None = None
    targetId: str | None = None


class CommentDraft(BaseModel):
    body: str = Field(min_length=1, max_length=500)


class MomentsRuntime:
    KEY='moments-control'
    # First opportunity arrives during a normal session; later opportunities
    # remain sparse. The hash spreads actors out without a polling model call.
    FIRST_THOUGHT_SECONDS=30*60
    FIRST_THOUGHT_SPREAD_SECONDS=90*60
    THOUGHT_INTERVAL_SECONDS=24*3600
    RESTART_GRACE_SECONDS=5*60

    def __init__(self, coordinator, service):
        self.c=coordinator
        self.service=service
        self.runtime=coordinator.cognition.runtime

    def initialize(self, *, restart=False):
        with session_scope() as session:
            row=session.get(MindControl,self.KEY)
            latest=session.scalar(select(LifeRecord.seq).order_by(LifeRecord.seq.desc()).limit(1)) or 0
            if not row:
                session.add(MindControl(id=self.KEY,data={'lastLifeSeq':latest,'startAt':time.time(),'pending':[],
                    'processed':[],'thoughtAt':{},'daily':{},'retry':{},'retryCounts':{}}))
            elif restart:
                # A fresh online decision may be due, but never publish an
                # offline backlog immediately on opening the application.
                row.data={**row.data,'lastLifeSeq':latest,'pending':[],'retry':{},'retryCounts':{},
                    'resumeThoughtAt':time.time()+self.RESTART_GRACE_SECONDS}

    def _thought_due_at(self, data, actor_id):
        last=data.get('thoughtAt',{}).get(actor_id)
        if last is not None:
            return last+self.THOUGHT_INTERVAL_SECONDS
        spread=int.from_bytes(hashlib.sha256(actor_id.encode()).digest()[:4],'big') % self.FIRST_THOUGHT_SPREAD_SECONDS
        return data.get('startAt',time.time())+self.FIRST_THOUGHT_SECONDS+spread

    def status(self):
        """Explain an empty feed without starting an autonomous model call."""
        self.initialize()
        with session_scope() as session:
            data=dict(session.get(MindControl,self.KEY).data)
            from backend.characters.character_identity import roster_actors
            actors=[actor.id for actor in roster_actors(session)]
            setting=session.get(WorkspaceSetting,1)
        cfg=self.runtime.control()
        now=time.time()
        due=min((self._thought_due_at(data,actor_id) for actor_id in actors),default=None)
        if due is not None:
            due=max(due,data.get('resumeThoughtAt',0))
        if not self.runtime.enabled:reason='mind_disabled'
        elif cfg['settings']['paused']:reason='paused'
        elif not cfg['settings']['momentsEnabled'] or not (setting and setting.allow_idle_social):reason='disabled'
        elif not actors:reason='no_actors'
        elif self.runtime.life.quiet():reason='quiet'
        elif self.runtime.background_blocked():reason='busy'
        elif cfg['callsLastHour']>=cfg['settings']['hourlyCalls']:reason='budget'
        elif data.get('pending'):reason='pending'
        else:reason='waiting'
        return {'reason':reason,'nextConsiderAt':due,'pendingCount':len(data.get('pending',[])),
            'consideredCount':len(data.get('thoughtAt',{}))}

    def _change(self, callback):
        with session_scope() as session:
            session.execute(update(MindControl).where(MindControl.id==self.KEY).values(id=self.KEY))
            row=session.get(MindControl,self.KEY)
            data=dict(row.data)
            callback(data)
            row.data=data

    def _observe(self, source_id, author_id, text, kind, audience, *, owner_only=False, shareable=False):
        targets=[author_id] if owner_only and author_id!='commander' else audience
        for actor_id in dict.fromkeys(targets):
            if actor_id=='commander':continue
            self.c.store.event(None,'mind.observation',{'key':'moments:'+kind+':'+source_id+':'+actor_id,
                'actorIds':[actor_id],'kind':'social','text':text[:2000],
                'data':{'sourceKind':'social.'+kind,'speakerId':author_id,
                    'ownSpeech':actor_id==author_id,'scope':'audience_visible',
                    'postId':source_id,'importance':1,'shareable':shareable}})
        self.c.cognition.pump()

    def notify_post(self, post_id):
        self.initialize()
        with session_scope() as session:
            post=session.get(SocialPost,post_id)
            publication=session.get(SocialPublication,post_id)
            if not post or not publication:return
            audience=self.service.audience_actor_ids(session,post)
            text=publication.data['body']
            author=post.author_id
            shareable=publication.data.get('audience',{}).get('kind')=='port'
        self._observe(post_id,author,text,'post',audience,shareable=shareable)
        def update(data):
            key='post:'+post_id
            if key not in data['pending']:data['pending']=[*data['pending'],key][-100:]
        self._change(update)
        self.c.store.event(None,'workspace.changed',{})

    def notify_comment(self, comment_id):
        self.initialize()
        with session_scope() as session:
            comment=session.get(SocialComment,comment_id)
            if not comment:return
            post=session.get(SocialPost,comment.post_id)
            publication=session.get(SocialPublication,comment.post_id)
            audience=self.service.audience_actor_ids(session,post)
            author=comment.author_id
            text=comment.body
            shareable=bool(publication and publication.data.get('audience',{}).get('kind')=='port')
        self._observe(comment_id,author,text,'comment',audience,shareable=shareable)
        def update(data):
            key='comment:'+comment_id
            if key not in data['pending']:data['pending']=[*data['pending'],key][-100:]
        self._change(update)
        self.c.store.event(None,'workspace.changed',{})

    def notify_like(self, post_id, actor_id='commander'):
        with session_scope() as session:
            post=session.get(SocialPost,post_id)
            if not post or post.author_id=='commander':return
            author=post.author_id
        self._observe(post_id+':'+actor_id,actor_id,'对动态点了赞。','like',[author],owner_only=False)

    def _collect(self):
        def scan(data):
            with session_scope() as session:
                rows=session.scalars(select(LifeRecord).where(
                    LifeRecord.seq>data.get('lastLifeSeq',0)).order_by(LifeRecord.seq)).all()
            pending=list(data.get('pending',[]))
            for row in rows:
                if row.data.get('kind')=='completed':pending.append('event:'+row.id)
                data['lastLifeSeq']=row.seq
            data['pending']=list(dict.fromkeys(pending))[-100:]
        self._change(scan)

    def _daily(self, data):
        today=local_time()['date']
        daily=dict(data.get('daily') or {})
        if daily.get('date')!=today:daily={'date':today,'posts':{},'interactions':{}}
        return daily

    def _count_in_session(self,session,actor_id,field):
        """Commit the quota use with the actual post/reaction, even across crashes."""
        session.execute(update(MindControl).where(MindControl.id==self.KEY).values(id=self.KEY))
        row=session.get(MindControl,self.KEY)
        data=dict(row.data)
        daily=self._daily(data)
        counts=dict(daily[field]);counts[actor_id]=counts.get(actor_id,0)+1
        daily[field]=counts
        row.data={**data,'daily':daily}

    def _permitted(self):
        if not self.runtime.enabled or self.runtime.background_blocked() or self.runtime.life.quiet():return False
        cfg=self.runtime.control()['settings']
        if cfg['paused'] or not cfg['momentsEnabled']:return False
        with session_scope() as session:
            settings=session.get(WorkspaceSetting,1)
            return bool(settings and settings.allow_idle_social)

    async def tick(self):
        if not self._permitted():return
        self.initialize()
        self._collect()
        with session_scope() as session:
            data=dict(session.get(MindControl,self.KEY).data)
            from backend.characters.character_identity import roster_actors
            actors=[a.id for a in roster_actors(session)]
        cfg=self.runtime.control()['settings']
        daily=self._daily(data)
        now=time.time()
        pending=list(data.get('pending',[]))
        if now>=data.get('resumeThoughtAt',0):
            # Due actors are independent. A busy actor or a delayed retry must
            # not starve everyone else behind the first candidate.
            pending.extend('thought:'+aid for aid in actors if now>=self._thought_due_at(data,aid))
        for key in pending:
            if data.get('retry',{}).get(key,0)>now:continue
            kind, _, identifier=key.partition(':')
            if kind=='post':
                with session_scope() as session:
                    post=session.get(SocialPost,identifier)
                    audience=self.service.audience_actor_ids(session,post) if post else []
                candidates=[aid for aid in audience if aid!=post.author_id] if post else []
                candidates=[aid for aid in candidates if key+':'+aid not in data.get('processed',[])]
                candidates=[aid for aid in candidates if aid not in self.c.busy_actor_ids()]
                if not candidates:
                    self._finish(key)
                    continue
                actor_id=candidates[0]
                if daily['interactions'].get(actor_id,0)>=cfg['momentsActorDailyInteractions'] or sum(daily['interactions'].values())>=cfg['momentsDailyInteractions']:
                    self._finish(key,actor_id)
                    continue
                await self._attempt(key,actor_id,lambda: self._interact(actor_id,identifier,key))
                return
            if kind=='comment':
                with session_scope() as session:
                    comment=session.get(SocialComment,identifier)
                    post=session.get(SocialPost,comment.post_id) if comment else None
                actor_id=post.author_id if post and post.author_id!='commander' else ''
                if not actor_id or actor_id==comment.author_id or key+':'+actor_id in data.get('processed',[]):
                    self._finish(key)
                    continue
                if actor_id in self.c.busy_actor_ids():continue
                if daily['interactions'].get(actor_id,0)>=cfg['momentsActorDailyInteractions'] or sum(daily['interactions'].values())>=cfg['momentsDailyInteractions']:
                    self._finish(key,actor_id)
                    continue
                await self._attempt(key,actor_id,lambda: self._interact(actor_id,post.id,key,comment.body))
                return
            actor_id=identifier if kind=='thought' else ''
            event=None
            if kind=='event':
                with session_scope() as session:
                    event=session.scalar(select(LifeRecord).where(LifeRecord.id==identifier))
                    event=dict(event.data) if event else None
                actor_id=event.get('actorId','') if event else ''
            if not actor_id or actor_id not in actors:
                self._finish(key)
                continue
            if actor_id in self.c.busy_actor_ids():continue
            if daily['posts'].get(actor_id,0)>=cfg['momentsActorDailyPosts'] or sum(daily['posts'].values())>=cfg['momentsDailyPosts']:
                self._finish(key)
                continue
            await self._attempt(key,actor_id,lambda: self._post(actor_id,key,event))
            return

    def _finish(self,key,actor_id=''):
        def change(data):
            if actor_id:data['processed']=[*data.get('processed',[]),key+':'+actor_id][-600:]
            if key.startswith(('event:','thought:')) or key.startswith('comment:') or (
                    key.startswith('post:') and not actor_id):
                data['pending']=[item for item in data.get('pending',[]) if item!=key]
            if key.startswith('thought:'):
                times=dict(data.get('thoughtAt',{}));times[key.split(':',1)[1]]=time.time()
                data['thoughtAt']=times
            daily=self._daily(data)
            data['daily']=daily
            retry=dict(data.get('retry',{}));retry.pop(key,None);data['retry']=retry
            counts=dict(data.get('retryCounts',{}));counts.pop(key,None);data['retryCounts']=counts
        self._change(change)

    async def _attempt(self,key,actor_id,operation):
        from backend.mind.mind_runtime import MindInterrupted
        try:
            await operation()
            self._finish(key,actor_id)
        except Exception as exc:
            self.c.store.event(None,'moments.failed',{'actorId':actor_id,'kind':key.split(':',1)[0],
                'error':type(exc).__name__})
            def delay(data):
                retry=dict(data.get('retry',{}))
                counts=dict(data.get('retryCounts',{}))
                counts[key]=min(7,counts.get(key,0)+1)
                retry[key]=time.time()+min(3600,60*(2**(counts[key]-1)))
                data['retry']=retry
                data['retryCounts']=counts
            self._change(delay)

    async def _share_approved(self,event,actor_id):
        participants=list(dict.fromkeys(event.get('participants',[])))
        if set(participants)=={actor_id}:return True
        if actor_id not in participants:return False
        for peer_id in participants:
            consent_id='share-'+hashlib.sha256((event['id']+':'+peer_id).encode()).hexdigest()[:32]
            with session_scope() as session:
                existing=session.get(SocialShareConsent,consent_id)
            if existing:
                if existing.status!='accepted':return False
                continue
            if peer_id==actor_id:
                accepted=True
            else:
                if peer_id in self.c.busy_actor_ids():
                    from backend.mind.mind_runtime import MindInterrupted
                    raise MindInterrupted('参与者正在执行任务。')
                decision=await self.runtime.think(peer_id,'moments-share:'+consent_id,
                    '同伴想在朋友圈分享共同活动。请独立决定是否同意公开；拒绝也可以。活动：'+event['text'],
                    mode='life-share',background=True,actions=['accept','decline'],
                    include_task_memory=False)
                accepted=decision['intent']['action']=='accept'
            with session_scope() as session:
                if not session.get(SocialShareConsent,consent_id):
                    session.add(SocialShareConsent(id=consent_id,event_id=event['id'],
                        actor_id=peer_id,status='accepted' if accepted else 'declined',at=time.time()))
            if not accepted:return False
        return True

    async def _post(self,actor_id,key,event):
        revision=self.runtime.revision
        source='moments-decision:'+key+':'+actor_id
        decision=await self.runtime.think(actor_id,source,
            '决定是否在朋友圈分享。只是想法可表达观点；声称新经历时先提出受校验的生活活动，不能直接编造已经发生。'
            +('当前已发生的活动：'+event['text'] if event else '当前没有待分享的已发生活动。'),
            mode='moments',background=True,actions=['speak','wait'],include_task_memory=False)
        if decision['intent']['action']!='speak':return 'wait'
        profile=self.runtime.profile(actor_id)
        draft=await self.runtime.call(actor_id,'moments_post',{
            'profile':profile['interpretation'],'intent':decision['intent'],
            'event':event,'allowedKinds':['event'] if event else ['thought','new_activity'],
            'rule':'只写本人会发表的短动态。thought 只能表达观点或打算，不能声称刚完成未经记录的活动。event 只能依据给定事件。new_activity 不发布，交给生活状态机。受众 selected 仅用真实人物编号。',
        },PostDraft,background=True,revision=revision)
        if self.runtime.background_blocked(revision):return 'wait'
        if draft.kind=='new_activity':
            if event or not draft.activityKind:return 'wait'
            self.runtime.life.start(actor_id,{'action':draft.activityKind,
                'purpose':decision['intent']['purpose'],'targetId':draft.targetId},decision['id'])
            return 'activity'
        if bool(event)!=(draft.kind=='event'):raise ValueError('动态与活动来源不一致。')
        body=draft.body.strip()
        if not body:return 'wait'
        if not event and re.search(
                r'(?:刚才|刚刚|今天|昨日|昨天|昨晚|今早|早上|午后|傍晚|我已经).{0,36}'
                r'(?:读了|读完|看完|练了|练习了|去了|散步了|散了会步|逛了|完成了|做完了|见到了|遇到了|聊了|吃了|喝了|休息了|整理了|学会了)',body):
            raise ValueError('没有对应模拟事件，不能声称活动已经发生。')
        if any(name not in (event or {}).get('text','') for name in re.findall(
                r'[\w\u4e00-\u9fff.-]+\.(?:pdf|epub|txt|docx?|xlsx?)(?![A-Za-z0-9])',body,re.I)):
            raise ValueError('动态不能声称读取未记录的具体文件。')
        if event and not await self._share_approved(event,actor_id):return 'wait'
        with session_scope() as session:
            if self.runtime.background_blocked(revision):return 'wait'
            actor=session.get(Actor,actor_id)
            if not actor or not actor.is_active:return 'wait'
            from backend.characters.character_identity import roster_actors
            if actor_id not in {member.id for member in roster_actors(session)}:return 'wait'
            audience=self.service._audience(session,{'kind':draft.audienceKind,
                'actorIds':draft.selectedIds},actor_id)
            post=self.service._create_social_post(session,author=actor,body=body,
                audience=audience,source_kind='life_event' if event else 'thought',
                source_event_id=event['id'] if event else '')
            self._count_in_session(session,actor_id,'posts')
            post_id=post.id
        self.notify_post(post_id)
        return 'post'

    async def _interact(self,actor_id,post_id,key,comment=''):
        revision=self.runtime.revision
        with session_scope() as session:
            post=session.get(SocialPost,post_id)
            if not post or not self.service.post_visible(session,post,actor_id):return 'wait'
            publication=session.get(SocialPublication,post_id)
            body=publication.data['body'] if publication else ''
        decision=await self.runtime.think(actor_id,'moments-reaction:'+key+':'+actor_id,
            '决定是否回应可见动态。action=speak 表示评论；accept 表示点赞；wait 表示不互动。'
            +'动态：'+body[:900]+('；用户评论：'+comment[:500] if comment else ''),
            mode='moments',background=True,actions=['speak','accept','wait'],include_task_memory=False)
        action=decision['intent']['action']
        if action=='wait':return 'wait'
        text=''
        if action=='speak':
            profile=self.runtime.profile(actor_id)
            draft=await self.runtime.call(actor_id,'moments_comment',{
                'profile':profile['interpretation'],'intent':decision['intent'],
                'post':body[:900],'comment':comment[:500],
                'rule':'只根据可见动态发表自然短评论；不假称自己亲历作者的活动，不重复套话。',
            },CommentDraft,background=True,revision=revision)
            text=draft.body.strip()
        if self.runtime.background_blocked(revision):return 'wait'
        with session_scope() as session:
            if self.runtime.background_blocked(revision):return 'wait'
            before=session.scalars(select(SocialComment).where(SocialComment.post_id==post_id)).all()
            changed=self.service.actor_react(session,post_id,actor_id,'comment' if action=='speak' else 'like',text)
            if changed:self._count_in_session(session,actor_id,'interactions')
            after=session.scalars(select(SocialComment).where(SocialComment.post_id==post_id)).all() if changed and action=='speak' else []
            new_id=next((c.id for c in after if c.id not in {b.id for b in before}),None)
        if not changed:return 'wait'
        if new_id:self.notify_comment(new_id)
        else:self.notify_like(post_id,actor_id)
        self.c.store.event(None,'workspace.changed',{})
        return 'interaction'
