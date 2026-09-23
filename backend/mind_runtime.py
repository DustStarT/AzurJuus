"""Unified, actor-local cognitive cycle. All model outputs are proposals."""
import asyncio
import hashlib
import json
import os
import time
from collections import defaultdict
from contextvars import ContextVar
from uuid import uuid4

from sqlalchemy import select, update

from .database import session_scope
from .models import Actor
from .cognition_models import Experience, MindState, MindOutbox
from .mind_models import MindProfile, MindDecision, PersonalGoal, MindControl, MindCall, LifeActivity
from .mind_contracts import MindFrame, DecisionIntent, StateDelta, ProfileInterpretation, GoalProposal, LifeSettings


def key(*parts):
    return hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()


class MindInterrupted(RuntimeError):
    pass


class MindRuntime:
    def __init__(self, mind, coordinator):
        self.mind, self.c = mind, coordinator
        self.enabled = os.getenv('AZURJUUS_MIND_ENABLED', '1') == '1' and mind.enabled
        self.locks = defaultdict(asyncio.Lock)
        self.background_gate = asyncio.Lock()
        self.revision = 0
        self.generate = None
        self.background_context = ContextVar('mind-background', default=None)
        from .life_runtime import LifeRuntime
        self.life = LifeRuntime(self)

    def active(self, actor_id):
        if not self.enabled:return False
        with session_scope() as s:
            actor = s.get(Actor, actor_id)
            if not actor or actor.kind != 'agent' or not actor.is_active:
                raise ValueError('人物不在当前名单中。')
            return self.enabled and self.mind.state(s, actor_id).enabled

    def control(self, patch=None):
        with session_scope() as s:
            if patch is not None:s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            row = s.get(MindControl, 'life')
            if row is None:
                from .social_models import SocialTopic
                old = s.get(SocialTopic, 'social-control')
                previous = (old.data or {}).get('settings', {}) if old else {}
                if old and old.data.get('defaultsVersion')=='life-v4' and not old.data.get('settingsEdited'):
                    previous={}
                cfg = LifeSettings(**{k:v for k,v in previous.items() if k in {'paused','hourlyCalls'}}).model_dump()
                row = MindControl(id='life', data={'settings':cfg})
                s.add(row)
            if patch is not None:
                cfg = LifeSettings(**{**row.data['settings'], **patch}).model_dump()
                row.data = {**row.data, 'settings':cfg}
                self.revision += 1
            cfg = dict(row.data['settings'])
            calls = s.scalars(select(MindCall).where(MindCall.at > time.time()-3600)).all()
            return {'enabled':self.enabled, 'settings':cfg,
                'callsLastHour':sum(bool(r.data.get('background')) for r in calls),
                'failuresLastHour':sum(r.data.get('status')=='failed' for r in calls),
                'tokensLastHour':sum((r.data.get('usage') or {}).get('total_tokens',0) or 0 for r in calls),
                'latencySeconds':sum(r.data.get('latencySeconds',0) for r in calls),
                'usageKnownCalls':sum('usage' in r.data for r in calls)}

    def interrupt(self):
        self.revision += 1
        self.life.suspend_busy()

    def background_blocked(self, revision=None):
        return (self.c.shutting_down or bool(self.c.tasks) or self.control()['settings']['paused']
                or revision is not None and self.revision != revision)

    async def call(self, actor_id, kind, payload, schema=None, background=False, revision=None, messages=None, generator=None, settings=None, validator=None):
        for attempt in range(2):
            try:
                return await self._call_once(actor_id,kind,payload,schema,background,revision,messages,generator,settings,validator)
            except ValueError as exc:
                if attempt:raise
                correction='上次输出未通过校验，请按以下错误修正，不放宽来源、权限或行动约束：'+str(exc)[:300]
                if messages:messages=[*messages,{'role':'user','content':correction}]
                else:payload={**payload,'validationCorrection':correction}

    async def _call_once(self, actor_id, kind, payload, schema=None, background=False, revision=None, messages=None, generator=None, settings=None, validator=None):
        if background and self.background_blocked(revision):
            raise MindInterrupted('后台认知已让出前台。')
        cfg = settings or self.c.settings_loader()
        if not cfg.get('llmApiKey') and not cfg.get('llmBaseUrl','').startswith(('http://127.0.0.1','http://localhost')):
            raise MindInterrupted('未配置模型，计划保持待处理。')
        self.control()
        call_id = uuid4().hex
        with session_scope() as s:
            # SQLite writer serialization makes quota reservation atomic.
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            settings = s.get(MindControl,'life').data['settings']
            if background:
                used = s.scalars(select(MindCall).where(MindCall.at > time.time()-3600)).all()
                if settings['paused'] or sum(bool(r.data.get('background')) for r in used) >= settings['hourlyCalls']:
                    raise MindInterrupted('后台调用额度已用尽，保留计划。')
            s.add(MindCall(id=call_id, actor_id=actor_id, kind=kind, at=time.time(),
                data={'background':background,'status':'running'}))
        rules = {
            'profile':'从人物背景编译可纠正的作者解释。身份与核心价值稳定；保留例外和不确定性，不能把人物背景当作真实工具能力。',
            'understand':'理解当前可见事件。区分发生的事实、角色猜测和未知。结合价值与目标评价事件；情绪须有对象与原因，不复述身世。不输出长篇内心独白。',
            'decide':'根据认知快照选择一个允许的行动。保留人物分歧与方法差异，承诺后的工作必须可靠。只有相关来源才能支持目标。不要代替他人同意，不创造工具能力。goal 只在需要新长期目标时提出，否则为 null。goalUpdate 仅在已有目标的 evidence 支持下一步或状态改变时提出；使用该目标已有 evidence 中的编号，不能把模拟活动当现实能力认证。',
            'reflect':'从给定经历提炼有限信念、关系或习惯候选。说过不等于发生过；一次事件不形成永久人格。来源与反证均必须存在。不得重写核心身份、价值或授予技能。commitments 只逐字引用当前角色自己说过的明确承诺，并给出对应经历 sourceId；不能把别人的话或背景设定当成自己的承诺。',
            'express':'按给定人物意图生成自然的远程文字消息。只输出 JSON：segments 为1至3段文字，sourceIds 原样复制指定来源。最多一张已知表情且独立成段。不泄露未分享的私事，不复述心理字段。只陈述允许公开的事实，不声称真实工具成功。',
        }
        instruction = rules.get(kind,'仅依据给定资料，返回结构化结果。')
        if kind=='decide' and payload.get('mode')=='social':
            instruction+='\n群聊参与判断优先于表达：只有尚未回答的问题、具体的新信息、真实的不同意见或新的相关问题才值得接话。最近几句已经表示赞同、总结、待命或等别人说话时，选择 wait/end；改写别人说过的话、再感谢一次、再邀请大家发言不算新内容。不要为了轮流说话而选择 speak。'
        if schema:
            instruction += '\n只返回符合此 JSON schema 的对象：'+json.dumps(schema.model_json_schema(),ensure_ascii=False)
        messages = messages or [{'role':'system','content':instruction},
                    {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
        started = time.monotonic()
        status = 'failed'
        task = None
        try:
            generator = generator or self.generate or self.c.expression.complete
            task = asyncio.create_task(generator(messages, {**cfg,'_mindCallId':call_id,'_mindStage':kind,
                '_structuredOutputTokens':cfg.get('_structuredOutputTokens',2400)}))
            while not task.done():
                await asyncio.wait({task}, timeout=.2)
                if background and self.background_blocked(revision):
                    raise MindInterrupted('前台消息打断了后台推理。')
                if time.monotonic()-started > 40:
                    raise TimeoutError('认知模型超时，状态未提交。')
            value = task.result()
            result = schema.model_validate(value) if schema else value
            if validator:validator(result)
            status = 'completed'
            return result
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            with session_scope() as s:
                row = s.get(MindCall,call_id)
                if row:
                    row.data={**row.data,'status':status,'latencySeconds':time.monotonic()-started}

    def profile(self, actor_id):
        from .terminal_characters import inspect
        from .character_behavior import behavior_context
        card = inspect(actor_id)
        with session_scope() as s:
            actor = s.get(Actor,actor_id)
            if not actor or actor.kind!='agent':
                raise ValueError('人物不存在。')
            anchor = card['text']
            sources = {'profile:'+actor_id:anchor,
                'author:'+actor_id:behavior_context(actor.source_character or actor.name)}
            for i,item in enumerate((actor.extra_json or {}).get('originalMindNotes',[])):
                sources['background:'+str(i)] = item.get('text','')
            digest = key(json.dumps(sources,sort_keys=True,ensure_ascii=False))
            row = s.get(MindProfile,actor_id)
            if row is None:
                if s.bind.dialect.name=='sqlite':
                    from sqlalchemy.dialects.sqlite import insert
                else:
                    from sqlalchemy.dialects.postgresql import insert
                s.execute(insert(MindProfile).values(actor_id=actor_id,version=1,data={}).on_conflict_do_nothing(index_elements=['actor_id']))
                row=s.get(MindProfile,actor_id)
            if row.data.get('anchorHash') != digest:
                old=dict(row.data)
                row.version += 1
                row.data = {'anchor':anchor,'anchorHash':digest,'sources':sources,'interpretation':{},
                    'status':'pending','origin':'author-interpretation','uncertainty':'资料解释不等于核实的原作事实',
                    'history':[*old.get('history',[]),{'version':row.version-1,'interpretation':old.get('interpretation',{}),'origin':old.get('origin') }][-10:]}
                if old.get('origin')=='user':row.data={**row.data,'interpretation':old['interpretation'],'origin':'user','status':'ready'}
            return {'actorId':actor_id,'version':row.version,**row.data}

    async def compile_profile(self, actor_id, background=False, revision=None):
        profile = self.profile(actor_id)
        if profile['status'] == 'ready':
            return profile
        value = await self.call(actor_id,'profile',{'sources':profile['sources']},ProfileInterpretation,background,revision)
        with session_scope() as s:
            row = s.get(MindProfile,actor_id)
            changed=s.execute(update(MindProfile).where(MindProfile.actor_id==actor_id,
                MindProfile.version==profile['version']).values(data={**row.data,
                    'interpretation':value.model_dump(),'status':'ready'}))
            if changed.rowcount!=1:
                raise MindInterrupted('人物资料已改变。')
        return self.profile(actor_id)

    def edit_profile(self, actor_id, interpretation=None):
        self.profile(actor_id)
        with session_scope() as s:
            s.execute(update(MindProfile).where(MindProfile.actor_id==actor_id).values(actor_id=actor_id))
            row=s.get(MindProfile,actor_id)
            previous={'version':row.version,'interpretation':row.data.get('interpretation',{}),'origin':row.data.get('origin')}
            row.version += 1
            row.data={**row.data,'interpretation':interpretation.model_dump() if interpretation else {},
                'status':'ready' if interpretation else 'pending','origin':'user' if interpretation else 'author-interpretation',
                'history':[*row.data.get('history',[]),previous][-10:]}
        self.revision += 1
        return self.profile(actor_id)

    def goals(self, actor_id):
        with session_scope() as s:
            self.mind.state(s,actor_id)
            return [{'id':r.id,'actorId':r.actor_id,'status':r.status,**r.data}
                for r in s.scalars(select(PersonalGoal).where(PersonalGoal.actor_id==actor_id)).all()]

    def save_goal(self, actor_id, value, goal_id=None, status='active', reason='', origin='user'):
        if status not in {'active','paused','completed','abandoned'}:
            raise ValueError('目标状态无效。')
        with session_scope() as s:
            self.mind.state(s,actor_id)
            s.execute(update(MindState).where(MindState.actor_id==actor_id).values(version=MindState.version+1))
            row = s.get(PersonalGoal,goal_id) if goal_id else None
            if goal_id and (row is None or row.actor_id != actor_id):
                raise ValueError('目标不存在。')
            if status=='active':
                count=len(s.scalars(select(PersonalGoal).where(PersonalGoal.actor_id==actor_id,PersonalGoal.status=='active')).all())
                if count >= 3 and (not row or row.status!='active'):
                    raise ValueError('每人最多三个活跃目标。')
            if row is None:
                row=PersonalGoal(id=goal_id or uuid4().hex,actor_id=actor_id,status=status,data={})
                s.add(row)
            if status in {'completed','abandoned','paused'} and not reason.strip():
                raise ValueError('请记录目标改变原因。')
            row.status=status
            row.data={**row.data,**value,'reason':reason,'origin':origin,'updatedAt':time.time(),
                      'evidence':row.data.get('evidence',[])}
            if status!='active':
                for activity in s.scalars(select(LifeActivity).where(LifeActivity.actor_id==actor_id)):
                    if activity.data.get('goalId')==row.id and activity.status in {'active','invited'}:
                        activity.status='suspended';activity.data={**activity.data,'reason':'关联目标已'+status+'，等待重新决定。'}
            result={'id':row.id,'status':status,**row.data}
        self.revision += 1
        return result

    def has_budget(self):
        state=self.control()
        return not self.background_blocked() and state['callsLastHour']<state['settings']['hourlyCalls']

    def snapshot(self, actor_id, prompt, source, conversation_id='', public=False):
        self.mind.pump()
        profile=self.profile(actor_id)
        state=self.mind.inspect(actor_id)
        goals=[g for g in self.goals(actor_id) if g['status']=='active']
        import re
        terms=set(re.findall(r'[\u4e00-\u9fff]|\w+',prompt.lower()))
        grouped={k:[] for k in ('episodic','semantic','procedural','prospective','working')}
        with session_scope() as s:
            recent=s.scalars(select(Experience).where(Experience.actor_id==actor_id,Experience.forgotten.is_(False))
                .order_by(Experience.at.desc()).limit(100)).all()
            rows=[{'id':r.id,'text':r.text,'kind':r.kind,'at':r.at,'runId':r.run_id,'data':r.data} for r in recent]
        # Public expression cannot receive memories from private channels.
        if public:
            rows=[r for r in rows if r['data'].get('shareable') or
                conversation_id and r['data'].get('conversationId')==conversation_id]
        goal_text=' '.join(g['title']+' '+g.get('nextStep','') for g in goals)
        rows.sort(key=lambda r:(bool(r['data'].get('userCorrection')),
            sum(t in r['text'].lower() for t in terms)+int(any(t in r['text'] and t in goal_text for t in terms)),
            r['data'].get('importance',0),r['at']),reverse=True)
        allowed={source,*profile['sources']}
        for r in rows[:12]:
            category='working' if r['kind']=='request' else 'episodic'
            if len(grouped[category])>=5:continue
            grouped[category].append({'id':r['id'],'text':r['text'][:500],
                'correction':r['data'].get('userCorrection'),'sourceKind':r['data'].get('sourceKind',r['kind']),
                'at':r['at'],'sessionId':r['runId'] or r['data'].get('conversationId') or r['data'].get('eventId')})
            allowed.add(r['id'])
        with session_scope() as s:
            for eid in list(allowed):
                row=s.get(Experience,eid)
                if not row:continue
                for belief in row.data.get('derived',[]):
                    if belief.get('valid',True) and (belief['kind']!='habit' or belief.get('stable')) and set(belief['sourceIds']+belief.get('counterSourceIds',[]))<=allowed:
                        category='procedural' if belief['kind']=='method' else 'semantic'
                        if len(grouped[category])<4:grouped[category].append(belief)
        grouped['prospective']=[v for v in state['data'].get('commitments',[]) if v.get('sourceId') in allowed][-4:]
        relationships=[]
        for relation in self.mind.relationships(actor_id):
            rid='relationship:'+actor_id+':'+relation['peerId']
            allowed.add(rid)
            relationships.append({'sourceId':rid,'peerId':relation['peerId'],'name':relation['name'],
                'background':relation.get('background',[]),'userDefined':relation.get('userDefined'),
                'defaultRelationship':relation.get('defaultRelationship')})
        profile_summary={k:v for k,v in profile['interpretation'].items()}
        return {'actorId':actor_id,'version':state['version'],'profileVersion':profile['version'],
            'profile':profile_summary,'background':dict(list(profile['sources'].items())[:5]),
            'memory':grouped,'goals':goals if not public else [],
            'relationships':relationships,
            'emotion':state['data'].get('emotion') if not public or set(state['data'].get('emotion',{}).get('sourceIds',[]))<=allowed else None,
            'mood':{k:v for k,v in (state['data'].get('mindMood') or {}).items() if k in {'text','intensity'}},
            'current':{'id':source,'text':prompt},'allowedSources':sorted(allowed),
            'conversationId':conversation_id}

    async def think(self, actor_id, source, prompt, *, mode='chat', conversation_id='', public=False,
                    background=False, actions=None):
        if not self.active(actor_id):return None
        queued=time.monotonic()
        async with self.locks[actor_id]:
            wait_seconds=time.monotonic()-queued
            revision=self.revision
            await self.compile_profile(actor_id,background,revision)
            did=key(actor_id,source,mode)
            snap=self.snapshot(actor_id,prompt,source,conversation_id,public)
            with session_scope() as s:
                previous=s.get(MindDecision,did)
                if (previous and previous.status=='committed' and previous.data.get('profileVersion')==snap['profileVersion']
                        and (mode!='life' or previous.data.get('stateVersion')==snap['version'])):
                    return previous.data
            allowed_actions=actions or (['execute','speak','wait'] if mode=='task' else ['speak','wait','end'])
            with session_scope() as s:
                row=s.get(MindDecision,did)
                if not row:
                    row=MindDecision(id=did,actor_id=actor_id,version=snap['version'],at=time.time(),data={})
                    s.add(row)
                row.status='pending';row.version=snap['version']
                row.data={'source':source,'input':prompt[:6000],'mode':mode,'profileVersion':snap['profileVersion'],
                    'conversationId':conversation_id,'waitSeconds':wait_seconds}
            try:
                def validate_frame(value):
                    if not set(value.sourceIds)<=set(snap['allowedSources']):
                        raise ValueError('理解 sourceIds 必须逐字选自 allowedSources，目标和活动编号不是来源编号。')
                def validate_decision(value):
                    if value.action not in allowed_actions:
                        raise ValueError('action 必须选自 allowedActions：'+','.join(allowed_actions))
                    if not set(value.sourceIds)<=set(snap['allowedSources']):
                        raise ValueError('决策 sourceIds 必须逐字选自 snapshot.allowedSources，不能引用目标或活动编号。')
                    if value.goal and not set(value.goal.sourceIds)<=set(snap['allowedSources']):
                        raise ValueError('新目标 sourceIds 必须选自 snapshot.allowedSources。')
                    if value.goalId and value.goalId not in {g['id'] for g in snap['goals']}:
                        raise ValueError('goalId 必须是 snapshot.goals 中的 id，否则留空；新目标使用 goal 字段。')
                    if value.goalUpdate:
                        goal=next((g for g in snap['goals'] if g['id']==value.goalUpdate.goalId),None)
                        if not goal or not set(value.goalUpdate.evidenceIds)<=set(goal.get('evidence',[])):
                            raise ValueError('goalUpdate.evidenceIds 只能使用该目标已有 evidence；证据不足时 goalUpdate 为 null，仍可选择允许的活动。')
                frame=await self.call(actor_id,'understand',snap,MindFrame,background,revision,validator=validate_frame)
                decision=await self.call(actor_id,'decide',{'snapshot':snap,'frame':frame.model_dump(),
                    'allowedActions':allowed_actions,'mode':mode},DecisionIntent,background,revision,validator=validate_decision)
                if background and self.background_blocked(revision):raise MindInterrupted('后台状态已过期。')
                with session_scope() as s:
                    profile=s.get(MindProfile,actor_id)
                    if profile.version != snap['profileVersion']:raise MindInterrupted('人物资料已改变。')
                    state=self.mind.state(s,actor_id)
                    mood=state.data.get('mindMood') or {}
                    intensity=.8*mood.get('intensity',0)+.2*frame.emotion.intensity
                    data={**state.data,'emotion':{**frame.emotion.model_dump(),'sourceIds':frame.sourceIds,
                        'eventId':source,'at':time.time()},'mindMood':{'text':frame.emotion.name if frame.emotion.intensity>mood.get('intensity',0) else mood.get('text','平静'),'intensity':intensity,'at':time.time()}}
                    changed=s.execute(update(MindState).where(MindState.actor_id==actor_id,MindState.version==snap['version'],
                        MindState.enabled.is_(True)).values(data=data,version=snap['version']+1))
                    if changed.rowcount!=1:raise MindInterrupted('人物已收到新事件，请依据最新状态重试。')
                    row=s.get(MindDecision,did)
                    row.status='committed'
                    row.data={**row.data,'id':did,'frame':frame.model_dump(),'intent':decision.model_dump(),
                        'sourceIds':list(dict.fromkeys([*frame.sourceIds,*decision.sourceIds])),
                        'stateVersion':snap['version']+1}
                    if decision.goal:
                        existing=s.scalars(select(PersonalGoal).where(PersonalGoal.actor_id==actor_id,PersonalGoal.status=='active')).all()
                        gid=key(actor_id,'goal',decision.goal.title)
                        if len(existing)<3 and not s.get(PersonalGoal,gid):
                            s.add(PersonalGoal(id=gid,actor_id=actor_id,status='active',data={**decision.goal.model_dump(),
                                'origin':'interpretation','evidence':[],'reason':'','updatedAt':time.time()}))
                            row.data={**row.data,'intent':{**row.data['intent'],'goalId':gid}}
                        elif any(g.id==gid for g in existing):
                            row.data={**row.data,'intent':{**row.data['intent'],'goalId':gid}}
                    if decision.goalUpdate:
                        change=decision.goalUpdate
                        goal=s.get(PersonalGoal,change.goalId)
                        goal.status=change.status
                        goal.data={**goal.data,'nextStep':change.nextStep,'reason':change.reason,'updatedAt':time.time(),
                            'progressHistory':[*goal.data.get('progressHistory',[]),{'decisionId':did,**change.model_dump()}][-30:]}
                    s.add(MindOutbox(id=key(did,'committed',snap['version']),payload={'actorId':actor_id,'decisionId':did,'version':snap['version']+1}))
                    result=dict(row.data)
                self.mind.flush_outbox()
                return result
            except BaseException as exc:
                with session_scope() as s:
                    row=s.get(MindDecision,did)
                    if row and row.status!='committed':
                        row.status='interrupted' if isinstance(exc,(MindInterrupted,asyncio.CancelledError)) else 'failed'
                        row.data={**row.data,'error':type(exc).__name__,
                            'validationError':str(exc)[:500] if isinstance(exc,ValueError) else ''}
                raise

    def invalidate(self, actor_id, source_id=None, reset=False):
        self.revision+=1
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            memories=s.scalars(select(Experience).where(Experience.actor_id==actor_id)).all()
            invalid={source_id} if source_id else set()
            while True:
                children={r.id for r in memories if any(invalid.intersection(v['sourceIds']+v.get('counterSourceIds',[])) for v in r.data.get('derived',[]))}
                if children<=invalid:break
                invalid|=children
            invalid_decisions=set()
            decisions=s.scalars(select(MindDecision).where(MindDecision.actor_id==actor_id)).all()
            for row in decisions:
                if reset or invalid.intersection(row.data.get('sourceIds',[])):
                    row.status='invalidated';invalid_decisions.add(row.id)
            for row in memories:
                derived=row.data.get('derived',[])
                if reset or any(invalid.intersection(v['sourceIds']+v.get('counterSourceIds',[])) for v in derived):
                    row.data={**row.data,'derived':[], 'mindReflection':'pending'}
            source=s.get(Experience,source_id) if source_id else None
            event_id=source.data.get('eventId') if source else None
            paused_goals=set()
            for goal in s.scalars(select(PersonalGoal).where(PersonalGoal.actor_id==actor_id)):
                if reset or invalid.intersection(goal.data.get('sourceIds',[])) or event_id and event_id in goal.data.get('evidence',[]):
                    goal.status='paused';goal.data={**goal.data,'reason':'来源已撤回，等待重新确认。','evidence':[]}
                    paused_goals.add(goal.id)
            for activity in s.scalars(select(LifeActivity)):
                if (actor_id in activity.data['participants'] and activity.status in {'active','invited','suspended'}
                        and (reset or activity.data.get('decisionId') in invalid_decisions or activity.data.get('goalId') in paused_goals)):
                    activity.status='cancelled' if reset else 'suspended';activity.data={**activity.data,'reason':'人物状态已重置或纠正，需重新决定。'}
            state=self.mind.state(s,actor_id)
            if reset or invalid.intersection(state.data.get('emotion',{}).get('sourceIds',[])):
                state.data={k:v for k,v in state.data.items() if k not in {'emotion','mindMood'}}

    def traces(self, actor_id, before=None, limit=20):
        with session_scope() as s:
            self.mind.state(s,actor_id)
            q=select(MindDecision).where(MindDecision.actor_id==actor_id)
            if before:q=q.where(MindDecision.at<float(before))
            rows=s.scalars(q.order_by(MindDecision.at.desc()).limit(min(50,max(1,limit)))).all()
            return [{'id':r.id,'status':r.status,'at':r.at,**r.data} for r in rows]

    def decay(self, now=None):
        now=now or time.time()
        with session_scope() as s:
            s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
            for state in s.scalars(select(MindState)):
                emotion=state.data.get('emotion')
                if not emotion:continue
                elapsed=max(0,now-emotion.get('decayedAt',emotion['at']))
                if elapsed<60:continue
                state.data={**state.data,'emotion':{**emotion,'intensity':emotion['intensity']*2**(-elapsed/7200),'decayedAt':now}}
                mood=state.data.get('mindMood') or {}
                state.data={**state.data,'mindMood':{**mood,'intensity':mood.get('intensity',0)*2**(-elapsed/28800)}}
                state.version+=1

    async def reflect(self):
        with session_scope() as s:
            rows=s.scalars(select(Experience).join(MindState,MindState.actor_id==Experience.actor_id).join(Actor,Actor.id==Experience.actor_id)
                .where(Experience.forgotten.is_(False),MindState.enabled.is_(True),Actor.is_active.is_(True))
                .order_by(Experience.at.desc()).limit(100)).all()
            candidate=next((r for r in rows if r.data.get('mindReflection') not in {'done','historical'}),None)
            if not candidate:return
            aid,eid=candidate.actor_id,candidate.id
        if not self.active(aid):return
        async with self.locks[aid]:
            revision=self.revision
            snap=self.snapshot(aid,candidate.text,eid)
            delta=await self.call(aid,'reflect',snap,StateDelta,True,revision)
            with session_scope() as s:
                s.execute(update(MindControl).where(MindControl.id=='life').values(id='life'))
                row=s.get(Experience,eid)
                state=self.mind.state(s,aid)
                profile=s.get(MindProfile,aid)
                if (row.forgotten or state.version!=snap['version'] or profile.version!=snap['profileVersion']
                        or not state.enabled or self.background_blocked(revision)):
                    raise MindInterrupted('反思依据已改变。')
                derived=[]
                for belief in delta.beliefs:
                    ids=set(belief.sourceIds+belief.counterSourceIds)
                    if not ids<=set(snap['allowedSources']):continue
                    evidence=s.scalars(select(Experience).where(Experience.id.in_(ids),Experience.actor_id==aid,Experience.forgotten.is_(False))).all()
                    if len(evidence)!=len(ids):continue
                    if belief.peerId and not any(belief.peerId in e.data.get('peers',[]) for e in evidence):continue
                    support=[e for e in evidence if e.id in belief.sourceIds]
                    sessions={e.run_id or e.data.get('activityId') or e.data.get('conversationId') for e in support}-{None,''}
                    stable=belief.kind!='habit' or len({e.data.get('eventId',e.id) for e in support})>=3 and len(sessions)>=2
                    derived.append({**belief.model_dump(),'confidence':belief.confidence/(1+len(belief.counterSourceIds)),
                        'stable':stable,'valid':True,'version':state.version,'origin':'interpretation'})
                row.data={**row.data,'derived':derived,'mindReflection':'done'}
                commitments=list(state.data.get('commitments',[]))
                for promise in delta.commitments:
                    evidence=s.get(Experience,promise.sourceId)
                    if (promise.sourceId not in snap['allowedSources'] or not evidence or evidence.actor_id!=aid or evidence.forgotten
                            or not evidence.data.get('ownSpeech') or promise.text not in evidence.text):continue
                    value={'text':promise.text,'sourceId':promise.sourceId,'origin':'quoted-promise'}
                    if not any(v.get('text')==promise.text and v.get('sourceId')==promise.sourceId for v in commitments):commitments.append(value)
                state.data={**state.data,'commitments':commitments[-8:]}
                state.version+=1
        self.c.store.event(None,'mind.changed',{'actorId':aid,'sourceId':eid})
