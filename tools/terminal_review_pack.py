"""Create readable anonymous review material; never invent human scores."""
import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
out = ROOT / 'validation' / 'terminal'
report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
rng = random.Random(917)
lines = ['# 聊天终端匿名对照评分', '',
    '请由两名评阅者分别独立阅读。A/B 顺序已随机；不要先查看答案映射。',
    '自然度：像人物在聊天终端打出的消息，是否简短、顺畅、不像模板报告。',
    '角色辨识：措辞和关注点是否符合给定角色。分别填写 A、B 或平局。',
    '另记录无来源知情、替用户行动、核心设定矛盾。短并不自动等于好。',
    '这是重建旧提示与当前表达服务的合成场景对照，不是历史用户消息重放。', '']
mapping = []
for sample in report['scenes']:
    variants = ['before', 'after']
    rng.shuffle(variants)
    sid = str(sample['scene'])
    lines += [f"## 场景 {sid}：{sample['actor']}", '', sample['intent'], '',
        '已知事实：' + json.dumps(sample['facts'], ensure_ascii=False), '']
    for label, variant in zip(('A', 'B'), variants):
        lines += [f'### {label}', '', sample.get(variant, '未生成'), '']
    mapping.append({'scene':sid,'A':variants[0],'B':variants[1]})
(out/'匿名对照.md').write_text('\n'.join(lines),encoding='utf-8')
(out/'review-key.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2),encoding='utf-8')
for reviewer in (1,2):
    path=out/f'评阅者{reviewer}.csv'
    if not path.exists():
        with path.open('w',encoding='utf-8-sig',newline='') as file:
            writer=csv.writer(file)
            writer.writerow(['场景','自然度_A_B_平局','角色辨识_A_B_平局','无来源知情','设定矛盾','备注'])
            writer.writerows([[s['scene'],'','','','',''] for s in report['scenes']])
full=ROOT/'validation/personality-benchmark'
pairs=json.loads((full/'blind-pairs.json').read_text(encoding='utf-8'))
actors={str(row['scene']-1)+':'+str(row['repeat']):row['actor']
        for row in (json.loads(line) for line in (full/'responses.jsonl').read_text(encoding='utf-8').splitlines())}
lines=['# 完整场景匿名对照', '', '24 个场景各重复三次。评分填写 reviewer-1.csv / reviewer-2.csv。',
       '该组比较使用受控提示和合成经历，不能证明生产认知数据库的连续性。', '']
for pair in pairs:
    lines += [f"## {pair['pair']} · {actors.get(pair['pair'], '')}：{pair['scenario']}", '', '### A', '', pair['A'], '', '### B', '', pair['B'], '']
(full/'匿名对照.md').write_text('\n'.join(lines),encoding='utf-8')
print('Created Chinese anonymous review packs; both reviewers remain pending.')
