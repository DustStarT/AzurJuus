"""Isolated UI regression: stream DOM continuity, IME, record deletion and clearing."""
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

os.environ['AZURJUUS_UI_TEST_DIRECTORY'] = 'ui-messages-' + uuid4().hex
from ui_test_server import build_server, ROOT
from playwright.sync_api import sync_playwright


def main():
    out = ROOT / 'validation/ui-messages'
    out.mkdir(parents=True, exist_ok=True)
    server = build_server(port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    report = {'status':'failed', 'checks':[]}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={'width':1600, 'height':900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            page.goto(base)
            page.get_by_label('消息输入').wait_for()
            fixture = page.request.post(base + '/__acceptance/history_stream').json()
            page.locator('.conversation-card').filter(has_text=fixture['conversationTitle']).click()
            mid = fixture['messageId']
            locator = page.locator('[data-message-id="' + mid + '"]')
            locator.locator('.message-bubble').first.wait_for()
            page.evaluate('''mid => {
                window.observedMessage = document.querySelector('[data-message-id="' + mid + '"]');
                window.continuity = {frames:0, missing:0, replaced:0, empty:0};
                window.sentenceReleases = [];
                window.monitorMessage = true;
                function tick() {
                    const current = document.querySelector('[data-message-id="' + mid + '"]');
                    const stats = window.continuity;
                    stats.frames++;
                    if (!current) stats.missing++;
                    if (current !== window.observedMessage) stats.replaced++;
                    if (!current?.querySelector('.message-bubble')?.textContent.trim()) stats.empty++;
                    const count = current?.querySelectorAll('.sentence-enter').length || 0;
                    if (count && count !== window.sentenceReleases.at(-1)?.count)
                        window.sentenceReleases.push({count, at:performance.now()});
                    if (window.monitorMessage) requestAnimationFrame(tick);
                }
                requestAnimationFrame(tick);
            }''', mid)
            composer = page.get_by_label('消息输入')
            composer.fill('中文组合输入保持焦点')
            composer.dispatch_event('compositionstart')
            composer.press('Enter')
            assert composer.input_value().strip() == '中文组合输入保持焦点'
            composer.dispatch_event('compositionend')
            composer.evaluate('e => e.setSelectionRange(2,5)')
            deadline = time.monotonic() + 20
            while page.request.get(base + '/api/runs/' + fixture['runId']).json()['run']['status'] != 'completed':
                assert time.monotonic() < deadline, 'Stream fixture did not finish'
                page.wait_for_timeout(100)
            page.wait_for_function('''mid => !document.querySelector('[data-message-id="' + mid + '"] .speech-stack')?.hasAttribute('aria-busy')''', arg=mid)
            stats = page.evaluate('window.monitorMessage = false; window.continuity')
            assert stats['frames'] > 30 and not any(stats[k] for k in ('missing','replaced','empty')), stats
            assert composer.evaluate('e => document.activeElement === e && e.selectionStart === 2 && e.selectionEnd === 5')
            report['continuity'] = stats
            releases = page.evaluate('window.sentenceReleases')
            assert len(releases) >= 5, releases
            for index, (previous, following) in enumerate(zip(releases, releases[1:])):
                assert following['count'] == previous['count'] + 1, releases
                # The first sample observes a sentence that was already visible
                # before the monitor attached; it is not its insertion timestamp.
                if index:
                    assert following['at'] - previous['at'] >= 500, releases
            report['sentenceReleases'] = releases
            report['checks'].append('Live sentences appear individually at reading pace; completion does not flush the queue')
            report['checks'].append('Same message DOM node survives 60 deltas, persistence and final refresh; no blank frame; IME/focus/selection preserved')
            assert locator.locator('.message-bubble').count() == 3
            original_text = ''.join(locator.locator('.message-bubble').all_text_contents())
            page.screenshot(path=str(out / 'stream-completed.png'))
            page.reload()
            page.locator('[data-message-id="' + mid + '"] .message-bubble').first.wait_for()
            assert ''.join(page.locator('[data-message-id="' + mid + '"] .message-bubble').all_text_contents()) == original_text
            seed = page.request.post(base + '/__acceptance/seed_run').json()
            page.get_by_label('打开任务中心', exact=True).click()
            page.get_by_label('选择任务').select_option(seed['runId'])
            page.get_by_role('heading', name='界面验收：生成并核验测试报告').wait_for()
            page.on('dialog', lambda dialog: dialog.accept())
            page.get_by_role('button', name='删除记录', exact=True).click()
            page.wait_for_function('''rid => fetch('/api/runtime/state').then(r=>r.json()).then(d=>!d.runs.some(r=>r.id===rid))''', arg=seed['runId'])
            page.get_by_role('button', name='清空已结束记录', exact=True).click()
            page.wait_for_function("fetch('/api/runtime/state').then(r=>r.json()).then(d=>d.runs.length===0)")
            page.screenshot(path=str(out / 'records-cleared.png'))
            page.reload()
            page.locator('[data-message-id="' + mid + '"] .message-bubble').first.wait_for()
            assert page.request.get(base + '/api/runtime/state').json()['runs'] == []
            assert ''.join(page.locator('[data-message-id="' + mid + '"] .message-bubble').all_text_contents()) == original_text
            from ui_test_server import TEST_ROOT
            assert (TEST_ROOT / 'workspace/ui-report.txt').is_file()
            report['checks'].append('Delete and clear buttons remove records after reload while preserving chat and produced files')
            group = page.request.post(base + '/__acceptance/seed_group').json()
            page.reload()
            foreign = page.locator('[data-message-id="' + group['foreignMessageId'] + '"]')
            foreign.wait_for()
            assert foreign.locator('.message-author').count() == 0
            page.get_by_label('查看成员资料').click()
            page.get_by_role('button', name='方法与成长', exact=True).click()
            page.get_by_role('dialog', name='方法与成长').wait_for()
            panel = page.get_by_role('dialog', name='方法与成长')
            boot = page.request.get(base + '/api/bootstrap').json()
            title = panel.locator('h2').inner_text().split(' · ')[0]
            aid = next(a['id'] for a in boot['snapshot']['agents'] if a['name'] == title)
            page.request.post(base + '/__acceptance/mind', data={'actorId':aid})
            panel.get_by_role('button', name='经历与关系', exact=True).click()
            experience = panel.locator('article').filter(has_text='我会在提交前提醒检查合成附件。').last
            experience.get_by_role('button',name='纠正理解').click()
            experience.get_by_label('纠正人物理解').fill('只提醒合成附件，不代表所有文件。')
            experience.get_by_role('button',name='保存纠正').click()
            page.wait_for_timeout(400)
            assert '只提醒合成附件' in experience.inner_text()
            experience.get_by_role('button',name='忘记这段经历').click()
            page.wait_for_timeout(400)
            assert panel.locator('article').filter(has_text='我会在提交前提醒检查合成附件。').count() == 0
            panel.screenshot(path=str(out/'mind-panel.png'))
            relationship = panel.locator('article').filter(has=page.get_by_role('button', name='设定关系')).first
            relationship.get_by_role('button', name='设定关系').click()
            relationship.get_by_label('关系说明').fill('配合过的同事，可以直接指出问题。')
            relationship.get_by_role('button', name='保存关系').click()
            page.wait_for_timeout(400)
            assert '配合过的同事' in relationship.inner_text()
            report['checks'].append('Mind panel corrects and forgets sourced experiences without replacing the composer')
            page.get_by_role('dialog', name='方法与成长').get_by_role('button', name='关闭', exact=True).click()
            page.locator('.conversation-card').filter(has_text='可删除的合成协作群').click()
            page.get_by_role('button', name='删除群聊', exact=True).click()
            page.wait_for_timeout(500)
            page.reload()
            page.get_by_label('消息输入').wait_for()
            assert page.locator('.conversation-card').filter(has_text='可删除的合成协作群').count() == 0
            report['checks'].append('Three actor-paced bubbles persist; legacy foreign persona shown as neutral notice; method panel opens; group deletion survives reload')
            page.get_by_label('打开设置', exact=True).click()
            settings = page.get_by_role('dialog', name='港区设置')
            settings.get_by_role('button', name='我的资料与数据').click()
            settings.get_by_label('希望被怎样称呼').fill('界面测试称呼')
            import base64
            png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
            settings.get_by_label('更换头像').set_input_files({'name':'avatar.png', 'mimeType':'image/png', 'buffer':png})
            settings.get_by_role('button', name='保存设置', exact=True).click()
            page.wait_for_function("fetch('/api/workspace/load').then(r=>r.json()).then(d=>d.workspace.data.user.name==='界面测试称呼' && d.workspace.data.user.avatarUrl.startsWith('data:image/png'))")
            settings.get_by_label('清理范围').select_option('memory')
            assert settings.get_by_role('button', name='执行清理').is_disabled()
            settings.get_by_label('输入“确认清理”后执行').fill('确认清理')
            settings.get_by_role('button', name='执行清理').click()
            page.get_by_label('消息输入').wait_for()
            page.wait_for_timeout(500)
            assert page.request.get(base + f'/api/actors/{aid}/experiences').json()['experiences'] == []
            report['checks'].append('User avatar/name save; directed relationship edit; guarded memory clearing through settings')
            assert not errors, errors
            report.update(status='passed', errors=errors)
            browser.close()
    except Exception as exc:
        report['error'] = str(exc)
        try: page.screenshot(path=str(out / 'failure.png'))
        except Exception: pass
        raise
    finally:
        server.shutdown()
        worker.join(15)
        server.server_close()
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
