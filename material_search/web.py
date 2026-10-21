"""Local/private-LAN preview UI. Only explicitly allowed LOCAL videos may be read."""
import json
import math
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from nas_scanner.safety import local_output
from .access import private_ipv4, password_file, session_cookie, valid_session
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

    def __init__(self, database, media_root, port=4174, encoder=None, model_cache='data/models', load_semantic=True, lan_ip=None, password=None):
        if lan_ip is not None and not private_ipv4(lan_ip):
            raise ValueError('局域网地址须为私有IPv4地址')
        if lan_ip and (not isinstance(password, str) or not 12 <= len(password) <= 256):
            raise ValueError('局域网模式必须设置12到256字符访问口令')
        self.lan_ip = lan_ip
        self.password = password
        self.login_failures = {}
        self.login_lock = threading.Lock()
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
        super().__init__(('0.0.0.0' if lan_ip else '127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value, content_type='application/json; charset=utf-8', cookie=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        if not private_ipv4(self.client_address[0], loopback=True):
            return False
        hosts = {f'127.0.0.1:{self.server.server_port}'}
        if self.server.lan_ip:
            hosts.add(f'{self.server.lan_ip}:{self.server.server_port}')
        host = self.headers.get('Host')
        if host not in hosts or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        return self.headers.get('Origin') in (None, f'http://{host}')

    def authenticated(self):
        return not self.server.lan_ip or valid_session(self.headers.get('Cookie'), self.server.token)

    def do_GET(self):
        if not self.allowed():
            self.reply(403, {'error': '访问地址或来源不被允许'}); return
        parsed = urlsplit(self.path)
        if parsed.path in ('/', '/app.js', '/style.css'):
            name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[parsed.path]
            content_type = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
            self.reply(200, (STATIC/name).read_bytes(), content_type); return
        if not self.authenticated():
            self.reply(401, {'error': '请输入访问口令'}); return
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
        if not self.allowed():
            self.reply(403, {'error': '访问来源不被允许'}); return
        if self.path != '/api/login' or not self.server.lan_ip:
            self.reply(404, {'error': '接口不存在'}); return
        peer = self.client_address[0]
        with self.server.login_lock:
            now = time.monotonic()
            self.server.login_failures = {ip: [t for t in times if now-t < 60]
                                          for ip, times in self.server.login_failures.items()
                                          if times and now-times[-1] < 60}
            failures = self.server.login_failures.get(peer, [])
            if len(failures) >= 5 or (peer not in self.server.login_failures and len(self.server.login_failures) >= 128):
                self.reply(429, {'error': '尝试次数过多，请一分钟后重试'}); return
            # Reserve the attempt before releasing the lock; parallel failures cannot bypass limits.
            self.server.login_failures[peer] = failures + [now]
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048:
                raise ValueError('请求大小无效')
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(length))
            password = data.get('password') if isinstance(data, dict) else None
            if not isinstance(password, str) or not secrets.compare_digest(password.encode(), self.server.password.encode()):
                self.reply(401, {'error': '访问口令不正确'}); return
            with self.server.login_lock:
                self.server.login_failures.pop(peer, None)
            self.reply(200, {'ok': True}, cookie=session_cookie(self.server.token))
        except (ValueError, OSError) as error:
            self.reply(400, {'error': str(error)})


def serve(database, media_root, port=4174, model_cache='data/models', lan_ip=None, access_file='data/lan-access.key'):
    password = password_file(access_file, protected=[media_root]) if lan_ip else None
    server = PreviewServer(database, media_root, port, model_cache=model_cache, lan_ip=lan_ip, password=password)
    print(f'素材检索页面：http://127.0.0.1:{server.server_port}（Ctrl+C停止）', flush=True)
    if lan_ip:
        print(f'局域网地址：http://{lan_ip}:{server.server_port}；访问口令文件：{access_file}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
