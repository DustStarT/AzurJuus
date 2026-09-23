"""Focused offline browser check using a fresh temporary database and workspace."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'validation' / 'interaction-20260922'


async def check(base):
    from playwright.async_api import async_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width':1280, 'height':800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def offline(route):
            url = route.request.url
            if url.startswith(base + '/api/stickers/'):
                await route.fulfill(path=str(ROOT / 'resources/ui/javelin.png'), content_type='image/png')
            elif url.startswith(base):
                await route.continue_()
            else:
                await route.abort()
        await page.route('**/*', offline)
        await page.goto(base, wait_until='networkidle')
        await page.get_by_label('打开设置', exact=True).click()
        await page.get_by_role('button', name='角色资料与心智', exact=True).click()
        await page.get_by_label('查看角色', exact=True).select_option(index=1)
        assert await page.locator('.member-config').count() == 1
        assert await page.get_by_label('人物背景与表达设定').count() == 1
        await page.screenshot(path=str(OUT / 'character-settings.png'))
        await page.get_by_text('心智、记忆与关系判断',exact=True).click()
        await page.get_by_role('button',name='目标',exact=True).click()
        await page.get_by_label('目标',exact=True).fill('完成一轮阅读')
        await page.get_by_label('动机',exact=True).fill('合成兴趣依据')
        await page.get_by_label('下一步',exact=True).fill('阅读已有资料')
        await page.get_by_role('button',name='保存目标',exact=True).click()
        await page.get_by_text('完成一轮阅读',exact=True).wait_for()
        await page.get_by_role('button',name='编辑或暂停',exact=True).click()
        await page.get_by_label('目标状态',exact=True).select_option('paused')
        await page.get_by_label('改变原因',exact=True).fill('先处理当前任务')
        await page.get_by_role('button',name='保存目标',exact=True).click()
        await page.get_by_text('原因：先处理当前任务',exact=True).wait_for()
        await page.screenshot(path=str(OUT / 'personal-goals.png'))
        await page.get_by_role('button',name='背景',exact=True).click()
        await page.get_by_role('button',name='编辑人格解释',exact=True).click()
        await page.get_by_label('自我认识（每行一项）',exact=True).fill('谨慎但愿意尝试')
        await page.get_by_role('button',name='保存人格解释',exact=True).click()
        await page.get_by_text('谨慎但愿意尝试',exact=True).wait_for()
        await page.get_by_role('button',name='自主生活',exact=True).click()
        await page.get_by_label('暂停所有后台生活与反思',exact=True).check()
        await page.get_by_label('每小时后台模型调用上限',exact=True).fill('42')
        await page.get_by_role('button',name='保存自主生活设置',exact=True).click()
        await page.get_by_role('status').filter(has_text='已保存').wait_for()
        assert (await (await page.request.get(base+'/api/life')).json())['settings']['hourlyCalls']==42
        await page.screenshot(path=str(OUT / 'autonomous-life.png'))
        await page.get_by_role('button', name='任务参与', exact=True).click()
        await page.get_by_label('任务秘书', exact=True).wait_for()
        assert await page.locator('.member-config').count() == 0
        await page.get_by_role('button', name='关系与世界观', exact=True).click()
        await page.get_by_label('可点击人物关系图').wait_for()
        await page.screenshot(path=str(OUT / 'relationships.png'))
        await page.get_by_role('button',name='我的资料与数据',exact=True).click()
        await page.get_by_label('清理范围',exact=True).select_option('reset-preserve')
        await page.get_by_text('保留已保存的 API Key、称呼和头像',exact=False).wait_for()
        assert await page.get_by_role('button',name='执行清理',exact=True).is_disabled()
        await page.screenshot(path=str(OUT / 'reset-preserve-option.png'))
        await page.get_by_label('关闭设置', exact=True).click()
        await page.get_by_label('聊天', exact=True).click()
        await page.locator('.conversation-card').first.click()
        await page.get_by_role('button', name='表情包', exact=True).click()
        await page.get_by_label('搜索表情').fill('标枪疑惑')
        await page.get_by_role('button', name='标枪疑惑', exact=True).click()
        assert await page.get_by_label('消息输入', exact=True).input_value() == '[表情:标枪疑惑]'
        await page.screenshot(path=str(OUT / 'sticker-draft.png'))
        assert not errors, errors
        (OUT / 'report.json').write_text(json.dumps({'passed':True, 'errors':errors,
            'checks':['single character editor', 'task settings', 'source relationship graph', 'sticker search and draft','goal editing and pause','profile scalar editing','unified life pause and budget','reset preserving keys and profile option'],
            'media':'synthetic local preview; no external downloads'}, ensure_ascii=False, indent=2), encoding='utf-8')
        await browser.close()


def main():
    temporary = Path(tempfile.mkdtemp(prefix='azur-interaction-ui-'))
    os.environ['AZURJUUS_UI_TEST_DIRECTORY'] = str(temporary)
    os.environ['AZURJUUS_SOCIAL_ENGINE_ENABLED'] = '0'
    os.environ['AZURJUUS_MIND_ENABLED'] = '0'
    os.environ['AZURJUUS_CHROMA_PATH'] = str(temporary / 'chroma')
    from ui_test_server import build_server
    server = build_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(check('http://127.0.0.1:' + str(server.server_address[1])))
    finally:
        server.shutdown()
        thread.join(timeout=15)
        server.server_close()
    print('UI acceptance passed:', OUT)


if __name__ == '__main__':
    main()
