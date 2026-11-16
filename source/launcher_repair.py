"""Repair layer for the user's archived launcher. No original source required.

Only stdlib modules already shipped with the existing executable are used.
The original compiled UI is retained; this file is the editable repair source.
"""
import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from launcher_build import APP_VERSION as PATCH_VERSION
COURSE_CORE = None  # Builder embeds the verified manifest here for frozen mode.
RANGE_BYTES = 2 * 1024 * 1024


class DownloadError(RuntimeError):
    pass


class PermanentDownloadError(DownloadError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def digest_file(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for piece in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(piece)
    return result.hexdigest()


def safe_repo_path(path):
    path = str(path).replace("\\", "/")
    parts = PurePosixPath(path)
    if (not path or parts.is_absolute() or ".." in parts.parts or ":" in path
            or any(p.endswith((".", " ")) for p in parts.parts)):
        raise PermanentDownloadError("仓库文件路径不合法")
    return path


def file_urls(repo_id, revision, remote_path):
    remote_path = safe_repo_path(remote_path)
    repo = urllib.parse.quote(repo_id.strip("/"), safe="/")
    revision = urllib.parse.quote(revision, safe="")
    remote = urllib.parse.quote(remote_path, safe="/")
    return [f"https://{host}/models/{repo}/resolve/{revision}/{remote}"
            for host in ("www.modelscope.cn", "modelscope.cn")]


class ResumableDownloader:
    """Bounded HTTP Range requests, persistent partials, strict final validation."""
    def __init__(self, log=None, opener=None, retries=10, block_bytes=RANGE_BYTES,
                 timeout=60, sleeper=time.sleep, reserve_bytes=16 * 1024 * 1024):
        self.log = log or (lambda message: None)
        self.opener = opener or urllib.request.build_opener()
        self.retries = retries
        self.block_bytes = block_bytes
        self.timeout = timeout
        self.sleep = sleeper
        self.reserve_bytes = reserve_bytes

    def open(self, url, headers):
        # SafeRedirectHandler prevents forwarding private-repo tokens to CDNs.
        return self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)

    def fetch(self, urls, destination, *, size, sha256="", headers=None, identity=""):
        if not urls:
            raise PermanentDownloadError("没有可用下载地址")
        if not isinstance(size, int) or size < 0:
            raise PermanentDownloadError("缺少文件长度，不能安全验证下载结果")
        sha256 = sha256.lower()
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise PermanentDownloadError("SHA256 数据不合法")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(path) + ".part")
        state_path = Path(str(path) + ".part.json")
        verified_path = Path(str(path) + ".verified.json")
        expected = {"identity": identity or urls[0].split("?", 1)[0], "size": size, "sha256": sha256}
        if path.is_file() and path.stat().st_size == size:
            if sha256:
                self.log(f"校验已有文件：{path.name}\n")
            if not sha256 or digest_file(path) == sha256:
                self.log(f"完整性校验通过，跳过下载：{path.name}\n")
                return str(path)
        if path.exists():
            # Preserve unverified existing user data; do not silently overwrite it.
            backup = Path(str(path) + f".unverified-{time.time_ns()}")
            os.replace(path, backup)
            self.log(f"已有文件不完整或校验失败，已保留为：{backup.name}\n")
        if part.exists():
            try:
                saved = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = None
            # A legacy partial is usable when a trusted final hash exists.
            if (saved is not None and saved != expected) or (saved is None and not sha256) or part.stat().st_size > size:
                os.replace(part, Path(str(part) + f".previous-{time.time_ns()}"))
        atomic_json(state_path, expected)
        offset = part.stat().st_size if part.exists() else 0
        remaining = size - offset
        if shutil.disk_usage(path.parent).free < remaining + self.reserve_bytes:
            raise PermanentDownloadError(f"磁盘空间不足，还需约 {remaining / 1024**3:.2f} GB")
        if offset:
            self.log(f"继续下载：{path.name}，从 {offset / 1024**2:.1f} MB 接着传\n")
        else:
            self.log(f"开始下载：{path.name}（{size / 1024**3:.2f} GB）\n")
        request_headers = {"User-Agent": "KritaLauncher/" + PATCH_VERSION,
                           "Accept-Encoding": "identity", **(headers or {})}
        consecutive_errors = 0
        source_index = 0
        last_progress = -1
        permanent_sources = set()
        with open(part, "ab") as stream:
            while offset < size:
                start = offset
                end = min(size - 1, start + self.block_bytes - 1)
                url = urls[source_index % len(urls)]
                try:
                    with self.open(url, {**request_headers, "Range": f"bytes={start}-{end}"}) as response:
                        status = response.status
                        if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                            raise PermanentDownloadError("服务器返回压缩后的字节流，不能安全续传")
                        if status == 206:
                            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                            if not match:
                                raise PermanentDownloadError("服务器缺少有效的 Content-Range")
                            response_start, response_end, response_total = map(int, match.groups())
                            if response_start != start or response_end < start or response_end > end or response_total != size:
                                raise PermanentDownloadError("服务器返回的续传位置或总长度不一致，已保护断点文件")
                            response_size = response_end - start + 1
                        elif status == 200 and start == 0:
                            # Range-less servers can still complete an uninterrupted transfer.
                            response_size = size
                        elif status == 200:
                            raise PermanentDownloadError("该地址不支持断点续传；进度已保留，请切换可续传的下载源或使用离线包")
                        else:
                            raise DownloadError(f"HTTP {status}")
                        length = response.headers.get("Content-Length")
                        if length is not None and int(length) != response_size:
                            raise PermanentDownloadError("响应长度与文件清单不一致")
                        if size > 1024 * 1024 and "text/html" in response.headers.get("Content-Type", "").lower():
                            raise PermanentDownloadError("下载地址返回网页，文件未被当作模型保存")
                        received = 0
                        while received < response_size:
                            piece = response.read(min(64 * 1024, response_size - received))
                            if not piece:
                                raise DownloadError("连接提前关闭")
                            stream.write(piece)
                            offset += len(piece)
                            received += len(piece)
                            progress = int(offset * 100 / size)
                            if progress // 5 > last_progress // 5:
                                self.log(f"[{path.name}] {progress}%（{offset / 1024**2:.1f} / {size / 1024**2:.1f} MB）\n")
                                last_progress = progress
                        stream.flush()
                    consecutive_errors = 0
                except PermanentDownloadError as error:
                    stream.flush()
                    permanent_sources.add(source_index % len(urls))
                    if len(permanent_sources) >= len(urls):
                        raise
                    source_index += 1
                    self.log(f"当前下载源无法使用：{error}；保留进度并切换地址\n")
                except urllib.error.HTTPError as error:
                    stream.flush()
                    code = error.code
                    retry_after = error.headers.get("Retry-After", "")
                    error.close()
                    if code in (401, 403, 404, 410):
                        permanent_sources.add(source_index % len(urls))
                        if len(permanent_sources) >= len(urls):
                            raise PermanentDownloadError(f"下载地址 HTTP {code}；请检查仓库权限、版本和文件是否存在") from None
                    consecutive_errors += 1
                    source_index += 1
                    if consecutive_errors > self.retries:
                        raise DownloadError(f"网络重试已用完，已保存 {offset} 字节；重新点击安装即可继续") from None
                    wait = min(30, 0.5 * 2**min(consecutive_errors - 1, 6))
                    if retry_after.isdigit():
                        wait = max(wait, min(60, int(retry_after)))
                    self.log(f"下载暂时中断（HTTP {code}），保留进度，{wait:g} 秒后重试\n")
                    self.sleep(wait)
                except (OSError, EOFError, http.client.HTTPException, DownloadError) as error:
                    stream.flush()
                    consecutive_errors = 1 if offset > start else consecutive_errors + 1
                    if consecutive_errors > self.retries:
                        raise DownloadError(f"网络重试已用完，已保存 {offset} 字节；重新点击安装即可继续") from error
                    source_index += 1
                    wait = min(15, 0.5 * 2**min(consecutive_errors - 1, 5))
                    self.log(f"连接中断，保留到 {offset / 1024**2:.1f} MB；{wait:g} 秒后从断点重试\n")
                    self.sleep(wait)
        if part.stat().st_size != size:
            raise DownloadError("文件长度不匹配，断点已保留")
        if sha256:
            self.log(f"正在验证 SHA256：{path.name}\n")
            if digest_file(part) != sha256:
                bad = Path(str(part) + f".bad-hash-{time.time_ns()}")
                os.replace(part, bad)
                raise PermanentDownloadError(f"文件校验失败，未标记为完成；已保留异常文件：{bad.name}")
        os.replace(part, path)
        state_path.unlink(missing_ok=True)
        atomic_json(verified_path, {**expected, "mtime_ns": path.stat().st_mtime_ns})
        self.log(f"下载完成，{'SHA256 和长度' if sha256 else '长度'}校验通过：{path.name}\n")
        return str(path)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        redirected = super().redirect_request(request, fp, code, message, headers, new_url)
        if redirected is not None and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(new_url).netloc:
            for key in ("Authorization", "Cookie", "X-Modelscope-Token"):
                redirected.remove_header(key)
        return redirected


