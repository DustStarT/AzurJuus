"""Offline popover hit testing and interactions against an isolated real backend."""
import json
import threading

from visual_ui_acceptance import STATE, ROOT, build_server, install_fixture
from playwright.sync_api import expect, sync_playwright


def main():
    out = ROOT / 'validation' / 'desktop-render-20261006' / 'popovers'
    out.mkdir(parents=True, exist_ok=True)
    server = build_server(port=0)
    install_fixture(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {'status': 'failed', 'isolatedData': str(STATE), 'checks': [],
              'scope': 'Real local catalog and UI; sticker pixels are offline placeholders; no messages sent'}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{server.server_address[1]}'

            def route(request):
                url = request.request.url
                if url.startswith(base + '/api/stickers/'):
                    request.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" rx="10" fill="#b8ddf5"/></svg>')
                elif url.startswith(base):
                    request.continue_()
                else:
                    request.abort()

            page.route('**/*', route)
            page.goto(base)
            page.get_by_label('消息输入', exact=True).wait_for()
            fixture = page.request.post(base + '/__acceptance/visual').json()
            page.reload()

            def visible_on_top(selector):
                panel = page.locator(selector)
                expect(panel).to_be_visible()
                data = panel.evaluate('''el => {
                    const r=el.getBoundingClientRect(), chat=document.querySelector('.chat-panel').getBoundingClientRect();
                    const points=[.12,.5,.88].flatMap(x=>[.12,.5,.88].map(y=>[r.left+r.width*x,r.top+r.height*y]));
                    return {onTop:points.every(([x,y])=>el.contains(document.elementFromPoint(x,y))),
                        fits:r.left>=chat.left&&r.right<=chat.right&&r.top>=chat.top&&r.bottom<=chat.bottom};
                }''')
                assert data['onTop'] and data['fits'], (selector, data)
                return data

            for width, height in [(1280, 720), (820, 560), (768, 600), (390, 700)]:
                page.set_viewport_size({'width': width, 'height': height})
                for view in ['private', 'group']:
                    title = fixture['dm' if view == 'private' else 'group']
                    back = page.get_by_label('返回会话列表', exact=True)
                    if back.is_visible():
                        back.click()
                    page.locator('.conversation-card').filter(has=page.get_by_text(title, exact=True)).click()
                    expect(page.locator('.chat-heading h1')).to_have_text(title)
                    page.wait_for_timeout(500)
                    page.get_by_label('表情包', exact=True).click()
                    expect(page.locator('.sticker-grid button').first).to_be_visible()
                    sticker_data = visible_on_top('.sticker-picker section')
                    page.screenshot(path=str(out / f'{view}-{width}-stickers.png'))
                    label = page.locator('.sticker-grid button').first.get_attribute('aria-label')
                    page.get_by_label('搜索表情', exact=True).fill('不可能存在的验收标签')
                    expect(page.locator('.sticker-grid button')).to_have_count(0)
                    page.get_by_label('搜索表情', exact=True).fill(label)
                    page.locator('.sticker-grid button').first.click()
                    expect(page.locator('.sticker-picker section')).to_have_count(0)
                    draft = page.get_by_label('消息输入', exact=True)
                    expect(draft).to_have_value(f'[表情:{label}]')
                    draft.fill('')
                    page.get_by_label('图片和文件', exact=True).click()
                    visible_on_top('.attachments .composer-tool-panel')
                    page.get_by_label('图片和文件', exact=True).click()
                    page.locator('.composer-mode summary').click()
                    visible_on_top('.composer-mode .composer-tool-panel')
                    page.locator('.composer-mode summary').click()
                    if view == 'group':
                        draft.fill('@')
                        visible_on_top('.mention-options')
                        page.locator('.mention-options button').first.click()
                        assert draft.input_value().startswith('@') and draft.input_value().endswith(' ')
                        draft.fill('')
                    report['checks'].append({'view': view, 'width': width, 'height': height,
                                             'sticker': sticker_data, 'passed': True})
            assert not errors, errors
            report.update(status='passed', browserErrors=errors)
            browser.close()
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        server.shutdown()
        thread.join(timeout=15)
        server.server_close()
        report['serverStopped'] = not thread.is_alive()
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
