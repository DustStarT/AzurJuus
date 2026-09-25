"""Local-user Moments API; autonomous actors use the internal service only."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from backend.database import session_scope


def install_moment_routes(app, service, publish):
    @app.get('/api/posts/status')
    async def api_post_status():
        return app.state.runs.moments.status()

    @app.get('/api/posts')
    async def api_posts(mode: str = 'feed', before: str = '', limit: int = 20):
        try:
            with session_scope() as session:
                return service.list_posts(session,mode=mode,before=before,limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error

    @app.post("/api/posts/like")
    async def api_post_like(payload: dict[str, Any]):
        try:
            with session_scope() as session:
                snapshot = service.toggle_post_like(session, str(payload.get("postId") or ""))
                liked=next((post['likedByUser'] for post in snapshot['posts'] if post['id']==payload.get('postId')),False)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if liked:app.state.runs.moments.notify_like(str(payload.get('postId')))
        await publish("post.created", {"snapshot": snapshot})
        return {"status": "ok", "snapshot": snapshot}

    @app.post("/api/posts/comment")
    async def api_post_comment(payload: dict[str, Any]):
        try:
            with session_scope() as session:
                snapshot = service.add_comment(
                    session,
                    post_id=str(payload.get("postId") or ""),
                    body=str(payload.get("body") or ""),
                )
                comment_id=session.info.get('created_social_comment_id')
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if comment_id:app.state.runs.moments.notify_comment(comment_id)
        await publish("post.comment.created", {"snapshot": snapshot})
        return {"status": "ok", "snapshot": snapshot}

    @app.post("/api/posts/publish")
    async def api_post_publish(payload: dict[str, Any]):
        try:
            with session_scope() as session:
                snapshot = service.publish_post(
                    session,
                    author_id=str(payload.get("authorId")) if payload.get("authorId") else None,
                    body=str(payload.get("body") or ""),
                    media_url=str(payload.get("mediaUrl")) if payload.get("mediaUrl") else None,
                    audience=payload.get('audience'),
                )
                post_id=session.info.get('created_social_post_id')
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if post_id:app.state.runs.moments.notify_post(post_id)
        await publish("post.created", {"snapshot": snapshot})
        return {"status": "ok", "snapshot": snapshot}

