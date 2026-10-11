"""Machine wall time for context; elapsed activity time stays monotonic."""
import time
import json
from datetime import datetime, timezone


def snapshot():
    stamp=time.time()
    local=datetime.fromtimestamp(stamp,timezone.utc).astimezone()
    return {'unixSeconds':stamp,'local':local.isoformat(timespec='seconds'),
        'date':local.date().isoformat(),'hour':local.hour,'weekday':local.isoweekday(),
        'timezone':local.tzname(),'utcOffsetSeconds':int(local.utcoffset().total_seconds()),
        'source':'machine-clock'}


TIME_CONTEXT_PREFIX = '当前 JUUS 聊天时间：'


def with_time_context(messages):
    """Refresh the clock at each speaking call without changing conversation data."""
    clock = {'role':'system','content':TIME_CONTEXT_PREFIX+json.dumps(snapshot(),ensure_ascii=False)+
        '。当前聊天中的今天、现在及未另指的时段以此为依据；旧消息和角色背景不改变当前时间。'
        '明确提到的回忆、未来安排或剧情时间保留其时间关系，不必主动报时。'}
    # Keep exactly one clock, ahead of conversation/attachment inputs. Retries
    # and queued replies need the new time, not the previous call's timestamp.
    clean = [m for m in messages if not (m.get('role')=='system' and
        isinstance(m.get('content'),str) and m['content'].startswith(TIME_CONTEXT_PREFIX))]
    index = next((i for i,m in enumerate(clean) if m.get('role')!='system'),len(clean))
    return [*clean[:index],clock,*clean[index:]]
