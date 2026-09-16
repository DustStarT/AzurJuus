from fastapi import HTTPException
from pydantic import BaseModel, Field


class MindOptions(BaseModel):
    enabled: bool | None = None
    reset: bool = False


class Correction(BaseModel):
    interpretation: str = Field(min_length=1, max_length=800)


class RelationshipOptions(BaseModel):
    description: str = Field(default='', max_length=400)


def install_cognition_api(app, coordinator):
    mind = coordinator.cognition

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get('/api/actors/{actor_id}/mind')
    async def inspect(actor_id: str):
        mind.pump()
        return call(mind.inspect, actor_id)

    @app.post('/api/actors/{actor_id}/mind')
    async def configure(actor_id: str, options: MindOptions):
        return call(mind.configure, actor_id, enabled=options.enabled, reset=options.reset)

    @app.get('/api/actors/{actor_id}/relationships')
    async def relationships(actor_id: str):
        return {'relationships': call(mind.relationships, actor_id)}

    @app.post('/api/actors/{actor_id}/relationships/{peer_id}')
    async def relationship(actor_id: str, peer_id: str, options: RelationshipOptions):
        return {'relationships': call(mind.set_relationship, actor_id, peer_id, options.description)}

    @app.get('/api/actors/{actor_id}/experiences')
    async def experiences(actor_id: str, before: int | None = None, limit: int = 30):
        rows = call(mind.experiences, actor_id, before, limit)
        return {'experiences': rows, 'nextCursor': rows[-1]['sourceSeq'] if rows else None}

    @app.post('/api/actors/{actor_id}/experiences/{experience_id}/forget')
    async def forget(actor_id: str, experience_id: str):
        call(mind.revise, actor_id, experience_id, forget=True)
        return {'status': 'forgotten'}

    @app.post('/api/actors/{actor_id}/experiences/{experience_id}/correct')
    async def correct(actor_id: str, experience_id: str, payload: Correction):
        call(mind.revise, actor_id, experience_id, interpretation=payload.interpretation)
        return {'status': 'corrected'}
