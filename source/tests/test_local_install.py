import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_local as local


class LocalInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / '云端备份下载'
        self.downloads.mkdir()
        self.core = self.downloads / 'comfyui_core.zip'
        self.models = self.downloads / 'models'
        for kind in ('checkpoints', 'loras', 'vae'):
            path = self.models / kind / (kind + '.safetensors')
            path.parent.mkdir(parents=True)
            path.write_bytes((kind * 300).encode())
        self.content = {'ComfyUI/main.py': b'# ComfyUI fixture', 'ComfyUI/requirements.txt': b'packaging==25.0\n',
                        'python_embeded/python.exe': b'non executable test fixture',
                        'python_embeded/Lib/site-packages/torch/__init__.py': b'# fixture torch',
                        'python_embeded/python313._pth': b'.\nLib/site-packages\nimport site\n'}
        self.make_core()
        self.destination = self.root / '安装位置'

    def make_core(self, prefix=''):
        with zipfile.ZipFile(self.core, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path, content in self.content.items():
                archive.writestr(prefix + path, content)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, **kwargs):
        return local.install_downloads(self.downloads, self.destination, **kwargs)

    def test_full_zip_and_three_model_types_install_to_correct_paths(self):
        progress = []
        result = self.install(progress=lambda *event: progress.append(event))
        root = Path(result['comfy_root'])
        self.assertEqual(root, self.destination / 'ComfyUI_windows_portable')
        self.assertEqual(result['copied'], 3)
        for kind in ('checkpoints', 'loras', 'vae'):
            self.assertEqual((root / 'ComfyUI/models' / kind / (kind + '.safetensors')).read_bytes(),
                             (self.models / kind / (kind + '.safetensors')).read_bytes())
        self.assertTrue(self.core.is_file())
        self.assertTrue(any(event[0] == '解压 ComfyUI' and event[1] > 0 for event in progress))
        self.assertTrue(any(event[0].startswith('复制模型') and event[1] > 0 for event in progress))

    def test_nested_download_folder_and_zip_root_are_recognized(self):
        self.make_core('ComfyUI_windows_portable/')
        result = local.install_downloads(self.root, self.destination)
        self.assertTrue((Path(result['comfy_root']) / 'ComfyUI/main.py').is_file())

    def test_retry_skips_identical_models_and_keeps_workflows(self):
        first = self.install()
        workflow = Path(first['comfy_root']) / 'ComfyUI/user/workflow.json'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('student workflow', encoding='utf-8')
        second = self.install()
        self.assertEqual(second['copied'], 0)
        self.assertEqual(second['skipped'], 3)
        self.assertEqual(workflow.read_text(encoding='utf-8'), 'student workflow')

    def test_different_existing_model_is_preserved_and_not_marked_complete(self):
        first = self.install()
        path = Path(first['comfy_root']) / 'ComfyUI/models/vae/vae.safetensors'
        path.write_bytes(b'existing custom model')
        with self.assertRaisesRegex(local.LocalInstallError, '同名'):
            self.install()
        self.assertEqual(path.read_bytes(), b'existing custom model')

    def test_unfinished_download_is_rejected_before_extraction(self):
        (self.models / 'checkpoints' / 'pending.safetensors.baiduyun').write_bytes(b'partial')
        with self.assertRaisesRegex(local.LocalInstallError, '还没有下载完成'):
            self.install()
        self.assertFalse(self.destination.exists())

    def test_flat_models_are_not_guessed_from_suffix(self):
        (self.models / 'unknown.safetensors').write_bytes(b'not enough information')
        with self.assertRaisesRegex(local.LocalInstallError, '分类目录已丢失'):
            self.install()

    def test_zip_traversal_is_blocked_before_writing_any_member(self):
        with zipfile.ZipFile(self.core, 'a') as archive:
            archive.writestr('../escaped.txt', b'no')
        with self.assertRaises(local.LocalInstallError):
            local.extract_zip(self.core, self.destination)
        self.assertFalse((self.root / 'escaped.txt').exists())
        self.assertFalse((self.destination / 'ComfyUI').exists())

    def test_zip_symlink_and_windows_duplicate_paths_are_blocked(self):
        with zipfile.ZipFile(self.core, 'a') as archive:
            info = zipfile.ZipInfo('ComfyUI/linked')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, 'outside')
        with self.assertRaises(local.LocalInstallError):
            local.extract_zip(self.core, self.destination)
        self.make_core()
        with zipfile.ZipFile(self.core, 'a') as archive:
            archive.writestr('ComfyUI/MAIN.py', b'duplicate on Windows')
        with self.assertRaises(local.LocalInstallError):
            local.extract_zip(self.core, self.destination)

    def test_cancelled_model_never_appears_as_complete(self):
        destination = self.root / 'models-out'
        cancel = threading.Event()
        def progress(phase, done, total, detail):
            if phase.startswith('复制模型'):
                cancel.set()
        with self.assertRaises(local.TaskCancelled):
            local.copy_models(self.models, destination, progress, cancel)
        self.assertFalse(any(destination.rglob('*.safetensors')))
        self.assertTrue(any(destination.rglob('*.part')))

    def test_restore_python_preserves_models_workflows_and_backs_up_old_runtime(self):
        result = self.install()
        root = Path(result['comfy_root'])
        python = root / 'python_embeded/python.exe'
        python.write_bytes(b'old damaged runtime')
        workflow = root / 'ComfyUI/user/workflow.json'
        workflow.parent.mkdir(parents=True)
        workflow.write_bytes(b'student workflow')
        model = root / 'ComfyUI/models/loras/loras.safetensors'
        model_before = model.read_bytes()
        restored = local.restore_python(self.core, root)
        self.assertEqual(python.read_bytes(), self.content['python_embeded/python.exe'])
        self.assertEqual((Path(restored['backup']) / 'python.exe').read_bytes(), b'old damaged runtime')
        self.assertEqual(model.read_bytes(), model_before)
        self.assertEqual(workflow.read_bytes(), b'student workflow')

    def test_restore_from_wrong_comfy_version_leaves_existing_runtime(self):
        result = self.install()
        root = Path(result['comfy_root'])
        (root / 'ComfyUI/main.py').write_bytes(b'another version')
        with self.assertRaisesRegex(local.LocalInstallError, '版本不同'):
            local.restore_python(self.core, root)
        self.assertEqual((root / 'python_embeded/python.exe').read_bytes(), self.content['python_embeded/python.exe'])
        self.assertFalse(list(root.glob('python_embeded.backup-*')))

    def test_restore_missing_python_is_possible_without_redownloading_models(self):
        result = self.install()
        root = Path(result['comfy_root'])
        (root / 'python_embeded/python.exe').unlink()
        local.restore_python(self.core, root)
        self.assertTrue((root / 'python_embeded/python.exe').is_file())

    def test_insufficient_disk_space_does_not_extract(self):
        with mock.patch.object(local.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 0})()):
            with self.assertRaisesRegex(local.LocalInstallError, '空间不足'):
                self.install()
        self.assertFalse((self.destination / 'ComfyUI_windows_portable').exists())

    def test_download_source_cannot_be_an_install_target(self):
        with self.assertRaisesRegex(local.LocalInstallError, '分开放置'):
            local.install_downloads(self.downloads, self.downloads / 'install')


if __name__ == '__main__':
    unittest.main(verbosity=2)
