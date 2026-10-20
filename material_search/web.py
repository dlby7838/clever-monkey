"""Loopback-only preview UI. Only explicitly allowed LOCAL videos may be read."""
import json
import math
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from nas_scanner.safety import local_output
from .preview import hardware_available, stream_preview
from .semantic import LocalEncoder, SemanticUnavailable, retrieve
from .store import connect

STATIC = Path(__file__).parent / 'static'


def preview_source(db, analysis_id, root):
    row = db.execute('''SELECT l.path FROM search_locations l JOIN search_analyses a
                        ON a.sha256=l.sha256 WHERE a.id=? AND l.kind='local' ORDER BY l.path''', (analysis_id,)).fetchall()
    for item in row:
        path = Path(item['path'])
        # Check containment before probing a candidate (NAS paths never get stat'ed).
        if not path.is_absolute() or not path.is_relative_to(root) or '..' in path.parts:
            continue
        local_output(path)  # Reject symlink ancestors, hardlinks, network mounts.
        if path.suffix.lower() not in ('.mov', '.mp4', '.m4v', '.mkv', '.avi', '.webm', '.mts', '.m2ts'):
            continue
        if path.is_file():
            return path
    raise FileNotFoundError('没有可预览的本地副本；不会回退读取NAS')


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, database, media_root, port=4174, encoder=None, model_cache='data/models', load_semantic=True):
        self.media_root = local_output(Path(media_root).absolute())
        if not self.media_root.is_dir():
            raise ValueError('Local media root does not exist')
        self.database = local_output(Path(database), protected=[self.media_root], database=True)
        if not self.database.is_file():
            raise ValueError('Import evaluation results before starting the web UI')
        db = connect(self.database)
        db.close()
        self.token = secrets.token_urlsafe(32)
        self.preview_lock = threading.Lock()
        self.hardware = hardware_available()
        self.encoder = encoder
        self.semantic_error = None
        if self.encoder is None and load_semantic:
            try:
                self.encoder = LocalEncoder(model_cache)
            except SemanticUnavailable as error:
                self.semantic_error = str(error)
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value, content_type='application/json; charset=utf-8'):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, token=False):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        if self.headers.get('Origin') not in (None, f'http://{expected}'):
            return False
        return not token or secrets.compare_digest(self.headers.get('X-Preview-Token', ''), self.server.token)

    def do_GET(self):
        if not self.allowed():
            self.reply(403, {'error': '仅允许本机同源访问'}); return
        parsed = urlsplit(self.path)
        if parsed.path in ('/', '/app.js', '/style.css'):
            name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[parsed.path]
            content_type = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
            self.reply(200, (STATIC/name).read_bytes(), content_type); return
        if parsed.path == '/api/session':
            self.reply(200, {'token': self.server.token, 'semantic_ready': self.server.encoder is not None,
                             'semantic_error': self.server.semantic_error}); return
        db = connect(self.server.database)
        try:
            params = parse_qs(parsed.query, keep_blank_values=True)
            def val(key, default=''):
                return params.get(key, [default])[0]
            if parsed.path == '/api/search':
                if len(val('q')) > 1000:
                    raise ValueError('查询过长')
                kwargs = dict(subject=val('subject'), action=val('action'), review=val('review') or None,
                              min_duration=float(val('min')) if val('min') else None,
                              max_duration=float(val('max')) if val('max') else None, include_ocr=val('ocr') == '1')
                mode = val('mode', 'hybrid')
                rows = retrieve(db, self.server.encoder, val('q'), limit=20 if val('q').strip() else 100, mode=mode, **kwargs)
                count = db.execute('SELECT count(*) FROM search_assets').fetchone()[0]
                self.reply(200, {'results': rows, 'assets': count, 'mode': mode})
            elif parsed.path == '/api/preview':
                if not secrets.compare_digest(val('token'), self.server.token):
                    self.reply(403, {'error': '预览请求缺少令牌'}); return
                source = preview_source(db, int(val('id')), self.server.media_root)
                start = float(val('start', '0'))
                duration = db.execute('SELECT duration FROM search_analyses WHERE id=?', (int(val('id')),)).fetchone()[0]
                if not math.isfinite(start) or start < 0 or (duration and start >= duration):
                    raise ValueError('预览起点超出素材时长')
                if not self.server.preview_lock.acquire(timeout=2):
                    self.reply(409, {'error': '正在切换预览，请稍后重试'}); return
                try:
                    with stream_preview(source, start, hardware=self.server.hardware) as (first, stream):
                        self.send_response(200)
                        self.send_header('Content-Type', 'video/mp4')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.close_connection = True
                        self.wfile.write(first); self.wfile.flush()
                        while block := stream.read1(32768):
                            self.wfile.write(block); self.wfile.flush()
                finally:
                    self.server.preview_lock.release()
            else:
                self.reply(404, {'error': '页面不存在'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError, sqlite3.Error) as error:
            self.reply(400, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': '预览生成失败，请检查本地素材和FFmpeg；未修改原片'})
        finally:
            db.close()

    def do_POST(self):
        self.reply(404 if self.allowed() else 403, {'error': '接口不存在'})


def serve(database, media_root, port=4174, model_cache='data/models'):
    server = PreviewServer(database, media_root, port, model_cache=model_cache)
    print(f'素材检索页面：http://127.0.0.1:{server.server_port}（Ctrl+C停止）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
