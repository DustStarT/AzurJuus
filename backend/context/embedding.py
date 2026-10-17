"""Optional CPU embeddings. Loading never downloads or falls back to a cloud."""
import hashlib
import json
from pathlib import Path
from threading import RLock

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
REPOSITORY = 'Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
REVISION = 'faf4aa4225822f3bc6376869cb1164e8e3feedd0'
HASHES = {
    'model_optimized.onnx': '634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99',
    'tokenizer.json': 'fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00',
    'config.json': 'c8ec081fdad2df991bf5abbf18418fec7a5cdaa421f60ffb060a30040b8c376f',
    'tokenizer_config.json': '0666eebf692422757e1dddf3c9fb1ded73ba3dc726c5828671fc89e45bf3609f',
    'special_tokens_map.json': '8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835',
}
FILES = list(HASHES)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class Embeddings:
    def __init__(self, root, settings):
        self.root, self.settings = Path(root), settings
        self.model = None
        self.tokenizer = None
        self.error = '尚未安装本机模型，使用词法检索'
        self.lock = RLock()
        self._signature = ''

    @property
    def version(self):
        cfg = self.settings()
        if cfg['provider'] == 'cloud':
            return 'cloud:' + hashlib.sha256(json.dumps([
                cfg['baseUrl'], cfg['model'], cfg['dimensions'], cfg.get('modelVersion', '')
            ]).encode()).hexdigest()
        return MODEL + ':' + REVISION + ':384'

    def load(self):
        with self.lock:
            cfg = self.settings()
            folder = Path(cfg.get('localModelPath') or self.root / 'embedding-model')
            signature = str(folder)
            if self.model is not None and self._signature == signature:
                return True
            try:
                for name, expected in HASHES.items():
                    if digest(folder / name) != expected:
                        raise ValueError('本机模型校验失败：' + name)
                from fastembed import TextEmbedding
                from tokenizers import Tokenizer
                self.model = TextEmbedding(model_name=MODEL, threads=2,
                    providers=['CPUExecutionProvider'], local_files_only=True,
                    specific_model_path=str(folder))
                self.tokenizer = Tokenizer.from_file(str(folder / 'tokenizer.json'))
                self.tokenizer.no_truncation()
                self._signature = signature
                self.error = ''
                return True
            except Exception as exc:
                self.error = '本机向量不可用（' + type(exc).__name__ + '），使用词法检索'
                self.model = None
                return False

    def prepare(self):
        """User-requested, revision-pinned download; no document leaves the machine."""
        import httpx
        folder = self.root / 'embedding-model'
        folder.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            for name in FILES:
                target = folder / name
                if target.exists() and (name not in HASHES or digest(target) == HASHES[name]):
                    continue
                temporary = target.with_suffix('.download')
                try:
                    with client.stream('GET', f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}') as response:
                        response.raise_for_status()
                        with temporary.open('wb') as stream:
                            for block in response.iter_bytes():
                                stream.write(block)
                    if name in HASHES and digest(temporary) != HASHES[name]:
                        raise ValueError('下载文件哈希不匹配')
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        if not self.load():
            raise ValueError(self.error)

    def chunks(self, text):
        if self.tokenizer:
            spans = self.tokenizer.encode(text, add_special_tokens=False).offsets
        else:
            # Conservative Unicode spans until the actual tokenizer is available.
            # Reindex with tokenizer when the model is installed.
            spans = [(i, i + 1) for i in range(len(text))]
        for start in range(0, len(spans), 80):
            end = min(start + 96, len(spans))
            left, right = spans[start][0], spans[end - 1][1]
            yield text[left:right], left, right
            if end == len(spans):
                break

    def embed(self, texts):
        with self.lock:
            cfg = self.settings()
            if cfg['provider'] == 'lexical':
                return None
            if cfg['provider'] == 'cloud':
                import httpx
                from backend.credentials import reveal
                if not cfg.get('apiKey') or not cfg.get('baseUrl'):
                    raise ValueError('未单独配置 embedding 接口')
                response = httpx.post(cfg['baseUrl'].rstrip('/') + '/embeddings',
                    headers={'Authorization': 'Bearer ' + reveal(cfg['apiKey'])},
                    json={'model': cfg['model'], 'input': texts}, timeout=30)
                response.raise_for_status()
                rows = sorted(response.json()['data'], key=lambda r: r['index'])
                vectors = [r['embedding'] for r in rows]
            else:
                if self.model is None:
                    return None  # Initialization belongs to the background worker.
                vectors = [v.tolist() for v in self.model.embed(texts, batch_size=16, parallel=None)]
            import numpy as np
            values = np.asarray(vectors, dtype=np.float32)
            dim = cfg['dimensions'] if cfg['provider'] == 'cloud' else 384
            if values.shape != (len(texts), dim) or not np.isfinite(values).all():
                raise ValueError('embedding 数量、维度或数值无效')
            norms = np.linalg.norm(values, axis=1, keepdims=True)
            return values / np.maximum(norms, 1e-12)
