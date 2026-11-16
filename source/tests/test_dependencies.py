import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_dependencies as deps
import launcher_local as local
import launcher_repair as repair


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.site = self.root / 'python_embeded/Lib/site-packages'
        self.site.mkdir(parents=True)
        self.python = self.root / 'python_embeded/python.exe'
        self.python.write_bytes(b'fixture, never executed')
        self.report = {'code': 'dependency_error', 'message': 'fixture', 'environment': {
            'installed': {'torch': '2.10.0+cu130'}, 'kernel_ok': True,
            'requirements_issues': [{'name': 'packaging', 'installed': None}], 'import_errors': {}}}
        self.baseline = {'packaging': '25.0'}

    def tearDown(self):
        self.temp.cleanup()

    def test_repairs_only_detected_dependencies_using_fixed_versions(self):
        self.assertEqual(deps.repair_requests(self.report, self.baseline), ['packaging==25.0'])
        self.report['environment']['requirements_issues'] = []
        self.report['environment']['import_errors'] = {'PIL': 'broken import'}
        self.report['environment']['installed']['pillow'] = '11.3.0'
        self.assertEqual(deps.repair_requests(self.report, {}), ['pillow==11.3.0'])

    def test_does_not_guess_a_new_cuda_or_latest_package_version(self):
        self.report['environment']['requirements_issues'] = [{'name': 'torch', 'installed': None}]
        with self.assertRaises(local.LocalInstallError):
            deps.repair_requests(self.report, self.baseline)
        self.report['environment']['requirements_issues'] = [{'name': 'unknown-package', 'installed': None}]
        with self.assertRaisesRegex(local.LocalInstallError, '课程版本'):
            deps.repair_requests(self.report, self.baseline)

    def test_driver_issue_is_not_repaired_by_reinstalling_python_packages(self):
        self.report['code'] = 'driver_too_old'
        with self.assertRaisesRegex(local.LocalInstallError, 'CUDA'):
            deps.repair_requests(self.report, self.baseline)

    def test_download_uses_domestic_sources_cache_raw_progress_no_uninstall(self):
        for name, command in deps.download_commands(self.python, self.root / 'cache', ['packaging==25.0']):
            self.assertIn('--progress-bar', command)
            self.assertIn('raw', command)
            self.assertIn('--no-deps', command)
            self.assertIn('--only-binary=:all:', command)
            self.assertNotIn('uninstall', command)
            self.assertNotIn('--no-cache-dir', command)
            self.assertNotIn('download.pytorch.org', ' '.join(command))

    def test_raw_progress_is_streamed_from_real_subprocess(self):
        events = []
        command = [sys.executable, '-u', '-c', "import time; print('Downloading fixture.whl', flush=True); print('Progress 20 of 100', flush=True); time.sleep(.2); print('Progress 100 of 100', flush=True)"]
        code, output = deps.run_visible(command, self.root, 'download', lambda *event: events.append(event))
        self.assertEqual(code, 0)
        self.assertTrue(any(event[1:3] == (20, 100) for event in events))
        self.assertTrue(any(event[1:3] == (100, 100) for event in events))
        self.assertIn('fixture.whl', output)

    def test_silent_subprocess_can_be_cancelled(self):
        cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set)
        timer.start()
        began = time.monotonic()
        try:
            with self.assertRaises(local.TaskCancelled):
                deps.run_visible([sys.executable, '-u', '-c', 'import time; time.sleep(60)'], self.root, 'waiting', None, cancel)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - began, 8)

    def test_global_pip_config_and_extra_sources_are_not_inherited(self):
        with mock.patch.dict(os.environ, {'PIP_INDEX_URL': 'https://wrong.example/simple', 'PIP_EXTRA_INDEX_URL': 'https://other.example', 'PIP_TRUSTED_HOST': 'wrong.example'}):
            environment = deps.pip_environment()
        self.assertNotIn('PIP_INDEX_URL', environment)
        self.assertNotIn('PIP_EXTRA_INDEX_URL', environment)
        self.assertNotIn('PIP_TRUSTED_HOST', environment)
        self.assertEqual(environment['PIP_CONFIG_FILE'], os.devnull)

    def make_wheel(self, directory, name='packaging', version='25.0'):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        wheel = directory / f'{name}-{version}-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr(f'{name}-{version}.dist-info/METADATA', f'Name: {name}\nVersion: {version}\n')
            archive.writestr(name + '/__init__.py', '# fixture')
        return wheel

    def test_wrong_wheel_or_torch_cannot_be_activated(self):
        cache = self.root / 'cache'
        self.make_wheel(cache, 'torch', '2.10.0')
        with self.assertRaises(local.LocalInstallError):
            deps.inspect_wheels(cache, ['packaging==25.0'])

    def fake_run(self, command, root, phase, *args):
        if 'download' in command:
            self.make_wheel(command[command.index('--dest') + 1])
        else:
            self.assertIn('--no-index', command)
            self.assertIn('--no-deps', command)
            stage = Path(command[command.index('--target') + 1])
            stage.mkdir(parents=True)
        return 0, 'fixture operation'

    def test_failed_verification_rolls_back_overlay_without_changing_base(self):
        base = self.site / 'existing.py'
        base.write_bytes(b'original dependency')
        activation = self.site / 'zz_drowm_repair.pth'
        activation.write_bytes(b'# existing repair\n')
        with mock.patch.object(deps, 'baseline_packages', return_value=self.baseline), \
             mock.patch.object(repair, 'diagnose_environment', side_effect=[self.report, self.report]), \
             mock.patch.object(deps, 'run_visible', side_effect=self.fake_run):
            with self.assertRaisesRegex(local.LocalInstallError, '已撤回'):
                deps.repair_ordinary_dependencies(self.root)
        self.assertEqual(base.read_bytes(), b'original dependency')
        self.assertEqual(activation.read_bytes(), b'# existing repair\n')

    def test_successful_repair_activates_only_after_all_downloads_finish(self):
        after = {'code': 'ok', 'message': 'passed', 'environment': {'requirements_issues': [], 'kernel_ok': True}}
        with mock.patch.object(deps, 'baseline_packages', return_value=self.baseline), \
             mock.patch.object(repair, 'diagnose_environment', side_effect=[self.report, after]), \
             mock.patch.object(deps, 'run_visible', side_effect=self.fake_run):
            result = deps.repair_ordinary_dependencies(self.root)
        self.assertIn('复查通过', result['message'])
        self.assertIn('_launcher_packages', (self.site / 'zz_drowm_repair.pth').read_text())
        self.assertTrue(list((self.root / '_launcher_dependency_repairs').rglob('after.json')))

    def test_download_failure_does_not_modify_or_uninstall_existing_environment(self):
        with mock.patch.object(deps, 'baseline_packages', return_value=self.baseline), \
             mock.patch.object(repair, 'diagnose_environment', return_value=self.report), \
             mock.patch.object(deps, 'run_visible', return_value=(1, 'offline')):
            with self.assertRaisesRegex(local.LocalInstallError, '旧依赖保持原样'):
                deps.repair_ordinary_dependencies(self.root)
        self.assertFalse((self.site / 'zz_drowm_repair.pth').exists())
        self.assertEqual(self.python.read_bytes(), b'fixture, never executed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
