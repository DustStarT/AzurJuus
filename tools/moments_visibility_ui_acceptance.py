"""Moments reader explanations and interaction permissions on isolated data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
STATE=Path(tempfile.mkdtemp(prefix='juus-moments-visibility-'))
OUT=ROOT/'validation'/'moments-publication-20261006'
os.environ.update(AZURJUUS_UI_TEST_DIRECTORY=str(STATE), AZURJUUS_MIND_ENABLED='0',
    AZURJUUS_EXPRESSION_ENABLED='0', AZURJUUS_SOCIAL_ENGINE_ENABLED='0',
    AZURJUUS_CHROMA_PATH=str(STATE/'chroma'))
sys.path.insert(0,str(ROOT))
from ui_test_server import build_server


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    server=build_server(port=0)

    @server.app.post('/__acceptance/moments_readers')
    async def seed():
        from sqlalchemy import select
        from backend.database import session_scope
        from backend.models import Actor, SocialPost
        with session_scope() as session:
            actors=session.scalars(select(Actor).where(Actor.kind=='agent')).all()
            author,peer=actors[:2]
            service=server.app.state.service
            restricted=service._create_social_post(session,author=author,body='只给同伴的一点心情。',
                audience={'kind':'selected','actorIds':[peer.id]})
            invited=service._create_social_post(session,author=author,body='这条也分享给指挥官。',
                audience={'kind':'selected','actorIds':[peer.id,'commander']})
            public=service._create_social_post(session,author=author,body='公开的一点想法。')
            session.add(SocialPost(id='visibility-archive',author_id=author.id,excerpt='旧记录只有摘要。',
                likes=0,liked_by_user=False,art_palette=[],following=False))
            return {'restricted':restricted.id,'invited':invited.id,'public':public.id,'peerName':peer.name}

    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    report={'status':'failed','checks':[],'realModelCalls':False}
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch()
            page=browser.new_page(viewport={'width':1280,'height':800})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            base=f'http://127.0.0.1:{server.server_address[1]}'
            page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
            page.goto(base)
            page.get_by_label('消息输入',exact=True).wait_for()
            response=page.request.post(base+'/__acceptance/moments_readers',headers={'Origin':base})
            assert response.ok,response.text()
            fixture=response.json()
            page.get_by_label('朋友圈',exact=True).click()
            page.locator('.moments-card[data-post-id="'+fixture['restricted']+'"]').click()
            expect(page.get_by_label('动态详情',exact=True).get_by_text('未向你分享',exact=False)).to_have_count(2)
            expect(page.get_by_text('作者仅向'+fixture['peerName']+'分享了这条动态，你不在分享范围内。这里以只读记录展示，无法点赞或评论。',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name='点赞',exact=True)).to_be_disabled()
            expect(page.get_by_placeholder('写条评论…')).to_have_count(0)
            page.evaluate("async () => { await Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {}))); }")
            page.screenshot(path=str(OUT/'restricted-post.png'))
            report['checks'].append('restricted post explains real recipients and disables interaction')
            response=page.request.post(base+'/api/posts/like',data={'postId':fixture['restricted']},headers={'Origin':base})
            assert response.status==404
            response=page.request.post(base+'/api/posts/comment',data={'postId':fixture['restricted'],'body':'越权评论'},headers={'Origin':base})
            assert response.status==404
            report['checks'].append('API rejects restricted like and comment')
            page.get_by_role('button',name='返回动态',exact=True).click()
            page.locator('.moments-card[data-post-id="'+fixture['invited']+'"]').click()
            expect(page.get_by_role('button',name='点赞',exact=True)).to_be_enabled()
            expect(page.get_by_placeholder('写条评论…')).to_be_visible()
            page.get_by_role('button',name='点赞',exact=True).click()
            expect(page.get_by_role('button',name='取消点赞',exact=True)).to_be_visible()
            page.get_by_placeholder('写条评论…').fill('已收到分享。')
            page.get_by_role('button',name='发送',exact=True).click()
            expect(page.get_by_text('已收到分享。',exact=True)).to_be_visible()
            report['checks'].append('selected post including commander allows actual like and comment')
            page.get_by_role('button',name='返回动态',exact=True).click()
            page.locator('.moments-card[data-post-id="'+fixture['public']+'"]').click()
            expect(page.get_by_role('button',name='点赞',exact=True)).to_be_enabled()
            expect(page.get_by_label('动态详情',exact=True).get_by_text('港区公开',exact=False)).to_be_visible()
            report['checks'].append('public post remains interactive')
            page.get_by_role('button',name='返回动态',exact=True).click()
            page.locator('.moments-card[data-post-id="visibility-archive"]').click()
            expect(page.get_by_text('历史归档，仅供查看',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name='点赞',exact=True)).to_be_disabled()
            report['checks'].append('archive remains explicitly read only')
            assert not errors,errors
            from sqlalchemy import select, func
            from backend.database import session_scope
            from backend.mind.mind_models import MindCall
            with session_scope() as session:
                assert session.scalar(select(func.count()).select_from(MindCall))==0
            report.update(status='passed',browserErrors=errors)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=15)
        server.server_close()
        report['serverStopped']=not thread.is_alive()
        (OUT/'ui.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['status']=='passed' and report['serverStopped'] else 1


if __name__=='__main__':raise SystemExit(main())
