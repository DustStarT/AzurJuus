"""Durable workflow plans, revisions and user-approved external membership."""
import re
import time
from uuid import uuid4


class WorkflowPlans:
    def __init__(self, host):
        self.host = host
        self.store = host.store

    @staticmethod
    def validate_tasks(tasks):
        if any(not isinstance(task, dict) for task in tasks):
            raise ValueError('子任务必须是对象。')
        for task in tasks:
            for field in ('id', 'actorId', 'brief'):
                value = task.get(field)
                if not isinstance(value, str) or not value.strip() or len(value) > 30000:
                    raise ValueError('子任务编号、成员、目标和验收条件必须是有效文字。')
            acceptance = task.get('acceptance')
            if isinstance(acceptance, str):
                acceptance = [acceptance]
            if (not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 24
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 30000 for item in acceptance)):
                raise ValueError('验收条件必须是非空文字或文字列表。')
            deps = task.get('dependsOn', [])
            if not isinstance(deps, list) or len(deps) > 24 or any(not isinstance(dep, str) for dep in deps):
                raise ValueError('子任务依赖必须是编号列表。')

    def plan(self, run_id, args):
        run = self.store.get(run_id)
        tasks = args.get("tasks") or []
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 24:
            raise ValueError("计划需要 1–24 个具体子任务。")
        self.validate_tasks(tasks)
        if len({t.get('actorId') for t in tasks}) > run.get('maxTaskMembers',12):
            raise ValueError('分工超过设置中的任务成员上限。')
        ids = {t.get("id") for t in tasks}
        actors = {a["id"] for a in run["actors"]}
        if len(ids) != len(tasks) or None in ids:
            raise ValueError("子任务 ID 必须唯一。")
        if any(not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", i) or i in {"planner", "reviewer", "chat"} or i.startswith("discussion_") for i in ids):
            raise ValueError("子任务 ID 只能包含字母、数字、下划线与连字符，且不能使用保留名称。")
        done = set()
        for task in tasks:
            if task.get("actorId") not in actors or not task.get("brief") or not task.get("acceptance") or not set(task.get("dependsOn", [])) <= ids:
                raise ValueError("分工缺少有效成员、目标、验收条件或依赖。")
            task["status"] = "pending"
            task.setdefault("dependsOn", [])
        while True:
            ready = {t["id"] for t in tasks if set(t["dependsOn"]) <= done}
            if ready <= done:
                break
            done |= ready
        if done != ids:
            raise ValueError("任务依赖存在循环。")
        self.store.update(run_id, assignments=tasks)
        self.store.event(run_id, 'workflow.plan.created', {'assignments': [t.get('id') for t in tasks], 'revision': 0})
        self.host.notify_plan(run_id)
        if self.host.plan_callback:
            self.host.plan_callback(run_id, tasks)
        return {"accepted": True, "count": len(tasks)}

    def replan(self, run_id, tasks):
        """Apply a revision without rewriting completed work or its evidence."""
        run = self.store.get(run_id)
        if run.get('status') not in {'running', 'waiting_approval', 'paused'}:
            raise ValueError('当前任务状态不允许修改计划。')
        if run_id in self.host.tasks and run_id not in self.host.plan_changes:
            raise ValueError('任务已离开子任务执行阶段，请先暂停再修订计划。')
        current = {item['id']: item for item in run.get('assignments', [])}
        if not isinstance(tasks, list) or not tasks or len(tasks) > 24:
            raise ValueError('修订计划需要 1–24 个子任务。')
        self.validate_tasks(tasks)
        if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in tasks):
            raise ValueError('修订计划的子任务编号无效。')
        proposed_ids = {item['id'] for item in tasks}
        if len(proposed_ids) != len(tasks) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item) or item in {'planner','reviewer','chat'} or item.startswith('discussion_') for item in proposed_ids):
            raise ValueError('修订计划的子任务编号无效或重复。')
        if len({item.get('actorId') for item in tasks}) > run.get('maxTaskMembers', 12):
            raise ValueError('修订计划超过任务成员上限。')
        preserved = {key: value for key, value in current.items() if value.get('status') in {'running', 'completed', 'submitted'}}
        if not set(preserved) <= proposed_ids:
            raise ValueError('不能移除正在执行或已完成的子任务。')
        actors = {actor['id'] for actor in run.get('actors', [])}
        merged = []
        for item in tasks:
            item = dict(item)
            if item.get('actorId') not in actors or not item.get('brief') or not item.get('acceptance'):
                raise ValueError('修订计划包含无效成员、目标或验收条件。')
            deps = list(dict.fromkeys(item.get('dependsOn') or []))
            if not set(deps) <= proposed_ids or item['id'] in deps:
                raise ValueError('修订计划存在无效依赖。')
            old = current.get(item['id'])
            if old and old.get('status') in {'running', 'completed', 'submitted'}:
                if item.get('actorId') != old.get('actorId') or deps != old.get('dependsOn', []):
                    raise ValueError('正在执行或已完成的子任务不能更换负责人或依赖。')
                item = dict(old)
            else:
                item['status'] = 'pending'
                item['dependsOn'] = deps
            item.setdefault('handoff', self._handoff_for_dependencies(run, deps))
            merged.append(item)
        ids = set()
        done = set()
        while True:
            ready = {item['id'] for item in merged if set(item.get('dependsOn', [])) <= done}
            if ready <= done:
                break
            done |= ready
            ids |= ready
        if ids != proposed_ids:
            raise ValueError('修订计划存在循环依赖。')
        revision = int(run.get('planRevision', 0)) + 1
        self.store.update(run_id, assignments=merged, planRevision=revision,
            planHistory=[*(run.get('planHistory') or []), {'revision': revision, 'at': time.time(), 'kind': 'replan', 'assignments': [item['id'] for item in merged]}])
        self.store.event(run_id, 'workflow.plan.revised', {'revision': revision, 'assignments': [item['id'] for item in merged]})
        self.host.reviews.invalidate(run_id,'plan')
        self.host.notify_plan(run_id)
        if self.host.plan_callback:
            self.host.plan_callback(run_id, merged)
        return {'accepted': True, 'revision': revision, 'count': len(merged)}

    def _handoff_for_dependencies(self, run, dependency_ids):
        handoff = []
        for item in run.get('assignments', []):
            if item.get('id') not in dependency_ids:
                continue
            result = item.get('result') or {}
            for artifact in result.get('artifacts') or []:
                if isinstance(artifact, dict) and artifact.get('path'):
                    handoff.append({'assignmentId': item['id'], 'path': artifact['path'], 'sha256': artifact.get('sha256')})
        return handoff

    def request_help(self, run_id, *, requester_id, assignment_id, brief, candidate_ids=None, reason=''):
        run = self.store.get(run_id)
        if run.get('status') not in {'running', 'waiting_approval', 'paused'}:
            raise ValueError('当前任务不接受外援申请。')
        assignment = next((item for item in run.get('assignments', []) if item.get('id') == assignment_id), None)
        if assignment is None:
            raise ValueError('外援申请必须关联现有子任务。')
        if requester_id != assignment.get('actorId'):
            raise ValueError('外援申请人必须是该子任务的负责人。')
        if candidate_ids is not None and (not isinstance(candidate_ids, list) or not all(isinstance(item, str) for item in candidate_ids)):
            raise ValueError('外援候选需要使用成员编号列表。')
        connected = set(self.host.settings_loader().get('connectedAgentIds') or [])
        if candidate_ids and not set(candidate_ids) <= connected:
            raise ValueError('外援候选必须是当前配置中已连接的成员。')
        if assignment.get('status') in {'completed', 'submitted'}:
            raise ValueError('已完成子任务不能申请外援。')
        requests = list(run.get('helpRequests') or [])
        if any(item.get('assignmentId') == assignment_id and item.get('status') in {'pending', 'approved'} for item in requests):
            raise ValueError('该子任务已有未完成的外援申请。')
        request = {
            'id': 'help-' + uuid4().hex[:12], 'assignmentId': assignment_id,
            'requesterId': requester_id, 'brief': str(brief).strip()[:1200],
            'reason': str(reason).strip()[:800], 'candidateIds': list(dict.fromkeys(candidate_ids or [])),
            'status': 'pending', 'createdAt': time.time(), 'handoff': self._handoff_for_dependencies(run, assignment.get('dependsOn', [])),
        }
        if not request['brief']:
            raise ValueError('外援申请需要说明缺口。')
        self.store.update(run_id, helpRequests=[*requests, request])
        self.store.event(run_id, 'workflow.agent.help_requested', request)
        return request

    def resolve_help(self, run_id, request_id, *, decision, actor=None):
        if decision not in {'approved', 'rejected'}:
            raise ValueError('无效的外援决定。')
        run = self.store.get(run_id)
        if run.get('status') not in {'running', 'waiting_approval', 'paused'}:
            raise ValueError('当前任务已结束，不能再处理外援申请。')
        if decision == 'approved' and run_id in self.host.tasks and run_id not in self.host.plan_changes:
            raise ValueError('任务已离开子任务执行阶段，请先暂停再加入外援。')
        requests = list(run.get('helpRequests') or [])
        request = next((item for item in requests if item.get('id') == request_id), None)
        if request is None or request.get('status') != 'pending':
            raise ValueError('外援申请不存在或已处理。')
        if decision == 'rejected':
            request['status'] = 'rejected'; request['resolvedAt'] = time.time()
        else:
            occupied = {run['actorId'], *(item.get('actorId') for item in run.get('assignments', []))}
            if not actor or actor.get('id') in occupied:
                raise ValueError('外援成员无效或已经在任务中。')
            if request.get('candidateIds') and actor.get('id') not in request['candidateIds']:
                raise ValueError('该成员不在申请的候选范围内。')
            if len(run.get('assignments', [])) >= 24:
                raise ValueError('计划已达到子任务数量上限。')
            assigned_ids = {item.get('actorId') for item in run.get('assignments', [])}
            if len(assigned_ids | {actor['id']}) > run.get('maxTaskMembers', 12):
                raise ValueError('外援会超过任务成员上限。')
            source = next(item for item in run['assignments'] if item['id'] == request['assignmentId'])
            helper_id = f"help-{request_id}"
            helper = {'id': helper_id, 'actorId': actor['id'], 'brief': request['brief'],
                'dependsOn': list(source.get('dependsOn') or []), 'acceptance': ['完成外援申请中的缺口并读取核验结果'],
                'handoff': request.get('handoff', []), 'status': 'pending', 'helpRequestId': request_id}
            request['status'] = 'approved'; request['resolvedAt'] = time.time(); request['actorId'] = actor['id']
            assignments = [*run.get('assignments', []), helper]
            revision = int(run.get('planRevision', 0)) + 1
            roster = run.get('actors', [])
            if actor['id'] not in {item['id'] for item in roster}:
                roster = [*roster, actor]
            self.store.update(run_id, actors=roster, assignments=assignments,
                helpRequests=requests, planRevision=revision,
                planHistory=[*(run.get('planHistory') or []), {'revision': revision, 'at': time.time(), 'kind': 'external-helper',
                    'assignments': [item['id'] for item in assignments]}])
            self.store.event(run_id, 'workflow.plan.revised', {'reason': 'external-helper-admitted', 'revision': revision,
                'assignmentId': helper_id, 'sourceAssignmentId': source['id']})
            self.store.event(run_id, 'workflow.agent.help_resolved', request)
            self.host.reviews.invalidate(run_id,'plan')
            self.host.notify_plan(run_id)
            if self.host.plan_callback:
                self.host.plan_callback(run_id, assignments)
            return {'request': request, 'assignment': helper}
        self.store.update(run_id, helpRequests=requests)
        self.store.event(run_id, 'workflow.agent.help_resolved', request)
        return {'request': request}