def make_opener(ignore_system_proxy=False):
    handlers = [SafeRedirectHandler()]
    if ignore_system_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def ui_log(app, message):
    try:
        app.after(0, app.log_to_console, message)
    except (RuntimeError, AttributeError):
        pass


def list_repo_files(app, repo_id, revision, token=""):
    query = urllib.parse.urlencode({"Revision": revision, "Recursive": "true"})
    url = f"https://www.modelscope.cn/api/v1/models/{urllib.parse.quote(repo_id, safe='/')}/repo/files?{query}"
    opener = make_opener(app.settings.get("download_ignore_system_proxy", False))
    last_error = None
    for attempt in range(6):
        try:
            request = urllib.request.Request(url, headers=app._get_modelscope_headers(token))
            with opener.open(request, timeout=30) as response:
                payload = json.load(response)
            if str(payload.get("Code", 200)) != "200":
                raise PermanentDownloadError("仓库清单读取失败：" + str(payload.get("Message", "未知错误")))
            files = payload.get("Data", {}).get("Files")
            if not isinstance(files, list):
                raise PermanentDownloadError("仓库清单结构不正确")
            metadata = {}
            for item in files:
                if item.get("Type") != "blob":
                    continue
                path = safe_repo_path(item.get("Path", ""))
                if not isinstance(item.get("Size"), int) or item["Size"] < 0:
                    raise PermanentDownloadError(f"仓库缺少文件长度：{path}")
                if item.get("IsLFS") and not re.fullmatch(r"[0-9a-fA-F]{64}", item.get("Sha256", "")):
                    raise PermanentDownloadError(f"仓库缺少大文件 SHA256：{path}")
                metadata[path] = item
            cache = getattr(app, "_repair_repo_metadata", {})
            cache[(repo_id, revision)] = metadata
            app._repair_repo_metadata = cache
            return list(metadata)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403, 404):
                raise PermanentDownloadError(f"仓库 HTTP {code}，请检查权限和仓库地址") from None
            last_error = f"HTTP {code}"
        except (OSError, ValueError) as error:
            last_error = error
        if attempt < 5:
            ui_log(app, "读取清单暂时失败，正在重试……\n")
            time.sleep(min(8, 0.5 * 2**attempt))
    raise DownloadError("无法读取文件清单，未删除任何下载进度：" + str(last_error))


