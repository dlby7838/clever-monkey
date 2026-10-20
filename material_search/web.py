"""Loopback-only preview UI. Only explicitly allowed LOCAL videos may be read."""
import json
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from nas_scanner.safety import local_output
from video_understanding.preprocess import compressed_video
from .store import connect, save_feedback, search

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

    def __init__(self, database, media_root, port=4174):
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
            self.reply(200, {'token': self.server.token}); return
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
                rows = search(db, val('q'), limit=100, **kwargs)
                feedback = db.execute('SELECT count(*) FROM search_feedback').fetchone()[0]
                count = db.execute('SELECT count(*) FROM search_assets').fetchone()[0]
                self.reply(200, {'results': rows, 'assets': count, 'feedback_count': feedback})
            elif parsed.path == '/api/feedback':
                rows = db.execute('SELECT * FROM search_feedback ORDER BY id DESC').fetchall()
                self.reply(200, [dict(r) for r in rows])
            elif parsed.path == '/api/preview':
                if not self.allowed(token=True):
                    self.reply(403, {'error': '预览请求缺少令牌'}); return
                source = preview_source(db, int(val('id')), self.server.media_root)
                if not self.server.preview_lock.acquire(blocking=False):
                    self.reply(409, {'error': '正在准备其他预览，请稍后重试'}); return
                try:
                    # Delete proxy before transmission; the browser owns only a session Blob.
                    with compressed_video(source) as proxy:
                        content = proxy.read_bytes()
                    self.reply(200, content, 'video/mp4')
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
        if not self.allowed(token=True):
            self.reply(403, {'error': '仅允许本机页面提交反馈'}); return
        if self.path != '/api/feedback':
            self.reply(404, {'error': '接口不存在'}); return
        db = None
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('请求大小无效')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or not isinstance(body.get('filters', {}), dict):
                raise ValueError('反馈格式无效')
            db = connect(self.server.database)
            key = save_feedback(db, body.get('analysis_id'), body.get('query'), body.get('verdict'),
                                body.get('note', ''), body.get('filters'))
            self.reply(201, {'id': key})
        except (ValueError, OSError, sqlite3.Error) as error:
            self.reply(400, {'error': str(error)})
        finally:
            if db is not None:
                db.close()


def serve(database, media_root, port=4174):
    server = PreviewServer(database, media_root, port)
    print(f'素材检索页面：http://127.0.0.1:{server.server_port}（Ctrl+C停止）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
