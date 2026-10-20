from contextlib import closing
import hashlib
import http.client
import json
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

from material_search.store import connect
from material_search.web import PreviewServer, preview_source
from nas_scanner.safety import StorageSafetyError


class MaterialWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.media = self.root/'videos'; self.media.mkdir()
        self.database = self.root/'index.sqlite'
        self.source = self.media/'test.mp4'; self.source.write_bytes(b'original')
        with closing(connect(self.database)) as db, db:
            db.execute('INSERT INTO search_assets VALUES(?,?)', ('a'*64, '{}'))
            db.execute('''INSERT INTO search_analyses(id,sha256,evidence_hash,model,summary,subjects,
              actions,description,ocr,duration,review_state,evidence) VALUES(1,?,?,?,'燕麦倒入碗',
              '碗','倒入','','',6,'not_reviewed',?)''',
              ('a'*64, 'fingerprint', 'lite', json.dumps({'case': {'raw_output': '{"summary":"燕麦倒入碗"}'}, 'sample': {'source_relative_path':'test.mp4'}})))
            db.execute('INSERT INTO search_locations VALUES(?,?,?)', ('a'*64,'local',str(self.source)))
            db.execute('INSERT INTO search_fts(rowid,terms) VALUES(1,?)', ('燕麦 麦倒 倒入 入碗',))
        with patch('material_search.web.hardware_available',return_value=False):
            self.server = PreviewServer(self.database, self.media, 0, load_semantic=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        def stop():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(stop)

    def request(self, method, url, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        conn.request(method, url, json.dumps(data) if data is not None else None, headers or {})
        response = conn.getresponse(); status = response.status; body = response.read(); conn.close()
        return status, body

    def test_search_feedback_removed_and_cross_site_protection(self):
        status, body = self.request('GET','/api/search?q=%E7%87%95%E9%BA%A6&mode=keyword')
        self.assertEqual(status,200); self.assertEqual(len(json.loads(body)['results']),1)
        self.assertNotIn('feedback_count',json.loads(body))
        self.assertEqual(self.request('POST','/api/feedback',{'analysis_id':1})[0],404)
        self.assertEqual(self.request('GET','/api/feedback')[0],404)
        self.assertEqual(self.request('GET','/api/search',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request('POST','/api/feedback',{}, {'Origin':'http://evil.test'})[0],403)
        self.assertEqual(self.request('GET','/api/search?q=test&mode=semantic')[0],400)
        with closing(connect(self.database)) as db, db:
            self.assertEqual(db.execute('SELECT evidence_hash FROM search_analyses').fetchone()[0],'fingerprint')
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='search_feedback'").fetchone())
        page = self.request('GET','/')[1].decode()
        script = self.request('GET','/app.js')[1].decode()
        self.assertNotIn('人工反馈',page)
        self.assertNotIn('/api/feedback',script)

    def test_allowlist_no_nas_fallback_and_symlink(self):
        with closing(connect(self.database)) as db, db:
            self.assertEqual(preview_source(db,1,self.media),self.source)
            db.execute('DELETE FROM search_locations')
            db.execute('INSERT INTO search_locations VALUES(?,?,?)',('a'*64,'nas','/unreachable-nas/test.mp4'))
            with patch.object(Path,'is_file',side_effect=AssertionError('Must not stat NAS')):
                with self.assertRaises(FileNotFoundError): preview_source(db,1,self.media)
            alias=self.media/'alias.mp4';alias.symlink_to(self.source)
            db.execute('INSERT INTO search_locations VALUES(?,?,?)',('a'*64,'local',str(alias)))
            with self.assertRaises(StorageSafetyError): preview_source(db,1,self.media)
        self.assertEqual(self.request('GET','/api/preview?id=1')[0],403)
        self.assertEqual(self.request('GET','/../../.env')[0],404)

    def test_bad_filters_and_preview_busy(self):
        self.assertEqual(self.request('GET','/api/search?min=nan')[0],400)
        self.assertEqual(self.request('GET','/api/search?min=10&max=1')[0],400)
        self.server.preview_lock.acquire()
        try:
            self.assertEqual(self.request('GET','/api/preview?id=1&token='+self.server.token)[0],409)
        finally:
            self.server.preview_lock.release()
        with closing(connect(self.database)) as db, db:
            db.execute('UPDATE search_locations SET path=?', (str(self.root/'outside.mp4'),))
            with self.assertRaises(FileNotFoundError): preview_source(db,1,self.media)

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg unavailable')
    def test_preview_plays_and_proxy_deleted_original_unchanged(self):
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=green:s=64x64:d=0.3',
                        '-c:v','libx264','-pix_fmt','yuv420p','-y',str(self.source)],check=True)
        before=hashlib.sha256(self.source.read_bytes()).hexdigest()
        from material_search.preview import stream_preview, command
        self.assertIn('pipe:1', command(self.source))
        self.assertIn('3.0', command(self.source,3.0))
        with self.assertRaises(ValueError): command(self.source,float('nan'))
        with stream_preview(self.source) as (first, stream):
            self.assertIn(b'ftyp',first[:40])
        self.assertTrue(stream.closed)
        status, body=self.request('GET','/api/preview?id=1&token='+self.server.token)
        self.assertEqual(status,200);self.assertIn(b'ftyp',body[:40])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),before)
        with patch('material_search.web.stream_preview',side_effect=RuntimeError('decode failed')):
            self.assertEqual(self.request('GET','/api/preview?id=1&token='+self.server.token)[0],500)
        self.assertFalse(self.server.preview_lock.locked())
        self.assertEqual(self.request('GET','/api/preview?id=1&token='+self.server.token+'&start=-1')[0],400)


    def test_hardware_failure_falls_back_and_cancel_reaps_process(self):
        from material_search.preview import stream_preview
        failed=MagicMock();failed.stdout=io.BytesIO(b'');failed.poll.return_value=0
        running=MagicMock();running.stdout=io.BytesIO(b'\x00\x00\x00\x18ftypmp42');running.poll.return_value=None
        with patch('material_search.preview.subprocess.Popen',side_effect=[failed,running]) as spawn:
            with patch('material_search.preview.threading.Timer'):
                with stream_preview(self.source,hardware=True) as (first,stream):
                    self.assertIn(b'ftyp',first)
        self.assertEqual(spawn.call_count,2)
        self.assertIn('h264_videotoolbox',spawn.call_args_list[0].args[0])
        self.assertIn('libx264',spawn.call_args_list[1].args[0])
        running.terminate.assert_called_once();running.wait.assert_called_once()
        self.assertTrue(stream.closed)
