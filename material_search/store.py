"""Local SQLite search baseline with immutable evaluation evidence."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from nas_scanner.safety import local_output

MODEL = 'doubao-seed-2-1-lite-260915'


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def tokens(text):
    text = text.casefold()
    words = re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', text)
    return ' '.join(t for w in words for t in ([w] if len(w) < 2 or w.isascii() else [w[i:i+2] for i in range(len(w)-1)]))


def connect(path):
    path = local_output(Path(path), database=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS search_assets(sha256 TEXT PRIMARY KEY, metadata TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS search_locations(sha256 TEXT REFERENCES search_assets, kind TEXT,
      path TEXT, UNIQUE(sha256,kind,path));
    CREATE TABLE IF NOT EXISTS search_analyses(id INTEGER PRIMARY KEY, sha256 TEXT REFERENCES search_assets,
      evidence_hash TEXT UNIQUE, model TEXT, summary TEXT, subjects TEXT, actions TEXT,
      description TEXT, ocr TEXT, duration REAL, review_state TEXT, evidence TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS search_attempts(evidence_hash TEXT PRIMARY KEY, evidence TEXT NOT NULL);
    CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(terms);
    ''')
    return db


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Source paths must be relative without parent traversal')
    return path


def import_results(db, comparison, run, manifest, testset):
    """Read JSON only. NAS locations are recorded as strings, never accessed."""
    samples = {s['sha256']: s for s in run['samples']}
    manifest_files = {s['sha256']: s for s in manifest['files']}
    root = manifest['source_root']
    protected = [root, testset]
    # Also protect source paths when library callers supply their own connection.
    database = db.execute('PRAGMA database_list').fetchone()['file']
    local_output(Path(database), protected=protected, database=True)
    selected = [c for c in comparison['cases'] if c['requested_model'] == MODEL]
    before = db.total_changes
    imported = 0
    with db:
        for case in selected:
            sha = case['sha256']
            if not re.fullmatch('[0-9a-f]{64}', sha):
                raise ValueError('Invalid SHA256')
            sample = samples[sha]
            source_record = manifest_files[sha]
            for key in ('source_relative_path', 'local_relative_path'):
                if sample[key] != source_record[key]:
                    raise ValueError('Manifest mapping mismatch')
            relative = safe_relative(sample['source_relative_path'])
            if case['source'] != str(relative):
                raise ValueError('Source mapping mismatch')
            local = safe_relative(sample['local_relative_path'])
            evidence = {'case': case, 'sample': sample, 'prompt_version': run.get('prompt_version'),
                        'compression_profile': run.get('compression_profile'), 'fps': run.get('fps'),
                        'thinking': run.get('thinking')}
            encoded = dump(evidence)
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            # Raw output is authoritative, never trust a separately supplied parsed field.
            try:
                parsed = json.loads(case.get('raw_output', ''))
                if not isinstance(parsed, dict):
                    parsed = {}
            except (TypeError, ValueError):
                parsed = {}
            def field(name):
                value = parsed.get(name, '')
                return value if isinstance(value, str) else ' '.join(x for x in value if isinstance(x, str)) if isinstance(value, list) else ''
            assessment = case.get('assessment', {})
            review = 'needs_review' if not parsed or not assessment.get('machine_usable') else 'not_reviewed'
            db.execute('INSERT OR IGNORE INTO search_assets VALUES(?,?)', (sha, dump(sample.get('metadata', {}))))
            for kind, location in [('nas', str(Path(root) / relative)), ('local', str(Path(testset).absolute() / local))]:
                db.execute('INSERT OR IGNORE INTO search_locations VALUES(?,?,?)', (sha, kind, location))
            cursor = db.execute('''INSERT OR IGNORE INTO search_analyses
                (sha256,evidence_hash,model,summary,subjects,actions,description,ocr,duration,review_state,evidence)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (sha, fingerprint, MODEL, field('summary'), field('subjects'), field('actions'),
                 field('visual_description'), field('visible_text'), case.get('duration_seconds'), review, encoded))
            if cursor.rowcount:
                text = ' '.join(field(k) for k in ('summary', 'subjects', 'actions', 'visual_description'))
                db.execute('INSERT INTO search_fts(rowid,terms) VALUES(?,?)', (cursor.lastrowid, tokens(text)))
                imported += 1
        for attempt in comparison.get('attempt_history', []):
            if attempt.get('requested_model') == MODEL:
                encoded = dump(attempt)
                db.execute('INSERT OR IGNORE INTO search_attempts VALUES(?,?)',
                           (hashlib.sha256(encoded.encode()).hexdigest(), encoded))
    return {'selected': len(selected), 'imported': imported, 'unchanged': len(selected)-imported,
            'changes': db.total_changes-before}


def search(db, query, limit=10, subject='', action='', review=None, min_duration=None, max_duration=None, include_ocr=False):
    """Literal keywords (space-separated AND); no semantic accuracy claims."""
    if not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    terms = query.casefold().split()
    conditions = ['a.id=(SELECT MAX(b.id) FROM search_analyses b WHERE b.sha256=a.sha256 AND b.model=a.model)']
    params = []
    expression = 'a.summary || char(32) || a.subjects || char(32) || a.actions || char(32) || a.description'
    if include_ocr:
        expression += ' || char(32) || a.ocr'
    # FTS narrows candidates; instr verifies literal matches and avoids MATCH syntax injection.
    match_tokens = tokens(query).split()
    join = ''
    rank = '0'
    if terms and all(re.fullmatch(r'[\u3400-\u9fff]{2,}', t) for t in terms) and not include_ocr:
        join = ' JOIN search_fts ON search_fts.rowid=a.id'
        conditions.append('search_fts MATCH ?')
        params.append(' OR '.join('"'+t+'"' for t in dict.fromkeys(match_tokens)))
        rank = 'bm25(search_fts)'
    for term in terms:
        conditions.append(f'instr(lower({expression}),?)>0')
        params.append(term)
    for column, value in [('subjects', subject), ('actions', action)]:
        if value:
            conditions.append(f'instr(lower(a.{column}),?)>0'); params.append(value.casefold())
    if review is not None:
        conditions.append('a.review_state=?'); params.append(review)
    for value, op in [(min_duration, '>='), (max_duration, '<=')]:
        if value is not None:
            conditions.append(f'a.duration{op}?'); params.append(value)
    rows = db.execute(f'SELECT a.*, {rank} AS rank FROM search_analyses a{join} WHERE '+
                      ' AND '.join(conditions)+' ORDER BY rank,a.sha256 LIMIT ?', params+[limit]).fetchall()
    results = []
    for row in rows:
        result = dict(row)
        result['evidence'] = json.loads(result['evidence'])
        result['locations'] = [dict(r) for r in db.execute('SELECT kind,path FROM search_locations WHERE sha256=?', (row['sha256'],))]
        # Scanner links resolve live by hash when sharing its SQLite database (moves stay traceable).
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='files'").fetchone():
            result['scanner_files'] = [dict(r) for r in db.execute('SELECT root_id,path,status FROM files WHERE sha256=?', (row['sha256'],))]
        results.append(result)
    return results
