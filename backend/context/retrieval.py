"""A disposable SQLite index. Authority and ACL always come from business rows."""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from threading import RLock


def terms(text):
    output = re.findall(r'[a-z0-9_]+', str(text).lower())
    for span in re.findall(r'[\u3400-\u9fff]+', str(text)):
        output.extend(span[i:i + 2] for i in range(len(span) - 1))
        if len(span) == 1:
            output.append(span)
    return list(dict.fromkeys(output))


class RetrievalService:
    def __init__(self, path, embedding):
        self.path, self.embedding = path, embedding
        self.lock = RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, source TEXT, version TEXT, ordinal INTEGER,
                    text TEXT, start INTEGER, stop INTEGER, hash TEXT,
                    vector BLOB, model TEXT);
                CREATE INDEX IF NOT EXISTS source_idx ON chunks(source,version);
                CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED, terms);
                CREATE TABLE IF NOT EXISTS vectors (
                    hash TEXT, model TEXT, vector BLOB, PRIMARY KEY(hash,model));
            ''')

    @contextmanager
    def db(self):
        with self.lock, sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            yield db

    def index(self, source, version, text, *, vectors=False):
        chunks = list(self.embedding.chunks(text))
        model = self.embedding.version
        with self.db() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM chunks WHERE source=?', (source,))]
            db.executemany('DELETE FROM fts WHERE id=?', [(i,) for i in ids])
            db.execute('DELETE FROM chunks WHERE source=?', (source,))
            for ordinal, (fragment, start, stop) in enumerate(chunks):
                key = source + ':' + str(ordinal)
                hashed = hashlib.sha256(fragment.encode()).hexdigest()
                cached = db.execute('SELECT vector FROM vectors WHERE hash=? AND model=?', (hashed, model)).fetchone()
                db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (key, source, version, ordinal, fragment, start, stop, hashed,
                     cached[0] if cached else None, model))
                db.execute('INSERT INTO fts VALUES(?,?)', (key, ' '.join(terms(fragment))))
        return len(chunks)

    def vector_batch(self):
        """One interruptible batch; foreground work can yield between batches."""
        model = self.embedding.version
        with self.db() as db:
            rows = db.execute('SELECT id,text,hash FROM chunks WHERE vector IS NULL OR model!=? LIMIT 16', (model,)).fetchall()
        if not rows:
            return False
        vectors = self.embedding.embed([r['text'] for r in rows])
        if vectors is None:
            return False
        with self.db() as db:
            for row, vector in zip(rows, vectors):
                blob = vector.astype('float32').tobytes()
                db.execute('UPDATE chunks SET vector=?,model=? WHERE id=?', (blob, model, row['id']))
                db.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?)', (row['hash'], model, blob))
        return True

    def search(self, query, allowed, pinned=(), limit=12):
        """Filter before both rankings; never rank inaccessible text or embeddings."""
        if not allowed:
            return []
        pinned = set(pinned)
        entities = re.findall(r'(?<![\w-])(?:[A-Za-z]+[-_]\d[\w-]*|\d{4}-\d{2}-\d{2})(?![\w-])', query)
        for sid, source in allowed.items():
            data = source.get('data', {})
            metadata = ' '.join(str(v) for v in [source.get('title', ''), data.get('path', ''), data.get('url', '')])
            if any(value and str(value).lower() in query.lower() for value in [source.get('title'), data.get('path')]):
                pinned.add(sid)
            if entities and any(re.search(r'(?<![\w-])' + re.escape(entity) + r'(?![\w-])',
                    metadata + ' ' + source.get('text', ''), re.I) for entity in entities):
                pinned.add(sid)
        with self.db() as db:
            db.execute('CREATE TEMP TABLE permitted(id TEXT PRIMARY KEY, version TEXT)')
            db.executemany('INSERT INTO permitted VALUES(?,?)', [(k, v['version']) for k, v in allowed.items()])
            tokens = terms(query)[:80]
            lexical = []
            if tokens:
                expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in tokens)
                lexical = db.execute('''SELECT c.*, bm25(fts) AS rank FROM fts
                    JOIN chunks c ON c.id=fts.id JOIN permitted p ON p.id=c.source AND p.version=c.version
                    WHERE fts MATCH ? ORDER BY rank LIMIT 30''', (expression,)).fetchall()
            exact = db.execute('''SELECT c.* FROM chunks c JOIN permitted p
                ON p.id=c.source AND p.version=c.version ORDER BY c.ordinal''').fetchall() if pinned else []
            exact = [r for r in exact if r['source'] in pinned]
            dense = []
            similarities = {}
            try:
                pieces = [p[0] for p in self.embedding.chunks(query)] or [query]
                q = self.embedding.embed(pieces)
                if q is not None:
                    import numpy as np
                    query_vector = q.mean(axis=0)
                    query_vector /= max(float(np.linalg.norm(query_vector)), 1e-12)
                    cursor = db.execute('''SELECT c.* FROM chunks c JOIN permitted p
                        ON p.id=c.source AND p.version=c.version WHERE c.vector IS NOT NULL AND c.model=?''', (self.embedding.version,))
                    while batch := cursor.fetchmany(256):
                        values = np.stack([np.frombuffer(r['vector'], dtype=np.float32) for r in batch])
                        scores = values @ query_vector
                        dense.extend((float(score), row) for score, row in zip(scores, batch) if score >= .35)
                    dense.sort(key=lambda x: x[0], reverse=True)
                    similarities = {r['id']: score for score, r in dense}
                    dense = [r for _, r in dense[:30]]
            except Exception as exc:
                self.embedding.error = '向量检索降级：' + type(exc).__name__
            scores, records, reasons = {}, {}, {}
            for reason, ranking in [('entity', exact), ('lexical', lexical), ('semantic', dense)]:
                for rank, row in enumerate(ranking):
                    rid = row['id']
                    scores[rid] = scores.get(rid, 0) + (1 if reason == 'entity' else 1 / (60 + rank + 1))
                    records[rid] = row
                    reasons.setdefault(rid, []).append(reason)
            result, per_source = [], {}
            for rid in sorted(scores, key=scores.get, reverse=True):
                row = records[rid]
                count = per_source.get(row['source'], 0)
                if count >= (8 if row['source'] in pinned else 3):
                    continue
                per_source[row['source']] = count + 1
                result.append({k: row[k] for k in ('id', 'source', 'version', 'text', 'start', 'stop', 'ordinal')} |
                    {'score': scores[rid], 'reasons': reasons[rid],
                     'similarity': similarities.get(rid),
                     'needsClarification': reasons[rid] == ['semantic'] and similarities.get(rid, 0) < .60})
                if len(result) >= limit:
                    break
            return result

    def purge(self, allowed):
        with self.db() as db:
            ids = [(r['id'],) for r in db.execute('SELECT id,source FROM chunks') if r['source'] not in allowed]
            db.executemany('DELETE FROM chunks WHERE id=?', ids)
            db.executemany('DELETE FROM fts WHERE id=?', ids)
            db.execute('DELETE FROM vectors WHERE hash NOT IN (SELECT hash FROM chunks)')

    def status(self):
        with self.db() as db:
            row = db.execute('SELECT count(*),sum(vector IS NOT NULL AND model=?) FROM chunks', (self.embedding.version,)).fetchone()
            return {'chunks': row[0], 'embeddedChunks': row[1] or 0}
