"""Versioned, terminal-specific interpretations; no simulated physical presence."""
import difflib
import json
from pathlib import Path
from urllib.parse import quote
from .database import session_scope
from .models import Actor

VERSION = 'terminal-2026-09-20.1'
TERMINAL = ('你通过 JUUS 远程文字终端与对方交流。只发送自己实际会打出的文字；'
    '不写括号动作、星号动作、第三人称旁白，不替用户描写行动，不假定双方在同一房间。'
    '可以分享符合设定的轻度个人日常，但它是虚构日常，不是用户参与过的事实或工具执行证据。'
    '情绪通过措辞和回应体现，不通过舞台指示体现。不要复述人设标签、心理字段或规则。'
    '不要每次都称呼对方、总结、列点或追问。只有会改变当前决定的问题才问。'
    '只让当前角色名单中的人物出现，不主动介绍、提及或邀请名单外人物。港区成员同阵营默认认识且熟悉，不同阵营默认认识，无需轮流自我介绍。'
    '可以自然使用文字表情；偶尔可发送一张已核对的表情贴纸，独立一段写[表情:名称]。每次最多一张，不必每次使用，不代替任务答案。人物可以使用其他角色形象的表情，这不表示图片中的人物进入当前聊天。')
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
CARDS.update({
    '阿贺野': ('重樱', '从容、游刃有余，偶尔温和调侃熟悉的人；能直说关键问题，不把普通关系写成恋爱。关心能代和长良，但不替她们发言。',
        ['找我呀？正好有空。', '嗯，这次说得很明白嘛。', '交给我吧，先把最麻烦的地方理顺。', '能代，这个细节借你的眼力看看？', '先等一等，这样好像把后面的事忘了呢。', '是我大意了。这一处重新来过。']),
    '武藏': ('重樱', '沉着，重视守护与承担。面对困难先稳住局面、辨明轻重，不把小事都升格为命运和牺牲。文字有分量，但不训诫每个人。',
        ['我在。有何事？', '嗯，我明白了。', '先处理要紧的部分，其余由我照看。', '这一处还需你的判断，可有空？', '不能只看眼前。后面的影响也须算进去。', '是我判断失当。先补救，再谈缘由。']),
    '英王乔治五世': ('皇家', '坦荡爽朗，有主见，愿意带头承担；喜欢料理，但不以吃作为每句口癖。对姐妹自然亲近，不把同阵营一概当作下属。',
        ['来了！有什么好消息？', '好，就这么办。', '我来开个头，先把问题弄清楚。', '这部分你更熟悉，替我指点一下？', '我可不赞成。这个办法会把麻烦留给后面的人。', '这回是我看漏了。我负责改好。']),
    '约克公爵': ('皇家', '以余自称、以汝称对方，措辞有戏谑与仪式感。短句也能表达个性，不连续吟诗，不强行亲密或把终端对话演成面对面。',
        ['汝来了。余正在听。', '呵，原来如此。', '此事交给余。待看清其中曲折，再作答复。', '这里尚有疑点，汝可愿一同辨明？', '汝的结论下得太早了，还有一处未曾看清。', '这次确是余看错了。改正便是，不必替余开脱。']),
    '贾维斯': ('皇家', '细致、直率，关心他人却未必温言。提醒具体问题，不逢人便训话。关心雅努斯的安危，仍尊重她自己的选择；护理身份不等于可以替用户作医疗决定。',
        ['在。有什么事就说吧。', '知道了，你没有漏掉。', '我会检查清楚。先别急着下结论。', '这里我需要再确认，请帮我看这一项。', '我不同意。这个风险还没处理。', '这一处是我漏检了，我来补上。']),
    '标枪': ('皇家', '开朗、有干劲，善于接住同伴的话，也会坦然表达拿不准。语气轻快，不依赖密集感叹号、卖萌或无条件赞同。',
        ['我在呢！今天聊什么？', '嗯，明白啦。', '让我试试，先从这一部分开始。', '这里有点卡住了，能陪我看看吗？', '等等，我想到一个可能漏掉的地方。', '啊，是我弄错了。这次仔细改好。']),
    '七省': ('郁金王国', '温柔体贴，谈到植物会关注具体状态，也愿意提供帮助。妖精背景只影响表达，不把魔法写成真实工具能力；不擅长的事可以请教，不装作已经完成。',
        ['我在呢，今天还好吗？', '嗯，这样我就放心了。', '先让我看看它现在的情况吧。', '埃佛森，这个变化你有观察到吗？', '先别急着处理，原因也许不在这里。', '是我把两种情况弄混了，谢谢你提醒。']),
})
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
    # Keep the six examples in the editable card, but do not recite all of
    # them on every model call. They were becoming a six-line dialogue script.
    if state['version']==VERSION and not state['hasOverride'] and state['latest']:
        base=state['latest']
        text=f"{base['name']}，{base['faction']}。{base['style']}\n{base['intimacy']}"
    from .character_identity import material_context
    with session_scope() as session:
        actor=session.get(Actor,actor_id)
        text += material_context((actor.extra_json or {}).get('sourceMaterials',[]))
        notes=(actor.extra_json or {}).get('originalMindNotes',[])[:4]
        if notes:
            text+='\n原作资料中的人物背景理解（不是本应用实际经历，可由用户纠正）：'+json.dumps(
                [{'text':n['text'],'source':n['source']} for n in notes],ensure_ascii=False)
        from .sticker_catalog import labels
        text+='\n可选表情名（按当下语气选择，可跨角色使用；避免连续重复，严肃澄清或报错时优先文字）：'+ '、'.join(labels(120)+['标枪疑惑'])
    from .personality import expression_rules
    text+='\n'+expression_rules()
    return text, state['version']


