"""Real configured-model image chat + Hermes task on isolated synthetic data."""
import os,json,sqlite3,sys,time
from pathlib import Path
from uuid import uuid4
import requests
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    from backend.credentials import reveal
    with sqlite3.connect((ROOT/'.azurjuus/azurjuus.db').as_uri()+'?mode=ro',uri=True) as db:
        base,model,secret=db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    home=ROOT/'validation/vision-live'/uuid4().hex
    work=home/'work';work.mkdir(parents=True)
    code='PORT-'+str(int(uuid4().hex[:6],16))
    picture=Image.new('RGB',(640,240),'white');draw=ImageDraw.Draw(picture)
    draw.text((36,80),code,fill='black',font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',48))
    picture.save(work/'sample.png')
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'),AZURJUUS_WORKSPACE_ROOT=str(work),
        AZURJUUS_SOCIAL_ENABLED='0',AZURJUUS_REFLECTION_ENABLED='0',AZURJUUS_SKILL_TRIALS_ENABLED='0',
        AZURJUUS_EXPRESSION_ENABLED='1',AZURJUUS_EXECUTION_BACKEND='hermes')
    from desktop import start_local_server,stop_local_server,wait_for_server
    server,thread,url,_=start_local_server(port=0)
    report={'model':model,'passed':False,'checks':[]}
    def get(path):
        r=requests.get(url+path,timeout=20);r.raise_for_status();return r.json()
    def post(path,data):
        r=requests.post(url+path,json=data,timeout=20);r.raise_for_status();return r.json()
    def wait(rid):
        started=time.monotonic()
        while time.monotonic()-started<240:
            run=next(r for r in get('/api/runs')['runs'] if r['id']==rid)
            if run['status'] in {'failed','paused','cancelled','completed'}:
                return run,round(time.monotonic()-started,2)
            time.sleep(.5)
        raise TimeoutError('Image run exceeded 240 seconds')
    try:
        assert wait_for_server(url)
        post('/api/workspace/save',{'workspace':{'settings':{'llmBaseUrl':base,'llmModel':model,'llmApiKey':reveal(secret),
            'authorizedWorkspaceRoot':str(work),'visionEnabled':True,'runTimeoutSeconds':180}}})
        snap=get('/api/bootstrap')['snapshot'];cid=next(c['id'] for c in snap['conversations'] if c['kind']=='dm')
        uploaded=requests.post(url+'/api/attachments',params={'conversationId':cid,'name':'sample.png'},data=(work/'sample.png').read_bytes(),timeout=20)
        uploaded.raise_for_status();aid=uploaded.json()['id']
        for mode,prompt in [('chat','请读出图片里的完整校验码，只回答这个码。'),('task','读取附件图片，把图片中完整校验码写入 answer.txt，检查文件内容后交付。不要猜测校验码。')]:
            if mode=='task':
                code='PORT-'+str(int(uuid4().hex[:6],16))
                picture=Image.new('RGB',(640,240),'white');draw=ImageDraw.Draw(picture)
                draw.text((36,80),code,fill='black',font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',48))
                picture.save(work/'second.png')
                uploaded=requests.post(url+'/api/attachments',params={'conversationId':cid,'name':'second.png'},data=(work/'second.png').read_bytes(),timeout=20)
                uploaded.raise_for_status();aid=uploaded.json()['id']
            rid=post('/api/messages/send',{'conversationId':cid,'content':prompt,'mode':mode,'attachments':[aid],'requestId':uuid4().hex})['runId']
            run,seconds=wait(rid)
            calls=get('/api/runs/'+rid)['calls']
            answer=(run.get('result') or {}).get('summary','')
            passed=run['status']=='completed' and (code in answer if mode=='chat' else
                (work/'answer.txt').exists() and code in (work/'answer.txt').read_text(encoding='utf-8') and
                any(c['name']=='read_image' and c['status']=='completed' for c in calls))
            report['checks'].append({'mode':mode,'passed':passed,'status':run['status'],'seconds':seconds,'error':run.get('error'),
                'answer':answer,'calls':[{'name':c['name'],'status':c['status']} for c in calls]})
            if not passed:break
        report['passed']=len(report['checks'])==2 and all(c['passed'] for c in report['checks'])
    except Exception as exc:report['error']=type(exc).__name__+': '+str(exc)
    finally:
        stop_local_server(server,thread)
        (home/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
        print(str(home/'report.json'))
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':main()
