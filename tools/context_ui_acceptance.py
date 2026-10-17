"""Browser acceptance of context settings against an isolated, offline app."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    home = Path(tempfile.mkdtemp(prefix='azur-context-ui-'))
    (home / 'work').mkdir()
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (home / 'business.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home / 'state/workspace.json'),
        AZURJUUS_WORKSPACE_ROOT=str(home / 'work'), AZURJUUS_REDIS_URL='', AZURJUUS_CHROMA_URL='',
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='0', AZURJUUS_REFLECTION_ENABLED='0',
        AZURJUUS_SKILL_TRIALS_ENABLED='0')
    from server import create_server
    server = create_server('127.0.0.1', 0)
    @server.app.post('/__acceptance/context-source')
    async def seed_source():
        from backend.database import session_scope
        from backend.models import Actor, Conversation
        from backend.context.contracts import ContextRequest
        c = server.app.state.runs
        service = server.app.state.service
        with session_scope() as s:
            data = service.build_snapshot(s)
        actor = data['agents'][0]
        dm = next(r for r in data['conversations'] if r['kind'] == 'dm' and actor['id'] in r['memberIds'])
        path = home / 'work/context-note.txt'
        path.write_text('Synthetic source CXT-UI-17: sailors exchange letters.', encoding='utf-8')
        run, _ = c.store.create({'actorId': actor['id'], 'actors': [actor], 'conversationId': dm['id'],
            'mode': 'task', 'collaborative': False, 'prompt': '读取 context-note.txt',
            'workspace': str(home / 'work')}, uuid4().hex)
        call_id = uuid4().hex
        result = await c.tools.execute(run, 'read_file', {'path': 'context-note.txt'}, call_id, lambda *_: None)
        c.store.put_call(call_id, run['id'], 'read_file', {'path': 'context-note.txt'}, 'completed', result, phase='main')
        c.store.update(run['id'], status='completed', result={'summary': '读取了合成资料 CXT-UI-17。'})
        bundle = c.context.collect(ContextRequest(actor['id'], dm['id'], query='context-note.txt', source_id='ui-source'))
        with session_scope() as s:
            service._append_message(s, s.get(Conversation, dm['id']), actor['id'], 'text',
                '这份资料的编号是 CXT-UI-17。', message_id='ui-context-message',
                metadata={'expression': True, 'contextTraceId': bundle.trace_id})
        stale, _ = c.store.create({'actorId': actor['id'], 'actors': [actor], 'conversationId': dm['id'],
            'mode': 'chat', 'collaborative': False, 'prompt': '历史聊天错误',
            'workspace': str(home / 'work')}, uuid4().hex)
        c.store.update(stale['id'], status='failed', error='历史错误不应常驻')
        other_dm = next(r for r in data['conversations'] if r['kind'] == 'dm' and r['id'] != dm['id'])
        task, _ = c.store.create({'actorId': actor['id'], 'actors': [actor], 'conversationId': other_dm['id'],
            'mode': 'task', 'collaborative': False, 'prompt': '另一个会话中需要处理的任务',
            'workspace': str(home / 'work')}, uuid4().hex)
        c.store.update(task['id'], status='failed', error='合成任务错误')
        with session_scope() as s:
            post = service._create_social_post(s, author=s.get(Actor, actor['id']), body='评论回复分层验收')
            parent = service._create_social_comment(s, post=post, author_id=data['agents'][1]['id'], body='被回复的评论')
            service._create_social_comment(s, post=post, author_id=data['agents'][2]['id'], body='另一条独立评论')
            reply = service._create_social_comment(s, post=post, author_id=actor['id'], body='应当紧跟目标评论的回复', reply_to_id=parent.id)
            post_id, parent_id, reply_id = post.id, parent.id, reply.id
        return {'title': dm['title'], 'dmId': dm['id'], 'actor': actor,
            'postId': post_id, 'parentId': parent_id, 'replyId': reply_id}
    @server.app.post('/__acceptance/chat-failure')
    async def fail_chat(payload: dict):
        c = server.app.state.runs
        actor = payload['actor']
        run, _ = c.store.create({'actorId': actor['id'], 'actors': [actor], 'conversationId': payload['dmId'],
            'mode': 'chat', 'collaborative': False, 'prompt': '合成重试内容',
            'workspace': str(home / 'work')}, uuid4().hex)
        c.store.update(run['id'], status='failed', error=payload.get('error', '当前回复失败'))
        return {'runId': run['id']}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_address[1]}'
    out = ROOT / 'validation/context-ui'
    out.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'checks': []}
    try:
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for _ in range(100):
                try:
                    if page.request.get(url + '/api/health').ok:
                        break
                except Exception:
                    pass
                time.sleep(.1)
            info = page.request.post(url + '/__acceptance/context-source').json()
            page.goto(url)
            page.get_by_text(info['title'], exact=True).first.click()
            expect(page.get_by_text('历史错误不应常驻', exact=True)).to_have_count(0)
            expect(page.locator('.rail-work i')).to_have_text('1')
            page.get_by_label('打开任务中心', exact=True).click()
            expect(page.locator('.run-overview h3')).to_have_text('另一个会话中需要处理的任务')
            page.get_by_label('关闭任务详情', exact=True).click()
            report['checks'].append('historical chat errors stay hidden; badge and drawer target the same task across conversations')
            page.request.post(url + '/__acceptance/chat-failure', data=info)
            expect(page.locator('.message-scroll [role="alert"]')).to_contain_text('当前回复失败')
            page.get_by_label('关闭回复错误提示', exact=True).click()
            expect(page.get_by_text('当前回复失败', exact=True)).to_have_count(0)
            page.reload()
            page.get_by_label('消息输入', exact=True).wait_for()
            expect(page.get_by_text('当前回复失败', exact=True)).to_have_count(0)
            page.request.post(url + '/__acceptance/chat-failure', data={**info, 'error': 'MindInterrupted: 新事件已到达'})
            page.wait_for_timeout(800)
            expect(page.get_by_text('MindInterrupted: 新事件已到达', exact=True)).to_have_count(0)
            report['checks'].append('live chat failure is dismissible and never resurrected on reload; superseded mind cycles stay silent')
            page.get_by_label('打开设置').click()
            page.get_by_role('button', name='上下文与检索', exact=True).click()
            page.get_by_role('heading', name='上下文与检索').wait_for()
            page.screenshot(path=str(out / 'context-settings-initial.png'), full_page=True)
            report['apiStatus'] = page.request.get(url + '/api/context/settings').status
            page.get_by_label('检索方式', exact=True).select_option('lexical')
            page.get_by_role('button', name='保存检索配置', exact=True).click()
            page.get_by_text('检索配置已保存。', exact=True).wait_for()
            assert page.request.get(url + '/api/context/settings').json()['provider'] == 'lexical'
            page.get_by_label('角色表达与消耗', exact=True).select_option('compact')
            page.get_by_role('button', name='保存设置', exact=True).click()
            actual = page.request.get(url + '/api/workspace/load').json()
            assert actual['workspace']['settings']['dialoguePreset'] == 'compact'
            report['checks'].append('preset and lexical provider persist through real settings controls')
            page.get_by_role('button', name='重建派生索引', exact=True).click()
            page.get_by_text('已完成，向量由后台继续建立。', exact=True).wait_for()
            report['checks'].append('reindex button completes without a model or cloud credentials')
            page.screenshot(path=str(out / 'context-settings.png'), full_page=True)
            page.get_by_label('检索方式', exact=True).select_option('cloud')
            page.get_by_label('Embedding 模型', exact=True).wait_for()
            assert page.get_by_label('独立密钥', exact=True).get_attribute('type') == 'password'
            page.screenshot(path=str(out / 'context-cloud-settings.png'), full_page=True)
            assert not errors, errors
            report['checks'].append('cloud settings reveal independent password input; no page errors')
            for label in ['角色资料与心智', '任务参与', '自主生活', '关系与世界观', '外观与动态']:
                page.get_by_role('button', name=label, exact=True).click()
                if label == '角色资料与心智':
                    page.locator('.profile-settings').wait_for()
                    selector = page.locator('.terminal-settings > label select')
                    values = selector.locator('option').evaluate_all('(items)=>items.map(item=>item.value)')
                    selector.select_option(values[1])
                    expect(page.locator('.profile-settings')).to_have_count(1)
                    expect(page.get_by_label('显示称呼', exact=True)).not_to_have_value('')
                elif label == '任务参与':
                    field = page.get_by_label('单次任务成员上限', exact=True)
                    expect(field).to_be_enabled()
                    field.fill('5')
                    page.get_by_role('button', name='保存任务设置', exact=True).click()
                    expect(page.get_by_text('设置已保存', exact=True)).to_be_visible()
                    assert page.request.get(url + '/api/terminal/settings').json()['settings']['maxTaskMembers'] == 5
                elif label == '自主生活':
                    field = page.get_by_label('每小时自主调用上限', exact=True)
                    field.fill('77')
                    page.get_by_role('button', name='保存自主生活设置', exact=True).click()
                    expect(page.get_by_text('已保存', exact=True)).to_be_visible()
                    assert page.request.get(url + '/api/life').json()['settings']['hourlyCalls'] == 77
                elif label == '关系与世界观':
                    page.get_by_label('可点击人物关系图', exact=True).wait_for()
                    page.locator('.relationship-graph g[role="button"]').first.click()
                    page.get_by_label('补充资料链接', exact=True).wait_for()
                else:
                    expect(page.get_by_label('消息节奏', exact=True)).to_have_value('natural')
                    page.get_by_role('button', name='预览气泡效果', exact=True).click()
                    expect(page.locator('.speech-preview .message-bubble')).to_have_count(1)
                    page.get_by_label('消息节奏', exact=True).select_option('instant')
                    page.get_by_role('button', name='预览气泡效果', exact=True).click()
                    expect(page.locator('.speech-preview .message-bubble')).to_have_count(3)
                page.wait_for_timeout(150)
                assert page.locator('.settings-content').evaluate('(e)=>e.scrollWidth<=e.clientWidth+2'), label
                page.locator('.settings-content').evaluate('(e)=>e.scrollTop=0')
                page.screenshot(path=str(out / ('secondary-' + label + '.png')))
            report['checks'].append('secondary pages have bounded forms; character selection, task/life saves and relationship selection work')
            page.get_by_label('关闭设置').click()
            assert page.get_by_text('查看回答来源', exact=True).count() == 0
            sources = page.request.get(url + '/api/context/messages/ui-context-message/sources').json()
            assert sources['sources']
            page.screenshot(path=str(out / 'context-chat.png'), full_page=True)
            report['checks'].append('chat hides source controls while evidence remains available internally')
            page.get_by_label('朋友圈', exact=True).click()
            page.locator(f'.moments-card[data-post-id="{info["postId"]}"]').click()
            parent = page.locator(f'.moments-comment-thread[data-comment-id="{info["parentId"]}"]')
            expect(parent.locator(f'.moments-replies [data-comment-id="{info["replyId"]}"]')).to_have_count(1)
            expect(page.locator('.moments-comment-thread')).to_have_count(2)
            page.screenshot(path=str(out / 'threaded-comments.png'), full_page=True)
            report['checks'].append('reply is nested beneath its target while unrelated comments stay independent')
            page.get_by_label('聊天', exact=True).click()
            page.get_by_label('新建群聊', exact=True).click()
            expect(page.get_by_label('群名称', exact=True)).to_be_focused()
            page.screenshot(path=str(out / 'secondary-create-group.png'))
            page.keyboard.press('Escape')
            expect(page.get_by_role('dialog', name='新建群聊', exact=True)).to_have_count(0)
            assert not errors, errors
            report['checks'].append('create-group dialog shares form styling and restores focus on Escape')
            report['status'] = 'passed'
            browser.close()
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        server.shutdown()
        thread.join(20)
        server.server_close()
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
