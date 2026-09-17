"""Nonblocking peer discussion alongside file execution, with durable messages."""
import asyncio
import logging
from uuid import uuid4


class CollaborationDialogue:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.gate = asyncio.Semaphore(1)
        self.tasks = {}

    async def notice_failure(self, run_id, sender_id, phase, call_id, error):
        """An actual tool failure, rather than a personality script, can start help."""
        c = self.coordinator
        run = c.store.get(run_id)
        if not run.get('collaborative') or phase in {'planner','reviewer','chat'} or phase.startswith('discussion_'):
            return
        if run.get('automaticDiscussion'):
            return
        peers = {a['actorId'] for a in run['assignments']} - {sender_id}
        if not peers:
            return
        preferences = {}
        if c.cognition and c.cognition.enabled:
            for row in c.cognition.relationships(sender_id):
                evidence = row['observations']
                preferences[row['peerId']] = sum(1 if e.get('approach') == 'seek_help' else -1 if e.get('approach') == 'verify' else 0 for e in evidence[-3:])
        target = sorted(peers, key=lambda aid:(-preferences.get(aid,0), aid))[0]
        c.store.update(run_id, automaticDiscussion=call_id)
        await self.send(run_id, sender_id, {'actorId':target, 'kind':'question',
            'text':'刚才的操作返回了问题：' + str(error)[:600] + '。请帮我判断该先核验什么，不要假定已经解决。'})

    async def send(self, run_id, sender_id, args):
        c = self.coordinator
        run = c.store.get(run_id)
        target_id, text = str(args.get('actorId', '')), str(args.get('text', '')).strip()
        members = {run['actorId'], *(a['actorId'] for a in run['assignments'])}
        if not run['collaborative'] or target_id not in members or target_id == sender_id:
            raise ValueError('请选择当前协作任务中的另一位成员。')
        if not text or len(text) > 2000:
            raise ValueError('讨论内容需要 1–2000 字。')
        discussions = run.get('discussions', [])
        if len(discussions) >= 8:
            raise ValueError('本任务已达到 8 次讨论预算，请先处理已有意见。')
        kind = str(args.get('kind', 'question'))
        if kind not in {'question', 'suggestion', 'objection', 'answer', 'resolved'}:
            raise ValueError('讨论类型无效。')
        parent_id = args.get('threadId')
        parent = next((d for d in discussions if d['id'] == parent_id), None) if parent_id else None
        if parent_id and (not parent or {sender_id, target_id} != {parent['senderId'], parent['actorId']}):
            raise ValueError('只能续接双方已有讨论。')
        thread = parent.get('threadId', parent['id']) if parent else None
        if thread and sum(d.get('threadId', d['id']) == thread for d in discussions) >= 2 and kind != 'resolved':
            raise ValueError('本问题已往返两轮，请提交协调者或解决已有讨论。')
        did = uuid4().hex
        item = {'id':did, 'threadId':thread or did, 'kind':kind, 'visibility':'team',
            'senderId':sender_id, 'actorId':target_id, 'text':text, 'status':'queued', 'consumedBy':[]}
        if kind == 'resolved':
            if not parent:
                raise ValueError('解决讨论必须指定 threadId。')
            for d in discussions:
                if d.get('threadId', d['id']) == thread:
                    d['resolution'] = text
            c.store.update(run_id, discussions=discussions)
            return {'discussionId':parent_id, 'status':'resolved', 'note':'观点已解决；并非工具执行证据。'}
        c.store.update(run_id, discussions=[*discussions, item])
        envelope = {'actorId':sender_id, 'assignmentId':'discussion_' + did, 'messageId':did + '-question', 'text':text}
        if c.expression and c.expression.enabled:
            sender = next(a for a in run['actors'] if a['id'] == sender_id)
            target = next(a for a in run['actors'] if a['id'] == target_id)
            spoken = await c.expression.speak(run, sender, 'discussion_' + did + '-question',
                '把这次需要对方回应的问题或观点直接说出来，保留决定所需条件。', facts={'statement':text}, source=did + '-question',
                audience={'kind':'peer', 'id':target_id, 'name':target['name']})
            if spoken:
                item['spokenText'] = spoken
                self.patch(run_id, did, spokenText=spoken)
        elif c.message_callback:
            await c.message_callback(run_id, envelope)
        if not c.expression or not c.expression.enabled:
            c.store.event(run_id, 'message.complete', envelope)
        task = asyncio.create_task(self.reply(run_id, item), name='discussion-' + did)
        self.tasks[did] = (run_id, task)
        task.add_done_callback(lambda _: self.tasks.pop(did, None))
        return {'discussionId':did, 'status':'queued', 'note':'同伴将在独立讨论通道回应；不必阻塞其他可执行工作。'}

    def patch(self, run_id, did, **values):
        run = self.coordinator.store.get(run_id)
        for item in run.get('discussions', []):
            if item['id'] == did:
                item.update(values)
        self.coordinator.store.update(run_id, discussions=run.get('discussions', []))

    async def reply(self, run_id, item):
        c = self.coordinator
        try:
            self.patch(run_id, item['id'], status='running')
            run = c.store.get(run_id)
            actor = next(a for a in run['actors'] if a['id'] == item['actorId'])
            sender = next(a for a in run['actors'] if a['id'] == item['senderId'])
            # Only the addressee's directed view is supplied; never expose the
            # sender's private judgments to the recipient.
            relation = None
            try:
                if c.cognition and c.cognition.enabled and c.cognition.inspect(actor['id'])['enabled']:
                    relation = next((r for r in c.cognition.relationships(actor['id']) if r['peerId'] == sender['id']), None)
            except Exception:
                logging.getLogger(__name__).exception('Relationship context unavailable; continuing peer reply')
            prompt = f"同伴{sender['name']}在任务中对你说：{item['text']}\n总目标：{run['prompt']}\n请直接对同伴回应，可以质疑、承认疏忽、解释偏好或建议修正。只根据已有证据讨论；这是无工具讨论通道，不声称刚执行了任何新操作。简短自然，必要时空行分条说。"
            if relation:
                prompt += '\n你对这位同伴的已有认识：' + relation['summary'] + '。称呼对方，不要把同伴当指挥官；只接这次问题，不轮流总结。未亲历的事不要说成共同回忆。'
            if c.expression and c.expression.enabled:
                facts = {'question':item.get('spokenText') or item['text'], 'originalPoint':item['text']}
                facts['recentTurns'] = [{'question':d.get('spokenText') or d['text'], 'reply':d['reply']}
                    for d in run.get('discussions', []) if d.get('threadId') == item.get('threadId')
                    and d['id'] != item['id'] and d.get('status') == 'completed'][-2:]
                reply = await c.expression.speak(run, actor, 'discussion_' + item['id'],
                    '回应同伴刚刚的问题。给出自己的判断或具体建议；不要代替对方执行、同意或宣布解决。', facts=facts,
                    audience={'kind':'peer','id':sender['id'],'name':sender['name'],
                        'relationship':relation['summary'] if relation else '未记录直接关系，不预设亲密'})
            else:
                reply = await c.execute_actor(run, actor, 'discussion_' + item['id'], prompt, c.settings_loader())
            if not str(reply or '').strip():
                raise ValueError('同伴没有返回可显示的讨论回复。')
            self.patch(run_id, item['id'], status='completed', reply=reply)
            thread = item.get('threadId', item['id'])
            if sum(d.get('threadId',d['id']) == thread for d in c.store.get(run_id).get('discussions',[])) >= 2:
                self.patch(run_id, item['id'], needsCoordination=True)
                c.store.event(run_id, 'discussion.coordination_required', {'threadId':thread, 'actorId':item['actorId']})
        except asyncio.CancelledError:
            self.patch(run_id, item['id'], status='interrupted')
            raise
        except Exception as exc:
            self.patch(run_id, item['id'], status='failed', error=str(exc))

    def take(self, run_id, actor_id, phase):
        c = self.coordinator
        run = c.store.get(run_id)
        items = [d for d in run.get('discussions', []) if d['status'] == 'completed' and actor_id in {d['senderId'], d['actorId']} and phase not in d.get('consumedBy', [])]
        for item in items:
            item.setdefault('consumedBy', []).append(phase)
        if items:
            c.store.update(run_id, discussions=run['discussions'])
        return [{'from':d['actorId'], 'question':d['text'], 'reply':d['reply']} for d in items]

    async def drain(self, run_id):
        tasks = [t for rid, t in list(self.tasks.values()) if rid == run_id]
        if tasks:
            await asyncio.gather(*tasks)

    async def cancel(self, run_id):
        tasks = [t for rid, t in list(self.tasks.values()) if rid == run_id]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
