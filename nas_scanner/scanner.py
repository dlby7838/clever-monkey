from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

EXTENSIONS = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.mts', '.m2ts', '.webm'}


class ScanError(RuntimeError):
    pass


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS roots(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS contents(sha256 TEXT PRIMARY KEY, metadata TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS files(
      id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES roots(id),
      path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
      stable_since REAL NOT NULL, sha256 TEXT REFERENCES contents(sha256),
      status TEXT NOT NULL, last_seen REAL NOT NULL, UNIQUE(root_id,path));
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY, at REAL NOT NULL, root_id INTEGER NOT NULL,
      kind TEXT NOT NULL, path TEXT NOT NULL, previous_path TEXT);
    ''')
    return db


def signature(path: Path) -> tuple[int, int, int, int]:
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino


def probe(path: Path, executable: str, timeout: float) -> dict:
    try:
        result = subprocess.run(
            [executable, '-v', 'error', '-show_format', '-show_streams',
             '-of', 'json', str(path)], capture_output=True, text=True,
            timeout=timeout, check=True)
        data = json.loads(result.stdout)
        videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video']
        if not videos:
            raise ValueError('No video stream')
        stream = videos[0]
        return {'duration': data.get('format', {}).get('duration'),
                'width': stream.get('width'), 'height': stream.get('height'),
                'codec': stream.get('codec_name'), 'raw': data}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ScanError(f'FFprobe failed for {path}: {exc}') from exc


def scan(root: Path, database: Path, stable_seconds: float = 30,
         ffprobe: str = 'ffprobe', timeout: float = 30,
         allow_local: bool = False, marker: str | None = None) -> dict:
    root = root.resolve()
    database = database.resolve()
    if database.is_relative_to(root):
        raise ScanError('Database must be outside the material root')
    if stable_seconds < 0 or timeout <= 0:
        raise ScanError('Invalid timing settings')
    if marker and (Path(marker).is_absolute() or '..' in Path(marker).parts):
        raise ScanError('Marker must be a relative path inside the root')

    def health():
        if not root.is_dir():
            raise ScanError(f'Root unavailable: {root}')
        if not allow_local and not any(os.path.ismount(p) for p in (root, *root.parents) if p != Path('/')):
            raise ScanError('No mounted volume found; refusing to scan (local tests require --allow-local)')
        # Force a directory read; existence alone may survive a disconnected SMB session.
        with os.scandir(root) as entries:
            next(entries, None)
        if marker:
            with (root / marker).open('rb') as source:
                digest = hashlib.sha256(source.read()).hexdigest()
            return digest
        return None

    initial_health = health()
    initial_root = signature(root)[2:]
    db = connect(database)
    counts = {'ready': 0, 'pending': 0, 'missing': 0, 'moved': 0, 'duplicates': 0}
    now = time.time()
    try:
        with db:
            db.execute('INSERT OR IGNORE INTO roots(path) VALUES (?)', (str(root),))
            root_id = db.execute('SELECT id FROM roots WHERE path=?', (str(root),)).fetchone()[0]
            old = {r['path']: dict(r) for r in db.execute('SELECT * FROM files WHERE root_id=?', (root_id,))}
            seen = set()
            processed = []
            def walk_error(exc):
                raise exc
            for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
                dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
                for name in sorted(names):
                    path = Path(directory) / name
                    if path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
                        continue
                    relative = str(path.relative_to(root))
                    before = signature(path)
                    seen.add(relative)
                    previous = old.get(relative)
                    unchanged = previous and (previous['size'], previous['mtime_ns']) == before[:2]
                    stable_since = previous['stable_since'] if unchanged else now
                    sha = previous['sha256'] if unchanged else None
                    status = 'pending'
                    if now - stable_since >= stable_seconds:
                        # Re-hash even known files: detect same-size replacements with preserved mtime.
                        hasher = hashlib.sha256()
                        with path.open('rb') as source:
                            for block in iter(lambda: source.read(1024 * 1024), b''):
                                hasher.update(block)
                        sha = hasher.hexdigest()
                        content = db.execute('SELECT 1 FROM contents WHERE sha256=?', (sha,)).fetchone()
                        if not content:
                            metadata = probe(path, ffprobe, timeout)
                            db.execute('INSERT INTO contents VALUES (?,?)', (sha, json.dumps(metadata)))
                        if signature(path) != before:
                            raise ScanError(f'File changed during processing: {path}')
                        status = 'ready'
                        processed.append((path, before))
                    else:
                        sha = None
                    db.execute('''INSERT INTO files(root_id,path,size,mtime_ns,stable_since,sha256,status,last_seen)
                      VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(root_id,path) DO UPDATE SET
                      size=excluded.size,mtime_ns=excluded.mtime_ns,stable_since=excluded.stable_since,
                      sha256=excluded.sha256,status=excluded.status,last_seen=excluded.last_seen''',
                      (root_id, relative, *before[:2], stable_since, sha, status, now))
                    counts[status] += 1
            absent = {p: r for p, r in old.items() if p not in seen}
            # Only a unique disappearing/appearing pair is called a move. Copies remain separate file rows.
            new_rows = list(db.execute('SELECT * FROM files WHERE root_id=? AND status="ready"', (root_id,)))
            for p, r in absent.items():
                matches = [n for n in new_rows if n['path'] in seen and (n['path'] not in old or old[n['path']]['status'] == 'pending') and n['sha256'] == r['sha256'] and r['sha256']]
                sources = [a for a in absent.values() if a['sha256'] == r['sha256']]
                moved = len(matches) == len(sources) == 1
                if not moved and r['status'] == 'missing':
                    continue
                kind = 'moved' if moved else 'missing'
                db.execute('UPDATE files SET status="missing" WHERE id=?', (r['id'],))
                db.execute('INSERT INTO events(at,root_id,kind,path,previous_path) VALUES (?,?,?,?,?)',
                           (now, root_id, kind, matches[0]['path'] if moved else p, p if moved else None))
                counts[kind] += 1
            counts['duplicates'] = db.execute('''SELECT COALESCE(SUM(n-1),0) FROM
              (SELECT COUNT(*) n FROM files WHERE root_id=? AND status='ready'
               GROUP BY sha256 HAVING COUNT(*)>1)''', (root_id,)).fetchone()[0]
            for path, before in processed:
                if signature(path) != before:
                    raise ScanError(f'File changed before commit: {path}')
            if health() != initial_health or signature(root)[2:] != initial_root:
                raise ScanError('Root identity changed during scan')
        return counts
    except OSError as exc:
        raise ScanError(f'NAS I/O failure; scan rolled back: {exc}') from exc
    finally:
        db.close()
