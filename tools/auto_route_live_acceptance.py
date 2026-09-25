"""Small real-provider route sample; only synthetic messages leave the machine."""
import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))


async def sample(coordinator, provider, actors, output):
    from backend.chat.intent_router import IntentRouter

    router = IntentRouter(coordinator.store, coordinator.expression, lambda:provider)
    members = ['commander', actors[0]['id']]
    examples = [
        ('hello', '你好，今天怎么样？', 'chat', None),
        ('opinion', '你觉得这个办法怎么样？', 'chat', None),
        ('read', '请实际读取工作区的电子书并概括前十页。', 'task', None),
        ('followup', '你觉得这本书写得怎么样？', 'followup', None),
        ('unclear', '能帮我看看吗？', 'clarify', None),
        ('group', '请你和拉菲分工核查工作区的甲.pdf和乙.pdf，最后合并两份结论。', 'swarm', None),
        ('progress', '现在进度如何？', 'chat', {'id':'synthetic-task','status':'running','prompt':'核查资料'}),
        ('steer', '把当前任务的第二阶段提前，先核查附件。', 'guidance',
            {'id':'synthetic-task','status':'running','prompt':'核查资料'}),
    ]
    results = []
    for label, content, expected, active in examples:
        try:
            value = await router.decide(uuid4().hex, 'synthetic-dm', content,
                room_kind='dm', members=members, roster=actors,
                history=[{'speakerId':'commander','body':'之前请你概括电子书，结果已经交付。'}]
                    if label == 'followup' else [], attachments=[], active_task=active)
            results.append({'case':label,'expected':expected,'actual':value['kind'],
                'confidence':value['confidence'],'reason':value['reason'],
                'match':expected == value['kind']})
        except Exception as exc:
            results.append({'case':label,'expected':expected,'error':type(exc).__name__ + ': ' + str(exc)[:120]})
            if len(results) == 1:
                break
    dialogue = []
    coordinator.settings_loader = lambda:provider
    speaker = next((a for a in actors if a['name'] == '标枪'), actors[0])
    for label, prompt in [('greeting','晚上好，各位。'),
        ('distance','拉菲困了就去睡吧，你们在群里继续聊也行。')]:
        run, _ = coordinator.store.create({'actorId':speaker['id'], 'actors':actors,
            'mode':'chat', 'collaborative':False, 'conversationId':'port-hub',
            'prompt':prompt, 'history':[{'role':'user','speakerId':'commander','content':prompt}],
            'workspace':''}, uuid4().hex)
        try:
            reply = await coordinator.expression.speak(run, speaker, 'chat', prompt,
                source=run['id']+':live-story', audience={'kind':'group_chat', 'name':'港区群聊',
                    'members':[{'id':a['id'],'name':a['name']} for a in actors]})
            dialogue.append({'case':label,'prompt':prompt,'reply':reply})
        except Exception as exc:
            dialogue.append({'case':label,'error':type(exc).__name__ + ': ' + str(exc)[:120]})
    output.write_text(json.dumps({'model':provider['llmModel'], 'scenes':results,'dialogue':dialogue,
        'mechanismPassed':None, 'humanNaturalnessReview':'pending'}, ensure_ascii=False, indent=2),
        encoding='utf-8')
    print(json.dumps({'completed':sum('actual' in r for r in results),
        'matched':sum(r.get('match',False) for r in results),
        'attempted':len(results),'dialogueReplies':sum('reply' in r for r in dialogue),
        'output':str(output)}, ensure_ascii=False))


def main():
    from backend.credentials import reveal
    database = ROOT / '.azurjuus' / 'azurjuus.db'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    if not row:
        raise SystemExit('No saved model configuration.')
    provider = {'llmBaseUrl':row[0], 'llmModel':row[1], 'llmApiKey':reveal(row[2])}
    isolated = Path(tempfile.mkdtemp(prefix='azur-route-live-'))
    workspace = isolated / 'workspace'
    workspace.mkdir()
    os.environ.update(AZURJUUS_DATABASE_URL='sqlite+pysqlite:///' + (isolated/'business.db').as_posix(),
        AZURJUUS_WORKSPACE_STATE_PATH=str(isolated/'state.json'), AZURJUUS_WORKSPACE_ROOT=str(workspace),
        AZURJUUS_CHROMA_PATH=str(isolated/'chroma'), AZURJUUS_CHROMA_URL='', AZURJUUS_REDIS_URL='',
        AZURJUUS_SOCIAL_ENABLED='0', AZURJUUS_MIND_ENABLED='0', AZURJUUS_EXPRESSION_ENABLED='0',
        AZURJUUS_REFLECTION_ENABLED='0', AZURJUUS_SKILL_TRIALS_ENABLED='0')
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.app import create_app
    from backend.database import session_scope
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        coordinator = client.app.state.runs
        with session_scope() as session:
            actors = client.app.state.service.build_snapshot(session)['agents'][:4]
        asyncio.run(sample(coordinator, provider, actors, isolated/'route-results.json'))


if __name__ == '__main__':
    main()
