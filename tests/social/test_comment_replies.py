from types import SimpleNamespace

from sqlalchemy import create_engine, text

from backend.database import session_scope
from backend.models import Actor
from backend.platform.migrations import migrate_comment_replies


def test_comment_reply_is_persistent_and_cannot_target_another_post(world):
    coordinator, client, actors = world
    service = coordinator.moments.service
    with session_scope() as session:
        author = session.get(Actor, actors[0])
        post = service._create_social_post(session, author=author, body='练习记录')
        other = service._create_social_post(session, author=author, body='另一条动态')
        parent = service._create_social_comment(session, post=post, author_id=actors[1], body='练得不错')
        post_id, other_id, parent_id = post.id, other.id, parent.id
    response = client.post('/api/posts/comment', json={'postId': post_id, 'body': '赞同', 'replyToId': parent_id})
    assert response.status_code == 200
    post = next(p for p in client.get('/api/posts').json()['posts'] if p['id'] == post_id)
    assert post['comments'][-1]['replyToId'] == parent_id
    assert client.post('/api/posts/comment', json={
        'postId': other_id, 'body': '不能跨动态回复', 'replyToId': parent_id}).status_code == 404
    assert client.post('/api/posts/comment', json={
        'postId': post_id, 'body': '不能回复不存在的评论', 'replyToId': 'missing'}).status_code == 404


def test_legacy_explicit_address_recovers_only_an_unambiguous_reply(world):
    coordinator, _, actors = world
    service = coordinator.moments.service
    with session_scope() as session:
        author, peer = session.get(Actor, actors[0]), session.get(Actor, actors[1])
        post = service._create_social_post(session, author=author, body='练习记录')
        parent = service._create_social_comment(session, post=post, author_id=peer.id, body='练得不错')
        service._create_social_comment(session, post=post, author_id=actors[2], body='有机会一起练习')
        reply = service._create_social_comment(session, post=post, author_id=author.id, body=peer.name+'，下次一起吧。')
        rows = service.serialize_post(session, post)['comments']
        assert next(row for row in rows if row['id'] == reply.id)['replyToId'] == parent.id
        service._create_social_comment(session, post=post, author_id=peer.id, body='周末如何？')
        ambiguous = service._create_social_comment(session, post=post, author_id=author.id, body=peer.name+'，可以。')
        rows = service.serialize_post(session, post)['comments']
        assert next(row for row in rows if row['id'] == ambiguous.id)['replyToId'] is None


def test_reply_schema_upgrade_backs_up_existing_comments_and_is_idempotent(tmp_path):
    path = tmp_path / 'old.db'
    engine = create_engine('sqlite:///' + path.as_posix())
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE social_comments(id VARCHAR PRIMARY KEY, body TEXT)'))
        conn.execute(text("INSERT INTO social_comments VALUES ('old', 'original')"))
    settings = SimpleNamespace(workspace_state_path=tmp_path / 'state/workspace.json')
    migrate_comment_replies(engine, settings)
    migrate_comment_replies(engine, settings)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT body,reply_to_id FROM social_comments')).one() == ('original', None)
    backups = list((tmp_path / 'state/migration-backups').glob('*-comment-replies.db'))
    assert len(backups) == 1
    backup = create_engine('sqlite:///' + backups[0].as_posix())
    with backup.connect() as conn:
        assert conn.execute(text('SELECT body FROM social_comments')).scalar() == 'original'
    backup.dispose()
    engine.dispose()
