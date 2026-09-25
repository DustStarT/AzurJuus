"""Model-selected message handling. Routing never grants tools or edits a task."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Literal

from pydantic import BaseModel, Field


class RouteConflict(ValueError):
    """A request key was reused with a different visible message."""


class RouteDecision(BaseModel):
    kind: Literal['chat', 'followup', 'task', 'swarm', 'guidance', 'clarify']
    actorIds: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(default='', max_length=160)


class IntentRouter:
    def __init__(self, store, expression, settings_loader):
        self.store=store
        self.expression=expression
        self.settings_loader=settings_loader
        self.lock=asyncio.Lock()

    def get(self, request_key):
        with self.store.connect() as db:
            row=db.execute('SELECT data,status,conversation_id FROM message_routes WHERE request_key=?',
                (request_key,)).fetchone()
        return {**json.loads(row['data']),'status':row['status'],'conversationId':row['conversation_id']} if row else None

    def save(self, request_key, conversation_id, data, status='decided'):
        with self.store.connect() as db:
            db.execute('INSERT INTO message_routes(request_key,conversation_id,status,data,updated) VALUES(?,?,?,?,?) '
                'ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,data=excluded.data,updated=excluded.updated',
                (request_key,conversation_id,status,json.dumps(data,ensure_ascii=False),time.time()))

    async def decide(self, request_key, conversation_id, content, *, room_kind, members, roster,
                     history, attachments, active_task=None):
        digest=hashlib.sha256((conversation_id+'\0'+content+'\0'+json.dumps(attachments,sort_keys=True,
            ensure_ascii=False)).encode()).hexdigest()
        async with self.lock:
            existing=self.get(request_key)
            if existing:
                if existing.get('contentHash')!=digest or existing['conversationId']!=conversation_id:
                    raise RouteConflict('请求编号已用于另一条消息。')
                return existing
            choices=[{'id':actor['id'],'name':actor['name'],'inChannel':actor['id'] in members,
                'taskEligible':not actor.get('socialOnly')} for actor in roster]
            recent=[{'speakerId':item.get('speakerId'),
                'text':str(item.get('content') or item.get('text') or item.get('body') or '')[:400]}
                for item in history[-8:] if item.get('speakerId')]
            prompt={'current':content[:4000],'conversationId':conversation_id,'channelKind':room_kind,
                'members':members,'actors':choices,'recentMessages':recent,
                'attachments':[{'name':a.get('name',''),'mime':a.get('mime','')} for a in attachments[:12]],
                'activeTask':{'id':active_task['id'],'status':active_task['status']}
                    if active_task else None}
            messages=[{'role':'system','content':(
                '你是 JUUS 消息路由器，只判断用户这条消息应如何处理，不扮演角色、不执行工具。'
                '返回 JSON：kind 为 chat/followup/task/swarm/guidance/clarify，actorIds 为建议执行者编号数组，'
                'confidence 为 0 到 1，reason 为不超过 160 字的简短依据。'
                '问候、普通感想和讨论做法走 chat。对已经完成工作的结果、读过的书或文件提出感想、评价、依据问题时选 followup；这仍是聊天，不重新读取。'
                '明确要求实际读取、写入或交付才选 task；确有多人分工价值才选 swarm。'
                '用户没有明确委托、只是询问能否做或范围含糊时选 clarify。'
                'guidance 仅用于修改当前正在执行的协作任务；询问进度或聊天走 chat。'
                '不得从历史或附件推导新的授权；成员列表不表示他们在同一地点。'
                '只使用给出的 actorIds，不输出思维链。')},
                {'role':'user','content':json.dumps(prompt,ensure_ascii=False)}]
            cfg={**self.settings_loader(),'_structuredOutputTokens':500,'_structuredTimeout':25,
                '_mindStage':'route'}
            value=await self.expression.complete(messages,cfg)
            decision=RouteDecision.model_validate(value)
            allowed={a['id'] for a in roster if not a.get('socialOnly')}
            selected=list(dict.fromkeys(a for a in decision.actorIds if a in allowed))
            kind=decision.kind
            changed_actor=False
            if kind=='task' and room_kind=='dm':
                peer=[a for a in members if a in allowed][:1]
                changed_actor=selected!=peer
                selected=peer
            elif kind=='task' and room_kind=='group':
                selected=[a for a in selected if a in members][:1]
            if kind=='guidance' and not active_task:kind='chat'
            if kind in {'task','swarm','guidance'} and decision.confidence<.7:kind='clarify'
            if kind=='task' and not selected:
                kind='clarify'
            evidence_promoted=False
            if room_kind=='dm' and kind in {'chat','clarify'}:
                # The router sees only recent messages. A deictic question can
                # still refer to a verified read in an older or different room.
                # Only the actual reader's durable evidence may promote it.
                from backend.chat.task_reference import DEICTIC, FILE_NAME, resolve_task_reference
                new_read_request=bool(re.search(
                    r'(?:重新|再|重读|从头).{0,12}(?:读|看|查|打开|概括|总结)|'
                    r'(?:请|帮我|麻烦).{0,12}(?:读取|查看|打开|扫描|概括|总结|修改|写入)', content))
                if not new_read_request and (DEICTIC.search(content) or FILE_NAME.search(content)):
                    peer=[aid for aid in members if aid in allowed][:1]
                    evidence=resolve_task_reference(self.store,peer[0],content,recent) if peer else None
                    if evidence and not evidence.get('ambiguous'):
                        kind='followup'
                        selected=peer
                        evidence_promoted=True
            reason = (decision.reason if kind == decision.kind and not changed_actor else
                '委托或成员选择尚不明确，需要先澄清；未启动工具任务。')
            if evidence_promoted:
                reason='当前聊天对象有可核验的相关读取记录，按结果追问回答；不启动工具。'
            if changed_actor and kind=='task':
                reason='单人委托由当前私聊对象承接；处理方式不改变工具授权。'
            data={'kind':kind,'actorIds':selected,'confidence':decision.confidence,
                'reason':reason[:160],'contentHash':digest,'content':content,
                'attachments':attachments,'at':time.time()}
            self.save(request_key,conversation_id,data)
            self.store.event(None,'message.route',{'requestId':request_key,'conversationId':conversation_id,
                'kind':kind,'confidence':decision.confidence})
            return {**data,'status':'decided','conversationId':conversation_id}
