"""Verified self-update for the recovered Python 3.10 launcher.

The old --apply-update arguments remain supported for the first migration.
No update is successful until the application starts at the expected path and
confirms its actual version and executable hash. No settings version is trusted.
"""
import ctypes
from ctypes import wintypes
import dis
import hashlib
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib

from archive_tools import LauncherArchive
import launcher_build as build


class UpdateError(RuntimeError):
    pass


def version_key(value):
    """Numeric release ordering, then SemVer prerelease ordering; ignore +build."""
    if not isinstance(value, str):
        raise UpdateError("版本号必须是文字")
    match = re.fullmatch(r"[vV]?(\d+(?:\.\d+){0,3})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?", value.strip())
    if not match:
        raise UpdateError("版本号格式不正确：" + value)
    release = tuple(int(part) for part in match[1].split("."))
    release += (0,) * (4 - len(release))
    suffix = match[2]
    prerelease = tuple((0, int(part)) if part.isdigit() else (1, part)
                       for part in suffix.split(".")) if suffix else ()
    return release, int(suffix is None), prerelease


def is_newer(remote, local=None):
    return version_key(remote) > version_key(build.APP_VERSION if local is None else local)


def file_hash(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for part in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(part)
    return result.hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + os.urandom(6).hex() + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_root():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "DrowmAI" / "launcher_updates"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def executable_identity(path):
    """Inspect literal build assignments without executing downloaded code."""
    archive = LauncherArchive(path)
    if archive.python_version != 310 or sys.version_info[:2] != (3, 10):
        raise UpdateError("更新包不是兼容的 Python 3.10 启动器；请使用完整安装包迁移")
    pyz = archive.read("PYZ.pyz")
    toc = dict(marshal.loads(pyz[int.from_bytes(pyz[8:12], "big"):]))
    for name in ("launcher_build", "launcher_repair", "launcher_main"):
        if name not in toc:
            continue
        _, start, length = toc[name]
        code = marshal.loads(zlib.decompress(pyz[start:start + length]))
        assignments = {}
        previous = None
        for instruction in dis.get_instructions(code):
            if (instruction.opname in ("STORE_NAME", "STORE_GLOBAL")
                    and previous is not None and previous.opname == "LOAD_CONST"):
                assignments[instruction.argval] = previous.argval
            previous = instruction
        version = assignments.get("APP_VERSION") or assignments.get("PATCH_VERSION")
        if isinstance(version, str):
            version_key(version)
            return {"version": version, "product_id": assignments.get("PRODUCT_ID", ""),
                    "protocol": assignments.get("UPDATE_PROTOCOL", 0), "module": name}
    raise UpdateError("更新文件内找不到启动器版本号")


def valid_url(value):
    if not isinstance(value, str):
        raise UpdateError("更新地址格式不正确")
    parts = urllib.parse.urlsplit(value)
    # HTTP is only permitted for local fault-injection tests.
    if (parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1"))
            or not parts.hostname or parts.username or parts.password or parts.fragment):
        raise UpdateError("更新地址必须是有效的 HTTPS 下载链接")
    return value


def validate_manifest(payload, require_package=False):
    if not isinstance(payload, dict):
        raise UpdateError("version.json 内容必须是一个对象")
    info = dict(payload)
    version_key(info.get("version"))
    if require_package:
        if info.get("package_type") != "launcher":
            raise UpdateError("发布清单缺少 package_type=launcher，无法确认是启动器还是安装器")
        if info.get("product_id") != build.PRODUCT_ID:
            raise UpdateError("发布清单的启动器标识不匹配")
        if not isinstance(info.get("size"), int) or isinstance(info["size"], bool) or not 0 < info["size"] <= 512 * 1024**2:
            raise UpdateError("发布清单缺少有效的 size，请使用配套构建脚本生成 version.json")
        if not isinstance(info.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", info["sha256"]):
            raise UpdateError("发布清单缺少 SHA256，请使用配套构建脚本生成 version.json")
        info["sha256"] = info["sha256"].lower()
        info["url"] = valid_url(info.get("url"))
        if "/releases/latest/" in info["url"]:
            raise UpdateError("自动更新需使用固定版本下载地址，不能使用 releases/latest")
        mirrors = info.get("mirrors", [])
        if not isinstance(mirrors, list) or len(mirrors) > 8:
            raise UpdateError("备用更新地址格式不正确")
        info["mirrors"] = [valid_url(url) for url in mirrors]
    return info


def verify_download(path, info):
    info = validate_manifest(info, require_package=True)
    path = Path(path)
    if path.stat().st_size != info["size"] or file_hash(path) != info["sha256"]:
        raise UpdateError("更新文件长度或 SHA256 校验失败，旧启动器未被替换")
    identity = executable_identity(path)
    if identity["product_id"] != build.PRODUCT_ID or identity["protocol"] != build.UPDATE_PROTOCOL:
        raise UpdateError("下载文件不是支持此更新协议的启动器")
    if version_key(identity["version"]) != version_key(info["version"]):
        raise UpdateError(f"清单为 {info['version']}，文件实际为 {identity['version']}；已停止更新")
    return identity


def frozen_child_environment():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("_PYI_") or key in ("_MEIPASS2", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch_executable(path, arguments=()):
    return subprocess.Popen([str(path), *map(str, arguments)], cwd=str(Path(path).parent),
                            env=frozen_child_environment(), close_fds=True)


def wait_parent(pid, timeout=60):
    pid = int(pid or 0)
    if pid <= 0:
        return
    if pid == os.getpid():
        raise UpdateError("更新进程不能等待自身退出")
    if os.name != "nt":
        raise UpdateError("自动替换仅支持 Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # PID no longer exists.
            return
        raise UpdateError(f"无法等待旧启动器退出（Windows {error}）")
    try:
        result = kernel.WaitForSingleObject(handle, int(timeout * 1000))
        if result != 0:
            raise UpdateError("旧启动器仍在运行，已保留旧文件；请关闭其他同目录启动器再更新")
    finally:
        kernel.CloseHandle(handle)


def same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def in_temp_directory(path):
    try:
        Path(path).resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        return True
    except ValueError:
        return False


def prepare_plan(source, target, info, parent_pid=0, root=None):
    source, target = Path(source).resolve(), Path(target).resolve()
    verify_download(source, info)
    if same_path(source, target) or not target.is_file() or target.suffix.lower() != ".exe":
        raise UpdateError("无法确认要更新的原启动器文件")
    if in_temp_directory(target):
        raise UpdateError("启动器位于临时目录，请先解压到普通文件夹再更新")
    # Check permissions before asking the running application to exit.
    probe = target.parent / (".launcher-write-test-" + os.urandom(8).hex())
    try:
        with probe.open("xb"):
            pass
    except OSError as error:
        raise UpdateError(f"当前目录不能写入：{target.parent}\n{error}") from error
    finally:
        probe.unlink(missing_ok=True)
    transaction = os.urandom(16).hex()
    plan_path = (Path(root) if root else update_root()) / transaction / "plan.json"
    plan = {"schema": 2, "transaction": transaction, "source": str(source), "target": str(target),
            "parent_pid": int(parent_pid or 0), "from_sha256": file_hash(target),
            "version": info["version"], "sha256": info["sha256"], "size": info["size"],
            "product_id": build.PRODUCT_ID, "package_type": "launcher", "url": info["url"]}
    write_json(plan_path, plan)
    set_state(plan_path, "prepared")
    return plan_path


def set_state(plan_path, status, **details):
    state = {"status": status, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), **details}
    write_json(Path(plan_path).with_name("state.json"), state)
    with Path(plan_path).with_name("update.log").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(state, ensure_ascii=False) + "\n")
    return state


def replace_with_retry(source, target, timeout=30, sleeper=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as error:
            retryable = isinstance(error, PermissionError) or getattr(error, "winerror", None) in (5, 32, 33)
            if not retryable or time.monotonic() >= deadline:
                raise
            sleeper(0.25)


def confirm_startup(plan_path, current_exe=None, current_version=None):
    """Called only after the new launcher's UI has been constructed."""
    plan = read_json(plan_path)
    current_exe = Path(current_exe or sys.executable).resolve()
    current_version = current_version or build.APP_VERSION
    if not same_path(current_exe, plan["target"]):
        raise UpdateError("重启路径不正确，当前仍是其他启动器副本")
    if version_key(current_version) != version_key(plan["version"]):
        raise UpdateError(f"重启版本不正确：期望 {plan['version']}，实际 {current_version}")
    if current_exe.stat().st_size != plan["size"] or file_hash(current_exe) != plan["sha256"]:
        raise UpdateError("重启文件与校验通过的更新文件不一致")
    return set_state(plan_path, "confirmed", version=current_version, executable=str(current_exe))


def apply_plan(plan_path, *, launcher=launch_executable, waiter=wait_parent,
               confirmation_timeout=45, replace_timeout=30):
    """Stage beside target, keep backup, atomically replace, restart and confirm."""
    plan_path = Path(plan_path).resolve()
    plan = read_json(plan_path)
    if plan.get("schema") != 2 or not re.fullmatch(r"[0-9a-f]{32}", plan.get("transaction", "")):
        raise UpdateError("更新任务文件格式不正确")
    source, target = Path(plan["source"]).resolve(), Path(plan["target"]).resolve()
    if same_path(source, target) or target.suffix.lower() != ".exe" or in_temp_directory(target):
        raise UpdateError("更新目标路径不正确")
    verify_download(source, plan)
    suffix = ".update-" + plan["transaction"]
    staged = target.with_name(target.name + suffix + ".new")
    backup = target.with_name(target.name + suffix + ".bak")
    replaced = False
    try:
        set_state(plan_path, "applying", target=str(target), version=plan["version"])
        shutil.copy2(source, staged)
        verify_download(staged, plan)
        waiter(plan["parent_pid"])
        existing_hash = file_hash(target)
        if existing_hash != plan["from_sha256"]:
            raise UpdateError("原文件在下载期间发生变化，请从当前启动器重新检查更新")
        shutil.copy2(target, backup)
        if file_hash(backup) != plan["from_sha256"]:
            raise UpdateError("旧版备份校验失败，已停止替换")
        replace_with_retry(staged, target, timeout=replace_timeout)
        replaced = True
        verify_download(target, plan)
        set_state(plan_path, "restart_pending", target=str(target), version=plan["version"], backup=str(backup))
        try:
            launcher(target, ["--update-receipt", str(plan_path)])
        except Exception:
            # A launch failure is reversible before any new UI has started.
            if file_hash(target) == plan["sha256"]:
                shutil.copy2(backup, staged)
                replace_with_retry(staged, target, timeout=replace_timeout)
                replaced = False
            raise
        deadline = time.monotonic() + confirmation_timeout
        while time.monotonic() < deadline:
            state = read_json(plan_path.with_name("state.json"))
            if state.get("status") == "confirmed":
                return state
            if state.get("status") == "confirmation_failed":
                raise UpdateError(state.get("error", "新版启动后核验失败"))
            time.sleep(0.2)
        raise UpdateError("新版文件已替换，但尚未收到启动确认。请从原位置打开启动器，查看版本和运行路径；旧版备份已保留")
    except Exception as error:
        set_state(plan_path, "failed", error=str(error), target=str(target),
                  replacement_present=replaced, backup=str(backup) if backup.exists() else "")
        raise
    finally:
        staged.unlink(missing_ok=True)


def argument(name):
    if name not in sys.argv:
        return None
    index = sys.argv.index(name)
    if index + 1 == len(sys.argv):
        raise UpdateError("缺少参数：" + name)
    return sys.argv[index + 1]


def run_apply_update_mode():
    plan_path = None
    try:
        source = Path(sys.executable).resolve()
        plan_value = argument("--update-plan")
        if plan_value:
            plan_path = Path(plan_value).resolve()
            if not same_path(read_json(plan_path)["source"], source):
                raise UpdateError("更新任务来源与当前程序不一致")
        else:
            # Old 1.2.1/1.2.2 clients launch this downloaded EXE directly.
            target = argument("--target-exe")
            if not target:
                raise UpdateError("缺少原启动器目标路径")
            info = {"version": build.APP_VERSION, "sha256": file_hash(source), "size": source.stat().st_size,
                    "product_id": build.PRODUCT_ID, "package_type": "launcher", "url": "https://localhost/legacy-migration"}
            plan_path = prepare_plan(source, target, info, argument("--parent-pid") or 0)
        apply_plan(plan_path)
        return 0
    except Exception as error:
        message = "更新未完成：" + str(error)
        if plan_path:
            message += "\n\n记录：" + str(plan_path.with_name("update.log"))
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "启动器更新结果", 0x10)
        return 1


def fetch_manifest(url, opener=None):
    from launcher_repair import make_opener
    valid_url(url)
    opener = opener or make_opener()
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("_launcher_check", str(time.time_ns())))
    request_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    last_error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(request_url, headers={
                "User-Agent": "KritaLauncher/" + build.APP_VERSION, "Cache-Control": "no-cache", "Pragma": "no-cache"})
            with opener.open(request, timeout=12) as response:
                data = response.read(128 * 1024 + 1)
            if len(data) > 128 * 1024:
                raise UpdateError("更新清单过大")
            return validate_manifest(json.loads(data.decode("utf-8-sig")))
        except (OSError, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * 2**attempt)
    raise UpdateError("无法获取更新清单：" + str(last_error))


def patch_update_methods(module):
    from launcher_repair import ResumableDownloader, make_opener, ui_log
    app = module.LauncherApp
    original_init = app.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.title(self.config_data.get("window_title", "卓尔谟 AI 启动器") + "  v" + module.APP_VERSION)
        path = str(Path(sys.executable).resolve())
        ui_log(self, f"当前启动器版本：{module.APP_VERSION}\n运行位置：{path}\n")
        receipt = argument("--update-receipt")
        if receipt:
            try:
                confirm_startup(receipt, current_version=module.APP_VERSION)
                self.set_status("启动器已更新并核验：v " + module.APP_VERSION)
            except Exception as error:
                set_state(receipt, "confirmation_failed", error=str(error))
                self.after(0, module.msgbox.showerror, "更新核验失败", str(error))
        try:
            name = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
            write_json(update_root() / ("startup-" + name + ".json"),
                       {"version": module.APP_VERSION, "executable": path, "receipt": receipt,
                        "started_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        except OSError:
            pass

    def compare(self, remote_version, local_version=None):
        # Resolve the actual runtime version, never a default captured at import.
        return is_newer(remote_version, module.APP_VERSION if local_version is None else local_version)

    def check_thread(self, manifest_url, manual):
        try:
            info = fetch_manifest(manifest_url)
            if self._is_remote_version_newer(info["version"]):
                info = validate_manifest(info, require_package=True)
                self.after(0, self._show_launcher_update_dialog, info)
            elif manual:
                self.after(0, module.msgbox.showinfo, "检查更新",
                           f"当前版本：{module.APP_VERSION}\n服务器版本：{info['version']}\n已是当前可用版本。\n\n运行位置：{sys.executable}")
        except Exception as error:
            ui_log(self, "检查更新失败：" + str(error) + "\n")
            if manual:
                self.after(0, module.msgbox.showerror, "检查更新失败", str(error))
        finally:
            self._update_check_in_progress = False

    def show_dialog(self, info):
        if not self._is_remote_version_newer(info["version"]):
            return
        if getattr(self, "_launcher_update_in_progress", False):
            return
        text = f"当前版本：{module.APP_VERSION}\n新版本：{info['version']}\n\n{info.get('notes', '')}\n\n是否下载并更新？启动器会关闭，并从当前目录重新打开。"
        if module.msgbox.askyesno("启动器有新版本", text, parent=self):
            self._start_launcher_update(info)

    def download_thread(self, update_info):
        try:
            info = validate_manifest(update_info, require_package=True)
            if not self._is_remote_version_newer(info["version"]):
                raise UpdateError("服务器版本没有高于当前运行版本，已停止重复更新")
            destination = update_root() / "downloads" / info["sha256"] / "KritaLauncherUpdate.exe"
            downloader = ResumableDownloader(log=lambda message: ui_log(self, message), opener=make_opener())
            downloader.fetch([info["url"], *info.get("mirrors", [])], destination,
                             size=info["size"], sha256=info["sha256"], identity=info["sha256"])
            verify_download(destination, info)
            self._verified_launcher_update = (str(destination), info)
            self.after(0, self._prompt_run_launcher_installer, str(destination), info["version"])
        except Exception as error:
            self.after(0, update_failed, self, str(error))

    def update_failed(self, error):
        self._launcher_update_in_progress = False
        self._set_launcher_update_button_busy(False)
        self.set_status("启动器更新未完成，当前文件已保留")
        ui_log(self, "更新未完成：" + error + "\n")
        module.msgbox.showerror("启动器更新未完成", error, parent=self)

    def launch_update(self, downloaded_path, remote_version):
        try:
            verified_path, info = getattr(self, "_verified_launcher_update", (None, None))
            if verified_path is None or not same_path(verified_path, downloaded_path) or info["version"] != remote_version:
                raise UpdateError("更新文件尚未通过校验，请重新检查更新")
            plan_path = prepare_plan(downloaded_path, sys.executable, info, os.getpid())
            launch_executable(downloaded_path, ["--apply-update", "--update-plan", str(plan_path)])
            ui_log(self, f"更新文件已核验，正在替换当前启动器：{sys.executable}\n更新记录：{plan_path.parent}\n")
            self.set_status("正在应用更新，完成后将从原位置重开")
            self.after(300, self.on_closing)
        except Exception as error:
            update_failed(self, str(error))

    app.__init__ = __init__
    app._is_remote_version_newer = compare
    app._parse_version = lambda self, value: version_key(value)
    app._check_launcher_updates_thread = check_thread
    app._show_launcher_update_dialog = show_dialog
    app._download_launcher_update_thread = download_thread
    app._prompt_run_launcher_installer = launch_update
    app._launch_launcher_self_replace = launch_update
    module.run_apply_update_mode = run_apply_update_mode
    return app


def handle_headless_arguments():
    if "--apply-update" in sys.argv:
        raise SystemExit(run_apply_update_mode())
    if "--launcher-version-json" in sys.argv:
        data = {"version": build.APP_VERSION, "product_id": build.PRODUCT_ID,
                "update_protocol": build.UPDATE_PROTOCOL, "executable": str(Path(sys.executable).resolve())}
        output = argument("--version-output")
        if output:
            write_json(output, data)
        elif sys.stdout is not None:
            print(json.dumps(data, ensure_ascii=False))
        raise SystemExit(0)
