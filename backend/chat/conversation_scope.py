"""Keep task evidence available for explicit follow-ups without steering casual talk."""
import re


def asks_about_work(text: str) -> bool:
    text=str(text or '')
    return bool(re.search(
        r'(?:任务进度|工作记录|交付成果|执行结果|'
        r'(?:之前|刚才|上次|前面|那份|那个|做完|已完成|进行中).{0,20}'
        r'(?:任务|工作|结果|文件|报告|资料|书|做了什么|查到|进度|交付))', text))


def task_experience(row: dict, store, modes: dict | None = None) -> bool:
    if row.get('kind')=='outcome':return True
    data=row.get('data') or {}
    if data.get('mode')=='task':return True
    if data.get('mode')=='chat':return False
    run_id=row.get('runId') or row.get('run_id')
    if not run_id:return False
    modes={} if modes is None else modes
    if run_id not in modes:
        try:modes[run_id]=store.get(run_id).get('mode')
        except KeyError:modes[run_id]=None
    return modes[run_id] not in {None,'chat'}
