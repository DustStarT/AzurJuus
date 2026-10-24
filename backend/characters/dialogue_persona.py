"""Project background into dialogue without treating game UI lines as live duties."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'resources' / 'characters'
PROFILE_POLICY = ('从人物自身的性格、思考方式、表达习惯和行为倾向理解其生活，而非替人物制定服务规范。'
    '人物有自己的兴趣、情绪、关系和说话节奏。认真、负责等特质也能表现为好奇、玩笑、迟疑或随意闲谈；'
    '身份与专长不是要求他人完成工作的职责。游戏中的任务、登录、回港台词是特定场景的参考，不是当前待办。'
    '提炼稳定人格，不把示例台词中的临时约定、活动或催促编译成永久目标。')


def imported_background(text, name):
    """Only project an unchanged shipped seed; user-authored text is authoritative."""
    if not name or Path(name).name != name:
        return text
    path = ROOT / (name + '.json')
    try:
        seed = json.loads(path.read_text(encoding='utf-8')).get('prompt_seed')
    except (OSError, ValueError):
        return text
    if text != seed:
        # Cache loading rebuilds prompt_seed with the current importer. Accept
        # that exact deterministic form too, without accepting arbitrary edits.
        from tools.blhx_character_import import load_profile
        try:
            current_seed = load_profile(path).prompt_seed
        except (OSError, ValueError, TypeError):
            return text
        if text != current_seed:
            return text
    # The archived source retains all skin/login/task samples. Dialogue gets
    # stable background rather than pretending their events are happening now.
    text = text.split('说话与互动风格参考：', 1)[0].strip()
    # Remove the old generation wrapper only from an exact shipped seed.
    # Original imports remain available for inspection; custom text wins.
    return text[text.index('角色定位：'):].strip() if '角色定位：' in text else text


def personality_for_dialogue(interpretation):
    # Decision strategies remain available to cognition and task execution.
    # Repeating them as a speaking instruction made even greetings sound like work.
    return {key: value for key, value in interpretation.items() if key in {
        'values', 'tendencies', 'selfImage', 'speechStyle', 'emotionalHabits',
        'interests', 'sensitiveSituations', 'exceptions'} and value}


def public_dialogue_intent(intent):
    """Expose a shareable reply direction without private appraisal fields."""
    if not isinstance(intent, dict):
        return {}
    result = {}
    if intent.get('action'):
        result['action'] = intent['action']
    if intent.get('targetId'):
        result['targetId'] = intent['targetId']
    if intent.get('action') in {'speak', 'publish', 'contact', 'invite'} and isinstance(intent.get('reply'), dict):
        reply = {key: intent['reply'][key] for key in ('focus', 'stance', 'tone')
                 if isinstance(intent['reply'].get(key), str) and intent['reply'][key].strip()}
        if reply:
            result['reply'] = reply
    return result
