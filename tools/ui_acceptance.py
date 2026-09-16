"""Browser regression and visual artifacts against tools/ui_test_server.py."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "validation" / "ui"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1600, "height": 900}, record_video_dir=str(OUT / "video"), record_video_size={"width": 1600, "height": 900})
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("http://127.0.0.1:8879", wait_until="networkidle")
        seed=await page.request.post("http://127.0.0.1:8879/__acceptance/seed_run")
        assert seed.ok, await seed.text()
        await page.reload(wait_until="networkidle")
        await page.get_by_label("聊天", exact=True).click()
        await page.locator('.conversation-card').first.click()
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "private-1600.png"), animations="disabled")
        composer = page.get_by_label("消息输入")
        await composer.fill("中文组合输入测试")
        await composer.dispatch_event("compositionstart")
        await composer.press("Enter")
        assert (await composer.input_value()).strip() == "中文组合输入测试"
        await composer.dispatch_event("compositionend")
        await composer.evaluate("e=>e.setSelectionRange(2,5)")
        await page.evaluate("fetch('/api/workspace/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:{uiSession:{view:'chat'}}})})")
        assert await composer.evaluate("e=>[e.selectionStart,e.selectionEnd]") == [2,5]
        await composer.fill("")
        await page.locator('.conversation-card').filter(has_text="港区协作频道").click()
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "group-1600.png"), animations="disabled")
        await page.get_by_label("朋友圈", exact=True).click()
        if await page.locator('.post-detail').count():
            await page.get_by_role('button',name='动态总览').click()
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "moments-1600.png"), animations="disabled")
        await page.locator('.post-card').first.click()
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "moment-detail-1600.png"), animations="disabled")
        await page.get_by_label("动态评论", exact=True).fill("隔离界面测试：港区通信正常。")
        await page.get_by_label("发送评论", exact=True).click()
        await page.get_by_text("隔离界面测试：港区通信正常。", exact=True).last.wait_for()
        await page.get_by_label("打开设置", exact=True).click()
        await page.get_by_label("港区设置", exact=True).wait_for()
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "settings-1600.png"), animations="disabled")
        await page.keyboard.press("Escape")
        await page.get_by_label("打开任务中心", exact=True).click()
        await page.get_by_role('heading',name='界面验收：生成并核验测试报告').wait_for()
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "work-drawer-1600.png"), animations="disabled")
        await page.get_by_role('button',name='交付成果',exact=True).click()
        async with page.expect_download() as download:
            await page.locator('.artifact-card').first.click()
        artifact=await download.value
        await artifact.save_as(OUT/'downloaded-ui-report.txt')
        assert 'no cloud model was used' in (OUT/'downloaded-ui-report.txt').read_text()
        await page.keyboard.press("Escape")
        await page.get_by_label("聊天", exact=True).click()
        layouts = []
        for width,height in [(1280,720),(1600,900),(1920,1080),(1024,576),(853,480)]:
            await page.set_viewport_size({"width":width,"height":height})
            box = await page.get_by_label("发送消息", exact=True).bounding_box()
            assert box and box['y']+box['height'] <= height, (width,height,box)
            layouts.append({"width":width,"height":height,"sendBottom":box['y']+box['height']})
            await page.screenshot(path=str(OUT / f"group-{width}x{height}.png"), animations="disabled")
        await page.set_viewport_size({"width":1600,"height":900})
        performance = await page.evaluate("""()=>new Promise(resolve=>{const frames=[],longTasks=[],memory=[];const observer=new PerformanceObserver(list=>longTasks.push(...list.getEntries().map(e=>e.duration)));observer.observe({type:'longtask',buffered:false});let prev=performance.now();function tick(now){frames.push(now-prev);prev=now;const n=frames.length%120;if(n===5)document.querySelector('[aria-label="打开设置"]').click();if(n===40)document.querySelector('[aria-label="关闭设置"]')?.click();if(n===65)document.querySelector('[aria-label="打开任务中心"]').click();if(n===100)document.querySelector('[aria-label="关闭任务详情"]')?.click();if(n===0)memory.push({frame:frames.length,heap:performance.memory?.usedJSHeapSize,nodes:document.querySelectorAll('*').length});if(frames.length<720)requestAnimationFrame(tick);else{observer.disconnect();resolve({frames,longTasks,memory})}}requestAnimationFrame(tick)})""")
        seeded = await page.request.post("http://127.0.0.1:8879/__acceptance/history_stream")
        assert seeded.ok
        fixture = await seeded.json()
        await page.locator('.conversation-card').filter(has_text=fixture['conversationTitle']).click()
        await page.get_by_role('button', name='查看更早的消息').wait_for()
        assert await page.locator('.message-row:not(.message-row--streaming)').count() <= 101
        await composer.fill('持续输入保持不变')
        await composer.focus()
        await composer.evaluate('e=>e.setSelectionRange(2,5)')
        await page.wait_for_function("document.querySelector('.streaming')?.textContent.includes('流59')")
        streamed = await page.locator('.streaming').inner_text()
        assert all(f'流{i:02d}' in streamed for i in range(60)), streamed
        assert await composer.input_value() == '持续输入保持不变'
        assert await composer.evaluate('e=>document.activeElement===e && e.selectionStart===2 && e.selectionEnd===5')
        await page.get_by_role('button', name='查看更早的消息').click()
        assert await page.locator('.message-row').count() >= 200
        await page.screenshot(path=str(OUT / 'long-history-stream.png'), animations="disabled")
        import platform
        report = {"title":await page.title(),"platform":platform.platform(),"layouts":layouts,"errors":errors,"performance":performance,"browser":browser.version,"checks":["private","group","moments","detail","comment persistence","IME Enter suppression","selection preservation","settings","drawer","artifact download bytes","five viewports","six repeated modal/drawer animation cycles","350-message incremental history","60 streaming deltas during workspace refresh","input focus and selection during streaming"]}
        (OUT / "report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        assert not errors, errors
        print(json.dumps({k:v for k,v in report.items() if k!='performance'},ensure_ascii=False))
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
