import ctypes
from ctypes import wintypes
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import launcher_build as build
import launcher_updates as updates
import rebuild

CANDIDATE = Path(os.environ.get("KRITA_UPDATE_TEST_EXE", str(SOURCE.parent / "Krita一键启动器-修复预览.exe"))).resolve()
TEST_WORK = Path(os.environ.get("KRITA_TEST_WORK", str(Path.cwd() / "work"))).resolve()


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CANDIDATE.is_file():
            raise RuntimeError("先构建预览 EXE，或用 KRITA_UPDATE_TEST_EXE 指定测试文件")
        cls.info = rebuild.release_manifest(CANDIDATE)
        TEST_WORK.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_WORK)
        self.root = Path(self.temp.name)
        self.target = self.root / "课程启动器.exe"
        self.old = b"old launcher fixture, never executed"
        self.target.write_bytes(self.old)
        self.settings = self.root / "settings.json"
        self.settings.write_text('{"keep":"student configuration"}', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        return updates.prepare_plan(CANDIDATE, self.target, self.info, root=self.root / "transactions")

    def confirming_launcher(self, path, arguments):
        self.assertEqual(Path(path), self.target)
        self.assertEqual(arguments[0], "--update-receipt")
        updates.confirm_startup(arguments[1], path, build.APP_VERSION)

    def make_app(self, version=None):
        class Dummy:
            pass
        module = types.SimpleNamespace(LauncherApp=Dummy, APP_VERSION=version or build.APP_VERSION,
                                       msgbox=mock.Mock())
        updates.patch_update_methods(module)
        instance = object.__new__(Dummy)
        instance.after = lambda _, function, *args: function(*args)
        instance.log_to_console = mock.Mock()
        instance._show_launcher_update_dialog = mock.Mock()
        return module, instance

    def test_version_ordering_and_prereleases(self):
        for remote, local, expected in [
            ("1.2.3", "1.2.3", False), ("v1.2.3", "1.2.3.0", False),
            ("1.2.10", "1.2.9", True), ("1.2.2", "1.2.3-preview.1", False),
            ("1.2.3", "1.2.3-preview.1", True),
            ("1.2.3-preview.10", "1.2.3-preview.2", True),
            ("1.2.3-preview.1", "1.2.3", False),
            ("1.2.3+build2", "1.2.3+build1", False),
        ]:
            with self.subTest(remote=remote, local=local):
                self.assertEqual(updates.is_newer(remote, local), expected)
        for bad in (None, "latest", "1.2.nope", "1.2.3.4.5", ""):
            with self.subTest(invalid=bad), self.assertRaises(updates.UpdateError):
                updates.version_key(bad)

    def test_runtime_version_does_not_capture_old_default(self):
        module, app = self.make_app("1.2.3")
        self.assertFalse(app._is_remote_version_newer("1.2.3"))
        self.assertTrue(app._is_remote_version_newer("1.2.4"))
        module.APP_VERSION = "1.2.4"
        self.assertFalse(app._is_remote_version_newer("1.2.4"))

    def test_same_version_never_prompts_even_with_force(self):
        _, app = self.make_app()
        with mock.patch.object(updates, "fetch_manifest", return_value={"version": build.APP_VERSION, "force": True}):
            app._check_launcher_updates_thread("https://example.com/version.json", True)
        app._show_launcher_update_dialog.assert_not_called()
        self.assertFalse(app._update_check_in_progress)

    def test_old_manifest_does_not_downgrade_preview(self):
        module, app = self.make_app()
        with mock.patch.object(updates, "fetch_manifest", return_value={"version": "1.2.2"}):
            app._check_launcher_updates_thread("https://example.com/version.json", True)
        app._show_launcher_update_dialog.assert_not_called()
        module.msgbox.showinfo.assert_called_once()

    def test_declining_update_still_allows_later_manual_retry(self):
        module, app = self.make_app("1.2.2")
        app._start_launcher_update = mock.Mock()
        module.msgbox.askyesno.side_effect = [False, True]
        show = type(app)._show_launcher_update_dialog
        show(app, self.info)
        app._start_launcher_update.assert_not_called()
        show(app, self.info)
        self.assertEqual(module.msgbox.askyesno.call_count, 2)
        app._start_launcher_update.assert_called_once_with(self.info)

    def test_new_update_preserves_full_verification_metadata(self):
        _, app = self.make_app("1.2.2")
        with mock.patch.object(updates, "fetch_manifest", return_value=self.info):
            app._check_launcher_updates_thread("https://example.com/version.json", True)
        delivered = app._show_launcher_update_dialog.call_args.args[0]
        self.assertEqual(delivered["sha256"], self.info["sha256"])
        self.assertEqual(delivered["size"], self.info["size"])

    def test_unverified_or_floating_manifest_is_blocked(self):
        for mutation in ({"sha256": ""}, {"size": 0}, {"product_id": "other"},
                         {"package_type": "installer"},
                         {"url": "https://github.com/owner/repo/releases/latest/download/file.exe"}):
            with self.subTest(mutation=mutation), self.assertRaises(updates.UpdateError):
                updates.validate_manifest({**self.info, **mutation}, require_package=True)

    def test_real_executable_version_matches_generated_manifest(self):
        identity = updates.executable_identity(CANDIDATE)
        self.assertEqual(identity["version"], self.info["version"])
        self.assertEqual(identity["product_id"], self.info["product_id"])
        self.assertEqual(CANDIDATE.stat().st_size, self.info["size"])
        self.assertEqual(updates.file_hash(CANDIDATE), self.info["sha256"])

    def test_manifest_executable_version_mismatch_is_blocked(self):
        with self.assertRaisesRegex(updates.UpdateError, "文件实际"):
            updates.verify_download(CANDIDATE, {**self.info, "version": "99.0.0"})
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_corrupt_download_is_blocked_before_plan(self):
        corrupt = self.root / "corrupt.exe"
        shutil.copy2(CANDIDATE, corrupt)
        with corrupt.open("r+b") as stream:
            stream.seek(300)
            byte = stream.read(1)
            stream.seek(300)
            stream.write(bytes([byte[0] ^ 0xFF]))
        with self.assertRaises(updates.UpdateError):
            updates.prepare_plan(corrupt, self.target, self.info, root=self.root / "transactions")
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_success_restarts_exact_target_and_preserves_settings_backup(self):
        settings_before = self.settings.read_bytes()
        plan_path = self.plan()
        result = updates.apply_plan(plan_path, launcher=self.confirming_launcher)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["executable"], str(self.target))
        self.assertEqual(updates.file_hash(self.target), self.info["sha256"])
        self.assertEqual(self.settings.read_bytes(), settings_before)
        backups = list(self.root.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.old)

    def test_replace_failure_keeps_original(self):
        plan_path = self.plan()
        real_replace = os.replace

        def deny_executable(source, target):
            if Path(target) == self.target:
                raise PermissionError("simulated locked executable")
            return real_replace(source, target)

        with mock.patch.object(updates.os, "replace", side_effect=deny_executable):
            with self.assertRaises(PermissionError):
                updates.apply_plan(plan_path, launcher=self.confirming_launcher, replace_timeout=0)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(updates.read_json(plan_path.with_name("state.json"))["status"], "failed")

    @unittest.skipUnless(os.name == "nt", "Windows file sharing semantics")
    def test_real_windows_file_lock_retries_until_released(self):
        plan_path = self.plan()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.target), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        timer = threading.Timer(0.8, lambda: kernel.CloseHandle(handle))
        timer.start()
        real_replace = os.replace
        failures = []

        def observe_replace(source, target):
            try:
                return real_replace(source, target)
            except PermissionError:
                if Path(target) == self.target:
                    failures.append(True)
                raise

        try:
            with mock.patch.object(updates.os, "replace", side_effect=observe_replace):
                result = updates.apply_plan(plan_path, launcher=self.confirming_launcher, replace_timeout=3)
        finally:
            timer.join()
        self.assertTrue(failures, "test must encounter the real Windows lock")
        self.assertEqual(result["status"], "confirmed")

    def test_parent_still_running_leaves_original(self):
        plan_path = self.plan()
        with self.assertRaises(updates.UpdateError):
            updates.apply_plan(plan_path, waiter=mock.Mock(side_effect=updates.UpdateError("parent alive")))
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_concurrent_file_change_is_not_overwritten(self):
        plan_path = self.plan()
        changed = b"another launcher version installed during download"
        def change_target(_):
            self.target.write_bytes(changed)
        with self.assertRaisesRegex(updates.UpdateError, "发生变化"):
            updates.apply_plan(plan_path, waiter=change_target)
        self.assertEqual(self.target.read_bytes(), changed)

    def test_restart_creation_failure_restores_original(self):
        plan_path = self.plan()
        with self.assertRaisesRegex(OSError, "cannot start"):
            updates.apply_plan(plan_path, launcher=mock.Mock(side_effect=OSError("cannot start")))
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(updates.read_json(plan_path.with_name("state.json"))["replacement_present"])

    def test_missing_restart_confirmation_never_reports_success(self):
        plan_path = self.plan()
        with self.assertRaisesRegex(updates.UpdateError, "尚未收到启动确认"):
            updates.apply_plan(plan_path, launcher=mock.Mock(), confirmation_timeout=0)
        self.assertEqual(updates.read_json(plan_path.with_name("state.json"))["status"], "failed")
        self.assertEqual(list(self.root.glob("*.bak"))[0].read_bytes(), self.old)

    def test_wrong_restart_path_or_version_cannot_confirm(self):
        plan_path = self.plan()
        other = self.root / "other.exe"
        shutil.copy2(CANDIDATE, other)
        with self.assertRaisesRegex(updates.UpdateError, "路径不正确"):
            updates.confirm_startup(plan_path, other, build.APP_VERSION)
        with self.assertRaisesRegex(updates.UpdateError, "版本不正确"):
            updates.confirm_startup(plan_path, self.target, "1.2.1")
        self.assertNotEqual(updates.read_json(plan_path.with_name("state.json"))["status"], "confirmed")

    def test_restart_discards_parent_bootloader_environment(self):
        with mock.patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "old-extraction", "_MEIPASS2": "old-extraction", "PYTHONHOME": "old-python"}):
            with mock.patch.object(updates.subprocess, "Popen") as start:
                updates.launch_executable(CANDIDATE, ["--apply-update"])
        environment = start.call_args.kwargs["env"]
        self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", environment)
        self.assertNotIn("_MEIPASS2", environment)
        self.assertNotIn("PYTHONHOME", environment)

    def test_manifest_request_bypasses_cache_and_bounds_content(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(json.dumps({"version": build.APP_VERSION}).encode())
        result = updates.fetch_manifest("https://example.com/version.json?channel=stable", opener)
        request = opener.open.call_args.args[0]
        self.assertIn("_launcher_check=", request.full_url)
        self.assertIn("channel=stable", request.full_url)
        self.assertEqual(request.get_header("Cache-control"), "no-cache")
        self.assertEqual(result["version"], build.APP_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
