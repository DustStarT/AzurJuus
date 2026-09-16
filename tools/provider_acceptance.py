"""Probe the saved provider with synthetic messages; never print credentials."""
import asyncio
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.credentials import reveal


async def main(social_only=False):
    with sqlite3.connect((ROOT / '.azurjuus/azurjuus.db').as_uri() + '?mode=ro', uri=True) as db:
        base, model, stored_key = db.execute('SELECT llm_base_url,llm_model,llm_api_key FROM workspace_settings LIMIT 1').fetchone()
    key = reveal(stored_key)
    report = {'host': urlsplit(base).hostname, 'model': model, 'checks': [], 'status': 'failed'}
    if social_only:
        from backend.llm_runtime import AgentRuntime
        runtime = AgentRuntime(60)
        settings = {'llmBaseUrl': base, 'llmModel': model, 'llmApiKey': key}
        actor = {'id': 'synthetic-secretary', 'name': '测试秘书', 'systemPrompt': '你是认真又亲切的港区秘书。使用简短自然的中文。'}
        try:
            started = time.monotonic()
            post = await runtime.generate_social_post(settings=settings, agent=actor, prompt='为刚刚核验完成的合成水果报告写一条简短动态：3 个苹果加 4 个橙子，总数是 7。不要写工具调用或假装操作其它软件。', memory_snippets=[])
            assert post.strip(), 'Empty social post'
            report['checks'].append({'name': 'social post', 'seconds': round(time.monotonic() - started, 2)})
            started = time.monotonic()
            comment = await runtime.generate_social_comment(settings=settings, agent=actor, prompt='为以下动态写一句简短评论：' + post, relationship_hint='合作愉快的同伴')
            assert comment.strip(), 'Empty social comment'
            report['checks'].append({'name': 'social comment', 'seconds': round(time.monotonic() - started, 2)})
            report.update(status='passed', post=post, comment=comment)
        except Exception as exc:
            report['error'] = str(exc).replace(key, '[redacted]')
        (ROOT / 'validation/provider-social-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0 if report['status'] == 'passed' else 1
    headers = {'Authorization': 'Bearer ' + key}
    async with httpx.AsyncClient(timeout=120, headers=headers) as client:
        async def request(name, path, payload=None):
            started = time.monotonic()
            response = await client.request('GET' if payload is None else 'POST', base.rstrip('/') + path, json=payload)
            data = response.json()
            item = {'name': name, 'httpStatus': response.status_code, 'seconds': round(time.monotonic() - started, 2)}
            if response.is_error:
                item['error'] = json.dumps(data, ensure_ascii=False).replace(key, '[redacted]')[:1500]
            report['checks'].append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            response.raise_for_status()
            return data
        try:
            models = await request('model list', '/models')
            report['availableModels'] = [m['id'] for m in models.get('data', [])]
            print(json.dumps({'availableModels': report['availableModels']}, ensure_ascii=False), flush=True)
            text = await request('chat', '/chat/completions', {'model': model, 'messages': [{'role': 'user', 'content': 'This is a synthetic connectivity check. Reply only with AZUR_OK.'}], 'max_tokens': 512, 'thinking': {'type': 'disabled'}})
            report['chatReply'] = text['choices'][0]['message'].get('content')
            assert 'AZUR_OK' in (report['chatReply'] or ''), 'Connectivity response did not contain AZUR_OK'
            messages = [{'role': 'user', 'content': 'Call add_numbers with a=3 and b=4. After its result reply with the total.'}]
            tool = {'type': 'function', 'function': {'name': 'add_numbers', 'description': 'Add two synthetic integers.', 'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b']}}}
            first = await request('tool call', '/chat/completions', {'model': model, 'messages': messages, 'tools': [tool], 'max_tokens': 1024, 'thinking': {'type': 'disabled'}})
            message = first['choices'][0]['message']
            messages.append(message)
            calls = message.get('tool_calls') or []
            assert calls, 'No tool call returned'
            for call in calls:
                assert call['function']['name'] == 'add_numbers'
                args = json.loads(call['function']['arguments'])
                assert args == {'a': 3, 'b': 4}
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps({'total': args['a'] + args['b']})})
            final = await request('tool result round trip', '/chat/completions', {'model': model, 'messages': messages, 'tools': [tool], 'max_tokens': 1024, 'thinking': {'type': 'disabled'}})
            report['toolReply'] = final['choices'][0]['message'].get('content')
            assert '7' in (report['toolReply'] or ''), 'Tool result was not reflected in reply'
            fragments = []
            started = time.monotonic()
            async with client.stream('POST', base.rstrip('/') + '/chat/completions', json={'model': model, 'messages': [{'role': 'user', 'content': 'Reply only with STREAM_OK.'}], 'stream': True, 'max_tokens': 512, 'thinking': {'type': 'disabled'}}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith('data: ') and line[6:] != '[DONE]':
                        chunk = json.loads(line[6:])
                        for choice in chunk.get('choices', []):
                            fragments.append(choice.get('delta', {}).get('content') or '')
            report['streamReply'] = ''.join(fragments)
            assert 'STREAM_OK' in report['streamReply'], 'Stream incomplete'
            report['checks'].append({'name': 'streaming', 'httpStatus': 200, 'seconds': round(time.monotonic() - started, 2)})
            report['status'] = 'passed'
        except Exception as exc:
            report['error'] = str(exc).replace(key, '[redacted]')
    (ROOT / 'validation/provider-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--social-only', action='store_true')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.social_only)))
