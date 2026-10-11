"""Model-directed method retrieval and composition; methods never grant tools."""
import hashlib
from sqlalchemy import select
from backend.database import session_scope
from backend.models import Actor, ActorSkill
from backend.tasks.skill_runtime import SkillSelection


class MethodRuntime:
    def __init__(self,host,service,selections):
        self.host,self.service,self.selections=host,service,selections

    def mode(self,run,phase):
        return 'review' if phase=='reviewer' else 'swarm' if run.get('collaborative') else 'task'

    def catalog(self,actor_id,mode):
        with session_scope() as session:
            actor=session.get(Actor,actor_id)
            if not actor or not actor.is_active:raise ValueError('方法使用者不存在或已停用。')
            public=self.service.bundle.skills.registry.public_catalog(self.service._approved_public_skill_proposals(session))
            methods=[{'id':s.id,'name':s.name,'summary':s.summary,'modes':list(s.modes),'tags':list(s.tags),
                'origin':s.origin,'visibility':'public','patch':s.prompt_patch,'tools':list(s.tool_allowlist)} for s in public]
            for row in session.scalars(select(ActorSkill).where(ActorSkill.actor_id==actor_id,ActorSkill.is_enabled.is_(True))):
                extra=row.extra_json or {}
                if not row.origin.startswith('builtin') and (extra.get('validationCount')!=3 or not (extra.get('trialSummary') or {}).get('stable')):
                    continue
                methods.append({'id':row.skill_id,'name':row.name,'summary':row.summary,'modes':row.mode_json or [],
                    'tags':row.tags_json or [],'origin':row.origin,'visibility':'private','patch':row.prompt_patch,
                    'tools':row.tool_allowlist_json or [],'trialFamily':extra.get('trialFamily')})
        return [m for m in methods if mode in m['modes'] or 'any' in m['modes']]

    def available(self,run,actor_id,phase):
        prompt=run['prompt']
        return [m for m in self.catalog(actor_id,self.mode(run,phase))
            if not m.get('trialFamily') or m['trialFamily']=='custom'
            or m['trialFamily']=='inventory' and any(w in prompt for w in ('列出','清单','目录','哪些文件'))
            or m['trialFamily']=='organize' and any(w in prompt for w in ('分类','整理文件'))]

    def search(self,run_id,actor_id,phase,args):
        query=args.get('query','')
        limit=args.get('limit',12)
        if not isinstance(query,str) or len(query)>1000 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=20:
            raise ValueError('方法检索需要 query 文字和 1–20 的 limit。')
        rows=self.available(self.host.store.get(run_id),actor_id,phase)
        words=query.lower().split()
        rows.sort(key=lambda m:sum(w in (m['name']+' '+m['summary']+' '+' '.join(m['tags'])).lower() for w in words),reverse=True)
        with session_scope() as session:
            from backend.models import SkillRun
            records=session.scalars(select(SkillRun).where(SkillRun.actor_id==actor_id).order_by(SkillRun.created_at.desc()).limit(10)).all()
            sources=[{'runId':(r.extra_json or {}).get('runId'),'methods':r.selected_skill_ids,'summary':r.output_summary}
                for r in records if (r.extra_json or {}).get('verified') and (r.extra_json or {}).get('runId')]
        return {'methods':[{k:v for k,v in m.items() if k not in {'patch','tools'}} for m in rows[:limit]],'verifiedRuns':sources,
            'instruction':'自行按任务选用或组合；调用 method_select 获取实际方法。也可不选，不扩大工具权限。'}

    def select(self,run_id,actor_id,phase,args):
        ids=args.get('ids')
        if (not isinstance(ids,list) or not 1<=len(ids)<=4 or any(not isinstance(i,str) for i in ids)
                or len(set(ids))!=len(ids)):
            raise ValueError('组合方法需要 1–4 个互不重复的真实方法编号。')
        run=self.host.store.get(run_id)
        catalog={m['id']:m for m in self.available(run,actor_id,phase)}
        if not set(ids)<=set(catalog):raise ValueError('方法不可见、已停用、范围不符或尚未通过验证。')
        rows=[catalog[i] for i in ids]
        patch='\n\n'.join(m['name']+'：'+m['patch'] for m in rows)
        if len(patch)>16000:raise ValueError('组合方法过长，请减少选择。')
        selection=SkillSelection(ids[0],rows[0]['name'],
            [{k:v for k,v in m.items() if k not in {'patch','tools'}} for m in rows],patch,
            list(dict.fromkeys(t for m in rows for t in m['tools'])))
        self.selections[(run_id,phase)]=(actor_id,self.mode(run,phase),selection)
        with self.host.store.lock:
            methods=self.host.store.get(run_id).get('methods',{})
            methods[phase]={'actorId':actor_id,'ids':ids,'names':[m['name'] for m in rows],
                'hashes':{m['id']:hashlib.sha256(m['patch'].encode()).hexdigest() for m in rows},'patch':patch,'selectedBy':'model'}
            self.host.store.update(run_id,methods=methods)
        return {'ids':ids,'method':patch,'instruction':'方法是可参考的处理方式，不是授权；遵守原始要求及当前工具权限。'}

    def validate_selection(self,run_id,actor_id,phase):
        run=self.host.store.get(run_id)
        choice=run.get('methods',{}).get(phase)
        if not choice or choice.get('selectedBy')!='model':return
        catalog={m['id']:m for m in self.available(run,actor_id,phase)}
        if choice['actorId']!=actor_id or any(i not in catalog or hashlib.sha256(catalog[i]['patch'].encode()).hexdigest()!=h
            for i,h in choice['hashes'].items()):raise ValueError('已选方法已停用或版本改变，请重新检索和选择。')

    def context(self,run_id,actor_id,phase):
        try:self.validate_selection(run_id,actor_id,phase)
        except ValueError:
            with self.host.store.lock:
                methods=self.host.store.get(run_id).get('methods',{})
                methods.pop(phase,None)
                self.host.store.update(run_id,methods=methods)
            self.selections.pop((run_id,phase),None)
        choice=self.host.store.get(run_id).get('methods',{}).get(phase)
        if choice and choice.get('selectedBy')=='model':return choice['patch']
        return '可以调用 method_search 检索当前可见的方法，再用 method_select 自主组合，也可以不使用方法。不根据工具目录推断额外授权。'

    def recover_selections(self,run):
        """Restore historical usage from successful gateway calls after restart."""
        calls=self.host.store.calls(run['id'])
        for phase,choice in run.get('methods',{}).items():
            if choice.get('selectedBy')!='model' or (run['id'],phase) in self.selections:continue
            proof=next((c for c in reversed(calls) if c.get('phase')==phase and c['name']=='method_select'
                and c['status']=='completed' and c['args'].get('ids')==choice['ids']
                and (c.get('result') or {}).get('method')==choice['patch']),None)
            if not proof:continue
            rows=[{'id':sid,'name':name} for sid,name in zip(choice['ids'],choice['names'])]
            self.selections[(run['id'],phase)]=(choice['actorId'],self.mode(run,phase),
                SkillSelection(rows[0]['id'],rows[0]['name'],rows,choice['patch'],[]))
