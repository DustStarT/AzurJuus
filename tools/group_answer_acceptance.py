"""Real group chat and six-file answers on an isolated local service."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from backend.credentials import reveal
    with sqlite3.connect((ROOT/'.azurjuus/azurjuus.db').as_uri()+'?mode=ro',uri=True) as db:
        base, model, secret = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    home = ROOT/'.azurjuus/group-answer-acceptance'/uuid4().hex
    work = home/'work'
    work.mkdir(parents=True)
    topics = {'systems.txt':'计算机系统：处理器、内存、指令执行。', 'statistics.txt':'统计方法：抽样、误差、假设检验。',
        'plants.txt':'植物分类：叶形、生境、检索表。', 'cooking.txt':'烹饪基础：火候、食材搭配、食品保存。',
        'history.txt':'航海历史：贸易航线、导航技术、港口发展。', 'scanned.txt':'仅有封面标题：古地图。正文为无法识别的扫描图像，不能确定具体内容。'}
    for name, body in topics.items(): (work/name).write_text(body,encoding='utf-8')
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///'+(home/'app.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(home/'state/workspace.json'), AZURJUUS_WORKSPACE_ROOT=str(work),
        AZURJUUS_DESKTOP_LOG_DIR=str(home/'logs'), AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_REFLECTION_ENABLED='0',
        AZURJUUS_SKILL_TRIALS_ENABLED='0', AZURJUUS_EXPRESSION_ENABLED='1', AZURJUUS_EXECUTION_BACKEND='hermes')
    report={'model':model,'scope':'Synthetic text files, not PDF parsing or real user books.'}
    if os.name == 'nt':
        proc = subprocess.run([str(ROOT/'.venv/Scripts/pythonw.exe'),'-X','utf8',str(ROOT/'tools/desktop_entry.py'),'--check'],
            env=os.environ.copy(),timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        report['windowlessCheck'] = proc.returncode == 0
        assert report['windowlessCheck']
    from desktop import start_local_server, stop_local_server, wait_for_server
    server, thread, url, _ = start_local_server(port=0)
    assert wait_for_server(url)
    def get(path):
        r=requests.get(url+path,timeout=15); r.raise_for_status(); return r.json()
    def post(path, data):
        r=requests.post(url+path,json=data,timeout=15); r.raise_for_status(); return r.json()
    def run(cid, text, mode):
        started=time.monotonic()
        rid=post('/api/messages/send',{'conversationId':cid,'content':text,'mode':mode,'requestId':uuid4().hex})['runId']
        while time.monotonic()-started < 240:
            row=next(r for r in get('/api/runs')['runs'] if r['id']==rid)
            # completed is persisted before expression finishes; wait for its callback.
            if row['status'] in {'failed','paused','waiting_approval'} or row.get('resultMessageId'):
                messages=get('/api/workspace/load')['workspace']['data']['messages'].get(cid,[])
                return row,[m for m in messages if m.get('metadata',{}).get('runId')==rid and m['speakerId']!='commander'],round(time.monotonic()-started,2)
            time.sleep(.5)
        raise TimeoutError('Synthetic task exceeded acceptance deadline')
    try:
        post('/api/workspace/save',{'workspace':{'settings':{'llmBaseUrl':base,'llmModel':model,'llmApiKey':reveal(secret),
            'authorizedWorkspaceRoot':str(work),'runTimeoutSeconds':180}}})
        data=get('/api/workspace/load')['workspace']['data']
        group,messages,seconds=run('port-hub','你好，各位。','chat')
        report['group']={'status':group['status'],'speakers':len({m['speakerId'] for m in messages}),
            'messages':[m['body'] for m in messages],'seconds':seconds}
        assert report['group']['speakers']>1,report['group']
        cid=next(c['id'] for c in data['conversations'] if c['kind']=='dm')
        task,messages,seconds=run(cid,'工作区有六份书籍简介，请逐项告诉我每个文件主要讲什么，明确无法确定正文的那一份。','task')
        answer='\n'.join(m['body'] for m in messages)
        report['answer']={'status':task['status'],'text':answer,'result':task.get('result'), 'error':task.get('error'), 'seconds':seconds,
            'allFilesMentioned':all(name in answer for name in topics)}
        report['passed']=task['status']=='completed' and report['answer']['allFilesMentioned']
    finally:
        stop_local_server(server,thread)
        out=ROOT/'validation/group-answer'; out.mkdir(parents=True,exist_ok=True)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if not report.get('passed'): raise SystemExit(1)


if __name__=='__main__': main()
