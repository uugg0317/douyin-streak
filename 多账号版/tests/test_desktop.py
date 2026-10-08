import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

from desktop import build, launcher
from core.log_tail import read_tail


class DesktopTests(unittest.TestCase):
    def test_source_worker_entry_finds_project_from_other_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            entry=Path(launcher.__file__).resolve()
            result=subprocess.run([sys.executable,str(entry),'--worker','--help'],
                                  cwd=folder,capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--account',result.stdout)

    def test_packaging_allows_only_resources_and_example(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'static').mkdir()
            (root/'data').mkdir()
            (root/'.env').write_text('dummy secret', encoding='utf-8')
            (root/'.env.example').write_text('HOST=127.0.0.1', encoding='utf-8')
            (root/'data'/'state.json').write_text('{}', encoding='utf-8')
            with mock.patch.object(build,'ROOT',folder), mock.patch.object(build,'BUILD_DIR',str(root/'build')):
                args=build.build_add_data_args()
            items=args[1::2]
            self.assertEqual(len(items),2)
            self.assertTrue(any('.env.example' in item for item in items))
            self.assertFalse(any(item.split(';')[0].endswith('.env') or item.endswith(';data') for item in items))
            self.assertFalse((root/'build').exists())

    def test_distribution_rejects_nested_user_state(self):
        with tempfile.TemporaryDirectory() as folder:
            exe=Path(folder)/'app.exe'
            exe.touch()
            build.verify_clean_artifact(str(exe))
            nested=Path(folder)/'_internal'/'other'
            nested.mkdir(parents=True)
            (nested/'state.json').write_text('{}',encoding='utf-8')
            with self.assertRaises(RuntimeError): build.verify_clean_artifact(str(exe))

    def test_first_run_creates_local_config_without_credentials_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with mock.patch.dict(os.environ,{},clear=True):
                launcher.prepare_first_run(first)
                launcher.prepare_first_run(second)
            one=(Path(first)/'.env').read_text(encoding='utf-8')
            two=(Path(second)/'.env').read_text(encoding='utf-8')
            self.assertIn('HOST=127.0.0.1',one)
            self.assertNotIn('AUTH_TOKEN',one)
            self.assertIn('SPARKKEEPER_MULTI_ACCOUNT=1',one)
            self.assertEqual(one,two)
            with mock.patch.dict(os.environ,{},clear=True): launcher.prepare_first_run(first)
            self.assertEqual(one,(Path(first)/'.env').read_text(encoding='utf-8'))

    def test_worker_dispatch_never_starts_tray(self):
        with mock.patch('core.worker.main',return_value=7) as worker:
            self.assertEqual(launcher.dispatch_worker(['--worker','--account','acc1']),7)
            worker.assert_called_once_with(['--account','acc1'])
        self.assertIsNone(launcher.dispatch_worker([]))

    def test_credential_worker_dispatch_never_starts_tray(self):
        with mock.patch('core.credential_worker.main',return_value=3) as worker:
            self.assertEqual(launcher.dispatch_worker(['--credential-worker','--help']),3)
            worker.assert_called_once_with(['--help'])
        self.assertIn('core.credential_worker',build.HIDDEN_IMPORTS)

    def test_source_credential_worker_entry_from_other_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            entry=Path(launcher.__file__).resolve()
            result=subprocess.run([sys.executable,str(entry),'--credential-worker','--help'],
                                  cwd=folder,capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--',result.stdout)

    def test_unrelated_port_is_not_our_instance(self):
        response=mock.MagicMock()
        response.__enter__.return_value.read.return_value=b'{"ok":true,"app":"other"}'
        with mock.patch('urllib.request.urlopen',return_value=response):
            self.assertFalse(launcher.port_in_use_by_us(8000))
        response.__enter__.return_value.read.return_value=b'{"ok":true,"app":"sparkkeeper"}'
        with mock.patch('urllib.request.urlopen',return_value=response):
            self.assertTrue(launcher.port_in_use_by_us(8000))

    def test_large_utf8_log_tail_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            log=Path(folder)/'app.log'
            log.write_text(''.join(f'{i}: 模拟日志\n' for i in range(40000)),encoding='utf-8')
            result=read_tail(log,12,4096)
            self.assertEqual(len(result.splitlines()),12)
            self.assertTrue(result.endswith('39999: 模拟日志'))


if __name__=='__main__': unittest.main()
