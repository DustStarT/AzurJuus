"""Opt-in real ONNX evaluation; never contacts a chat or embedding provider."""
import json
import os
from pathlib import Path

import pytest
from backend.context.embedding import Embeddings
from backend.context.retrieval import RetrievalService
from backend.context.service import DEFAULTS


@pytest.mark.skipif(not os.getenv('AZURJUUS_CONTEXT_TEST_MODEL'), reason='需要显式指定已校验的本机模型目录')
def test_sixty_labelled_queries_real_multilingual_model(tmp_path):
    cfg = {**DEFAULTS, 'localModelPath': os.environ['AZURJUUS_CONTEXT_TEST_MODEL']}
    embedding = Embeddings(tmp_path, lambda: cfg)
    assert embedding.load(), embedding.error
    index = RetrievalService(tmp_path / 'retrieval.db', embedding)
    cases = json.loads(Path(__file__).with_name('retrieval_cases.json').read_text(encoding='utf-8'))
    allowed = {}
    for i, case in enumerate(cases):
        source = str(i)
        allowed[source] = {'version': '1'}
        index.index(source, '1', case['text'])
        for chunk, _, _ in embedding.chunks(case['text']):
            assert len(embedding.tokenizer.encode(chunk, add_special_tokens=False).ids) <= 96
    while index.vector_batch():
        pass
    exact, semantic, failures = 0, 0, []
    for i, case in enumerate(cases):
        expected = str(i)
        hits = index.search(case['file'], allowed, pinned=[expected])
        exact += bool(hits and hits[0]['source'] == expected)
        for query in case['queries']:
            hits = index.search(query, allowed, limit=5)
            found = expected in {h['source'] for h in hits}
            semantic += found
            if not found:
                failures.append({'query': query, 'expected': case['file'], 'sources': [h['source'] for h in hits]})
        # The exact same index must not leak an excluded source through FTS or vectors.
        private = {k: v for k, v in allowed.items() if k != expected}
        assert all(h['source'] != expected for h in index.search(case['text'], private, pinned=[expected]))
    report = {'queries': 60, 'exactHits': exact, 'exactTotal': 20, 'semanticHitsAt5': semantic,
              'semanticTotal': 40, 'semanticRecallAt5': semantic / 40, 'privacyLeaks': 0,
              'model': embedding.version, 'failures': failures}
    output = Path('validation/context-retrieval-acceptance.json')
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert exact == 20, report
    assert semantic / 40 >= .9, report