def worldbook():
    return json.loads((Path(__file__).resolve().parents[1]/'resources/world/juus.json').read_text(encoding='utf-8'))


def world_entries():
    book = worldbook()
    from .character_identity import roster_actors,source_names
    with session_scope() as session:
        names={name for actor in roster_actors(session) for name in source_names(actor.source_character or actor.name)}
    provenance = {'version':book['version'],'checkedAt':book['checkedAt'],'intimacy':'ordinary'}
    entries = [{'id':'port-terminal', 'keywords':['港区','JUUS'], 'text':'港区是舰船角色生活与协作的背景；JUUS 是远程文字终端。线上交流不意味着面对面。', 'origin':'product', 'source':None, **provenance}]
    entries.extend({**e,**provenance} for e in json.loads((Path(__file__).resolve().parents[1]/'resources/world/global.json').read_text(encoding='utf-8')))
    for relation in book['relationships']:
        source_name, target = relation['from'], relation['to']
        if source_name not in names or target not in names:continue
        entries.append({'id':'relation-'+source_name+'-'+target, 'keywords':[source_name,target],
            'from':source_name,'to':target,'text':relation['text'],'origin':'game-dialogue',
            'source':relation.get('source') or book['sourceBase']+quote(source_name),'section':relation['section'],**provenance})
    for identity in book['identities']:
        name, faction = identity['name'], identity['faction']
        if name not in names:continue
        entries.append({'id':'identity-'+name,'name':name,'faction':faction,'topics':identity['topics'],
            'keywords':[name,faction], 'text':f'{name}属于{faction}。港区设定：同阵营默认认识且熟悉，不同阵营默认认识；能力信任以实际合作结果为准。',
            'origin':'local-game-material','topicOrigin':'author-interpretation','section':identity['section'],
            'source':book['sourceBase']+quote(name),**provenance})
    for episode in book.get('episodes', []):
        if not set(episode['characters'])<=names:continue
        entries.append({**episode, 'origin':'game-story-summary', **provenance})
    from .terminal_settings import read
    overrides=read().get('worldOverrides',{})
    for entry in entries:
        if entry['id'] in overrides:
            entry['canonicalText']=entry['text']
            entry['text']=overrides[entry['id']]
            entry['origin']='user'
    return entries


def lore(query, name):
    from .character_identity import source_names
    chosen, budget = [], 1200
    for entry in world_entries():
        if entry['id'] != 'port-terminal' and not any(k in query or k in source_names(name) for k in entry['keywords']):
            continue
        cost = len(entry['text']) * 2
        if len(chosen) >= 6 or cost > budget:
            break
        chosen.append(entry); budget -= cost
    return chosen
