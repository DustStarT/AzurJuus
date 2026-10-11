"""Keep task evidence available for explicit follow-ups without steering casual talk."""
import re
import json
from datetime import UTC, datetime


def current_dialogue_text(payload):
    """Use the current utterance for retrieval, not every word in history/schema."""
    if isinstance(payload,str):
        try:value=json.loads(payload)
        except (ValueError,TypeError):return payload
    else:value=payload
    if not isinstance(value,dict):return str(payload or '')
    focus=value.get('focus')
    current=focus.get('current') if isinstance(focus,dict) else None
    if isinstance(current,dict) and isinstance(current.get('text'),str):return current['text']
    if isinstance(value.get('intent'),str):return value['intent']
    room=value.get('room')
    history=room.get('history') if isinstance(room,dict) else None
    if isinstance(history,list) and history and isinstance(history[-1],dict):
        return str(history[-1].get('text') or '')
    return str(payload or '')


def is_greeting(text):
    # Whole utterance only: "晚上好，帮我核对进度" is still a concrete request.
    return bool(re.fullmatch(r'\s*(?:(?:大家|各位|你们|指挥官)[，,、\s]*)?'
        r'(?:早上好|早安|早|上午好|中午好|下午好|晚上好|晚安|你好|你们好|嗨|哈[喽啰]|hi|hello)'
        r'[呀啊哦呢啦~～!！。\s]*',str(text or ''),re.I))


def relevance_terms(text):
    terms=set(re.findall(r'[a-zA-Z][a-zA-Z0-9_-]{2,}',str(text).lower()))
    for phrase in re.findall(r'[\u4e00-\u9fff]{2,}',str(text)):
        terms.update(phrase[i:i+2] for i in range(len(phrase)-1))
    return terms-{'你好','晚上','上好','早上','早安','晚安','谢谢','什么','怎么','一下','这个','那个','现在'}


def message_timestamp(item):
    raw=item.get('at') or item.get('createdAt')
    if isinstance(raw,(int,float)):return float(raw)
    if not isinstance(raw,str):return None
    try:
        value=datetime.fromisoformat(raw.replace('Z','+00:00'))
        return value.replace(tzinfo=value.tzinfo or UTC).timestamp()
    except ValueError:return None


def dialogue_history(history, current_text='', preserve_context=False):
    """Select the current chat session, retaining the archived conversation in UI."""
    items=list(history or [])
    if preserve_context:return items
    if is_greeting(current_text):
        index=next((i for i in range(len(items)-1,-1,-1)
            if items[i].get('speakerId')=='commander' and
            (items[i].get('text') or items[i].get('content'))==current_text),None)
        return items[index:] if index is not None else []
    boundary=0
    for i in range(1,len(items)):
        before,after=message_timestamp(items[i-1]),message_timestamp(items[i])
        if before is not None and after is not None and after-before>=4*3600:
            boundary=i
    return items[boundary:]


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
