"""Explicit synthetic-user approvals for isolated acceptance fixtures only."""
import hashlib
from pathlib import Path


def pending_reviews(run,workspace):
    if Path(run.get('workspace','')).resolve()!=Path(workspace).resolve():
        raise AssertionError('Acceptance cannot approve another workspace')
    for review in run.get('stageReviews',{}).values():
        if review['status']!='waiting_user':continue
        assert review['secretary']['approved'] is True
        material=review['material']
        assert not (material.get('result') or {}).get('unresolved'),material
        for artifact in material.get('artifacts',[]):
            path=(Path(workspace)/artifact['path']).resolve()
            assert path.is_relative_to(Path(workspace).resolve()) and path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest()==artifact['sha256']
        yield review['id']


async def approve_async(client,run,workspace):
    approved=[]
    for review_id in pending_reviews(run,workspace):
        response=await client.post(f"/api/runs/{run['id']}/reviews/{review_id}",
            json={'decision':'approved','note':'隔离合成场景：已核对产物和当前阶段。'})
        response.raise_for_status();approved.append(review_id)
    return approved


def approve_sync(post,run,workspace):
    approved=[]
    for review_id in pending_reviews(run,workspace):
        post(f"/api/runs/{run['id']}/reviews/{review_id}",
            {'decision':'approved','note':'隔离合成场景：已核对产物和当前阶段。'})
        approved.append(review_id)
    return approved
