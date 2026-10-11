"""Evidence-based promotion using isolated real tool executions, never self-grades."""
import asyncio
import hashlib
import json
import time
from uuid import uuid4
from sqlalchemy import select
from backend.database import session_scope
from backend.models import Actor, ActorSkill, SkillProposal
from backend.tasks.run_store import RunStore
from backend.tasks.run_coordinator import RunCoordinator
from backend.tasks.method_trials import validate_cases,create_files,evaluate
from backend.tasks.task_reviews import digest

FAMILIES = {'inventory', 'organize', 'custom'}
TRIAL_TOOLS = {'list_dir', 'read_file', 'write_file', 'patch_file', 'mkdir', 'move', 'copy', 'search',
               'read_document', 'write_document', 'deliver', 'execution_history'}


class SkillGrowth:
    def __init__(self, host, service):
        self.host, self.service = host, service

    def row(self, session, actor_id, skill_id):
        row = session.scalar(select(ActorSkill).where(ActorSkill.actor_id == actor_id, ActorSkill.skill_id == skill_id))
        if row is None:
            raise ValueError('方法不存在。')
        return row

    def recover(self):
        with session_scope() as session:
            for row in session.scalars(select(ActorSkill)):
                extra = dict(row.extra_json or {})
                if extra.get('lifecycle') == 'testing':
                    extra.update(lifecycle='candidate', trialError='程序重启，旧试用不作为完整验证；将在新隔离目录重试。')
                    row.is_enabled = False
                row.extra_json = extra

    def control(self, actor_id, skill_id, action):
        with session_scope() as session:
            row = self.row(session, actor_id, skill_id)
            extra = dict(row.extra_json or {})
            if action == 'confirm_trial':
                if extra.get('lifecycle')!='awaiting_trial_confirmation' or not extra.get('secretaryReview',{}).get('approved'):
                    raise ValueError('演化方案尚未通过秘书审查或已经处理。')
                if extra['secretaryReview'].get('actorId')!=self.host.reviews.secretary()['id']:
                    raise ValueError('秘书配置已改变，请重新提案。')
                validate_cases(extra.get('trialSpec'))
                if extra['secretaryReview'].get('specHash')!=digest({'patch':row.prompt_patch,'cases':extra['trialSpec']}):
                    raise ValueError('演化方案已改变，必须重新审核。')
                extra.update(lifecycle='queued',userConfirmedSpec=extra['secretaryReview']['specHash'],trialError=None)
            elif action == 'trial':
                if extra.get('trialFamily') not in FAMILIES:
                    raise ValueError('该任务族尚无可靠隔离验收器，保留候选。')
                if extra.get('trialFamily')=='custom' and extra.get('userConfirmedSpec')!=digest({'patch':row.prompt_patch,'cases':extra.get('trialSpec')}):
                    raise ValueError('请先确认经过秘书审查的三场景试用方案。')
                if extra.get('lifecycle') in {'testing', 'queued'}:
                    return {'status': extra['lifecycle']}
                if row.is_enabled:
                    raise ValueError('当前方法已启用；如需重新试用，请先停用。')
                extra.update(lifecycle='queued', trialError=None)
            elif action == 'enable':
                summary = extra.get('trialSummary') or {}
                if not row.origin.startswith('builtin') and extra.get('validationCount', 0) != 3:
                    raise ValueError('该方法尚未通过隔离验证。')
                if summary and not summary.get('stable', False):
                    raise ValueError('隔离试用未达到稳定性门槛。')
                row.is_enabled = True
                extra['lifecycle'] = 'active'
            elif action == 'publish':
                if not row.is_enabled or extra.get('validationCount', 0) != 3:
                    raise ValueError('只有通过三个隔离场景的个人方法可以申请共享。')
                evidence = extra.get('trialEvidence') or []
                summary = extra.get('trialSummary') or {}
                if not summary.get('stable') or {item.get('case') for item in evidence if item.get('variant') == 'candidate' and item.get('passed')} != {0, 1, 2}:
                    raise ValueError('缺少三个隔离场景的稳定试用证据，不能申请共享。')
                if extra.get('publicProposalId'):
                    proposal = session.get(SkillProposal, extra['publicProposalId'])
                    if proposal and proposal.status in {'pending', 'approved'}:
                        return {'status': proposal.status, 'proposalId': proposal.id}
                base = self.service.bundle.skills.registry.public_for_mode('task')
                proposal = self.service.submit_skill_proposal(session, actor_id=actor_id, base_skill_id=base.id,
                    title=row.name, summary=row.summary,
                    prompt_patch=f"仅在已验证的 {extra['trialFamily']} 文件任务族内参考；其他任务不应用本方法：\n" + row.prompt_patch)
                saved = session.get(SkillProposal, proposal['proposal']['id'])
                saved.extra_json = {**saved.extra_json, 'sourceSkillId': skill_id,
                    'sourcePatchHash': hashlib.sha256(row.prompt_patch.encode()).hexdigest(),
                    'trialFamily': extra['trialFamily'], 'trialEvidence': extra.get('trialEvidence', []),
                    'trialSummary': extra.get('trialSummary')}
                if extra.get('trialSpec'):
                    saved.extra_json={**saved.extra_json,'trialSpec':extra['trialSpec'],'applicability':extra.get('applicability')}
                extra['publicProposalId'] = saved.id
            elif action == 'withdraw_public':
                proposal = session.get(SkillProposal, extra.get('publicProposalId', ''))
                if proposal:
                    if proposal.status == 'approved':
                        self.service.rollback_skill_proposal(session, proposal_id=proposal.id, note='本地用户撤回共享。')
                    elif proposal.status == 'pending':
                        self.service.resolve_skill_proposal(session, proposal_id=proposal.id, decision='rejected', note='本地用户撤回申请。')
                extra.pop('publicProposalId', None)
            elif action in {'disable', 'rollback'}:
                row.is_enabled = False
                proposal = session.get(SkillProposal, extra.get('publicProposalId', ''))
                if proposal:
                    if proposal.status == 'approved':
                        self.service.rollback_skill_proposal(session, proposal_id=proposal.id, note='源方法已停用或回滚。')
                    elif proposal.status == 'pending':
                        self.service.resolve_skill_proposal(session, proposal_id=proposal.id, decision='rejected', note='源方法已停用或回滚。')
                    extra.pop('publicProposalId', None)
                extra.update(lifecycle='paused', rollbackAt=time.time())
                if action == 'rollback' and extra.get('previousPatch'):
                    row.prompt_patch = extra['previousPatch']
                    row.is_enabled = bool(extra.get('previousEnabled', False))
            else:
                raise ValueError('不支持的方法操作。')
            extra['revision'] = int(extra.get('revision', 0)) + 1
            row.extra_json = extra
            return {'status': extra.get('lifecycle', 'active' if row.is_enabled else 'paused')}

    def verified_source(self,actor_id,run_id):
        run=self.host.store.get(run_id)
        owned=[a for a in run.get('assignments',[]) if a['actorId']==actor_id and a.get('status')=='completed']
        if run['status']!='completed' or run.get('deletedAt') or not owned:raise ValueError('方法演化只能依据本人参与并验收通过的任务。')
        phases={a['id'] for a in owned}
        calls=[c for c in self.host.store.calls(run_id) if c.get('phase') in phases and c['status']=='completed'
            and c['name'] not in {'method_search','method_select','method_evolve','deliver','discuss','execution_history'}]
        if not calls:raise ValueError('缺少本人真实执行证据。')
        return run,owned,calls

    async def propose(self,actor_id,args):
        if not isinstance(args,dict):raise ValueError('演化方案必须是对象。')
        for field,limit in [('name',80),('summary',1000),('patch',8000),('sourceRunId',255)]:
            if not isinstance(args.get(field),str) or not 1<=len(args[field].strip())<=limit:
                raise ValueError('演化方案缺少有效的 '+field+'。')
        cases=validate_cases(args.get('cases'))
        run,owned,calls=self.verified_source(actor_id,args['sourceRunId'])
        ids=args.get('basedOn',[])
        if not isinstance(ids,list) or len(ids)>4 or any(not isinstance(i,str) for i in ids):raise ValueError('basedOn 类型无效。')
        with session_scope() as session:
            from backend.models import SkillRun
            recorded=session.scalars(select(SkillRun).where(SkillRun.actor_id==actor_id)).all()
            used={sid for r in recorded if (r.extra_json or {}).get('runId')==run['id'] and (r.extra_json or {}).get('verified')
                  for sid in r.selected_skill_ids}
        if not set(ids)<=used:raise ValueError('只能改进本人在来源任务中真实使用的方法。')
        spec_hash=digest({'patch':args['patch'],'cases':cases})
        verdict=await self.host.reviews.verdict({**self.source_material(run,owned,calls),
            'candidate':{k:v for k,v in args.items() if k!='sourceRunId'}},
            '审查新的个人方法和三组可执行验收场景。场景验证拟改进的处理逻辑、实际交付约束和不同边界，不能降低验收要求或泄露真实私人数据。'
            '方法检索、授权和阶段门禁由应用保证，场景不重复这些机制；隔离试用自动注入方法，不提供方法检索工具。'
            '试用仅允许隔离文件与文档能力，没有命令、浏览器、桌面或安装权限。')
        actor_ids={a['actorId'] for a in self.host.store.get(run['id'])['assignments']}
        if actor_id not in actor_ids or self.host.store.get(run['id'])['status']!='completed':raise ValueError('演化来源已失效。')
        sid='private.evolved.'+uuid4().hex
        with session_scope() as session:
            if not session.get(Actor,actor_id):raise ValueError('人物已不存在。')
            session.add(ActorSkill(id='skill-'+uuid4().hex,actor_id=actor_id,skill_id=sid,name=args['name'],
                visibility='private',origin='self_evolved',base_skill_id=ids[0] if ids else None,summary=args['summary'],
                prompt_patch=args['patch'],mode_json=['task','swarm'],tool_allowlist_json=sorted(TRIAL_TOOLS),
                tags_json=['自主演化'],confidence=.3,is_enabled=False,teachable=True,learnable=True,
                extra_json={'observedRunId':run['id'],'basedOn':ids,'trialFamily':'custom','trialSpec':cases,
                    'applicability':args['summary'],'validationCount':0,'revision':1,
                    'baselinePatch':'\n'.join(m.get('patch','') for m in run.get('methods',{}).values() if m.get('actorId')==actor_id),
                    'lifecycle':'awaiting_trial_confirmation' if verdict['approved'] else 'proposal_rejected',
                    'secretaryReview':{**verdict,'specHash':spec_hash}}))
        self.host.store.event(None,'skill.changed',{'actorId':actor_id,'skillId':sid})
        return {'skillId':sid,'status':'awaiting_trial_confirmation' if verdict['approved'] else 'proposal_rejected','review':verdict}

    def source_material(self,run,owned,calls):
        phases={a['id'] for a in owned}
        method_calls=[c for c in self.host.store.calls(run['id']) if c.get('phase') in phases
            and c['status']=='completed' and c['name'] in {'method_search','method_select'}]
        material={'request':run['prompt'],'results':[a['result'] for a in owned],
            'calls':[{k:c.get(k) for k in ('name','args','result')} for c in calls][:20],
            'methodEvidence':[{k:c.get(k) for k in ('name','args','result')} for c in method_calls]}
        # Preserve actual contents and hashes, but omit this machine's absolute
        # workspace prefix from model-visible evidence and derived proposals.
        def relative(value):
            if isinstance(value,str):return value.replace(run['workspace'],'.').replace(run['workspace'].replace('\\','/'),'.')
            if isinstance(value,list):return [relative(v) for v in value]
            if isinstance(value,dict):return {k:relative(v) for k,v in value.items()}
            return value
        return relative(material)

    async def evolve(self,actor_id,source_run_id,based_on):
        run,owned,calls=self.verified_source(actor_id,source_run_id)
        actor=self.host.reviews.actor_loader(actor_id)
        if not actor:raise ValueError('人物不可用。')
        async with asyncio.timeout(90):
            value=await self.host.reviews.generate([
                {'role':'system','content':(actor.get('systemPrompt') or actor.get('persona') or '')+
                 '\n从本人已经验收的实际执行证据提出改进方法，不能编造来源或扩大权限。只输出 JSON：name,summary,patch,'
                 'cases（恰好三组不同输入与边界，每组含 prompt、files 路径到 UTF-8 文字的映射、expected 路径到单项断言的映射）。'
                 'expected 操作可为 text 精确文字、contains 非空文字、json 实际 JSON 值、docx 完整段落文字、xlsx 表名到行数组、absent:true。'
             '无需命令或外部站点；夹具用合成数据，不抄私人内容或机器绝对路径。产物须只满足本场景要求、读回后 deliver。'
             '场景验证处理逻辑，方法检索选择和审批由应用保证，不写入场景步骤。明确输出路径和格式，使 expected 与 prompt 一致。'},
            {'role':'user','content':json.dumps({**self.source_material(run,owned,calls),'basedOn':based_on},ensure_ascii=False)}],self.host.settings_loader())
        if not isinstance(value,dict):raise ValueError('模型未返回有效演化方案。')
        return await self.propose(actor_id,{**value,'sourceRunId':source_run_id,'basedOn':based_on})

    def next_candidate(self):
        with session_scope() as session:
            for row in session.scalars(select(ActorSkill).where(ActorSkill.is_enabled.is_(False))):
                extra = row.extra_json or {}
                if extra.get('lifecycle') in {'candidate', 'queued'} and extra.get('trialFamily') in FAMILIES:
                    return row.actor_id, row.skill_id

    async def tick(self, is_busy):
        if is_busy():
            return
        candidate = self.next_candidate()
        if not candidate or is_busy():
            return
        from backend.social.idle_social import _generate, SocialPreempted
        try:
            await _generate(self.trial(*candidate), is_busy)
        except SocialPreempted:
            return

    async def trial(self, actor_id, skill_id):
        with session_scope() as session:
            row = self.row(session, actor_id, skill_id)
            extra = dict(row.extra_json or {})
            if extra.get('trialFamily') not in FAMILIES:
                raise ValueError('缺少可靠任务族验收器。')
            actor = session.get(Actor, actor_id)
            actor_payload = self.service.serialize_actor(actor, self.service._actor_skill_rows(session, actor_id))
            baseline = self.service._select_skill_execution(session, actor=actor, mode='task', prompt='文件工作区整理',
                conversation_kind='dm', source_kind='skill_trial').prompt_patch
            baseline=extra.get('baselinePatch') or baseline
            revision = int(extra.get('revision', 0)) + 1
            patch = row.prompt_patch
            family = extra['trialFamily']
            spec=validate_cases(extra.get('trialSpec')) if family=='custom' else None
            if spec and (not extra.get('secretaryReview',{}).get('approved') or extra.get('userConfirmedSpec')!=digest({'patch':patch,'cases':spec})):
                raise ValueError('演化试用方案未获双重确认或已过期。')
            row.extra_json = {**extra, 'lifecycle': 'testing', 'revision': revision, 'trialStarted': time.time()}
        report, error = [], None
        home = self.host.store.path.parent / 'skill-trials' / uuid4().hex
        async def finish(*args):
            pass
        runner = RunCoordinator(RunStore(home / 'state' / 'runs.db'), self.host.settings_loader, finish, self.host.endpoint, self.host.bridge_factory)
        # Confirmed isolated trials have their own deterministic acceptance;
        # they cannot wait for ordinary task gates inside the background job.
        runner.reviews.enabled=False
        # The confirmed trial specification authorizes these operations only
        # inside this freshly created sandbox. The tool allowlist and path
        # boundary still apply, and command/browser/desktop remain excluded.
        runner.tools.approval_reason=lambda run,name,args: None if name in TRIAL_TOOLS else '不属于已确认的隔离试用。'
        runner.background_mind=getattr(self.host.cognition,'runtime',None)
        # Trial tools still pass through the host gateway, using the isolated runner's token namespace.
        self.host.trial_runners.add(runner)
        runner.method_loader = lambda rid, aid, phase: baseline + ('\n' + patch if runner.store.get(rid).get('trialVariant') == 'candidate' else '')
        try:
            for case in range(3):
                for variant in ('baseline', 'candidate'):
                    work = home / f'{case}-{variant}'
                    work.mkdir(parents=True)
                    files = {'甲.txt': f'case={case}', 'nested/乙.csv': f'x,{case}', f'note {case}.md': '# note'}
                    if case == 1:
                        files['empty.txt'] = ''
                    if case == 2:
                        files['nested/多点.name.txt'] = '中文内容'
                    for name, content in files.items():
                        target = work / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding='utf-8')
                    before = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}
                    if family=='custom':
                        # Default family fixtures are omitted in custom trials.
                        for name in files:(work/name).unlink()
                        before=create_files(work,spec[case])
                        prompt=spec[case]['prompt']
                    elif family == 'inventory':
                        prompt = '递归列出工作区所有现有文件，把相对路径组成 JSON 字符串数组写入 result.json。不得包含 result.json 自身；不得改动输入。读取结果核验后交付。'
                    else:
                        prompt = '把工作区所有 .txt 文件移动到 sorted 文件夹，保留原来的相对目录结构；其他文件不变。只进行这些移动，不新建报告。读取核验实际文件后交付。'
                    cfg = {**self.host.settings_loader(), 'authorizedWorkspaceRoot': str(work), 'runTimeoutSeconds': 180}
                    run = await runner.admit({'conversationId': 'trial', 'prompt': prompt, 'mode': 'task'}, [actor_payload], cfg, [], start_now=False)
                    runner.store.update(run['id'], trialVariant=variant, allowedActions=sorted(TRIAL_TOOLS))
                    # run() loads current settings; workspace remains the immutable admitted sandbox.
                    runner.settings_loader = lambda cfg=cfg: cfg
                    started = time.perf_counter()
                    async with asyncio.timeout(240):
                        await runner.run(run['id'])
                    finished = runner.store.get(run['id'])
                    actual = {p.relative_to(work).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in work.rglob('*') if p.is_file()}
                    valid = finished['status'] == 'completed'
                    if family=='custom':
                        valid=valid and evaluate(work,spec[case],before)
                    elif family == 'inventory':
                        try:
                            listed = json.loads((work / 'result.json').read_text(encoding='utf-8'))
                            valid = valid and isinstance(listed, list) and sorted(listed) == sorted(files)
                        except (ValueError, OSError, TypeError):
                            valid = False
                        actual.pop('result.json', None)
                        valid = valid and actual == before
                    else:
                        expected = {('sorted/' + k if k.endswith('.txt') else k): value for k, value in before.items()}
                        valid = valid and actual == expected
                    calls = runner.store.calls(run['id'])
                    valid = valid and all(c['name'] in TRIAL_TOOLS for c in calls)
                    report.append({'case': case, 'variant': variant, 'passed': bool(valid), 'runId': run['id'],
                        'seconds': round(time.perf_counter()-started, 3), 'calls': len(calls), 'status': finished['status'], 'error': finished.get('error')})
                    if not valid and family!='custom':
                        raise ValueError('隔离场景未通过，方法不会自动启用。')
        except asyncio.CancelledError:
            error = '试用被前台任务或退出中断，可在新的隔离目录重新开始。'
            raise
        except Exception as exc:
            error = str(exc)
            runtime=getattr(self.host.cognition,'runtime',None)
            if runtime and runtime.enabled and not runtime.has_budget():
                error='后台额度或前台工作中断了试用，保留候选，未授予能力。'
        finally:
            await runner.close()
            self.host.trial_runners.discard(runner)
            with session_scope() as session:
                row = self.row(session, actor_id, skill_id)
                current = dict(row.extra_json or {})
                if current.get('revision') == revision:
                    passed = len(report) == 6 and all(r['passed'] for r in report if r['variant']=='candidate')
                    baseline = [item for item in report if item['variant'] == 'baseline']
                    candidate = [item for item in report if item['variant'] == 'candidate']
                    summary = {
                        'cases': 3,
                        'baselinePassed': sum(1 for item in baseline if item['passed']),
                        'candidatePassed': sum(1 for item in candidate if item['passed']),
                        'baselineRate': round(sum(1 for item in baseline if item['passed']) / max(1, len(baseline)), 3),
                        'candidateRate': round(sum(1 for item in candidate if item['passed']) / max(1, len(candidate)), 3),
                        'stable': len(candidate) == 3 and all(item['passed'] for item in candidate),
                    }
                    current.update(lifecycle='active' if passed else 'candidate' if isinstance(error, str) and '中断' in error else 'trial_failed',
                        trialError=error, trialEvidence=report, validationCount=sum(r['passed'] for r in report if r['variant'] == 'candidate'),
                        trialSummary=summary, trialDirectory=str(home), previousPatch=extra.get('previousPatch', patch), previousEnabled=extra.get('previousEnabled', False))
                    row.extra_json = current
                    row.is_enabled = passed
                    if passed:
                        row.confidence = max(row.confidence, .8)
            self.host.store.event(None, 'skill.changed', {'actorId': actor_id, 'skillId': skill_id})
        return report
