from __future__ import annotations

import fcntl
import inspect
from functools import wraps
import math
import tempfile
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from .safety import local_output, readonly_source, StorageSafetyError, validate_scan_scope

EXTENSIONS = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.mts', '.m2ts', '.webm'}


class ScanError(RuntimeError):
    pass


def connect(path: Path) -> sqlite3.Connection:
    path = local_output(path, database=True)
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


def single_instance(function):
    @wraps(function)
    def locked(root, database, *args, **kwargs):
        validate_scan_scope(root)
        database = local_output(database, (root,), database=True)
        options = inspect.signature(function).bind(root, database, *args, **kwargs)
        options.apply_defaults()
        cache = options.arguments['cache_dir'] or Path(tempfile.gettempdir())
        local_output(cache, (root,))
        readonly_source(root, options.arguments['allow_local'])
        lock_path = local_output(Path(str(database) + '.lock'), (root,))
        if lock_path.is_relative_to(root.resolve()):
            raise ScanError('Database must be outside the material root')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ScanError('Another scan is using this database') from exc
            return function(root, database, *args, **kwargs)
    return locked


def read_video(path, destination, rate_mib):
    """Pace each read, with at most one 256 KiB burst; stage for local probing."""
    hasher = hashlib.sha256()
    total = 0
    with path.open('rb') as source:
        while True:
            started = time.monotonic()
            block = source.read(256 * 1024)
            if not block:
                break
            hasher.update(block)
            destination.write(block)
            total += len(block)
            if rate_mib:
                delay = len(block) / (rate_mib * 1024 * 1024) - (time.monotonic() - started)
                if delay > 0:
                    time.sleep(delay)
    destination.flush()
    return hasher.hexdigest(), total


@single_instance

def scan(root: Path, database: Path, stable_seconds: float = 30,
         ffprobe: str = 'ffprobe', timeout: float = 30,
         allow_local: bool = False, marker: str | None = None,
         read_mib_per_second: float = 5, file_delay: float = 0.1,
         directory_delay: float = 0.02, max_files: int = 20,
         rehash: bool = False, cache_dir: Path | None = None) -> dict:
    root = root.resolve()
    database = database.resolve()
    if database.is_relative_to(root):
        raise ScanError('Database must be outside the material root')
    if (any(not math.isfinite(v) or v < 0 for v in
            (stable_seconds, read_mib_per_second, file_delay, directory_delay))
            or not math.isfinite(timeout) or timeout <= 0 or max_files < 0):
        raise ScanError('Invalid timing settings')
    if marker and (Path(marker).is_absolute() or '..' in Path(marker).parts):
        raise ScanError('Marker must be a relative path inside the root')

    def health():
        validate_scan_scope(root)
        readonly_source(root, allow_local)
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

    cache_dir = local_output(cache_dir or Path(tempfile.gettempdir()), (root,))
    if cache_dir.is_relative_to(root):
        raise ScanError('Cache must be outside the material root')
    cache_dir.mkdir(parents=True, exist_ok=True)
    initial_health = health()
    initial_root = signature(root)[2:]
    db = connect(database)
    counts = {'ready': 0, 'pending': 0, 'missing': 0, 'moved': 0, 'duplicates': 0,
              'reused': 0, 'deferred': 0, 'hashed': 0, 'bytes_read': 0}
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
                if directory_delay:
                    time.sleep(directory_delay)
                dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
                for name in sorted(names):
                    path = Path(directory) / name
                    if path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
                        continue
                    if file_delay:
                        time.sleep(file_delay)
                    relative = str(path.relative_to(root))
                    before = signature(path)
                    seen.add(relative)
                    previous = old.get(relative)
                    unchanged = previous and (previous['size'], previous['mtime_ns']) == before[:2]
                    stable_since = previous['stable_since'] if unchanged else now
                    sha = previous['sha256'] if unchanged else None
                    status = 'pending'
                    cached = unchanged and sha and previous['status'] == 'ready'
                    if cached and not rehash:
                        status = 'ready'
                        counts['reused'] += 1
                    elif now - stable_since >= stable_seconds:
                        if max_files and counts['hashed'] >= max_files:
                            counts['deferred'] += 1
                            status = 'ready' if cached else 'pending'
                            if not cached:
                                sha = None
                        else:
                            # FFprobe reads a local temporary copy, never an unthrottled SMB path.
                            with tempfile.NamedTemporaryFile(suffix=path.suffix, dir=cache_dir) as local:
                                sha, size = read_video(path, local, read_mib_per_second)
                                counts['bytes_read'] += size
                                counts['hashed'] += 1
                                content = db.execute('SELECT 1 FROM contents WHERE sha256=?', (sha,)).fetchone()
                                if not content:
                                    metadata = probe(Path(local.name), ffprobe, timeout)
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
