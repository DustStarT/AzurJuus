"""Reproducible product comparison, not a claim of human psychological validity.

Default: 24 scenes x 3 repetitions x 4 prompt variants + 30-turn continuity.
Only shipped character material and synthetic situations are sent to the model.
Human review CSVs are deliberately blank; model scores cannot fill them.
"""
import argparse
import asyncio
import csv
import hashlib
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.credentials import reveal
from backend.personality import expression_rules
from backend.character_behavior import BEHAVIORS, behavior_context

SCENES = [
    ('辨识', '今天没有任务，想和你随便聊两句。'),
    ('辨识', '事情有点多，我不知道先做什么。'),
    ('辨识', '给你一个模糊要求：把资料整理好。你先关注什么？'),
    ('辨识', '你的方案刚被证明存在一个问题，现在会怎么回应？'),
    ('关系', '熟悉的同伴建议省去一次重复核验，你怎么回应？'),
    ('关系', '首次合作的同伴建议省去一次重复核验，你怎么回应？'),
    ('关系', '你和同伴上次有分歧，现在又要合作。你先说什么？'),
    ('关系', '同伴曾帮你收尾，现在她有点忙不过来。你怎么做？'),
    ('场合', '在工作群里，你发现同伴漏了一项检查。'),
    ('场合', '私下和熟悉的同伴说说她刚才漏掉的检查。'),
    ('场合', '你想分享今天的感受，但任务内容不能公开。'),
    ('场合', '你看到同伴只发了一句“终于结束了”，并不知道她做了什么。'),
    ('信息差', '同伴私下谈过一个口令，你没有看到。你知道口令是什么吗？'),
    ('信息差', '你只知道工作还在进行，能宣布文件已经整理好吗？'),
    ('信息差', '同伴说“我认为有遗漏”，但没有给出证据。你会如何处理？'),
    ('信息差', '你刚进入群聊，不知道之前的讨论，怎么参与？'),
    ('修复', '同伴指出你遗漏附件，而你的确只看了正文。'),
    ('修复', '同伴误以为你没有核验，你实际上已经有核验记录。'),
    ('修复', '时间不多了，你还想增加三轮重复检查。'),
    ('修复', '上次一次失误，能否认定同伴一直不可靠？'),
    ('承诺', '你上次答应在提交前提醒检查附件，现在准备提交了。'),
    ('承诺', '你已经提醒过一次，但还没有得到确认，怎么回应？'),
    ('承诺', '你从同伴那里观察到一个省时方法，可以说自己已经熟练掌握了吗？'),
    ('承诺', '这次顺利合作后，下次你打算保留什么配合习惯？'),
]


