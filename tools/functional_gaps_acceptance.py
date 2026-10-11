"""Real browser/API acceptance of gates and evolution confirmation, controlled model.

Everything is synthetic in a fresh temporary database/workspace. No cloud calls.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    home=Path(tempfile.mkdtemp(prefix='azur-gaps-ui-'));work=home/'work';work.mkdir()
    out=ROOT/'validation/gaps-20261004';out.mkdir(parents=True,exist_ok=True)
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'),AZURJUUS_WORKSPACE_ROOT=str(work),
        AZURJUUS_REDIS_URL='',AZURJUUS_CHROMA_URL='',AZURJUUS_CHROMA_PATH=str(home/'chroma'),
        AZURJUUS_SOCIAL_ENABLED='0',AZURJUUS_MIND_ENABLED='0',AZURJUUS_SKILL_TRIALS_ENABLED='0',
        AZURJUUS_COGNITION_ENABLED='0',AZURJUUS_REFLECTION_ENABLED='0',
        AZURJUUS_TASK_REVIEWS_ENABLED='1')
    from server import create_server
    server=create_server('127.0.0.1',0)
    c=server.app.state.runs
    report={'status':'failed','model':'controlled','workspace':str(work),'checks':[]}

    @server.app.post('/__acceptance/start')
    async def fixture():
        from backend.database import session_scope
        with session_scope() as session:
            service=server.app.state.service
            boot=service.build_snapshot(session)
            cfg=service.get_workspace(session)
            actor=next(a for a in boot['agents'] if a['id']==cfg.secretary_agent_id)
            cfg.llm_api_key='synthetic';cfg.llm_base_url='http://127.0.0.1:1/v1';cfg.authorized_workspace_root=str(work)
            cfg.connected_agent_ids=[actor['id']]
        cid=next(v['id'] for v in boot['conversations'] if v['kind']=='dm' and actor['id'] in v['memberIds'])
        c.expression.enabled=False
        async def generate(messages,*_):
            if 'cases（' in messages[0]['content']:
                return {'name':'合成文本核验','summary':'将合成文本转换为大写并读回',
                    'patch':'按要求读取源文件、转换并核验，保留输入。',
                    'cases':[{'prompt':'将 input.txt 转为大写写入 output.txt 并读回交付。',
                        'files':{'input.txt':s},'expected':{'output.txt':{'text':s.upper()}}}
                        for s in ['hello','two\nlines','中文 abc 123']]}
            return {'approved':True,'reason':'受控秘书检查：合成文件与阶段条件一致。'}
        c.reviews.generate=generate
        async def execute(run,owner,phase,*_):
            token=uuid4().hex;c.tokens[token]=(run['id'],owner['id'],phase)
            async def tool(name,args):
                result=await c.tool(token,{'callId':uuid4().hex,'name':name,'args':args})
                assert result['status']=='completed',result
                return result
            try:
                if phase!='reviewer':
                    methods=(await tool('method_search',{}))['result']['methods']
                    selected=next(m for m in methods if m['visibility']=='private')
                    await tool('method_select',{'ids':[selected['id']]})
                    await tool('write_file',{'path':'report.txt','content':'SYNTHETIC_REPORT_OK'})
                read=await tool('read_file',{'path':'report.txt'})
                await tool('deliver',{'summary':'SYNTHETIC_REPORT_OK','artifacts':['report.txt'],
                    'checks':[{'callId':read['callId']}],'unresolved':[]})
                return 'SYNTHETIC_REPORT_OK'
            finally:c.tokens.pop(token,None)
        c.execute_actor=execute
        run=await c.admit({'conversationId':cid,'mode':'task','prompt':'生成合成 report.txt 并核验',
            'requestId':uuid4().hex},[actor],c.settings_loader(),[],start_now=True)
        return {'runId':run['id'],'actorId':actor['id'],
            'title':next(v['title'] for v in boot['conversations'] if v['id']==cid)}

    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_address[1])
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1440,'height':900})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            for _ in range(100):
                try:
                    if page.request.get(url+'/api/health').ok:break
                except Exception:pass
                time.sleep(.1)
            response=page.request.post(url+'/__acceptance/start');assert response.ok,response.text()
            info=response.json();rid=info['runId'];report['runId']=rid
            page.goto(url);page.get_by_label('消息输入').wait_for()
            page.get_by_text(info['title'],exact=True).first.click()
            page.get_by_label('查看工作记录').click()
            page.get_by_text('执行计划确认',exact=True).wait_for()
            assert not (work/'report.txt').exists()
            page.get_by_role('button',name='验收通过，继续',exact=True).click()
            page.get_by_role('link',name='report.txt',exact=True).wait_for(timeout=15000)
            link=page.get_by_role('link',name='report.txt',exact=True)
            response=page.request.get(url+link.get_attribute('href'))
            assert response.ok and response.body()==b'SYNTHETIC_REPORT_OK'
            page.screenshot(path=str(out/'stage-gate.png'))
            page.get_by_placeholder('需要修改的地方或补充要求').fill('已检查实际文件。')
            page.get_by_role('button',name='验收通过，继续',exact=True).click()
            page.get_by_text('最终交付确认',exact=True).wait_for(timeout=15000)
            page.get_by_role('button',name='验收通过，继续',exact=True).click()
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                state=page.request.get(url+'/api/runs/'+rid).json()['run']
                if state['status']=='completed' and state.get('resultMessageId'):break
                time.sleep(.2)
            assert state['status']=='completed' and all(r['status']=='approved' for r in state['stageReviews'].values())
            report['checks'].append('real UI plan/stage/final approvals and pre-final hashed artifact download')
            page.get_by_label('关闭任务详情').click()
            page.get_by_label('查看成员资料').click()
            page.get_by_role('button',name='方法与成长',exact=True).click()
            page.get_by_role('button',name='从已验收任务改进方法',exact=True).first.click()
            button=page.get_by_role('button',name='确认方案，安排隔离试用',exact=True)
            button.wait_for(timeout=15000)
            page.get_by_text('合成文本核验',exact=True).wait_for()
            page.screenshot(path=str(out/'method-confirmation.png'))
            button.click()
            page.get_by_text('等待试用',exact=False).wait_for()
            methods=page.request.get(url+'/api/actors/'+info['actorId']+'/methods').json()['skills']
            evolved=next(s for s in methods if s['name']=='合成文本核验')
            assert evolved['lifecycle']=='queued' and not evolved['enabled'] and len(evolved['trialSpec'])==3
            assert not errors,errors
            report['checks'].append('real UI evolves verified source, displays three cases, confirms disabled queued candidate')
            report['status']='passed';browser.close()
    except Exception as exc:report['error']=str(exc)
    finally:
        server.shutdown();thread.join(20);server.server_close()
        (out/'functional-ui.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
