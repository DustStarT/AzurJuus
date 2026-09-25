from __future__ import annotations

from typing import Any
from uuid import uuid4
import time

from sqlalchemy import select
from sqlalchemy.orm import Session


from backend.platform.constants import DEFAULT_USER
from backend.models import (
    Actor,
    SocialComment,
    SocialPost,
)
from backend.platform.service_utils import (
    preview_text,
)
from backend.social.social_models import SocialPublication, SocialReaction


class SocialService:
    """Social operations shared by the application service."""

    def toggle_favorite_agent(self, session: Session, agent_id: str) -> dict[str, Any]:
        agent = session.get(Actor, agent_id)
        if agent is None:
            raise ValueError("Agent not found.")
        agent.favorite = not agent.favorite
        session.add(agent)
        posts = session.scalars(select(SocialPost).where(SocialPost.author_id == agent_id)).all()
        for post in posts:
            post.following = agent.favorite
            session.add(post)
        session.flush()
        return self.build_snapshot(session)

    def _audience(self, session: Session, audience: dict | None, author_id: str) -> dict:
        from backend.characters.character_identity import roster_actors
        audience = audience or {'kind': 'port', 'actorIds': []}
        kind = audience.get('kind', 'port')
        if kind not in {'port', 'selected'}:
            raise ValueError('动态可见范围无效。')
        ids = audience.get('actorIds', []) if kind == 'selected' else []
        if not isinstance(ids, list) or len(ids) > 24 or any(not isinstance(v, str) for v in ids):
            raise ValueError('动态可见对象无效。')
        ids = list(dict.fromkeys(ids))
        valid = {DEFAULT_USER['id'],*(a.id for a in roster_actors(session))}
        if kind == 'selected' and (not ids or not set(ids) <= valid):
            raise ValueError('请选择当前可见的成员。')
        return {'kind': kind, 'actorIds': ids}

    @staticmethod
    def post_visible(session: Session, post: SocialPost, viewer_id: str, observer=False) -> bool:
        publication = session.get(SocialPublication, post.id)
        if not publication or publication.status != 'published':
            return bool(observer and viewer_id == DEFAULT_USER['id'])
        if observer and viewer_id == DEFAULT_USER['id']:
            return True
        audience = publication.data.get('audience') or {'kind': 'port'}
        return (viewer_id == post.author_id or audience.get('kind') == 'port'
                or viewer_id in audience.get('actorIds', []))

    @staticmethod
    def audience_actor_ids(session: Session, post: SocialPost) -> list[str]:
        from backend.characters.character_identity import roster_actors
        publication=session.get(SocialPublication,post.id)
        if not publication or publication.status!='published':return []
        audience=publication.data.get('audience') or {'kind':'port'}
        roster_members=roster_actors(session)
        roster={a.id for a in roster_members}
        if audience.get('kind')=='port':
            return [a.id for a in roster_members]
        ids=[aid for aid in audience.get('actorIds',[]) if aid in roster]
        return list(dict.fromkeys([*ids,*([post.author_id] if post.author_id in roster else [])]))

    def toggle_post_like(self, session: Session, post_id: str) -> dict[str, Any]:
        post = session.get(SocialPost, post_id)
        if post is None or not self.post_visible(session, post, DEFAULT_USER['id']):
            raise ValueError('动态不可见。')
        existing = session.scalar(select(SocialReaction).where(
            SocialReaction.post_id == post_id, SocialReaction.actor_id == DEFAULT_USER['id']))
        if existing:
            session.delete(existing)
        else:
            session.add(SocialReaction(id=f'reaction-{uuid4().hex}',post_id=post_id,
                actor_id=DEFAULT_USER['id'],at=time.time()))
        session.flush()
        post.likes = len(session.scalars(select(SocialReaction).where(SocialReaction.post_id == post_id)).all())
        post.liked_by_user = not bool(existing)
        return self.build_snapshot(session)

    def add_comment(self, session: Session, post_id: str, body: str) -> dict[str, Any]:
        post = session.get(SocialPost, post_id)
        if post is None or not self.post_visible(session, post, DEFAULT_USER['id']):
            raise ValueError('动态不可见。')
        content = str(body or "").strip()
        if not content or len(content) > 1000:
            raise ValueError('评论长度须为 1 至 1000 字。')
        comment=self._create_social_comment(session, post=post, author_id=DEFAULT_USER["id"], body=content)
        session.info['created_social_comment_id']=comment.id
        session.flush()
        return self.build_snapshot(session)

    def publish_post(self, session: Session, author_id: str | None, body: str,
                     media_url: str | None = None, audience: dict | None = None) -> dict[str, Any]:
        if author_id not in {None, DEFAULT_USER['id']}:
            raise ValueError('用户不能代角色发表动态。')
        content = str(body or "").strip()
        if not content or len(content) > 4000:
            raise ValueError('动态长度须为 1 至 4000 字。')
        author = session.get(Actor, DEFAULT_USER['id'])
        if author is None:raise ValueError('用户不存在。')
        post=self._create_social_post(
            session,
            author=author,
            body=content,
            media_url=media_url,
            audience=self._audience(session,audience,author.id),source_kind='user',
        )
        session.info['created_social_post_id']=post.id
        session.flush()
        return self.build_snapshot(session)

    def list_posts(self, session: Session, *, mode='feed', before='', limit=20) -> dict:
        if mode not in {'feed','observe'}:raise ValueError('动态视图无效。')
        limit=min(50,max(1,int(limit)))
        query=select(SocialPost).order_by(SocialPost.created_at.desc(),SocialPost.id.desc())
        if before:
            cursor=session.get(SocialPost,before)
            if not cursor:raise ValueError('分页位置无效。')
            from sqlalchemy import or_, and_
            query=query.where(or_(SocialPost.created_at<cursor.created_at,
                and_(SocialPost.created_at==cursor.created_at,SocialPost.id<cursor.id)))
        rows=[]
        for post in session.scalars(query):
            if self.post_visible(session,post,DEFAULT_USER['id'],observer=mode=='observe'):
                rows.append(self.serialize_post(session,post))
                if len(rows)>limit:break
        return {'posts':rows[:limit],'nextCursor':rows[limit-1]['id'] if len(rows)>limit else None,
            'mode':mode}

    def actor_react(self, session: Session, post_id: str, actor_id: str, action: str,
                    body: str = '') -> bool:
        from backend.characters.character_identity import roster_actors
        post=session.get(SocialPost,post_id)
        actor=session.get(Actor,actor_id)
        if not post or not actor or actor.kind!='agent' or not actor.is_active or post.author_id==actor_id:
            return False
        if actor_id not in {member.id for member in roster_actors(session)}:return False
        if not self.post_visible(session,post,actor_id):return False
        if action=='like':
            if session.scalar(select(SocialReaction).where(SocialReaction.post_id==post_id,
                    SocialReaction.actor_id==actor_id)):
                return False
            session.add(SocialReaction(id=f'reaction-{uuid4().hex}',post_id=post_id,
                actor_id=actor_id,at=time.time()))
            session.flush()
            post.likes=len(session.scalars(select(SocialReaction).where(SocialReaction.post_id==post_id)).all())
        elif action=='comment':
            content=body.strip()
            if not content or len(content)>1000:return False
            prior=session.scalars(select(SocialComment).where(SocialComment.post_id==post_id,
                SocialComment.author_id==actor_id)).all()
            if len(prior)>=2:return False
            self._create_social_comment(session,post=post,author_id=actor_id,body=content)
        else:return False
        return True

    def _create_social_post(
        self,
        session: Session,
        *,
        author: Actor,
        body: str,
        media_url: str | None = None,
        likes: int = 0,
        audience: dict | None = None,
        source_kind: str = 'thought',
        source_event_id: str = '',
    ) -> SocialPost:
        content = " ".join(str(body or "").split())
        if not content:
            raise ValueError('动态内容不能为空。')
        if len(content)>4000:raise ValueError('动态内容过长。')
        visibility=self._audience(session,audience,author.id)
        post = SocialPost(
            id=f"post-{uuid4().hex[:12]}",
            author_id=author.id,
            excerpt=preview_text(content, 96),
            likes=max(0, likes),
            liked_by_user=False,
            art_label=author.name,
            art_mark=author.initials,
            art_palette=list(author.palette or ["#b8c5ef", "#f0f4ff", "#ffffff"]),
            media_url=media_url,
            following=author.favorite,
        )
        session.add(post)
        session.add(SocialPublication(post_id=post.id,status='published',data={
            'body':content,'audience':visibility,'sourceKind':source_kind,
            'sourceEventId':source_event_id,'createdAt':time.time()}))
        session.flush()
        self._record_memory(session, actor_id=author.id, conversation_id=None,
            source_kind='social.post',text=content)
        return post

    def _create_social_comment(self, session: Session, *, post: SocialPost, author_id: str, body: str) -> SocialComment:
        content = " ".join(str(body or "").split())
        comment = SocialComment(
            id=f"comment-{uuid4().hex[:12]}",
            post_id=post.id,
            author_id=author_id,
            body=content,
        )
        session.add(comment)
        session.flush()
        self._record_memory(session, actor_id=author_id, conversation_id=None, source_kind="social.comment", text=content)
        return comment