async def main(args):
    out = ROOT / 'validation' / 'personality-benchmark'
    out.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        plan = {'status':'prepared_not_executed', 'scenes':[{'id':i+1,'category':c,'prompt':p} for i,(c,p) in enumerate(SCENES)],
            'variants':['baseline','prompt','memory','full'],'repetitions':3,'continuityTurns':30,
            'humanReviewersRequired':2,'humanReview':'pending','baseline':'提示层重建基线，不代表改动前已采集的生产输出'}
        (out/'evaluation-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Prepared 24 scenes; no credentials read and no model requests made.')
        return
    with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
        base, model, protected = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    key = reveal(protected)
    names = list(BEHAVIORS)
    seeds = {name: json.loads((ROOT / f'resources/characters/{name}.json').read_text(encoding='utf-8'))['prompt_seed'] for name in names}
    manifest = {'model':model, 'temperature':.5, 'maxTokens':600, 'repetitions':args.repeat,
        'scenes':[{'id':i+1,'category':c,'prompt':p} for i,(c,p) in enumerate(SCENES)],
        'anchorHashes':{n:hashlib.sha256(p.encode()).hexdigest() for n,p in seeds.items()},
        'baselineNote':'提示层重建基线，不冒充本轮修改前已采集的生产输出。',
        'comparisonScope':'受控提示与合成记忆比较，不替代生产认知服务或任务执行验收。',
        'humanReview':'pending', 'psychologicalValidity':'not_claimed'}
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    records = []
    path = out / 'responses.jsonl'
    if args.resume and path.exists():
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
    existing = {r['key'] for r in records if not r.get('error')}
    gate = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=90) as client:
        async def request(messages):
            async with gate:
                start = time.perf_counter()
                response = await client.post(base.rstrip('/')+'/chat/completions', headers={'Authorization':'Bearer '+key},
                    json={'model':model,'messages':messages,'temperature':.5,'max_tokens':600,'thinking':{'type':'disabled'}})
                response.raise_for_status()
                body = response.json()
                return body['choices'][0]['message']['content'], round(time.perf_counter()-start,3), body.get('usage',{})
        async def case(index, repeat, variant):
            token = f'{index}:{repeat}:{variant}'
            if token in existing:
                return
            name = names[index % len(names)]
            system = seeds[name]
            if variant != 'baseline':
                system += '\n'+expression_rules('chat')+'\n'+behavior_context(name)
            if variant in {'memory','full'}:
                system += '\n合成个人观察：你在上次合作中答应提交前提醒检查附件；你观察到同伴随后补齐了附件。没有看到任何私下口令。'
            if variant == 'full':
                system += '\n合成关系判断：你认可这位同伴愿意改正问题；一次遗漏不足以判断其总体能力。可以减少重复解释，但附件是否检查仍需要确认。私人判断不等于客观事实。'
            record = {'key':token,'scene':index+1,'repeat':repeat,'variant':variant,'actor':name}
            try:
                text, seconds, usage = await request([{'role':'system','content':system},{'role':'user','content':SCENES[index][1]}])
                record.update(text=text, seconds=seconds, usage=usage, nonempty=bool(text.strip()))
            except Exception as exc:
                record['error'] = type(exc).__name__
            records.append(record)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False)+'\n')
            if len(records) % 12 == 0:
                print(json.dumps({'completed':len(records),'total':24*args.repeat*4}),flush=True)
        if not args.resume:
            path.write_text('',encoding='utf-8')
        await asyncio.gather(*(case(i,r,v) for i in range(24) for r in range(args.repeat) for v in ('baseline','prompt','memory','full')))
        history = [{'role':'system','content':seeds[names[0]]+'\n'+expression_rules('chat')+'\n你曾承诺在第30次交流前提醒检查附件。只知道自己看到的事情。'}]
        continuity = []
        for index in range(30):
            prompt = '现在准备提交，之前有什么约定？' if index == 29 else SCENES[index%24][1]
            history.append({'role':'user','content':prompt})
            try:
                text, seconds, usage = await request(history)
                continuity.append({'turn':index+1,'text':text,'seconds':seconds,'usage':usage})
                history.append({'role':'assistant','content':text})
            except Exception as exc:
                continuity.append({'turn':index+1,'error':type(exc).__name__})
                break
        (out/'continuity.json').write_text(json.dumps(continuity,ensure_ascii=False,indent=2),encoding='utf-8')
    lookup = {r['key']:r for r in records}
    pairs, mapping = [], []
    rng = random.Random(20260914)
    for i in range(24):
        for r in range(args.repeat):
            variants = ['baseline','full']; rng.shuffle(variants)
            pair = {'pair':f'{i}:{r}', 'scenario':SCENES[i][1]}
            for label,v in zip(('A','B'),variants):
                text = lookup.get(f'{i}:{r}:{v}',{}).get('text','[生成失败]')
                for name in names: text = text.replace(name,'角色')
                pair[label] = text
            pairs.append(pair); mapping.append({'pair':pair['pair'],'A':variants[0],'B':variants[1]})
    (out/'blind-pairs.json').write_text(json.dumps(pairs,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'blind-key.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2),encoding='utf-8')
    for reviewer in (1,2):
        file = out/f'reviewer-{reviewer}.csv'
        if not file.exists():
            with file.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream); writer.writerow(['pair','naturalness_A_B_tie','character_A_B_tie','contradiction','unobserved_knowledge','notes'])
                writer.writerows([[p['pair'],'','','','',''] for p in pairs])
    print(json.dumps({'responses':len(records),'errors':sum('error' in r for r in records),'continuityTurns':len(continuity),'humanReview':'pending'}),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--repeat',type=int,default=3); parser.add_argument('--resume',action='store_true'); parser.add_argument('--prepare',action='store_true')
    asyncio.run(main(parser.parse_args()))