def download_modelscope_file(app, repo_id, revision, remote_path, local_path, token="", minimum_size_bytes=0):
    remote_path = safe_repo_path(remote_path)
    cache = getattr(app, "_repair_repo_metadata", {})
    if (repo_id, revision) not in cache:
        list_repo_files(app, repo_id, revision, token)
        cache = app._repair_repo_metadata
    metadata = cache[(repo_id, revision)].get(remote_path)
    if metadata is None:
        raise PermanentDownloadError(f"文件不在仓库清单内：{remote_path}")
    if metadata["Size"] < minimum_size_bytes:
        raise PermanentDownloadError(f"仓库中文件长度异常：{remote_path}")
    opener = make_opener(app.settings.get("download_ignore_system_proxy", False))
    downloader = ResumableDownloader(log=lambda message: ui_log(app, message), opener=opener)
    return downloader.fetch(file_urls(repo_id, revision, remote_path), local_path,
                            size=metadata["Size"], sha256=metadata.get("Sha256", ""),
                            headers=app._get_modelscope_headers(token),
                            identity=f"{repo_id}@{revision}/{remote_path}")


DIAGNOSTIC_CODE = r'''
import importlib
import importlib.metadata
import json
import pathlib
import re
import sys
info = {'python': sys.version.split()[0], 'packages': {}, 'import_errors': {}, 'kernel_ok': False}
for name in ('torch', 'torchvision', 'torchaudio', 'numpy', 'comfy-aimdo', 'comfy-kitchen', 'transformers'):
    try:
        info['packages'][name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        info['packages'][name] = None
try:
    import torch
    info['torch_version'] = str(torch.__version__)
    info['torch_cuda'] = torch.version.cuda
    info['cuda_available'] = torch.cuda.is_available()
    if info['cuda_available']:
        info['gpu'] = torch.cuda.get_device_name(0)
        info['capability'] = list(torch.cuda.get_device_capability(0))
        info['compiled_arches'] = torch.cuda.get_arch_list()
        value = torch.ones((32, 32), device='cuda')
        value = value @ value
        torch.cuda.synchronize()
        info['kernel_ok'] = float(value[0, 0].item()) == 32.0
    else:
        try:
            torch.cuda.init()
        except Exception as error:
            info['cuda_error'] = str(error)
except Exception as error:
    field = 'cuda_error' if info.get('torch_version') else 'torch_error'
    info[field] = type(error).__name__ + ': ' + str(error)
required = {'torchvision', 'torchaudio', 'aiohttp', 'safetensors', 'yaml', 'transformers', 'numpy', 'PIL'}
for file in (pathlib.Path.cwd() / 'ComfyUI' / 'comfy').rglob('*.py'):
    try:
        text = file.read_text(encoding='utf-8', errors='replace')
    except OSError:
        continue
    if 'comfy_aimdo.host_buffer' in text or re.search(r'from\s+comfy_aimdo\s+import[^\n]*\bhost_buffer\b', text):
        required.add('comfy_aimdo.host_buffer')
    if re.search(r'^\s*(?:from|import)\s+comfy_kitchen\b', text, re.M):
        required.add('comfy_kitchen')
info['required_modules'] = sorted(required)
for name in sorted(required):
    try:
        importlib.import_module(name)
    except Exception as error:
        info['import_errors'][name] = type(error).__name__ + ': ' + str(error)
print('__KRITA_DIAGNOSTIC__=' + json.dumps(info, ensure_ascii=True))
'''


