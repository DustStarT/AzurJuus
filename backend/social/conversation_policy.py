"""Cheap publication checks grounded in the current public conversation."""
import re
from difflib import SequenceMatcher


def normalized(text):
    return re.sub(r'\W+', '', str(text or '').lower())


def solicits_reply(text):
    text = ' '.join(str(text or '').split())
    return bool(re.search(r'[?？]|能否|是否|要不要|能不能|可不可以|'
        r'(?:请|麻烦).{0,16}(?:回答|解释|告诉|确认|帮|提醒)|'
        r'(?:谁|怎么|为什么|多少|哪(?:个|位|里)).{0,18}(?:呢|吗|么|呀|啊|[?？])', text))


def closing_context(history, topic, member_ids=()):
    """A farewell ends by intent, rather than by a fixed number of turns."""
    text = ' '.join(str(topic.get('prompt') or '').split())
    closing = not topic.get('proactive') and bool(re.search(
        r'晚安|再见|拜拜|回头聊|下次再聊|先下线|先走了|该睡觉|我要睡|我去睡|'
        r'今天(?:先|就)?聊到这|good\s*night|goodbye', text, re.I))
    if re.search(r'先别.{0,6}(?:睡|走|晚安|再见)|不(?:想|要|打算)?睡|继续聊|接着聊|再聊一会|别说晚安', text):
        closing = False
    if re.search(r'(?:解释|翻译|讨论|分析|教我).{0,10}(?:晚安|再见)|'
            r'(?:晚安|再见).{0,15}(?:是什么意思|怎么说|英文|日语|出处|这句话|这个词)', text):
        closing = False
    user = next((h for h in reversed(history) if h.get('speakerId') == 'commander'), {})
    # A concrete request accompanying a farewell still needs a real answer.
    request = solicits_reply(text) or bool(re.search(r'帮我|替我|麻烦你|提醒我|告诉我|说说|讲讲|总结一下|'
        r'请.{0,15}(?:看|读|查|做|解释|回答|提醒|聊聊|谈谈)', text))
    everyone = bool(re.search(r'(?:每个|每位|所有|全体|大家都|各位都).{0,14}(?:说|道|回|祝).{0,8}(?:晚安|再见|拜拜)', text))
    targets = set(member_ids) if everyone else set(user.get('mentions') or [])
    boundary = next((i for i in range(len(history)-1, -1, -1) if history[i].get('id') == user.get('id')), len(history))
    responded = {h.get('speakerId') for h in history[boundary+1:]}
    return {'closing': closing, 'hasUserRequest': request,
        'pendingTargets': sorted(targets - responded), 'userMessageId': user.get('id')}


def closing_violation(body):
    """Do not turn an acknowledged farewell into a new question or assignment."""
    body = ' '.join(str(body or '').split())
    return solicits_reply(body) or bool(re.search(
        r'(?:明天|明早|早餐).{0,20}(?:交给我|我来|准备|摆盘|帮忙|负责)|'
        r'(?:交给我|我来|准备|负责).{0,20}(?:明天|明早|早餐)|'
        r'需要.{0,10}(?:随时|找我)|我还没睡', body))


def repeats_own_speech(body, previous, current=''):
    """Only suppress a near replay by its original speaker within one topic.

    Changed quantities, negation, corrections, and new answers remain eligible.
    This is not a general semantic-similarity gate on other people's replies.
    """
    if (re.search(r'再说一遍|重复一遍|重说|复述|原话|再发一次', current)
            and not re.search(r'(?:不要|不用|不必|别|无需).{0,6}(?:再说|重复|复述|重说|再发)', current)):
        return False
    value = normalized(body)
    # Short confirmations may legitimately answer different direct questions.
    if len(value) <= 24 and solicits_reply(current):
        return False
    for prior in previous:
        old = normalized(prior)
        if not value or not old:
            continue
        if value == old:
            return True
        match = SequenceMatcher(None, old, value, autojunk=False)
        # Small wording edits (for example deleting a quoted clause or swapping
        # one filler word) are still the same turn.  A high threshold alone
        # misses those edits; cap the changed material so a genuinely new answer
        # with a shared opening remains eligible.
        if match.ratio() < .84:
            continue
        numbers = r'[\d零一二两三四五六七八九十百千万]+'
        if re.findall(numbers, value) != re.findall(numbers, old):
            continue
        if len(re.findall(r'不|没|无|别', value)) != len(re.findall(r'不|没|无|别', old)):
            continue
        changed = ''.join(value[j1:j2] for tag, _, _, j1, j2 in match.get_opcodes() if tag != 'equal')
        removed = ''.join(old[i1:i2] for tag, i1, i2, _, _ in match.get_opcodes() if tag != 'equal')
        if re.search(r'不|没|无|别', changed + removed):
            continue
        if re.search(r'纠正|更正|应该是|实际上|可以|同意|拒绝|接受|明白|好的|没问题|是的', changed):
            continue
        # A substantial addition is not the old response being replayed.
        if len(value) - len(old) >= 4:
            continue
        if len(changed) + len(removed) > max(12, int(max(len(value), len(old)) * .32)):
            continue
        return True
    return False
