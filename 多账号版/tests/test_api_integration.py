"""Route integration checks, each in a clean process and temporary data root."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ApiIntegrationTests(unittest.TestCase):
    def run_isolated(self, code):
        with tempfile.TemporaryDirectory(prefix="sparkkeeper-api-") as folder:
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(("SMTP_", "URL_", "SPARKKEEPER_")) or key in {"WHITELIST_IPS", "TRUSTED_PROXY_IPS", "STATE_FILE_PATH"}:
                    env.pop(key, None)
            env.update(DATA_DIR=folder, ENV_FILE_PATH=str(Path(folder) / "absent.env"),
                       INSTANCE_LOCK_PATH=str(Path(folder) / "server.pid"),
                       HOST="127.0.0.1", SPARKKEEPER_MULTI_ACCOUNT="1",
                       PYTHONUTF8="1", KEEP_BROWSER_ALWAYS="false")
            prelude = """
import asyncio, json, threading
from pathlib import Path
from unittest import mock
from fastapi import HTTPException, UploadFile
import app
from core import accounts as A, jobs, ledger
A.add_account('acc1', display_name='模拟账号')
"""
            result = subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_single_account_lock_handoff_and_release(self):
        self.run_isolated("""
        done = threading.Event()
        assert app._acquire_lock(False)
        app._start_daemon_or_rollback(lambda: done.set())
        assert done.wait(3)
        for _ in range(100):
            if not app.run_lock.locked(): break
            threading.Event().wait(.01)
        assert not app.run_lock.locked()
        assert app._acquire_lock(False)
        assert app.run_lock.release()
        """)

    def test_thread_start_failure_returns_lock(self):
        self.run_isolated("""
        assert app._acquire_lock(False)
        with mock.patch('threading.Thread.start', side_effect=RuntimeError('simulated')):
            try: app._start_daemon_or_rollback(lambda: None)
            except HTTPException as exc: assert exc.status_code == 500
            else: raise AssertionError('expected failure')
        assert not app.run_lock.locked()
        """)

    def test_multi_route_reserves_before_background_starts(self):
        self.run_isolated("""
        with mock.patch('threading.Thread.start'):
            result = app.api_multi_run({'account_id':'acc1', 'dry_run':True})
            assert result['started'] and result['job_id']
            assert jobs.is_active(result['job_id'])
            try: app.api_multi_run({'account_id':'acc1', 'dry_run':True})
            except HTTPException as exc: assert exc.status_code == 409
            else: raise AssertionError('second run was accepted')
        """)

    def test_multi_start_failure_releases_reservation(self):
        self.run_isolated("""
        with mock.patch('threading.Thread.start', side_effect=RuntimeError('simulated')):
            try: app.api_multi_run({'account_id':'acc1','dry_run':True})
            except HTTPException as exc: assert exc.status_code == 500
            else: raise AssertionError('expected failure')
        assert not jobs.snapshot()
        """)

    def test_collection_blocks_send_and_state_upload(self):
        self.run_isolated("""
        import io
        lease = jobs.reserve(account_id='acc1', kind='contacts')
        try:
            try: app.api_multi_run({'account_id':'acc1','dry_run':True})
            except HTTPException as exc: assert exc.status_code == 409
            else: raise AssertionError('send overlapped collection')
            payload = json.dumps({'cookies':[{'name':'fake','domain':'example.invalid','path':'/','value':'dummy'}]}).encode()
            upload = UploadFile(filename='dummy.json', file=io.BytesIO(payload))
            try: asyncio.run(app.api_multi_account_upload_state('acc1',upload))
            except HTTPException as exc: assert exc.status_code == 409
            else: raise AssertionError('state update overlapped collection')
            assert app.api_multi_contacts_status('acc1')['fetching']
        finally: jobs.release(lease['job_id'])
        assert not app.api_multi_contacts_status('acc1')['fetching']
        """)

    def test_active_task_cannot_be_reset(self):
        self.run_isolated("""
        assert app._acquire_lock(False)
        try:
            try: app.api_reset_running()
            except HTTPException as exc: assert exc.status_code == 409
            else: raise AssertionError('active lock was reset')
            assert app.run_lock.locked()
        finally: app._release_lock()
        lease = jobs.reserve(account_id='acc1',kind='send')
        try:
            try: app.api_multi_reset()
            except HTTPException as exc: assert exc.status_code == 409
            else: raise AssertionError('active send was reset')
        finally: jobs.release(lease['job_id'])
        """)

    def test_copy_does_not_inherit_other_account_delivery(self):
        self.run_isolated("""
        A.add_account('acc2')
        ledger.save_ledger([{'display_name':'模拟好友','selected':True,'user_id':'fake-id',
            'last_status':'success','last_ok':True,'last_sent_at':'2026-10-02T00:00:00+08:00',
            'delivery_records':{'2026-10-02':{'status':'success'}}}],path=A.account_file('acc1','ledger.json'))
        app.api_multi_copy_from('acc2',app.MultiCopyBody(source_id='acc1',copy_ledger=True))
        copied=ledger.load_ledger(A.account_file('acc2','ledger.json'))[0]
        assert copied['selected'] and copied['user_id']=='fake-id'
        assert copied.get('last_status')=='pending' and not copied.get('delivery_records')
        """)

    def test_account_mutations_conflict_with_task(self):
        self.run_isolated("""
        lease=jobs.reserve(account_id='acc1',kind='contacts')
        try:
            for operation in (
                lambda: app.api_multi_account_remove('acc1',{'delete_dir':True}),
                lambda: app.api_multi_account_update('acc1',{'enabled':False}),
                lambda: app.api_multi_save_config('acc1',{'max_friends_per_run':5}),
                lambda: app.api_multi_review_account('acc1',{'confirmed':True})):
                try: operation()
                except HTTPException as exc: assert exc.status_code==409
                else: raise AssertionError('mutation accepted during collection')
        finally: jobs.release(lease['job_id'])
        assert A.get_account('acc1') is not None
        """)

    def test_review_requires_confirmation_and_preserves_unknown(self):
        self.run_isolated("""
        try: app.api_multi_review_account('acc1',{'confirmed':False})
        except HTTPException as exc: assert exc.status_code==400
        else: raise AssertionError('review accepted without confirmation')
        lp=A.account_file('acc1','ledger.json')
        ledger.save_ledger([{'display_name':'模拟好友','selected':True,'last_status':'unknown','delivery_records':{'2026-10-02':{'status':'unknown'}}}],path=lp)
        before=lp.read_bytes()
        result=app.api_multi_review_account('acc1',{'confirmed':True})
        assert result['ok'] and not result['manual_required']
        assert lp.read_bytes()==before
        """)

    def test_second_controller_cannot_start_same_data_directory(self):
        self.run_isolated("""
        import os, subprocess, sys
        code="from pathlib import Path; import os,time; from core.storage import file_lock; g=file_lock(Path(os.environ['INSTANCE_LOCK_PATH'])); g.__enter__(); print('ready',flush=True); time.sleep(10)"
        child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        try:
            assert child.stdout.readline().strip()=='ready'
            async def attempt():
                async with app.lifespan(app.app):
                    raise AssertionError('second controller started')
            with mock.patch.object(app,'_check_local_bind'):
                try: asyncio.run(attempt())
                except RuntimeError as exc: assert '已有服务' in str(exc)
                else: raise AssertionError('instance guard failed')
        finally:
            child.terminate();child.communicate(timeout=5)
        """)

    def test_controller_lifespan_starts_and_releases_instance_guard(self):
        self.run_isolated("""
        async def start_and_stop():
            async with app.lifespan(app.app):
                assert app.PID_PATH.exists()
        with mock.patch.object(app.scheduler,'configure') as configure, mock.patch.object(app.scheduler,'shutdown') as shutdown:
            asyncio.run(start_and_stop())
            configure.assert_called_once()
            shutdown.assert_called_once()
        assert not app.PID_PATH.exists()
        with app.file_lock(app.PID_PATH,timeout=.1):pass
        """)

    def test_multi_state_reports_actual_automatic_task_switches(self):
        self.run_isolated("""
        import os
        with mock.patch.dict(os.environ,{'SPARKKEEPER_AUTO_RUN':'0','SPARKKEEPER_BACKUP_ENABLED':'0','SPARKKEEPER_SCHEDULE_TIME':'00:00'}):
            state=app.api_multi_state()
            assert state['auto_run_enabled'] is False
            assert state['backup_enabled'] is False
            assert state['schedule_time']=='00:00'
        with mock.patch.dict(os.environ,{'SPARKKEEPER_AUTO_RUN':'1','SPARKKEEPER_BACKUP_ENABLED':'1'}):
            state=app.api_multi_state()
            assert state['auto_run_enabled'] is True
            assert state['backup_enabled'] is True
        """)

    def test_credential_result_filters_secrets_and_maps_errors(self):
        self.run_isolated("""
        snapshot={'job_id':'safe-job','account_id':'acc1','display_name':'模拟账号',
                  'status':'ready','running':True,'count':2,'deadline':123456,
                  'cookies':[{'value':'synthetic-secret'}],'path':'private-path',
                  'screenshot':'private-image','storage_state':{'cookies':[]}}
        result=app._credential_result(lambda:snapshot)
        assert result['ok'] and result['job_id']=='safe-job'
        assert not {'cookies','path','screenshot','storage_state'} & result.keys()
        for error,status in [(app.credential_extract.CredentialExtractBusy('busy'),409),
                             (app.credential_extract.CredentialExtractNotFound('old-job'),404),
                             (app.credential_extract.CredentialExtractInvalid('invalid'),400),
                             (RuntimeError('synthetic-secret'),500)]:
            def fail():raise error
            try:app._credential_result(fail)
            except HTTPException as exc:
                assert exc.status_code==status
                assert 'synthetic-secret' not in exc.detail
            else:raise AssertionError('expected error mapping')
        """)

    def test_credential_start_and_legacy_operations_are_mutually_exclusive(self):
        self.run_isolated("""
        assert app._acquire_lock(False)
        with mock.patch.object(app.credential_extract,'manager') as manager:
            try:app.api_multi_credentials_extract('acc1')
            except HTTPException as exc:assert exc.status_code==409
            else:raise AssertionError('credential extraction overlapped old operation')
            manager.start.assert_not_called()
        app._release_lock()
        with mock.patch.object(app.credential_extract.manager,'start',side_effect=RuntimeError('fake launch failure')):
            try:app.api_multi_credentials_extract('acc1')
            except HTTPException as exc:assert exc.status_code==500
            else:raise AssertionError('start failure accepted')
        assert not app.run_lock.locked()
        assert app._acquire_lock(False)
        with mock.patch.object(app.credential_extract.manager,'status',return_value={'running':True}):
            try:app._reject_if_extracting()
            except HTTPException as exc:assert exc.status_code==409
            else:raise AssertionError('legacy operation overlapped credential extraction')
        assert not app.run_lock.locked()
        """)

    def test_multi_mode_rejects_global_extraction_and_screenshot(self):
        self.run_isolated("""
        with mock.patch.object(app,'open_browser') as browser:
            for route in [app.api_credentials_extract,app.api_credentials_extract_status,
                          app.api_credentials_extract_screenshot]:
                try:route()
                except HTTPException as exc:assert exc.status_code==409
                else:raise AssertionError('global credentials path accepted in multi mode')
            browser.assert_not_called()
        """)

    def test_credential_http_routes_need_no_management_login_and_filter_secrets(self):
        self.run_isolated("""
        import socket,urllib.request,urllib.error,time
        listener=socket.socket();listener.bind(('127.0.0.1',0))
        base='http://127.0.0.1:'+str(listener.getsockname()[1])
        server=app.uvicorn.Server(app.uvicorn.Config(app.app,lifespan='off',log_config=None,access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
        def request(path,method='GET',body=None):
            headers={'Content-Type':'application/json','Origin':base}
            payload=json.dumps(body).encode() if body is not None else None
            req=urllib.request.Request(base+path,data=payload,headers=headers,method=method)
            try:
                with urllib.request.urlopen(req,timeout=5) as response:
                    return response.status,json.load(response),response.headers
            except urllib.error.HTTPError as exc:
                return exc.code,json.load(exc),exc.headers
        safe={'job_id':'fake-task','account_id':'acc1','display_name':'模拟账号',
              'status':'ready','running':True,'count':2,'deadline':time.time()+300,
              'cookies':[{'value':'synthetic-secret'}],'path':'private-path'}
        try:
            thread.start()
            for _ in range(100):
                if server.started:break
                time.sleep(.01)
            assert server.started
            with mock.patch.object(app.credential_extract,'manager') as manager:
                manager.start.return_value=safe;manager.status.return_value=safe
                manager.confirm.return_value={**safe,'status':'success','running':False}
                manager.cancel.return_value={**safe,'status':'cancelled','running':False}
                status,data,_=request('/api/multi/accounts/acc1/credentials/extract','POST')
                assert status==202 and data['job_id']=='fake-task'
                assert 'cookies' not in data and 'path' not in data
                status,data,_=request('/api/multi/credentials/extract-status')
                assert status==200 and data['account_id']=='acc1'
                status,data,_=request('/api/multi/accounts/acc1/credentials/extract/fake-task/confirm','POST')
                assert status==200 and data['status']=='success'
                manager.confirm.assert_called_once_with('acc1','fake-task')
                status,data,_=request('/api/multi/accounts/acc1/credentials/extract/fake-task/cancel','POST')
                assert status==200 and data['status']=='cancelled'
                manager.cancel.assert_called_once_with('acc1','fake-task')
        finally:
            server.should_exit=True;thread.join(timeout=5);listener.close()
        """)

    def test_orphan_login_browser_blocks_jobs_for_other_accounts(self):
        self.run_isolated("""
        import os,subprocess,sys
        A.add_account('acc2',display_name='另一个模拟账号')
        code="from pathlib import Path; import os,time; from core.storage import file_lock; g=file_lock(Path(os.environ['DATA_DIR'])/'.credential-extract'/'.guard'); g.__enter__(); print('ready',flush=True); time.sleep(10)"
        child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        try:
            assert child.stdout.readline().strip()=='ready'
            for kind in ['send','contacts','state','remove']:
                try:jobs.reserve(account_id='acc2',kind=kind)
                except jobs.JobBusy:pass
                else:raise AssertionError('orphan login browser did not block '+kind)
            assert not jobs.snapshot()
            assert app._acquire_lock(False)
            try:app._reject_if_extracting()
            except HTTPException as exc:assert exc.status_code==409
            else:raise AssertionError('legacy execution overlapped orphan login browser')
            assert not app.run_lock.locked()
        finally:
            child.terminate();child.communicate(timeout=5)
        reservation=jobs.reserve(account_id='acc2',kind='state')
        assert jobs.release(reservation['job_id'])
        """)


if __name__ == "__main__":
    unittest.main()
