"""Explorer UIA acceptance, confined to a newly opened synthetic workspace window."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.tasks.capabilities import LocalCapabilities


async def main():
    out=ROOT/'validation'/'desktop'
    out.mkdir(parents=True,exist_ok=True)
    work=out/('ExplorerAcceptance-'+uuid4().hex[:8])
    work.mkdir()
    (work/'documents').mkdir()
    (work/'archive').mkdir()
    (work/'documents'/'fixture.txt').write_text('Synthetic Explorer fixture',encoding='utf-8')
    tools=LocalCapabilities(ROOT/'.azurjuus'/'desktop-acceptance')
    run={'id':work.name,'workspace':str(work)}
    async def execute(args):
        return await tools.execute(run,'desktop',args,'desktop-test',lambda *a:None)
    report={'status':'failed','workspace':str(work),'checks':[]}
    handle=None
    try:
        prior=await execute({'op':'windows'})
        prior_handles={w['handle'] for w in prior['windows']}
        await execute({'op':'explorer','path':'.'})
        for _ in range(30):
            windows=await execute({'op':'windows'})
            match=next((w for w in windows['windows'] if work.name in w['title'] and w['handle'] not in prior_handles),None)
            if match:
                handle=match['handle'];break
            await asyncio.sleep(.2)
        if not handle: raise RuntimeError('A separate fixture Explorer window was not found; no existing user window was operated.')
        report['checks'].append('new Explorer window')
        inspected=await execute({'op':'inspect','handle':handle})
        item=next(e for e in inspected['elements'] if e['name']=='documents' and e['type']=='ListItem')
        report['checks'].append('UIA inspect located fixture folder')
        await execute({'op':'focus','handle':handle})
        report['checks'].append('window focus')
        await execute({'op':'select','handle':handle,'index':item['index']})
        report['checks'].append('select fixture folder')
        await execute({'op':'open','handle':handle,'index':item['index']})
        await asyncio.sleep(.4)
        inspected=await execute({'op':'inspect','handle':handle})
        assert any(e['name'].startswith('fixture') for e in inspected['elements']), inspected['title']
        report['checks'].append('navigate into fixture folder')
        item=next(e for e in inspected['elements'] if e['name'].startswith('fixture') and e['type']=='ListItem')
        await execute({'op':'select','handle':handle,'index':item['index']})
        await execute({'op':'shortcut','handle':handle,'action':'cut'})
        await execute({'op':'inspect','handle':handle})
        await execute({'op':'shortcut','handle':handle,'action':'up'})
        await asyncio.sleep(.5)
        inspected=await execute({'op':'inspect','handle':handle})
        item=next(e for e in inspected['elements'] if e['name']=='archive' and e['type']=='ListItem')
        await execute({'op':'open','handle':handle,'index':item['index']})
        await asyncio.sleep(.4)
        await execute({'op':'inspect','handle':handle})
        await execute({'op':'shortcut','handle':handle,'action':'paste'})
        for _ in range(30):
            if (work/'archive'/'fixture.txt').exists() and not (work/'documents'/'fixture.txt').exists(): break
            await asyncio.sleep(.1)
        assert (work/'archive'/'fixture.txt').read_text()=='Synthetic Explorer fixture'
        assert not (work/'documents'/'fixture.txt').exists()
        report['checks'].append('Explorer cut/paste moves the actual file')
        await execute({'op':'inspect','handle':handle})
        await execute({'op':'shortcut','handle':handle,'action':'undo'})
        for _ in range(30):
            if (work/'documents'/'fixture.txt').exists() and not (work/'archive'/'fixture.txt').exists(): break
            await asyncio.sleep(.1)
        assert (work/'documents'/'fixture.txt').read_text()=='Synthetic Explorer fixture'
        assert not (work/'archive'/'fixture.txt').exists()
        report['checks'].append('Explorer undo restores the original file')
        from pywinauto import Desktop
        Desktop(backend='uia').window(handle=handle).capture_as_image().save(out/'explorer.png')
        report['status']='passed'
    except Exception as exc:
        report['error']=str(exc)
    finally:
        if handle:
            # Only the newly identified synthetic window is closed.
            import ctypes
            ctypes.windll.user32.PostMessageW(handle,0x0010,0,0)
        tools.release_desktop(run['id'])
        await tools.close()
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
