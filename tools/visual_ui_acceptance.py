"""JUUS visual fixtures and browser checks; synthetic data, no model calls.

Run with --baseline before UI changes. Output is separate from earlier evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'validation' / 'juus-visual-20260925'
STATE = Path(tempfile.mkdtemp(prefix='juus-visual-'))
os.environ.update(AZURJUUS_UI_TEST_DIRECTORY=str(STATE), AZURJUUS_MIND_ENABLED='0',
                  AZURJUUS_EXPRESSION_ENABLED='0', AZURJUUS_SOCIAL_ENGINE_ENABLED='0')
from ui_test_server import build_server
from playwright.sync_api import sync_playwright, expect


def install_fixture(server):
    @server.app.post('/__acceptance/visual')
    async def seed():
        from datetime import datetime, timedelta, timezone
        from backend.database import session_scope
        from backend.models import Actor, Conversation
        from backend.social.social_models import SocialPublication
        service = server.app.state.service
        with session_scope() as session:
            data = service.build_snapshot(session)
            actors = [session.get(Actor, a['id']) for a in data['agents']]
            dm = next(c for c in data['conversations'] if c['kind'] == 'dm')
            group = next(c for c in data['conversations'] if c['kind'] == 'group')
            for room in [dm, group]:
                speakers = [a for a in actors if a.id in room['memberIds']]
                for i, body in enumerate(['今天港区的风很舒服。', '刚好整理完资料，要一起去看看吗？', '好，等我一会儿。', '那就老地方见。']):
                    speaker = 'commander' if i == 2 else speakers[i % len(speakers)].id
                    service._append_message(session, session.get(Conversation, room['id']), speaker,
                                            'text', body, message_id=f"visual-{room['id']}-{i}")
            posts = []
            for i in range(26):
                body = (['今天的海风，适合在甲板上待一会儿。', '午后的茶点准备好了，也给你留了一份。',
                         '整理书架时找到了一张旧照片。下次再和你说说那时候的事。',
                         '和同伴约好了明天的练习，今天就早点休息。'][i % 4] + f' · {i + 1:02}')
                media = '/resources/ui/javelin.png' if i == 25 else '/resources/ui/missing-visual.png' if i == 23 else None
                post = service._create_social_post(session, author=actors[i % len(actors)], body=body,
                    media_url=media, audience={'kind':'selected','actorIds':[actors[0].id]} if i == 22 else {'kind':'port','actorIds':[]})
                post.created_at = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=i)
                if i == 21:
                    session.delete(session.get(SocialPublication, post.id))
                if i == 25:
                    service._create_social_comment(session, post=post, author_id=actors[1].id, body='这个表情，倒是很适合今天的心情。')
                posts.append(post.id)
            session.flush()
        return {'posts': posts, 'dm': dm['title'], 'group': group['title']}


def run(baseline=False):
    out = OUT / ('before' if baseline else 'after')
    out.mkdir(parents=True, exist_ok=True)
    assets = OUT / 'assets'
    assets.mkdir(exist_ok=True)
    server = build_server(port=0)
    install_fixture(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {'status':'failed', 'checks':[], 'isolatedData':str(STATE)}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={'width':1280,'height':720},
                                          record_video_dir=str(out/'video'))
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            base = f'http://127.0.0.1:{server.server_address[1]}'

            def cached_assets(route):
                if route.request.url.startswith(base):
                    route.continue_()
                    return
                if route.request.resource_type != 'image':
                    route.abort()
                    return
                path = assets / hashlib.sha256(route.request.url.encode()).hexdigest()
                try:
                    if path.exists():
                        route.fulfill(body=path.read_bytes(), content_type='image/png')
                    elif baseline:
                        response = route.fetch(timeout=15000)
                        if response.ok:
                            path.write_bytes(response.body())
                            route.fulfill(response=response)
                        else:
                            route.abort()
                    else:
                        route.abort()
                except Exception:
                    route.abort()
            page.route('**/*', cached_assets)
            page.goto(base)
            page.get_by_label('消息输入', exact=True).wait_for()
            fixture = page.request.post(base+'/__acceptance/visual').json()
            page.reload()
            page.get_by_label('消息输入', exact=True).wait_for()

            def shot(name):
                page.wait_for_timeout(300)
                page.screenshot(path=str(out/f'{name}.png'))
                report.setdefault('geometry', {})[name]=page.evaluate("""()=>Object.fromEntries(['html','body','#app','.workspace','.topbar','.moments-page','.moments-feed'].map(s=>{const e=document.querySelector(s);const r=e?.getBoundingClientRect();return [s,e?{y:r.y,height:r.height,scroll:e.scrollTop,scrollHeight:e.scrollHeight}:null]}))""")
            page.locator('.conversation-card').filter(has=page.get_by_text(fixture['dm'],exact=True)).click()
            shot('private-1280')
            page.locator('.conversation-card').filter(has=page.get_by_text(fixture['group'],exact=True)).click()
            shot('group-1280')
            page.get_by_label('查看成员资料', exact=True).click()
            shot('members')
            page.get_by_label('查看成员资料', exact=True).click()
            page.get_by_label('打开设置', exact=True).click()
            shot('settings')
            page.get_by_role('button', name='角色资料与心智', exact=True).click()
            shot('character')
            page.get_by_label('关闭设置', exact=True).click()
            page.request.post(base+'/__acceptance/seed_run')
            page.get_by_label('查看工作记录', exact=True).click()
            shot('work')
            page.get_by_label('关闭任务详情', exact=True).click()
            page.get_by_label('朋友圈', exact=True).click()
            page.locator('.moments-card').first.wait_for()
            shot('moments-list')
            if not baseline:
                check_moments(page, base, fixture, shot, report)
            page.get_by_role('button', name='新建动态', exact=True).click()
            page.get_by_label('动态内容').fill('今天也辛苦了。一起分享港区的新鲜事吧。')
            shot('moments-compose')
            page.get_by_role('button', name='返回动态', exact=True).click()
            if not baseline:
                check_sizes(page, fixture, shot, report)
                check_messages(page, base, shot, report)
            assert not errors, errors
            report.update(status='passed', browserErrors=errors)
            context.close()
            if page.video:
                page.video.save_as(str(out/'walkthrough.webm'))
                report['video']='walkthrough.webm'
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=15)
        server.server_close()
        (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status':report['status'],'checks':report['checks'],'report':str(out/'report.json')}, ensure_ascii=False))


def check_moments(page, base, fixture, shot, report):
    def card(index):
        return page.locator(f'.moments-card[data-post-id="{fixture["posts"][index]}"]')
    back = page.get_by_role('button', name='返回动态', exact=True)
    card(25).click()
    expect(page.get_by_alt_text('动态配图')).to_be_visible()
    shot('moments-image-detail')
    back.click()
    expect(card(25)).to_be_focused()
    card(24).click()
    expect(page.locator('.moment-detail--media')).to_have_count(0)
    shot('moments-text-detail')
    draft = page.get_by_placeholder('写条评论…')
    draft.fill('保留这条草稿')
    draft.focus()
    response = page.request.post(base+'/api/posts/comment', data={'postId':fixture['posts'][24], 'body':'实时到达的评论'})
    assert response.ok, response.text()
    expect(page.locator('.moments-comment').filter(has_text='实时到达的评论')).to_be_visible()
    expect(draft).to_have_value('保留这条草稿')
    expect(draft).to_be_focused()
    # A composing Enter must not submit the comment form.
    draft.evaluate("e=>e.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',isComposing:true,bubbles:true,cancelable:true}))")
    expect(draft).to_have_value('保留这条草稿')
    back.click()
    card(24).click()
    expect(draft).to_have_value('保留这条草稿')
    back.click()
    card(23).click()
    expect(page.locator('.moment-detail--media')).to_have_count(0)
    expect(page.get_by_alt_text('动态配图')).to_have_count(0)
    back.click()
    for index in [22, 21]:
        card(index).click()
        expect(page.get_by_role('button', name='点赞', exact=True)).to_be_disabled()
        expect(page.get_by_placeholder('写条评论…')).to_have_count(0)
        shot('moments-readonly' if index == 22 else 'moments-archive')
        back.click()
    page.get_by_role('button', name='查看更多', exact=True).click()
    expect(card(0)).to_be_attached()
    expect(page.locator('.moments-feed')).to_have_attribute('aria-busy','false')
    card(0).scroll_into_view_if_needed()
    scroll = page.locator('.moments-feed').evaluate('e=>e.scrollTop')
    card(0).click()
    page.get_by_role('button', name='点赞', exact=True).click()
    expect(page.get_by_role('button', name='取消点赞', exact=True)).to_be_visible()
    page.get_by_placeholder('写条评论…').fill('分页之后也能评论。')
    page.get_by_role('button', name='发送', exact=True).click()
    expect(page.locator('.moments-comment').filter(has_text='分页之后也能评论。')).to_be_visible()
    back.click()
    expect(card(0)).to_be_focused()
    restored=page.locator('.moments-feed').evaluate('e=>e.scrollTop')
    assert abs(restored-scroll)<2, {'before':scroll,'after':restored,'count':page.locator('.moments-card').count()}
    expect(page.locator('.moments-card')).to_have_count(26)
    expect(card(0).locator('.moments-count strong')).to_have_text('1')
    # Failed loading keeps the old window, with an explicit retry.
    page.route('**/api/posts?*', lambda route: route.fulfill(status=503, json={'detail':'合成刷新失败'}))
    page.evaluate("window.dispatchEvent(new CustomEvent('azur-run-event',{detail:{type:'workspace.changed'}}))")
    expect(page.get_by_role('alert').filter(has_text='合成刷新失败')).to_be_visible()
    expect(page.locator('.moments-card')).to_have_count(26)
    page.unroute('**/api/posts?*')
    page.get_by_role('button', name='重试刷新', exact=True).click()
    expect(page.get_by_role('alert').filter(has_text='合成刷新失败')).to_have_count(0)
    page.get_by_role('button', name='新建动态', exact=True).click()
    page.get_by_label('动态内容').fill('指定成员的界面验收动态。')
    page.get_by_label('可见范围', exact=False).select_option('selected')
    expect(page.get_by_role('button', name='发布动态', exact=True)).to_be_disabled()
    page.locator('.moments-audience input').first.check()
    page.get_by_role('button', name='发布动态', exact=True).click()
    expect(page.locator('.moments-card').filter(has_text='指定成员的界面验收动态。')).to_be_visible()
    page.wait_for_function("document.querySelector('.moments-feed').scrollTop===0")
    report['checks'] += ['Image/text detail, return focus and scroll', '26-post pagination survives likes/comments',
        'Live comments preserve draft and focus', 'Readonly/archive rules and missing media fallback',
        'Refresh failure retains feed; explicit retry', 'Selected-audience publication validation']


def check_sizes(page, fixture, shot, report):
    report['layouts'] = []
    for width, height in [(1280,720),(1600,900),(1920,1080),(820,560),(768,600)]:
        page.set_viewport_size({'width':width,'height':height})
        page.get_by_label('聊天', exact=True).click()
        if width<=800 and page.get_by_label('返回会话列表', exact=True).is_visible():
            page.get_by_label('返回会话列表', exact=True).click()
        page.locator('.conversation-card').filter(has=page.get_by_text(fixture['group'],exact=True)).click()
        composer = page.get_by_label('消息输入', exact=True)
        expect(composer).to_be_visible()
        bounds = composer.bounding_box()
        assert bounds['y']+bounds['height']<=height, bounds
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert page.locator('.chat-panel').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        shot(f'group-{width}x{height}')
        page.get_by_label('查看成员资料', exact=True).click()
        expect(page.locator('.member-popover')).to_be_visible()
        panel=page.locator('.member-popover').bounding_box()
        assert panel['x']>=0 and panel['x']+panel['width']<=width+1
        page.get_by_label('查看成员资料', exact=True).click()
        page.get_by_label('打开设置', exact=True).click()
        page.get_by_role('button', name='自主生活', exact=True).click()
        expect(page.locator('.settings-content')).to_be_visible()
        assert page.locator('.settings-modal').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        shot(f'settings-life-{width}x{height}')
        page.get_by_label('关闭设置', exact=True).click()
        page.get_by_label('朋友圈', exact=True).click()
        page.locator(f'.moments-card[data-post-id="{fixture["posts"][25]}"]').click()
        expect(page.get_by_alt_text('动态配图')).to_be_visible()
        assert page.locator('.moment-detail').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        shot(f'moments-detail-{width}x{height}')
        page.get_by_role('button', name='返回动态', exact=True).click()
        report['layouts'].append({'width':width,'height':height,'composer':bounds})
    report['checks'].append('Five viewports: chat, members, settings, image detail; no horizontal overflow')


def check_messages(page, base, shot, report):
    page.set_viewport_size({'width':1280,'height':720})
    page.get_by_label('聊天', exact=True).click()
    alignment=page.request.post(base+'/__acceptance/alignment').json()
    page.locator('.conversation-card').filter(has=page.get_by_text(alignment['conversationTitle'],exact=True)).click()
    for mid in alignment['messageIds']:
        bubble=page.locator(f'[data-message-id="{mid}"] .message-bubble')
        expect(bubble).to_be_visible()
        assert bubble.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
    shot('long-message')
    fixture=page.request.post(base+'/__acceptance/history_stream').json()
    page.locator('.conversation-card').filter(has=page.get_by_text(fixture['conversationTitle'],exact=True)).click()
    page.get_by_role('button', name='查看更早的消息', exact=True).wait_for()
    assert page.locator('.message-row').count()<=101
    composer=page.get_by_label('消息输入', exact=True)
    composer.fill('中文输入焦点与选区保持')
    composer.dispatch_event('compositionstart')
    composer.press('Enter')
    assert composer.input_value().strip()=='中文输入焦点与选区保持'
    composer.dispatch_event('compositionend')
    composer.evaluate('e=>e.setSelectionRange(2,5)')
    page.wait_for_function("rid=>fetch('/api/runs/'+rid).then(r=>r.json()).then(d=>d.run.status==='completed')", arg=fixture['runId'])
    assert composer.evaluate('e=>document.activeElement===e&&e.selectionStart===2&&e.selectionEnd===5')
    page.get_by_role('button', name='立即显示', exact=True).first.click() if page.get_by_role('button', name='立即显示', exact=True).count() else None
    page.wait_for_function("mid=>{const e=document.querySelector('[data-message-id=\"'+mid+'\"]');return e&&e.textContent.includes('流59')}", arg=fixture['messageId'])
    assert composer.input_value().strip()=='中文输入焦点与选区保持'
    # Clicking show-now legitimately moves focus; check selection through streaming before returning focus.
    composer.focus()
    assert composer.evaluate('e=>e.selectionStart===2&&e.selectionEnd===5')
    page.get_by_role('button', name='查看更早的消息', exact=True).click()
    assert page.locator('.message-row').count()>=200
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('.send-button').evaluate('e=>getComputedStyle(e).transitionDuration')=='0s'
    page.emulate_media(reduced_motion='no-preference')
    shot('history-stream')
    report['checks'].append('Long messages, 350-message incremental history, 60 deltas, IME Enter and selection, reduced motion')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    run(parser.parse_args().baseline)
