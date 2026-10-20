"""Local Chinese dense embeddings plus lexical reranking; no remote inference."""
from array import array
import hashlib
import json
import math
import re
from pathlib import Path
import threading

from nas_scanner.safety import local_output
from .store import search, tokens

MODEL = 'BAAI/bge-small-zh-v1.5'
PROFILE = 'bge-zh-cls-unit-foreground-timeline-v1'
QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
# Small, explicit lexical action prior supplements (never replaces) dense retrieval.
ACTION_GROUPS = (('倒入', '倒进', '倾倒', '倾入', '倒出', '倒到', '倒回'),
                 ('舀', '盛起', '挖一勺', '挖起'),
                 ('堆叠', '叠放', '摞', '叠在', '叠起'),
                 ('撕开', '打开包装', '拆封', '开袋', '开封'),
                 ('搅拌', '搅动', '拌匀'))


class SemanticUnavailable(ValueError):
    pass


class LocalEncoder:
    def __init__(self, cache_dir='data/models', download=False):
        try:
            from fastembed import TextEmbedding
        except ImportError as error:
            raise SemanticUnavailable('请安装语义检索依赖：pip install -e ".[semantic]"') from error
        cache = local_output(Path(cache_dir))
        cache.mkdir(parents=True, exist_ok=True)
        try:
            self.model = TextEmbedding(MODEL, cache_dir=str(cache), threads=2,
                                       local_files_only=not download)
        except Exception as error:
            raise SemanticUnavailable('本地向量模型尚未准备好，请先运行 material-search index') from error
        # Bind persisted vectors to actual local weights/tokenizer, not just a model alias.
        model_path = Path(self.model.model._model_dir)
        digest = hashlib.sha256()
        for name in ('model_optimized.onnx', 'tokenizer.json', 'tokenizer_config.json'):
            path = model_path / name
            if path.is_file():
                with path.open('rb') as stream:
                    digest.update(hashlib.file_digest(stream, 'sha256').digest())
        self.identity = MODEL + ':' + PROFILE + ':' + digest.hexdigest()
        self.lock = threading.Lock()

    def documents(self, texts):
        with self.lock:
            return [list(map(float, v)) for v in self.model.embed(texts, batch_size=16)]

    def query(self, text):
        return self.documents([QUERY_PREFIX + text])[0]


def normalize(vector):
    values = list(map(float, vector))
    norm = math.sqrt(sum(v*v for v in values))
    if not values or not math.isfinite(norm) or norm == 0:
        raise ValueError('Invalid embedding vector')
    return [v/norm for v in values]


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS search_vectors(
      analysis_id INTEGER NOT NULL REFERENCES search_analyses(id), chunk_id INTEGER NOT NULL,
      encoder TEXT NOT NULL, text_hash TEXT NOT NULL, channel TEXT NOT NULL,
      text TEXT NOT NULL, start REAL, end REAL, vector BLOB NOT NULL, dimensions INTEGER NOT NULL,
      PRIMARY KEY(analysis_id,chunk_id,encoder))''')


def chunks(hit):
    # Keep foreground fields ahead of potentially distracting scene descriptions.
    main = '\n'.join(('摘要：'+hit['summary'][:260], '主体：'+hit['subjects'][:140],
                      '动作：'+hit['actions'][:320]))
    yield {'channel': 'main', 'text': main, 'start': None, 'end': None}
    try:
        parsed = json.loads(hit['evidence']['case']['raw_output'])
    except (ValueError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    timeline = parsed.get('timeline', [])
    if isinstance(timeline, list):
        for segment in timeline:
            if not isinstance(segment, dict):
                continue
            text = segment.get('description', segment.get('action', ''))
            if not isinstance(text, str) or not text.strip():
                continue
            start, end = segment.get('start'), segment.get('end')
            valid = (isinstance(start, (float, int)) and isinstance(end, (float, int))
                     and math.isfinite(start) and math.isfinite(end) and 0 <= start < end
                     and isinstance(hit['duration'], (float, int)) and start < hit['duration']
                     and end <= hit['duration'] + 0.3)
            yield {'channel': 'timeline', 'text': '动作：'+text[:450]+'\n主体：'+hit['subjects'][:100],
                   'start': float(start) if valid else None, 'end': float(end) if valid else None}
    if hit['ocr']:
        yield {'channel': 'ocr', 'text': '画面文字：'+hit['ocr'][:500], 'start': None, 'end': None}


def index(db, encoder):
    schema(db)
    pending = []
    all_chunks = 0
    for hit in search(db, '', _all=True):
        for chunk_id, chunk in enumerate(chunks(hit)):
            all_chunks += 1
            digest = hashlib.sha256(chunk['text'].encode()).hexdigest()
            old = db.execute('SELECT text_hash FROM search_vectors WHERE analysis_id=? AND chunk_id=? AND encoder=?',
                             (hit['id'], chunk_id, encoder.identity)).fetchone()
            if old and old['text_hash'] == digest:
                continue
            pending.append((hit['id'], chunk_id, digest, chunk))
    # Bounded batches, transactional commit: interruption never advertises partial readiness.
    with db:
        for offset in range(0, len(pending), 16):
            batch = pending[offset:offset+16]
            vectors = encoder.documents([c['text'] for _, _, _, c in batch])
            if len(vectors) != len(batch):
                raise ValueError('Embedding count mismatch')
            for (analysis_id, chunk_id, digest, chunk), vector in zip(batch, vectors):
                vector = normalize(vector)
                packed = array('f', vector)
                # Persist portable little-endian float32.
                import sys
                if sys.byteorder != 'little':
                    packed.byteswap()
                db.execute('''INSERT OR REPLACE INTO search_vectors VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (analysis_id, chunk_id, encoder.identity, digest, chunk['channel'], chunk['text'],
                            chunk['start'], chunk['end'], packed.tobytes(), len(vector)))
    return {'assets': len(search(db, '', _all=True)), 'chunks': all_chunks, 'embedded': len(pending),
            'model': MODEL, 'encoder': encoder.identity}



