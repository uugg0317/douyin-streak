"""Isolated local-console regressions: no personal data, scheduler or real browser."""
from __future__ import annotations
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
SANDBOX = tempfile.TemporaryDirectory(prefix="sparkkeeper-open-console-")
TEMP = Path(SANDBOX.name)
for key in list(os.environ):
    if key.startswith(("AUTH_", "URL_", "SMTP_", "SPARKKEEPER_")) or key in {"LOGIN_GATE", "HOST", "WHITELIST_IPS", "STATE_FILE_PATH"}:
        os.environ.pop(key, None)
os.environ.update(DATA_DIR=str(TEMP/'data'), ENV_FILE_PATH=str(TEMP/'absent.env'),
                  INSTANCE_LOCK_PATH=str(TEMP/'server.pid'), HOST="127.0.0.1",
                  SPARKKEEPER_MULTI_ACCOUNT="1", KEEP_BROWSER_ALWAYS="false")
import app as entry
from local_access import _check_local_bind
import uvicorn
from starlette.requests import Request
from starlette.responses import Response


class LocalConsoleRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        listener.bind(("127.0.0.1",0))
        cls.base=f"http://127.0.0.1:{listener.getsockname()[1]}"
        cls.server=uvicorn.Server(uvicorn.Config(entry.app,lifespan="off",log_config=None,access_log=False,log_level="error"))
        cls.thread=threading.Thread(target=cls.server.run,kwargs={"sockets":[listener]},daemon=True)
        cls.thread.start()
        deadline=time.monotonic()+10
        while not cls.server.started and cls.thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.02)
        if not cls.server.started: raise RuntimeError("isolated local server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit=True
        cls.thread.join(timeout=5)
        logging.shutdown()
        SANDBOX.cleanup()

    def request(self,path,body=None,headers=None):
        data=None if body is None else json.dumps(body).encode()
        req=urllib.request.Request(self.base+path,data=data,headers={"Content-Type":"application/json",**(headers or {})})
        try: result=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5)
        except urllib.error.HTTPError as error: result=error
        with result:
            raw=result.read().decode()
            payload=json.loads(raw) if 'application/json' in result.headers.get('Content-Type','') else raw
            return result.status,result.headers,payload

    def test_page_opens_without_login_form(self):
        code,headers,text=self.request('/')
        self.assertEqual(code,200)
        self.assertIn('多账号控制台',text)
        self.assertNotIn('admin-token',text)
        self.assertNotIn('doLogin',text)
        self.assertNotIn('Set-Cookie',headers)

    def test_status_and_accounts_need_no_cookie_or_token(self):
        self.assertEqual(self.request('/api/status')[0],200)
        self.assertFalse(self.request('/api/status')[2]['auth_required'])
        self.assertEqual(self.request('/api/multi/state')[0],200)
        self.assertEqual(self.request('/api/multi/accounts')[0],200)

    def test_local_account_write_needs_no_management_login(self):
        code,_,body=self.request('/api/multi/accounts',{'id':'open-test','display_name':'模拟账号'})
        self.assertEqual(code,201)
        self.assertEqual(body['account']['id'],'open-test')
        self.assertEqual(self.request('/api/multi/accounts/open-test/config')[0],200)

    def test_removed_login_route_cannot_mint_sessions(self):
        self.assertEqual(self.request('/api/auth/login',{})[0],404)
        self.assertEqual(self.request('/api/auth/url-token-login',{})[0],404)

    def test_local_bind_config_needs_no_secret(self):
        _check_local_bind()
        with patch.dict(os.environ,{'HOST':'0.0.0.0'}):
            with self.assertRaises(SystemExit): _check_local_bind()

    def test_cross_origin_write_is_rejected_before_business_logic(self):
        code,_,_=self.request('/api/multi/accounts',{'id':'bad-test'}, {'Origin':'https://example.invalid'})
        self.assertEqual(code,403)

    def test_nonlocal_peer_is_rejected_before_business_logic(self):
        scope={'type':'http','method':'GET','scheme':'http','path':'/api/status','query_string':b'',
               'headers':[(b'host',b'127.0.0.1:8000')], 'server':('127.0.0.1',8000),'client':('203.0.113.4',1)}
        async def next_handler(request): return Response('unexpected',status_code=200)
        response=asyncio.run(entry._local_access_and_origin_guard(Request(scope),next_handler))
        self.assertEqual(response.status_code,403)

    def test_nonlocal_host_is_rejected_before_business_logic(self):
        self.assertEqual(self.request('/api/status',headers={'Host':'example.invalid'})[0],403)

    def test_import_does_not_start_workers_or_scheduler(self):
        self.assertFalse((TEMP/'server.pid').exists())
        self.assertEqual(entry.ENV_PATH,TEMP/'absent.env')

if __name__=='__main__': unittest.main()
