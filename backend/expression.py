"""No-tool speech generation with a durable, idempotent delivery outbox."""
import asyncio
import hashlib
import json
import os
import re
import time
import httpx
from .terminal_characters import TERMINAL, context, lore


def validate(value, source_id, detailed=False, facts=None):
    if not isinstance(value, dict) or value.get('sourceIds') != [source_id]:
        raise ValueError('缺少正确来源标识')
    segments = value.get('segments')
    if not isinstance(segments, list) or not 1 <= len(segments) <= (12 if detailed else 3):
        raise ValueError('分段数量无效')
    if any(not isinstance(s, str) or not s.strip() for s in segments):
        raise ValueError('消息不能为空')
    text = '\n\n'.join(s.strip() for s in segments)
    if len(text) > (12000 if detailed else 180):
        raise ValueError('回复过长，请保留必要内容')
    if re.search(r'[（(][^）)\n]*(?:抬头|低头|微笑|点头|摇头|摊开|摊在|合上|放下|轻声|小声|笔尖|叹气|笑了|看着你)[^）)\n]*[）)]|\*[^*\n]+\*|(?:她|他)(?:轻轻|缓缓|微笑着|抬起|低下)|你(?:接过|喝下|坐到|吃下)', text):
        raise ValueError('只能输出终端文字，不使用动作旁白或替对方行动')
    if facts is not None:
        corpus = json.dumps(facts, ensure_ascii=False)
        for number in re.findall(r'(?<![\w])\d+(?:\.\d+)?', text):
            if number not in corpus:
                raise ValueError('消息包含来源没有提供的数字')
    return segments


class ExpressionService:
    def __init__(self, coordinator):
        self.c = coordinator
        self.enabled = os.getenv('AZURJUUS_EXPRESSION_ENABLED', '1') == '1'
        self.chat_gate, self.task_gate = asyncio.Semaphore(1), asyncio.Semaphore(1)
        self.generate = self.complete
        with self.c.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS speeches(id TEXT PRIMARY KEY, run_id TEXT, actor_id TEXT, status TEXT, data TEXT NOT NULL)')

    async def complete(self, messages, settings):
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(settings['llmBaseUrl'].rstrip('/') + '/chat/completions',
                headers={'Authorization':'Bearer ' + settings['llmApiKey']},
                json={'model':settings['llmModel'], 'messages':messages, 'temperature':.75,
                    'max_tokens':1800, 'response_format':{'type':'json_object'}})
            if response.status_code != 200:
                raise RuntimeError(f'表达模型 HTTP {response.status_code}')
            data = response.json()
            self.c.store.event(None, 'expression.usage', {'model':settings['llmModel'], 'usage':data.get('usage', {}), 'cost':None})
            return json.loads(data['choices'][0]['message']['content'])

    def save(self, sid, run_id, actor_id, status, data):
        with self.c.store.connect() as db:
            db.execute('INSERT INTO speeches VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data',
                (sid, run_id, actor_id, status, json.dumps(data, ensure_ascii=False)))

    async def speak(self, run, actor, phase, intent, facts=None, source=None):
        source = source or f"{run['id']}:{phase}"
        sid = 'speech-' + hashlib.sha256(source.encode()).hexdigest()[:24]
        with self.c.store.connect() as db:
            row = db.execute('SELECT status,data FROM speeches WHERE id=?', (sid,)).fetchone()
        if row and row['status'] == 'delivered':
            return '\n\n'.join(json.loads(row['data'])['segments'])
        data = json.loads(row['data']) if row else {'intent':intent, 'facts':facts, 'phase':phase, 'source':source}
        self.save(sid, run['id'], actor['id'], 'queued', data)
        try:
            async with (self.chat_gate if phase == 'chat' else self.task_gate):
                if 'segments' not in data:
                    persona, version = context(actor['id'])
                    mind = self.c.cognition.context(actor['id'], intent, peers={a['id'] for a in run['actors']}) if self.c.cognition else ''
                    detailed = phase == 'chat' and bool(re.search('详细|展开|代码|报告|过程|依据', intent))
                    policy = TERMINAL + '返回 JSON：{"segments":["短消息"],"sourceIds":["指定来源"]}。'
                    policy += ('用户明确请求详细内容，可以展开。' if detailed else '通常一两条气泡、合计30至120字，上限180字。简单确认可以更短。')
                    policy += '工作事实只能来自给定事实包；不播报调用编号、哈希，不新增成功声明。讨论保留实际观点，不代替对方同意。'
                    messages = [{'role':'system','content':policy}, {'role':'system','content':'当前角色：' + persona},
                        {'role':'system','content':'人物可见状态：' + mind},
                        {'role':'system','content':'相关背景：' + json.dumps(lore(intent, actor['name']), ensure_ascii=False)}]
                    messages.extend(run.get('history', [])[-12:])
                    messages.append({'role':'user', 'content':json.dumps({'intent':intent, 'facts':facts, 'sourceId':source,
                        'address':self.c.settings_loader().get('userAddress', '指挥官')}, ensure_ascii=False)})
                    started = time.monotonic()
                    for attempt in range(2):
                        value = await asyncio.wait_for(self.generate(messages, self.c.settings_loader()), 15)
                        try:
                            data['segments'] = validate(value, source, detailed, facts)
                            break
                        except ValueError as exc:
                            if attempt:
                                raise
                            messages.append({'role':'user','content':'请重新生成：' + str(exc)})
                    data.update(version=version, latencySeconds=time.monotonic()-started)
                    self.save(sid, run['id'], actor['id'], 'ready', data)
                envelope = {'messageId':sid, 'actorId':actor['id'], 'assignmentId':phase,
                    'text':'\n\n'.join(data['segments']), 'segments':data['segments'], 'expression':True, 'sourceIds':[source]}
                await self.c.message_callback(run['id'], envelope)
                self.c.store.event(run['id'], 'message.complete', envelope)
                self.save(sid, run['id'], actor['id'], 'delivered', data)
                return envelope['text']
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            data['error'] = type(exc).__name__ + ': ' + str(exc)[:160]
            self.save(sid, run['id'], actor['id'], 'failed', data)
            self.c.store.event(run['id'], 'expression.failed', {'messageId':sid, 'actorId':actor['id'], 'error':data['error']})
            if phase == 'chat':
                raise RuntimeError('回复生成失败，请重试。' + data['error']) from exc
            return ''
