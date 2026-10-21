from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from material_search.access import private_ipv4, password_file, session_cookie, valid_session
from material_search.store import connect
from material_search.web import PreviewServer


class AccessTests(unittest.TestCase):
    def test_addresses_password_and_cookie(self):
        self.assertTrue(private_ipv4('192.168.112.33'))
        self.assertFalse(private_ipv4('8.8.8.8'))
        self.assertFalse(private_ipv4('0.0.0.0'))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder).resolve()/'access.key'
            password=password_file(path)
            self.assertEqual(password_file(path),password)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            cookie=session_cookie('secret')
            self.assertTrue(valid_session(cookie,'secret'))
            self.assertFalse(valid_session(cookie,'other'))
            self.assertFalse(valid_session(cookie.replace('cm_session=','cm_session=x',1),'secret'))
            with patch('material_search.access.time.time',return_value=9999999999):
                self.assertFalse(valid_session(cookie,'secret'))

    def test_lan_login_and_protected_endpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder).resolve();media=base/'videos';media.mkdir()
            database=base/'search.sqlite'
            with closing(connect(database)): pass
            with patch('material_search.web.hardware_available',return_value=False):
                server=PreviewServer(database,media,0,load_semantic=False,lan_ip='192.168.10.5',password='correct-password-123')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(method,path,body=None,headers=None):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                conn.request(method,path,json.dumps(body) if body is not None else None,headers or {})
                response=conn.getresponse();status=response.status;cookie=response.getheader('Set-Cookie');data=response.read();conn.close()
                return status,cookie,data
            try:
                self.assertEqual(request('GET','/')[0],200)
                for path in ('/api/session','/api/search','/api/preview?id=1&token='+server.token):
                    self.assertEqual(request('GET',path)[0],401)
                self.assertEqual(request('POST','/api/login',{'password':'wrong'})[0],401)
                status,cookie,_=request('POST','/api/login',{'password':'correct-password-123'})
                self.assertEqual(status,200)
                self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
                headers={'Cookie':cookie.split(';')[0],'Host':f'192.168.10.5:{server.server_port}'}
                self.assertEqual(request('GET','/api/search',headers=headers)[0],200)
                self.assertEqual(request('GET','/api/session',headers={**headers,'Origin':'http://evil.test'})[0],403)
                self.assertEqual(request('GET','/api/session',headers={**headers,'Host':'evil.test'})[0],403)
                self.assertEqual(request('GET','/api/session',headers={**headers,'Cookie':'cm_session=tampered'})[0],401)
                for _ in range(5): request('POST','/api/login',{'password':'wrong'})
                self.assertEqual(request('POST','/api/login',{'password':'correct-password-123'})[0],429)
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_no_public_bind_or_unprotected_lan(self):
        for kwargs in ({'lan_ip':'8.8.8.8','password':'correct-password-123'},{'lan_ip':'192.168.10.5'}):
            with self.assertRaises(ValueError):PreviewServer('unused','unused',load_semantic=False,**kwargs)
