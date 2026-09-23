"""Local inspection/editing APIs. Inspection never performs model inference."""
from fastapi import HTTPException
from pydantic import Field
from .mind_contracts import Record, ProfileInterpretation


class GoalEdit(Record):
    title: str = Field(min_length=1,max_length=100)
    motivation: str = Field(min_length=1,max_length=400)
    nextStep: str = Field(min_length=1,max_length=400)
    status: str = 'active'
    reason: str = Field(default='',max_length=400)
    dueAt: float | None = None


class ProfileEdit(Record):
    interpretation: ProfileInterpretation | None = None


def install_mind_api(app, runtime):
    def invoke(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc

    @app.get('/api/life')
    def life_settings():
        return runtime.control()

    @app.post('/api/life')
    def save_settings(payload:dict):
        return invoke(runtime.control,payload)

    @app.get('/api/actors/{actor_id}/mind-profile')
    def profile(actor_id:str):
        return invoke(runtime.profile,actor_id)

    @app.post('/api/actors/{actor_id}/mind-profile')
    def save_profile(actor_id:str,payload:ProfileEdit):
        return invoke(runtime.edit_profile,actor_id,payload.interpretation)

    @app.get('/api/actors/{actor_id}/goals')
    def goals(actor_id:str):
        return {'goals':invoke(runtime.goals,actor_id)}

    @app.post('/api/actors/{actor_id}/goals')
    def create_goal(actor_id:str,payload:GoalEdit):
        data=payload.model_dump(exclude={'status','reason'})
        return invoke(runtime.save_goal,actor_id,{**data,'sourceIds':['user-setting']},status=payload.status,reason=payload.reason)

    @app.post('/api/actors/{actor_id}/goals/{goal_id}')
    def edit_goal(actor_id:str,goal_id:str,payload:GoalEdit):
        return invoke(runtime.save_goal,actor_id,payload.model_dump(exclude={'status','reason'}),
            goal_id,status=payload.status,reason=payload.reason)

    @app.get('/api/actors/{actor_id}/life')
    def life(actor_id:str,before:int|None=None,limit:int=20):
        rows=invoke(runtime.life.events,actor_id,before,limit)
        return {'activities':invoke(runtime.life.activities,actor_id),'events':rows,'nextCursor':rows[-1]['seq'] if rows else None}

    @app.get('/api/actors/{actor_id}/decisions')
    def traces(actor_id:str,before:float|None=None,limit:int=20):
        rows=invoke(runtime.traces,actor_id,before,limit)
        return {'decisions':rows,'nextCursor':rows[-1]['at'] if rows else None}

    @app.get('/api/actors/{actor_id}/beliefs')
    def beliefs(actor_id:str):
        from .database import session_scope
        from .cognition_models import Experience
        from sqlalchemy import select
        with session_scope() as s:
            invoke(runtime.mind.state,s,actor_id)
            rows=s.scalars(select(Experience).where(Experience.actor_id==actor_id,Experience.forgotten.is_(False)))
            return {'beliefs':[b for row in rows for b in row.data.get('derived',[]) if b.get('valid',True)]}