def pouring_prior(query, hit):
    """Conservative hybrid relation hint: poured object must precede destination.

    This is lexical evidence from model actions, not an independent visual fact.
    """
    match = re.search(r'(?:把|将)([^，。！？]{1,12}?)(?:倒进|倒入|倒到)', query)
    if not match:
        return 0.0
    subject = match.group(1).strip()
    aliases = ('麦片', '燕麦', '谷物') if subject in ('麦片', '燕麦', '燕麦片') else (subject,)
    try:
        parsed = json.loads(hit['evidence']['case']['raw_output'])
        actions = parsed.get('actions', []) if isinstance(parsed, dict) else []
    except (ValueError, TypeError):
        actions = []
    if not isinstance(actions, list):
        return 0.0
    sources = []
    for action in actions:
        if not isinstance(action, str):
            continue
        verb = re.search(r'倾倒|倒入|倒进|倒到|倒回|倒出', action)
        if not verb:
            continue
        prefix = action[:verb.start()]
        if '将' in prefix or '把' in prefix:
            sources.append(re.split(r'将|把', prefix)[-1])
        elif action[verb.start():verb.end()] in ('倾倒', '倒出'):
            sources.append(re.split(r'至|到|入|进|向', action[verb.end():])[0])
    if not sources:
        return 0.0
    return 0.08 if any(word in source for word in aliases for source in sources) else -0.08


def retrieve(db, encoder, query, limit=20, mode='hybrid', **filters):
    if mode not in ('semantic', 'hybrid', 'keyword'):
        raise ValueError('Unknown search mode')
    if not 1 <= limit <= 100:
        raise ValueError('limit must be 1..100')
    if mode == 'keyword' or not query.strip():
        return search(db, query, limit=limit, **filters)
    if encoder is None:
        raise SemanticUnavailable('语义索引尚未准备好，请先运行 material-search index；可切换关键词检索')
    schema(db)
    hits = search(db, '', _all=True, **filters)
    if not hits:
        return []
    by_id = {h['id']: h for h in hits}
    vectors = [dict(r) for r in db.execute('SELECT * FROM search_vectors WHERE encoder=?', (encoder.identity,))
               if r['analysis_id'] in by_id and (filters.get('include_ocr') or r['channel'] != 'ocr')]
    indexed = {r['analysis_id'] for r in vectors}
    if set(by_id) - indexed:
        raise SemanticUnavailable('素材分析有更新，语义索引需要重建；请运行 material-search index')
    q = normalize(encoder.query(query))
    # One asset can match several timeline chunks; strongest chunk determines dense ranking.
    best = {}
    for row in vectors:
        v = array('f'); v.frombytes(row['vector'])
        import sys
        if sys.byteorder != 'little':
            v.byteswap()
        if len(v) != len(q) or row['dimensions'] != len(q):
            raise SemanticUnavailable('向量维度不匹配，请重建索引')
        score = sum(a*b for a, b in zip(q, v))
        if row['analysis_id'] not in best or score > best[row['analysis_id']][0]:
            best[row['analysis_id']] = (score, row)
    query_terms = set(tokens(query).split())
    ranked = []
    for analysis_id, (cosine, chunk) in best.items():
        hit = by_id[analysis_id]
        # BGE cosine is not a calibrated probability. Keep the cutoff explicit and conservative.
        if cosine < 0.45:
            continue
        text = ' '.join((hit['summary'], hit['subjects'], hit['actions']))
        if filters.get('include_ocr'):
            text += ' '+hit['ocr']
        overlap = len(query_terms & set(tokens(text).split())) / max(1, len(query_terms))
        action_prior = 0.0
        if mode == 'hybrid':
            for group in ACTION_GROUPS:
                if any(word in query for word in group):
                    action_prior += 0.08 if any(word in hit['actions'] for word in group) else -0.12
        relation_prior = pouring_prior(query, hit) if mode == 'hybrid' else 0
        score = cosine + (0.08 * overlap if mode == 'hybrid' else 0) + action_prior + relation_prior
        hit['semantic_score'] = round(cosine, 5)
        hit['match'] = {'text': chunk['text'], 'start': chunk['start'], 'end': chunk['end'], 'channel': chunk['channel']}
        hit['retrieval_mode'] = mode
        hit['rank'] = -score
        ranked.append(hit)
    return sorted(ranked, key=lambda h: (h['rank'], h['sha256']))[:limit]
