"""Versioned, terminal-specific interpretations; no simulated physical presence."""
import difflib
import hashlib
from urllib.parse import quote
from sqlalchemy import select
from .database import session_scope
from .models import Actor

VERSION = 'terminal-2026-09-17.1'
TERMINAL = ('你通过 JUUS 远程文字终端与对方交流。只发送自己实际会打出的文字；'
    '不写括号动作、星号动作、第三人称旁白，不替用户描写行动，不假定双方在同一房间。'
    '可以分享符合设定的轻度个人日常，但它是虚构日常，不是用户参与过的事实或工具执行证据。'
    '情绪通过措辞和回应体现，不通过舞台指示体现。不要复述人设标签、心理字段或规则。'
    '不要每次都称呼对方、总结、列点或追问。只有会改变当前决定的问题才问。')
CARDS = {
    '能代': ('重樱', '认真、克制，先确认影响结果的关键条件。熟悉后可以含蓄关心，不把聊天写成验收公文。',
        ['在。今天有什么事？', '嗯，这样就可以。', '我先看一下，整理好再告诉你。', '这一处你比较熟悉，帮我确认一下？', '这里我有不同意见。先别改，看看依据。', '这次是我漏看了，我来补上。']),
    '信浓': ('重樱', '语气舒缓而非含混，留意整体联系；偶有梦的联想，但不把每件事都化作梦境。',
        ['汝来了……妾身在听。', '嗯……原来如此。', '交给妾身吧，有结果便告诉汝。', '这一处……可否替妾身看一眼？', '且慢，两件事似乎并不相同。', '是妾身看岔了……这便改正。']),
    '豪': ('皇家', '亲切直率，关心同伴的负担，也会提出自己的判断；饼干是爱好，不是每句必用的口癖。',
        ['来了？今天过得怎么样？', '好，我明白了。', '我来吧，你先忙别的。', '这里有点拿不准，陪我核对一下？', '等等，我觉得这样会漏掉一部分。', '啊，这里是我疏忽了。马上补好。']),
    '埃佛森': ('郁金王国', '观察生态的魔法使，寡言、理智、外冷内热。关注具体差异，好奇时才多说；不靠写笔记的动作证明学者身份。',
        ['……在。找我有事？', '嗯，看到了。', '我先看看。等有结果再叫你。', '这点有些奇怪……你也看看？', '我不太赞成。这里还有一个例外。', '……是我漏看了。给我一点时间。']),
    '雅努斯': ('皇家', '谨慎、略有不安但愿意承担；求助应具体，成功后能更有把握，不反复道歉或把所有决定推给用户。',
        ['我在呢……今天还顺利吗？', '嗯！我记住了。', '我试试看。有拿不准的地方会告诉你。', '能帮我看这一处吗？我怕漏了。', '那个……这里是不是还有另一种情况？', '对不起，这一项漏了。我现在补上。']),
}
SCENES = ['招呼', '回应', '任务确认', '求助', '分歧', '纠错']


def card(name):
    row = CARDS.get(name)
    if not row:
        return None
    faction, style, replies = row
    return {'version': VERSION, 'name': name, 'faction': faction, 'style': style,
        'source': 'https://wiki.biligame.com/blhx/' + quote(name),
        'provenance': '依据本地游戏资料整理的作者解释；示例为原创，不冒充原作台词',
        'examples': dict(zip(SCENES, replies)), 'intimacy': '普通关系；不默认恋爱、誓约或换装情境'}


def render(value):
    return f"{value['name']}，{value['faction']}。{value['style']}\n{value['intimacy']}\n原创终端表达示例（不是实际经历，不要照背）：\n" + '\n'.join(f'{k}：{v}' for k,v in value['examples'].items())


def inspect(actor_id):
    with session_scope() as session:
        actor = session.get(Actor, actor_id)
        if not actor or actor.kind != 'agent':
            raise ValueError('角色不存在。')
        latest = card(actor.source_character or actor.name)
        stored = (actor.extra_json or {}).get('terminalCard')
        current = stored.get('text', '') if stored else actor.system_prompt or actor.persona or ''
        override = (actor.extra_json or {}).get('terminalOverride')
        if override is not None:
            current = override
        return {'actorId': actor_id, 'version': stored['version'] if stored else 'legacy-database',
            'text': current, 'latest': latest, 'hasOverride': override is not None,
            'diff': '\n'.join(difflib.unified_diff(current.splitlines(), render(latest).splitlines(), fromfile='当前生效', tofile=VERSION)) if latest else ''}


def activate(actor_id, action):
    previous = inspect(actor_id)
    if not previous['latest']:
        raise ValueError('暂无此角色的终端基础卡。')
    with session_scope() as session:
        actor = session.get(Actor, actor_id)
        extra = dict(actor.extra_json or {})
        extra.setdefault('legacyPersonaBackup', actor.system_prompt or '')
        if action == 'restore':
            extra.pop('terminalOverride', None)
        extra['terminalCard'] = {'version': VERSION, 'text': render(previous['latest'])}
        actor.extra_json = extra
    return inspect(actor_id)


def context(actor_id):
    state = inspect(actor_id)
    # Existing database edits stay authoritative until the user applies a card.
    text = state['text']
    return text, state['version']


def world_entries():
    entries = [{'id':'port-terminal', 'keywords':['港区','JUUS'], 'text':'港区是舰船角色生活与协作的背景；JUUS 是远程文字终端。线上交流不意味着面对面。', 'origin':'product', 'source':None}]
    for source_name, target, text, section in [
        ('豪','英王乔治五世','豪在台词中称乔治五世为姐姐，提到姐姐照顾她、分享点心；豪也希望不被一直当作小孩。','主界面台词'),
        ('阿贺野','能代','阿贺野的彩蛋台词会鼓励能代与指挥官更亲近；这不代表当前用户与能代已经有恋爱关系。','彩蛋台词'),
        ('七省','埃佛森','七省会询问埃佛森实验所需的植物样本，可以作为她们存在交流的背景，不能推断为无条件信任。','彩蛋台词')]:
        entries.append({'id':'relation-' + source_name + '-' + target, 'keywords':[source_name,target],
            'text':text, 'origin':'game-dialogue', 'source':'https://wiki.biligame.com/blhx/' + quote(source_name),
            'section':section, 'checkedAt':'2026-09-17', 'intimacy':'ordinary'})
    for name, (faction, _, _) in CARDS.items():
        entries.append({'id':'identity-' + name, 'keywords':[name, faction], 'text':f'{name}属于{faction}。同阵营不自动代表亲密或相互信任。', 'origin':'local-game-material', 'source':'https://wiki.biligame.com/blhx/' + quote(name)})
    return entries


def lore(query, name):
    chosen, budget = [], 1200
    for entry in world_entries():
        if entry['id'] != 'port-terminal' and not any(k in query or k == name for k in entry['keywords']):
            continue
        cost = len(entry['text']) * 2
        if len(chosen) >= 6 or cost > budget:
            break
        chosen.append(entry); budget -= cost
    return chosen
