import contextlib
import os
import hashlib
import http.server
import importlib.util
import json
import pathlib
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock
import types
import zipfile

ROOT = pathlib.Path(os.environ.get("KRITA_TEST_ROOT", tempfile.gettempdir())) / "krita-launcher-tests"
(ROOT / "work").mkdir(parents=True, exist_ok=True)
SOURCE = pathlib.Path(__file__).resolve().parents[1] / "launcher_repair.py"
sys.path.insert(0, str(SOURCE.parent))
spec = importlib.util.spec_from_file_location("launcher_repair", SOURCE)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)
PAYLOAD = bytes(range(256)) * (4 * 1024 * 1024 // 256 + 11)
HASH = hashlib.sha256(PAYLOAD).hexdigest()


class DownloadHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "normal"
    requests_seen = []
    fail_count = 0
    authorization_seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/data")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).authorization_seen.append(self.headers.get("Authorization"))
        begin, end = map(int, self.headers.get("Range", f"bytes=0-{len(PAYLOAD)-1}").split("=")[1].split("-"))
        type(self).requests_seen.append(begin)
        end = min(end, len(PAYLOAD) - 1)
        if self.mode == "no_range":
            self.send_response(200)
            begin, end = 0, len(PAYLOAD) - 1
        else:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {begin if self.mode != 'bad_range' else 0}-{end}/{len(PAYLOAD)}")
        self.send_header("Content-Length", str(end - begin + 1))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        body = PAYLOAD[begin:end + 1]
        if type(self).fail_count:
            type(self).fail_count -= 1
            self.wfile.write(body[:131072])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            self.close_connection = True
        else:
            self.wfile.write(body)


class RepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "work")
        self.path = pathlib.Path(self.temp.name) / "model.safetensors"
        DownloadHandler.mode = "normal"
        DownloadHandler.requests_seen = []
        DownloadHandler.authorization_seen = []
        DownloadHandler.fail_count = 0
        self.url = f"http://127.0.0.1:{self.server.server_port}/data"
        self.downloader = repair.ResumableDownloader(retries=2, sleeper=lambda n: None, reserve_bytes=0,
                                                    opener=repair.make_opener())

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, **kw):
        return self.downloader.fetch([self.url], self.path, size=len(PAYLOAD), sha256=kw.pop("sha256", HASH), identity="test", **kw)

    def test_dropped_connection_resumes_without_restarting(self):
        DownloadHandler.fail_count = 3
        self.fetch()
        self.assertEqual(self.path.read_bytes(), PAYLOAD)
        self.assertEqual(DownloadHandler.requests_seen[:4], [0, 131072, 262144, 393216])

    def test_restart_resumes_persistent_partial(self):
        part = pathlib.Path(str(self.path) + ".part")
        part.write_bytes(PAYLOAD[:700000])
        self.fetch()
        self.assertEqual(DownloadHandler.requests_seen[0], 700000)
        self.assertEqual(self.path.read_bytes(), PAYLOAD)

    def test_no_progress_exhaustion_keeps_partial(self):
        # A failing opener never gets far enough to receive a response.
        class Offline:
            def open(self, *a, **kw):
                raise OSError("offline")
        self.downloader.opener = Offline()
        with self.assertRaises(repair.DownloadError):
            self.fetch()
        self.assertTrue(pathlib.Path(str(self.path) + ".part").exists())

    def test_existing_corrupt_file_not_silently_skipped(self):
        self.path.write_bytes(bytes(len(PAYLOAD)))
        self.fetch()
        self.assertEqual(self.path.read_bytes(), PAYLOAD)
        self.assertEqual(len(list(self.path.parent.glob("*.unverified-*"))), 1)

    def test_checksum_failure_never_becomes_final(self):
        with self.assertRaises(repair.PermanentDownloadError):
            self.fetch(sha256="0" * 64)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob("*.bad-hash-*"))), 1)

    def test_no_range_server_cannot_append_full_file(self):
        DownloadHandler.mode = "no_range"
        part = pathlib.Path(str(self.path) + ".part")
        part.write_bytes(PAYLOAD[:700000])
        with self.assertRaises(repair.PermanentDownloadError):
            self.fetch()
        self.assertEqual(part.stat().st_size, 700000)

    def test_wrong_content_range_is_rejected(self):
        DownloadHandler.mode = "bad_range"
        part = pathlib.Path(str(self.path) + ".part")
        part.write_bytes(PAYLOAD[:700000])
        with self.assertRaises(repair.PermanentDownloadError):
            self.fetch()
        self.assertEqual(part.stat().st_size, 700000)

    def test_cross_host_redirect_drops_token(self):
        self.url = f"http://127.0.0.1:{self.server.server_port}/redirect"
        self.fetch(headers={"Authorization": "Bearer test-only"})
        self.assertTrue(DownloadHandler.authorization_seen)
        self.assertTrue(all(x is None for x in DownloadHandler.authorization_seen))

    def test_environment_error_classification(self):
        self.assertEqual(repair.classify_environment({"torch_cuda": "13.0", "cuda_error": "driver too old"}, {"cuda_api": 12080, "cuda_max": "12.8"})[0], "driver_too_old")
        self.assertEqual(repair.classify_environment({"torch_cuda": None}, {})[0], "cpu_torch")
        self.assertEqual(repair.classify_environment({"torch_cuda": "13.0", "cuda_error": "no kernel image is available"}, {})[0], "gpu_architecture")
        self.assertEqual(repair.classify_environment({"torch_cuda": "13.0", "kernel_ok": True, "import_errors": {"thing": "missing"}}, {})[0], "dependency_error")
        self.assertEqual(repair.classify_environment({"torch_cuda": "13.0", "kernel_ok": True, "requirements_scan_error": "missing pip"}, {})[0], "dependency_error")
        self.assertEqual(repair.classify_environment({"torch_cuda": "13.0", "kernel_ok": True}, {})[0], "ok")

    def test_zip_traversal_preflight(self):
        archive = self.path.parent / "bad.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../outside.txt", "blocked")
        with self.assertRaises(repair.PermanentDownloadError):
            repair.checked_extract_zip(archive, self.path.parent / "extract")

    def test_installer_stages_core_and_resumes_models(self):
        archive = self.path.parent / "core.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("ComfyUI/main.py", "# test only")
            z.writestr("python_embeded/python.exe", b"not executed")
        target = self.path.parent / "installed"
        target.mkdir()
        downloaded = []
        def fake_download(repo, revision, remote, local, **kwargs):
            downloaded.append(remote)
            pathlib.Path(local).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(local).write_bytes(archive.read_bytes() if remote == "comfyui_core.zip" else b"test model")
        app = types.SimpleNamespace(config_data={}, settings={},
                                    _get_modelscope_token=lambda: ("", ""),
                                    _list_modelscope_repo_files=lambda *a: ["comfyui_core.zip", "models/checkpoints/test.safetensors"],
                                    _download_modelscope_file=fake_download,
                                    after=lambda delay, fn, *args: fn(*args),
                                    log_to_console=lambda text: None, set_status=lambda text: None,
                                    save_settings=lambda: None,
                                    btn_download_ms=types.SimpleNamespace(configure=lambda **kw: None))
        report = {"code": "ok", "message": "test", "environment": {}}
        with mock.patch.object(repair, "diagnose_environment", return_value=report), mock.patch.object(repair, "present_report"):
            repair.install_repository(app, target)
            self.assertEqual(pathlib.Path(app.comfy_dir), target / "ComfyUI_windows_portable")
            self.assertTrue((pathlib.Path(app.comfy_dir) / "ComfyUI/models/checkpoints/test.safetensors").exists())
            repair.install_repository(app, target)
        self.assertEqual(downloaded.count("comfyui_core.zip"), 1)

    def test_missing_python_is_reported_without_installing_anything(self):
        result = repair.diagnose_environment(self.path.parent)
        self.assertEqual(result["code"], "environment_missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
