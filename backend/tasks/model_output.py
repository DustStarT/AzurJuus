"""Validate provider replies before treating text or function calls as success."""
import json
import math


def completion_message(payload):
    if not isinstance(payload, dict):
        raise RuntimeError('模型响应格式无效。')
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError('模型返回了空响应或无效的 choices。')
    message = choices[0].get('message')
    if not isinstance(message, dict):
        raise RuntimeError('模型响应缺少有效的 message。')
    text_content(message.get('content'))
    function_calls(message.get('tool_calls'))
    return message


def text_content(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get('text'), str):
                parts.append(block['text'])
            elif isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('content'), str):
                parts.append(block['content'])
            else:
                raise RuntimeError('模型响应包含无效的正文块。')
        return '\n'.join(part.strip() for part in parts if part.strip()).strip()
    raise RuntimeError('模型响应正文必须是文字或正文块数组。')


def function_calls(calls):
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise RuntimeError('模型工具调用必须是数组。')
    parsed = []
    for call in calls:
        if not isinstance(call, dict) or call.get('type') != 'function' or not isinstance(call.get('id'), str) or not call['id']:
            raise RuntimeError('模型工具调用缺少有效类型或编号。')
        function = call.get('function')
        if not isinstance(function, dict) or not isinstance(function.get('name'), str) or not function['name'].strip():
            raise RuntimeError('模型工具调用缺少有效名称。')
        try:
            def nonfinite(_):
                raise ValueError('Non-finite JSON number')
            def finite_float(value):
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError('Non-finite JSON number')
                return number
            args = json.loads(function['arguments'], parse_constant=nonfinite, parse_float=finite_float)
            if not isinstance(args, dict):
                raise ValueError('Arguments must be an object')
        except (KeyError, ValueError, TypeError, RecursionError):
            raise RuntimeError('模型工具参数必须是有效的 JSON 对象。') from None
        parsed.append({'id': call['id'], 'name': function['name'], 'args': args})
    return parsed
