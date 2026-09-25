"""Offline Moments browser smoke test against an isolated database and built UI.

Set AZURJUUS_TEST_BUILD to a directory produced by Vite before running.
No model credentials or live workspace files are used.
"""
import asyncio
import os
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
BUILD=Path(os.environ['AZURJUUS_TEST_BUILD']).resolve()
if not (BUILD/'index.html').is_file():
    raise SystemExit('AZURJUUS_TEST_BUILD must point to a built frontend.')


async def check(front,backend,out):
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        page=await browser.new_page(viewport={'width':1280,'height':800})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        async def proxy(route):
            parsed=urlsplit(route.request.url)
            if parsed.path.startswith(('/api/','/resources/')):
                headers={**route.request.headers,'origin':backend}
                response=await route.fetch(url=backend+parsed.path+('?' + parsed.query if parsed.query else ''),headers=headers)
                await route.fulfill(response=response)
            else:
                await route.continue_()
        await page.route('**/*',proxy)
        await page.goto(front,wait_until='networkidle')
        await page.get_by_label('朋友圈',exact=True).click()
        await expect(page.get_by_role('heading',name='港区动态')).to_be_visible()
        await expect(page.get_by_text('这里还没有动态。',exact=False)).to_be_visible()
        await expect(page.get_by_text('观察视图')).to_have_count(0)
        await page.get_by_role('button',name='新建动态').click()
        await page.get_by_label('动态内容').fill('隔离界面验收：在港区分享一条想法。')
        await page.get_by_role('button',name='发布动态').click()
        await expect(page.get_by_text('隔离界面验收：在港区分享一条想法。',exact=True)).to_be_visible()
        await page.locator('.moments-card').filter(has_text='隔离界面验收：在港区分享一条想法。').click()
        await expect(page.get_by_label('动态详情',exact=True)).to_be_visible()
        await page.get_by_role('button',name='点赞',exact=True).click()
        await expect(page.get_by_role('button',name='取消点赞',exact=True)).to_be_visible()
        await page.get_by_placeholder('写条评论…').fill('收到。')
        await page.get_by_role('button',name='发送',exact=True).click()
        await expect(page.get_by_text('收到。',exact=False)).to_be_visible()
        await page.get_by_role('button',name='返回动态',exact=True).click()
        await expect(page.get_by_role('button',name='新建动态')).to_be_visible()
        await expect(page.locator('.moments-card')).to_be_focused()
        await page.screenshot(path=str(out/'moments.png'))
        assert not errors,errors
        await browser.close()


def main():
    isolated=Path(tempfile.mkdtemp(prefix='azur-moments-ui-'))
    os.environ['AZURJUUS_UI_TEST_DIRECTORY']=str(isolated/'data')
    os.environ['AZURJUUS_MIND_ENABLED']='0'
    os.environ['AZURJUUS_EXPRESSION_ENABLED']='0'
    sys.path.insert(0,str(ROOT/'tools'))
    from ui_test_server import build_server
    backend=build_server(port=0)
    static=ThreadingHTTPServer(('127.0.0.1',0),partial(SimpleHTTPRequestHandler,directory=str(BUILD)))
    threads=[threading.Thread(target=server.serve_forever,daemon=True) for server in (backend,static)]
    for thread in threads:thread.start()
    try:
        asyncio.run(check(f'http://127.0.0.1:{static.server_address[1]}',
            f'http://127.0.0.1:{backend.server_address[1]}',isolated))
    finally:
        for server in (backend,static):server.shutdown();server.server_close()
        for thread in threads:thread.join(timeout=10)
    print('Moments UI acceptance passed:',isolated/'moments.png')


if __name__=='__main__':main()