def clean_environment():
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "_PYI_APPLICATION_HOME_DIR"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def driver_information():
    result = {}
    if os.name != "nt":
        return result
    try:
        driver = ctypes.WinDLL("nvcuda.dll")
        value = ctypes.c_int()
        if driver.cuDriverGetVersion(ctypes.byref(value)) == 0:
            result["cuda_api"] = value.value
            result["cuda_max"] = f"{value.value // 1000}.{value.value % 1000 // 10}"
    except (OSError, AttributeError):
        pass
    smi = shutil.which("nvidia-smi")
    if not smi:
        candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        smi = str(candidate) if candidate.is_file() else None
    if smi:
        try:
            process = subprocess.run([smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
                                     capture_output=True, encoding="utf-8", errors="replace", timeout=10,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if process.returncode == 0:
                result["gpus"] = process.stdout.strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def classify_environment(info, driver):
    if info.get("runtime_error"):
        return "python_error", "内置 Python 未能执行检测；请查看报告中的原始错误。"
    if info.get("torch_error"):
        return "torch_import_error", "Torch 导入失败；可能是缺文件、VC++ 运行库或包冲突，不能直接判定为驱动问题。"
    cuda = info.get("torch_cuda")
    if not cuda:
        return "cpu_torch", "当前是 CPU 版 Torch；NVIDIA 本地模式需要匹配的 GPU 环境包。"
    if not info.get("kernel_ok"):
        message = (info.get("cuda_error", "") + info.get("torch_error", "")).lower()
        if "no kernel image" in message or "not compatible" in message:
            return "gpu_architecture", "Torch 中的 GPU 架构与本机显卡不匹配，需要对应显卡的课程环境包。"
        cuda_major = int(str(cuda).split(".", 1)[0])
        api = driver.get("cuda_api", 0)
        if "too old" in message or (api and api // 1000 < cuda_major):
            return "driver_too_old", f"本机驱动支持到 CUDA {driver.get('cuda_max', '未知')}，当前包是 CUDA {cuda}。需要更新适配驱动，或改用兼容环境包。"
        return "cuda_unavailable", "GPU 实际计算测试未通过；请根据报告区分驱动、显卡架构和环境问题。"
    if info.get("requirements_scan_error"):
        return "dependency_error", "依赖版本清单读取失败，请查看报告或从同版下载包恢复 Python 环境。"
    if info.get("import_errors") or info.get("requirements_issues"):
        return "dependency_error", "GPU 计算通过，但存在核心依赖错误；请在“检测 / 修复依赖”中修复普通依赖或从下载包恢复环境。"
    if info.get("pip_check_returncode", 0) != 0:
        return "dependency_conflict", "GPU 计算通过，但 pip 检测到依赖冲突；请查看报告，恢复已测试的课程依赖版本。"
    return "ok", "GPU 计算和核心依赖检测通过。"


def diagnose_environment(root, full=True):
    root = Path(root)
    python = root / "python_embeded" / "python.exe"
    report = {"launcher_version": PATCH_VERSION, "comfy_root": str(root),
              "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "driver": driver_information()}
    if not python.is_file() or not (root / "ComfyUI" / "main.py").is_file():
        report.update(code="environment_missing", message="ComfyUI 环境不完整，请先安装或重新指定便携版目录。")
        return report
    options = dict(cwd=str(root), env=clean_environment(), capture_output=True,
                   encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        from launcher_dependencies import REQUIREMENT_DIAGNOSTIC
        code = DIAGNOSTIC_CODE.replace("print('__KRITA_DIAGNOSTIC__='", REQUIREMENT_DIAGNOSTIC + "\nprint('__KRITA_DIAGNOSTIC__='")
        process = subprocess.run([str(python), "-s", "-c", code], timeout=90, **options)
        report["python_exit_code"] = process.returncode
        report["stderr"] = process.stderr[-12000:]
        lines = [line.split("=", 1)[1] for line in process.stdout.splitlines() if line.startswith("__KRITA_DIAGNOSTIC__=")]
        info = json.loads(lines[-1]) if lines else {"runtime_error": process.stdout[-12000:] or process.stderr[-12000:]}
        if full and lines:
            check = subprocess.run([str(python), "-s", "-m", "pip", "check"], timeout=60, **options)
            info["pip_check_returncode"] = check.returncode
            info["pip_check_output"] = (check.stdout + check.stderr)[-16000:]
        report["environment"] = info
        report["code"], report["message"] = classify_environment(info, report["driver"])
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        report.update(code="diagnostic_error", message="环境检测未完成：" + str(error))
    return report


def present_report(app, report):
    directory = Path(app.base_dir) / "_launcher_diagnostics"
    path = directory / ("environment-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    atomic_json(path, report)
    ui_log(app, "\n--- 环境检测 ---\n" + report["message"] + "\n报告：" + str(path) + "\n")
    environment = report.get("environment", {})
    if environment:
        ui_log(app, f"Python {environment.get('python', '?')} / Torch {environment.get('torch_version', '?')} / CUDA {environment.get('torch_cuda', '?')}\n")
        for name, error in environment.get("import_errors", {}).items():
            ui_log(app, f"{name}: {error}\n")
        if environment.get("cuda_error") or environment.get("torch_error"):
            ui_log(app, str(environment.get("cuda_error") or environment.get("torch_error")) + "\n")
    return str(path)


def inspect_environment_button(app):
    root = app._resolve_comfy_root()
    if not root:
        app.set_status("请先安装或指定 ComfyUI 路径")
        return
    if app.comfy_process:
        app.set_status("请先停止本地服务，再进行完整环境检测")
        return
    if getattr(app, "_repair_diagnostic_busy", False):
        return
    app._repair_diagnostic_busy = True
    app.show_console()
    app.btn_repair_comfy_deps.configure(state="disabled")
    app.set_status("正在检测 GPU 和依赖，请稍候……")

    def worker():
        try:
            report = diagnose_environment(root)
            present_report(app, report)
            app.after(0, app.set_status, report["message"])
        except Exception as error:
            ui_log(app, "检测失败：" + str(error) + "\n")
            app.after(0, app.set_status, "环境检测失败，请查看控制台")
        finally:
            app._repair_diagnostic_busy = False
            app.after(0, lambda: app.btn_repair_comfy_deps.configure(state="normal"))
    threading.Thread(target=worker, daemon=True).start()


def start_checked_process(app):
    def reset():
        app.start_btn.configure(state="normal", text="一键启动")
        app.stop_btn.configure(state="disabled")
    try:
        if getattr(app, "_launcher_task_cancel", None) is not None or getattr(app, "_launcher_install_busy", False):
            raise RuntimeError("正在安装文件或修复依赖，请等待操作结束后再启动 ComfyUI")
        root = app._resolve_comfy_root()
        if not root:
            raise RuntimeError("请先安装或指定 ComfyUI 便携版目录")
        app.comfy_dir = root
        ui_log(app, "正在检测本机 GPU、驱动和核心依赖……\n")
        report = diagnose_environment(root)
        present_report(app, report)
        if report["code"] not in ("ok", "dependency_conflict"):
            raise RuntimeError(report["message"])
        if report["code"] == "dependency_conflict":
            ui_log(app, "pip 检测到版本约束冲突，报告已保存；GPU 和核心导入通过，继续尝试启动并保留实际错误日志。\n")
        startup = subprocess.STARTUPINFO() if os.name == "nt" else None
        if startup:
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        # Run the checked interpreter directly; the stock .bat prints a generic
        # driver warning after every exit, including unrelated import failures.
        command = [str(Path(root) / "python_embeded" / "python.exe"), "-s", "ComfyUI/main.py", "--windows-standalone-build"]
        app.comfy_process = subprocess.Popen(command, cwd=root, env=clean_environment(),
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, startupinfo=startup)
        threading.Thread(target=app.read_process_output, args=(app.comfy_process,), daemon=True).start()
        app.after(0, app.set_status, "等待 ComfyUI 就绪，然后自动打开 Krita……")
    except Exception as error:
        app.comfy_process = None
        ui_log(app, "启动前检测：" + str(error) + "\n")
        app.after(0, app.set_status, str(error))
        app.after(0, reset)


def refuse_blind_torch_repair(app, python_exe, comfy_root):
    raise RuntimeError("已停止自动卸载 Torch 并轮流试装 CUDA。请先运行环境检测，再使用适配显卡和驱动的课程环境包。")


def verify_core_modules(app, python_exe, comfy_root):
    report = diagnose_environment(comfy_root, full=False)
    errors = report.get("environment", {}).get("import_errors", {})
    if report["code"] in ("environment_missing", "diagnostic_error", "python_error"):
        return False, report["message"]
    return not errors, "\n".join(f"{name}: {error}" for name, error in errors.items()) or "当前 ComfyUI 所需核心模块导入通过"


def verify_gpu_compute(app, python_exe, comfy_root):
    report = diagnose_environment(comfy_root, full=False)
    return bool(report.get("environment", {}).get("kernel_ok")), report["message"]


def checked_extract_zip(archive_path, target):
    """Validate paths and disk capacity before extracting a trusted ZIP."""
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        for item in entries:
            name = item.filename.replace("\\", "/")
            if name.rstrip("/"):
                safe_repo_path(name.rstrip("/"))
            candidate = (target / name).resolve()
            try:
                candidate.relative_to(target)
            except ValueError:
                raise PermanentDownloadError("安装包包含超出安装目录的路径") from None
        size = sum(item.file_size for item in entries)
        if shutil.disk_usage(target).free < size + 256 * 1024 * 1024:
            raise PermanentDownloadError(f"解压空间不足，需要约 {size / 1024**3:.1f} GB 空闲空间")
        archive.extractall(target)


def resolve_portable(root):
    root = Path(root)
    for candidate in (root, root / "ComfyUI_windows_portable"):
        if (candidate / "python_embeded" / "python.exe").is_file() and (candidate / "ComfyUI" / "main.py").is_file():
            return candidate
    return None


def install_repository(app, target_dir):
    """Extract a core only into an empty staging folder; preserve models on retry."""
    target = Path(target_dir).resolve()
    try:
        if getattr(app, "_launcher_task_cancel", None) is not None:
            raise RuntimeError("本地文件或依赖正在处理中，请等待进度窗口结束后再在线安装")
        app._launcher_install_busy = True
        repo = app.config_data.get("repo_id", "shiwu000001/comfyui-local-env")
        revision = app.config_data.get("revision", "master")
        core_name = app.config_data.get("core_package", "comfyui_core.zip")
        token, _ = app._get_modelscope_token()
        paths = app._list_modelscope_repo_files(repo, revision, token)
        if core_name not in paths:
            raise PermanentDownloadError(f"仓库缺少 {core_name}，请使用已测试的课程核心包")
        root = resolve_portable(target)
        if root is None:
            core = target / "_launcher_downloads" / core_name
            app._download_modelscope_file(repo, revision, core_name, str(core), token=token, minimum_size_bytes=100 * 1024 * 1024)
            staging = target / ("_launcher_install_" + str(time.time_ns()))
            ui_log(app, "下载已校验，开始解压核心环境……\n")
            checked_extract_zip(core, staging)
            unpacked = resolve_portable(staging)
            if unpacked is None:
                raise PermanentDownloadError("核心包没有完整的 ComfyUI 和内置 Python；暂存文件已保留")
            final = target / "ComfyUI_windows_portable"
            if final.exists():
                raise PermanentDownloadError("目标中已有不完整环境，为保护现有文件，请选一个新安装目录")
            os.replace(unpacked, final)
            if staging.exists() and not any(staging.iterdir()):
                staging.rmdir()
            root = final
        app.comfy_dir = str(root)
        app.settings["comfy_dir"] = str(root)
        app.settings["last_install_dir"] = str(target)
        app.save_settings()
        models = [p for p in paths if p.startswith("models/") and not Path(p).name.startswith("put_")]
        ui_log(app, f"核心环境已就绪；开始同步 {len(models)} 个模型文件。失败后选择同一安装目录可继续。\n")
        for remote_path in models:
            app._download_modelscope_file(repo, revision, remote_path, str(root / "ComfyUI" / remote_path), token=token)
        ui_log(app, "\n模型完整性校验完成，开始检测 GPU 和依赖……\n")
        report = diagnose_environment(root)
        present_report(app, report)
        app.after(0, app.set_status, "安装文件已完整，" + report["message"])
    except Exception as error:
        ui_log(app, "\n安装尚未完成：" + str(error) + "\n已下载的文件和断点均保留。\n")
        app.after(0, app.set_status, "安装尚未完成，请查看控制台；再次安装可续传")
    finally:
        app._launcher_install_busy = False
        app.after(0, lambda: app.btn_download_ms.configure(state="normal", text="1. 一键安装本地版 ComfyUI 环境 (ModelScope)"))


def import_offline_core(app):
    from tkinter import filedialog
    archive_path = filedialog.askopenfilename(title="选择教师提供的 comfyui_core.zip", filetypes=[("课程环境包", "*.zip")])
    if not archive_path:
        return
    destination = filedialog.askdirectory(title="选择新的空目录，导入离线核心环境")
    if not destination:
        return
    destination = Path(destination).resolve()
    if any(destination.iterdir()):
        app.set_status("请选择空目录，避免覆盖已有环境")
        return
    app.show_console()
    app.btn_offline_core.configure(state="disabled")

    def worker():
        try:
            # Published package metadata is embedded at build time for offline validation.
            manifest_path = Path(__file__).with_name("course-core.json")
            if not manifest_path.exists() and hasattr(__import__("sys"), "_MEIPASS"):
                manifest_path = Path(__import__("sys")._MEIPASS) / "course-core.json"
            manifest = COURSE_CORE or json.loads(manifest_path.read_text(encoding="utf-8"))
            if Path(archive_path).stat().st_size != manifest["size"] or digest_file(archive_path) != manifest["sha256"]:
                raise PermanentDownloadError("离线包与本版登记的课程核心包不一致，未解压。请使用配套包或重新生成登记清单。")
            checked_extract_zip(archive_path, destination)
            root = resolve_portable(destination)
            if root is None:
                raise PermanentDownloadError("包中缺少内置 Python 或 ComfyUI 主程序")
            app.comfy_dir = str(root)
            app.settings["comfy_dir"] = str(root)
            app.save_settings()
            report = diagnose_environment(root)
            present_report(app, report)
            ui_log(app, "离线核心包已导入。模型可以从教师的 models 文件夹复制到 ComfyUI/models，或稍后联网补齐。\n")
            app.after(0, app.set_status, "离线核心包已导入；" + report["message"])
        except Exception as error:
            ui_log(app, "离线导入未完成：" + str(error) + "\n")
            app.after(0, app.set_status, "离线导入未完成，请查看控制台")
        finally:
            app.after(0, lambda: app.btn_offline_core.configure(state="normal"))
    threading.Thread(target=worker, daemon=True).start()


def apply_patch(module):
    """Patch in memory before the existing LauncherApp is constructed."""
    app = module.LauncherApp
    original_build = app.build_ui

    def build_ui(self):
        original_build(self)
        from launcher_local import open_manual_download
        self.btn_repair_comfy_deps.configure(text="2. 检测 / 修复依赖（显示进度）", command=self.repair_comfy_dependencies)
        self.btn_manual_download = module.ctk.CTkButton(self.btn_download_ms.master, text="手动下载",
                                                       command=lambda: open_manual_download(self, module), height=40)
        self.btn_manual_download.pack(fill="x", padx=20, pady=(8, 12))
    app.build_ui = build_ui
    app._list_modelscope_repo_files = list_repo_files
    app._download_modelscope_file = download_modelscope_file
    app.build_modelscope_file_urls = lambda self, repo, revision, path: file_urls(repo, revision, path)
    from launcher_dependencies import open_dependency_tools
    app.repair_comfy_dependencies = lambda self: open_dependency_tools(self, module)
    app._repair_torch_cuda_environment = refuse_blind_torch_repair
    app._repair_comfy_dependencies_thread = lambda self, *args: open_dependency_tools(self, module)
    app._verify_comfy_core_dependency = verify_core_modules
    app._verify_torch_cuda = verify_gpu_compute
    app._prompt_repair_missing_core_dependency = lambda self: ui_log(self, "检测到核心依赖错误，请使用环境检测导出报告并恢复同版课程环境。\n")
    app._prompt_repair_torch_cuda_environment = lambda self: ui_log(self, "检测到 GPU 运行错误，请查看环境报告；不会自动卸载 Torch。\n")
    app._download_ms_thread_requests = install_repository
    app._start_process = start_checked_process
    app._patch_krita_plugin = lambda self: None
    def close_preview(self):
        cancel = getattr(self, "_launcher_task_cancel", None)
        if cancel is not None:
            cancel.set()
            self.set_status("正在停止文件处理，请等待进度窗口结束后关闭")
            return
        self._close_cloud_tunnel()
        if self.comfy_process is not None:
            try:
                self._kill_process_tree(self.comfy_process)
            except Exception:
                pass
        self.destroy()
    app.on_closing = close_preview
    # Standalone preview must not replace the user's existing desktop shortcut.
    module.create_or_update_desktop_shortcut = lambda target: None
    module.APP_VERSION = PATCH_VERSION
    from launcher_updates import patch_update_methods
    patch_update_methods(module)
    return app
