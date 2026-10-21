"""Original download integrity, streaming, access and source safety."""
from contextlib import closing
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from material_search.download import build_plan, open_plan, stream_archive
from material_search.store import connect
from material_search.web import PreviewServer, preview_source


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.root=self.base/'videos';self.root.mkdir()
        self.database=self.base/'db.sqlite'
        self.contents={1:b'original first video',2:b'original second video'}
        with closing(connect(self.database)) as db, db:
            for i,folder in ((1,'中文目录甲'),(2,'中文目录乙')):
                parent=self.root/folder;parent.mkdir();path=parent/'same.MOV';path.write_bytes(self.contents[i])
                sha=hashlib.sha256(self.contents[i]).hexdigest()
                db.execute('INSERT INTO search_assets VALUES(?,?)',(sha,'{}'))
                db.execute('INSERT INTO search_analyses(id,sha256,evidence_hash,evidence) VALUES(?,?,?,?)',(i,sha,str(i),'{}'))
                db.execute('INSERT INTO search_locations VALUES(?,?,?)',(sha,'local',str(path)))
        with patch('material_search.web.hardware_available',return_value=False):
            self.server=PreviewServer(self.database,self.root,0,load_semantic=False)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        def stop():self.server.shutdown();self.server.server_close();self.thread.join()
        self.addCleanup(stop)

    def request(self,method,url,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        conn.request(method,url,json.dumps(data) if data is not None else None,headers or {})
        response=conn.getresponse();status=response.status;meta=dict(response.getheaders());body=response.read();conn.close()
        return status,meta,body

    def prepare(self,ids):
        return self.request('POST','/api/download/prepare',{'ids':ids},{'X-Preview-Token':self.server.token})

    def test_zip_originals_flat_names_dedupe_and_single_use(self):
        status,_,body=self.prepare([1,2,1]);self.assertEqual(status,200)
        task=json.loads(body);self.assertEqual(task['files'],2)
        self.assertEqual(task['bytes'],sum(map(len,self.contents.values())))
        status,headers,body=self.request('GET',task['url'])
        self.assertEqual(status,200);self.assertEqual(headers['Content-Type'],'application/zip')
        self.assertIn('attachment;',headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(),['same.MOV','same-2.MOV'])
            self.assertTrue(all('/' not in n and '\\' not in n for n in archive.namelist()))
            self.assertEqual(archive.read('same.MOV'),self.contents[1])
            self.assertEqual(archive.read('same-2.MOV'),self.contents[2])
        self.assertEqual(self.request('GET',task['url'])[0],403)
        self.assertEqual((self.root/'中文目录甲/same.MOV').read_bytes(),self.contents[1])
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),['db.sqlite','videos'])

    def test_validation_csrf_and_no_nas_fallback(self):
        self.assertEqual(self.request('POST','/api/download/prepare',{'ids':[1]})[0],403)
        for ids in ([],[True],['1'],[1]*101,[999],'1,2'):
            self.assertEqual(self.prepare(ids)[0],400)
        self.assertEqual(self.request('GET','/api/download?ticket=invalid')[0],403)
        with closing(connect(self.database)) as db, db:
            db.execute("UPDATE search_locations SET kind='nas',path='/unreachable-nas/video.mov'")
        real_is_file=Path.is_file
        def guarded(path):
            if str(path).startswith('/unreachable-nas'):
                raise AssertionError('Must not touch NAS')
            return real_is_file(path)
        with patch.object(Path,'is_file',autospec=True,side_effect=guarded):
            self.assertEqual(self.prepare([1])[0],400)

    def test_missing_changed_and_symlink_sources(self):
        task=json.loads(self.prepare([1])[2])
        source=self.root/'中文目录甲/same.MOV';source.write_bytes(b'changed')
        self.assertEqual(self.request('GET',task['url'])[0],400)
        source.unlink();source.symlink_to(self.root/'中文目录乙/same.MOV')
        self.assertEqual(self.prepare([1])[0],400)
        source.unlink()
        self.assertEqual(self.prepare([1])[0],400)

    def test_fingerprint_mismatch_never_finalizes_archive(self):
        task=json.loads(self.prepare([1])[2]);source=self.root/'中文目录甲/same.MOV';metadata=source.stat()
        source.write_bytes(b'x'*metadata.st_size);os.utime(source,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
        with self.assertRaises(http.client.IncompleteRead) as error:
            self.request('GET',task['url'])
        with self.assertRaises(zipfile.BadZipFile):zipfile.ZipFile(io.BytesIO(error.exception.partial))
        self.assertFalse(self.server.download_lock.locked())

    def test_ticket_expiry_session_binding_and_busy(self):
        task=json.loads(self.prepare([1])[2]);key=task['url'].split('=')[1]
        self.assertEqual(self.request('GET',task['url'],headers={'Cookie':'other-session'})[0],403)
        self.server.download_tickets[key]['expires']=0
        self.assertEqual(self.request('GET',task['url'])[0],403)
        self.server.download_lock.acquire()
        try:self.assertEqual(self.prepare([1])[0],409)
        finally:self.server.download_lock.release()

    def test_bounded_streaming_and_disconnect_cleanup(self):
        source=self.root/'中文目录甲/same.MOV';content=b'x'*(2*1024*1024+13);source.write_bytes(content)
        with closing(connect(self.database)) as db, db:
            old=db.execute('SELECT sha256 FROM search_analyses WHERE id=1').fetchone()[0]
            sha=hashlib.sha256(content).hexdigest()
            db.execute('INSERT INTO search_assets VALUES(?,?)',(sha,'{}'))
            db.execute('UPDATE search_analyses SET sha256=? WHERE id=1',(sha,))
            db.execute('UPDATE search_locations SET sha256=? WHERE sha256=?',(sha,old))
            plan=build_plan(db,[1],self.root,preview_source)
            class Output:
                maximum=0;total=0
                def write(self,data):self.maximum=max(self.maximum,len(data));self.total+=len(data)
                def flush(self):pass
            output=Output()
            with open_plan(db,plan,self.root,preview_source) as opened:
                stream_archive(opened,output)
            self.assertLessEqual(output.maximum,1024*1024);self.assertGreater(output.total,len(content))
            broken=Output();broken.write=lambda _: (_ for _ in ()).throw(BrokenPipeError())
            with open_plan(db,plan,self.root,preview_source) as opened:
                stream=opened[0][1]
                with self.assertRaises(BrokenPipeError):stream_archive(opened,broken)
            self.assertTrue(stream.closed)


    def test_flat_unicode_names_and_case_insensitive_collisions(self):
        with closing(connect(self.database)) as db, db:
            for i,folder,name in ((1,'中文目录甲','燕麦.MOV'),(2,'中文目录乙','燕麦.mov')):
                source=self.root/folder/'same.MOV';destination=source.with_name(name);source.rename(destination)
                db.execute('UPDATE search_locations SET path=? WHERE path=?',(str(destination),str(source)))
        task=json.loads(self.prepare([1,2])[2]);status,_,body=self.request('GET',task['url'])
        self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(archive.namelist(),['燕麦.MOV','燕麦-2.mov'])
            self.assertEqual(archive.read('燕麦.MOV'),self.contents[1])
            self.assertEqual(archive.read('燕麦-2.mov'),self.contents[2])
