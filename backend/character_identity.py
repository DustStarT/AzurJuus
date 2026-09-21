"""Source editions share one roster identity; sources never create actors."""
SOURCE_EDITIONS = {'拉菲II': ('拉菲', '拉菲II'), '约克城II': ('约克城', '约克城II')}
SOURCE_ALIASES={'拉菲':['ラフィー'],'标枪':['ジャベリン'],'绫波':['綾波'],'Z23':['ニーミ','Z23'],
    '雅努斯':['ジェーナス'],'贾维斯':['ジャーヴィス'],'信浓':['信濃','鵗']}

def source_aliases(name):
    names=source_names(name)
    return list(dict.fromkeys(names+[alias for n in names for alias in SOURCE_ALIASES.get(n,[])]))

def source_names(name):
    return list(SOURCE_EDITIONS.get(name, (name,)))

def roster_actors(session):
    from sqlalchemy import select
    from .models import Actor, WorkspaceSetting
    workspace=session.get(WorkspaceSetting,1)
    names={n.strip() for n in (workspace.character_roster_text if workspace else '').splitlines() if n.strip()}
    return [a for a in session.scalars(select(Actor).where(Actor.kind=='agent',Actor.is_active.is_(True))).all()
        if (a.source_character or a.name) in names]

def material_context(materials):
    if not materials:return ''
    return '\n同一人物的原版与II版背景资料（不是两个成员；不将不同情境同时发生，不覆盖用户设定或任务事实）：\n' + '\n'.join(
        f"资料：{m['name']}；出处：{m['source']}\n{m['text']}" for m in materials)

def default_relationship(owner, peer):
    same=bool(owner.faction and owner.faction not in {'未知阵营','用户设定','未整理'} and owner.faction==peer.faction)
    return {'origin':'user-world-setting','text':('同阵营，默认彼此认识且熟悉。' if same else '默认彼此认识。')+
        '不需要重新自我介绍；这不代表有过本应用中的共同任务，能力信任仍依据实际结果。',
        'familiarity':'familiar' if same else 'known'}

def references_hidden(value, hidden):
    if isinstance(value,str):return value in hidden
    if isinstance(value,dict):return any(references_hidden(v,hidden) for v in value.values())
    if isinstance(value,(list,tuple)):return any(references_hidden(v,hidden) for v in value)
    return False
