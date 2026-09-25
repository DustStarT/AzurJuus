"""Provider-compatible, stage-specific reasoning controls.

The application never stores a provider's raw reasoning text. Capability probes
are optional and cached by model connection in the existing control table.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from backend.database import session_scope
from backend.mind.mind_models import MindControl
from backend.tasks.model_connections import connection_id, normalize


def cache_id(base_url: str, model: str) -> str:
    return 'reasoning:' + connection_id(*normalize(base_url, model))


def capabilities(base_url: str, model: str) -> dict:
    try: cid = cache_id(base_url, model)
    except ValueError: return {}
    with session_scope() as session:
        row = session.get(MindControl, cid)
        return dict(row.data) if row else {}


def remember(base_url: str, model: str, result: dict) -> None:
    with session_scope() as session:
        cid = cache_id(base_url, model)
        row = session.get(MindControl, cid)
        if row: row.data = dict(result)
        else: session.add(MindControl(id=cid, data=dict(result)))


def apply(payload: dict, settings: dict) -> dict:
    """Apply only known or explicitly probed controls to a fresh request body."""
    stage = settings.get('_mindStage', 'express')
    cognitive = stage in {'understand', 'decide', 'reflect', 'profile'}
    model = settings['llmModel']
    base_url = settings['llmBaseUrl']
    cap = capabilities(base_url, model)
    if cap.get('tokenLimitParameter') == 'max_completion_tokens' and 'max_tokens' in payload:
        payload['max_completion_tokens'] = payload.pop('max_tokens')
    if cap.get('structuredOutput') == 'unsupported':
        payload.pop('response_format', None)
    official_deepseek = urlsplit(base_url).hostname == 'api.deepseek.com' and model.startswith('deepseek-')
    if official_deepseek or cap.get('thinkingMode') == 'supported':
        payload['thinking'] = {'type': 'enabled' if cognitive else 'disabled'}
        if cognitive and (official_deepseek or cap.get('reasoningEffort') == 'supported'):
            payload['reasoning_effort'] = 'high' if stage == 'decide' and settings.get('_mindComplex') else 'low'
        if cognitive:
            payload.pop('temperature', None)
    elif cognitive and cap.get('reasoningEffort') == 'supported':
        payload['reasoning_effort'] = 'high' if stage == 'decide' and settings.get('_mindComplex') else 'low'
        payload.pop('temperature', None)
    return payload
