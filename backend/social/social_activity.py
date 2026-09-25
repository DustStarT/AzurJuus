"""Online-only social scheduling and durable rolling background-call budget."""
import time
from sqlalchemy import update
from backend.database import session_scope
from backend.models import Message
from backend.social.social_models import SocialTopic


class SocialActivity:
    KEY = 'social-control'
    DEFAULTS = {'paused': False, 'hourlyCalls': 30, 'topicIntervalSeconds': 1800, 'idleSeconds': 900}

    def __init__(self, engine):
        self.engine = engine
        self.last_input = None
        self.source = None
        self.revision = 0

    def initialize(self):
        with session_scope() as s:
            if not s.get(SocialTopic, self.KEY):
                s.add(SocialTopic(id=self.KEY, conversation_id='', status='control', version=0,
                    data={'settings': dict(self.DEFAULTS), 'calls': [], 'lastTopic': 0,'defaultsVersion':'life-v4'}))

    def touch(self, cid, message_id):
        self.last_input = time.monotonic()
        self.source = (cid, message_id)
        self.revision += 1

    def inspect(self):
        runtime=getattr(self.engine.c.cognition,'runtime',None)
        if runtime and runtime.enabled:
            result=runtime.control()
            return {**result,'settings':{**self.DEFAULTS,**result['settings']},'lastTopic':0}
        with session_scope() as s:
            row = s.get(SocialTopic, self.KEY)
            data = row.data if row else {}
            return {'settings': {**self.DEFAULTS, **data.get('settings', {})},
                'callsLastHour': sum(t > time.time()-3600 for t in data.get('calls', [])),
                'lastTopic': data.get('lastTopic', 0)}

    def change(self, payload):
        limits = {'hourlyCalls': (1, 1000), 'topicIntervalSeconds': (1800, 86400), 'idleSeconds': (60, 3600)}
        for key, value in payload.items():
            if key == 'paused':
                if not isinstance(value, bool): raise ValueError('暂停设置应为布尔值')
            elif key not in limits or type(value) is not int or not limits[key][0] <= value <= limits[key][1]:
                raise ValueError('社交参数超出范围')
        self.initialize()
        with session_scope() as s:
            # Acquire the SQLite writer before reading and replacing the JSON.
            s.execute(update(SocialTopic).where(SocialTopic.id == self.KEY).values(version=SocialTopic.version+1))
            row = s.get(SocialTopic, self.KEY)
            row.data = {**row.data, 'settingsEdited':True, 'settings': {**self.DEFAULTS, **row.data.get('settings', {}), **payload}}
        self.revision += 1
        runtime=getattr(self.engine.c.cognition,'runtime',None)
        if runtime and runtime.enabled:
            runtime.control({k:v for k,v in payload.items() if k in {'paused','hourlyCalls'}})
        return self.inspect()

    def reserve(self, *, topic=False):
        """Reserve before inference, including failures; never invent token/cost data."""
        self.initialize()
        now = time.time()
        with session_scope() as s:
            s.execute(update(SocialTopic).where(SocialTopic.id == self.KEY).values(version=SocialTopic.version+1))
            row = s.get(SocialTopic, self.KEY)
            data = dict(row.data)
            cfg = {**self.DEFAULTS, **data.get('settings', {})}
            calls = [t for t in data.get('calls', []) if t > now-3600]
            if cfg['paused'] or len(calls) >= cfg['hourlyCalls']: return False
            if topic:
                if now-data.get('lastTopic', 0) < cfg['topicIntervalSeconds']: return False
                data['lastTopic'] = now
            else:
                calls.append(now)
            row.data = {**data, 'calls': calls}
        return True

    async def tick(self):
        e = self.engine
        if not e.enabled or self.last_input is None or not self.source or e.c.tasks: return
        elapsed = time.monotonic()-self.last_input
        if not 60 <= elapsed <= self.inspect()['settings']['idleSeconds']: return
        cid, source_id = self.source
        try:
            room = e.snapshot(cid)
        except ValueError:
            return
        if room['kind'] != 'group' or room['config']['muted']: return
        from backend.social.social_engine import stable
        tid = stable(source_id, 'proactive')
        with session_scope() as s:
            if s.get(SocialTopic, tid): return
            source = s.get(Message, source_id)
            if not source or source.conversation_id != cid or source.speaker_id != 'commander': return
        if not self.reserve(topic=True): return
        with session_scope() as s:
            s.add(SocialTopic(id=tid, conversation_id=cid, status='active', version=0,
                data={'runId': None, 'sourceMessageId': source_id, 'proactive': True,
                    'prompt': '看看近期可见交流中是否还有值得接续的话题。有自己的想法才开口，不重复回答、不强行热场；没有则沉默。',
                    'spoken': 0, 'invites': 0, 'recent': []}))
        revision = self.revision
        await e.exchange(tid, is_busy=lambda: bool(e.c.tasks) or self.revision != revision, background=True)
