"""Visible, cancellable domestic-mirror repairs staged outside the base packages."""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import zipfile

from launcher_local import LocalInstallError, TaskCancelled, check_cancel, emit, sha_file, task_window, restore_python

PYPI_MIRRORS = [("清华源", "https://pypi.tuna.tsinghua.edu.cn/simple"),
                ("阿里云源", "https://mirrors.aliyun.com/pypi/simple/")]
BASELINE_PACKAGES = None  # Filled by rebuild.py from the inspected course package.
PROTECTED = {"torch", "torchvision", "torchaudio", "xformers", "triton", "triton-windows",
             "comfy-aimdo", "comfy-kitchen"}
IMPORT_TO_PACKAGE = {"PIL": "pillow", "yaml": "pyyaml", "cv2": "opencv-python", "skimage": "scikit-image",
                     "sklearn": "scikit-learn", "numpy": "numpy", "safetensors": "safetensors",
                     "aiohttp": "aiohttp", "transformers": "transformers", "requests": "requests",
                     "packaging": "packaging", "einops": "einops", "tqdm": "tqdm",
                     "huggingface_hub": "huggingface-hub", "tokenizers": "tokenizers",
                     "scipy": "scipy", "sentencepiece": "sentencepiece"}

REQUIREMENT_DIAGNOSTIC = r'''
info['installed'] = {}
for distribution in importlib.metadata.distributions():
    name = re.sub(r'[-_.]+', '-', distribution.metadata.get('Name', '')).lower()
    if name and name not in info['installed']:
        info['installed'][name] = distribution.version
info['requirements_issues'] = []
try:
    from pip._vendor.packaging.requirements import Requirement
    required_file = pathlib.Path.cwd() / 'ComfyUI' / 'requirements.txt'
    if required_file.is_file():
        for line in required_file.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            item = Requirement(line)
            if item.marker and not item.marker.evaluate():
                continue
            name = re.sub(r'[-_.]+', '-', item.name).lower()
            version = info['installed'].get(name)
            if version is None or not item.specifier.contains(version, prereleases=True):
                info['requirements_issues'].append({'name': name, 'requirement': str(item), 'installed': version})
except Exception as error:
    info['requirements_scan_error'] = str(error)
'''


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def protected(name):
    name = normalized(name)
    return name in PROTECTED or name.startswith(("nvidia-", "cuda-", "pytorch-"))


def baseline_packages():
    return BASELINE_PACKAGES or json.loads(Path(__file__).with_name("course-packages.json").read_text(encoding="utf-8"))


def repair_requests(report, baseline=None):
    """Repair only detected course dependencies and pin every selected version."""
    baseline = baseline if baseline is not None else baseline_packages()
    environment = report.get("environment", {})
    if not environment or environment.get("runtime_error") or environment.get("requirements_scan_error"):
        raise LocalInstallError("无法读取内置 Python 的依赖清单，请使用“从下载包恢复 Python 环境”")
    if report.get("code") in ("torch_import_error", "cpu_torch", "gpu_architecture", "cuda_unavailable", "driver_too_old"):
        raise LocalInstallError(report["message"] + "\n普通 PyPI 镜像不能替代 CUDA 专用包。Torch 缺失或损坏时可从下载包恢复；驱动/显卡问题需使用兼容环境。")
    installed = environment.get("installed", {})
    names = set()
    for item in environment.get("requirements_issues", []):
        name = normalized(item["name"])
        if protected(name):
            raise LocalInstallError("GPU 相关依赖需要配套恢复：" + name + "。请使用“从下载包恢复 Python 环境”")
        if item.get("installed") is not None:
            raise LocalInstallError(f"{name} 的已安装版本与当前 ComfyUI 要求不一致，请使用配套包恢复，避免升级破坏其他节点")
        names.add(name)
    for module, error in environment.get("import_errors", {}).items():
        if module.startswith(("torch", "comfy_")):
            raise LocalInstallError("GPU 或核心扩展导入失败，请用同版下载包恢复 Python 环境")
        package = IMPORT_TO_PACKAGE.get(module.split(".", 1)[0])
        if package is None:
            raise LocalInstallError("暂不能自动判断该模块对应的包：" + module + "；请查看环境报告")
        names.add(package)
        missing = re.search(r"No module named ['\"]([^'\"]+)", error)
        if missing:
            missing_name = IMPORT_TO_PACKAGE.get(missing[1].split(".", 1)[0])
            if missing_name:
                names.add(missing_name)
    requests = []
    for name in sorted(names):
        name = normalized(name)
        if protected(name):
            raise LocalInstallError("自动修复不会下载或替换 GPU 包：" + name)
        version = installed.get(name) or baseline.get(name)
        if not version or not re.fullmatch(r"[0-9A-Za-z.!+_-]+", version):
            raise LocalInstallError("该依赖没有已验证的课程版本：" + name + "，请使用同版课程包恢复")
        requests.append(name + "==" + version)
    return requests


def pip_environment():
    from launcher_repair import clean_environment
    environment = clean_environment()
    for key in list(environment):
        if key.startswith("PIP_"):
            environment.pop(key, None)
    # Apply these sources to this operation only, not the student's global pip.
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1",
                       PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return environment


def parse_progress(line):
    match = re.fullmatch(r"\s*Progress\s+(\d+)\s+of\s+(\d+|None)\s*", line, re.I)
    if match:
        return int(match[1]), int(match[2]) if match[2].isdigit() and int(match[2]) > 0 else None
    return None


def run_visible(command, cwd, phase, progress, cancel=None, log=None, timeout=900):
    """Drain pip stdout continuously; raw progress works without an attached TTY."""
    log = log or (lambda message: None)
    log("\n" + phase + "\n")
    process = subprocess.Popen(command, cwd=str(cwd), env=pip_environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace", bufsize=1,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = queue.Queue()
    def reader():
        try:
            for line in process.stdout:
                output.put(line.rstrip("\r\n"))
        finally:
            output.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    began, ended, lines, detail = time.monotonic(), False, [], "正在连接下载源……"
    emit(progress, phase, detail=detail)
    try:
        while not ended or process.poll() is None:
            check_cancel(cancel)
            if time.monotonic() - began > timeout:
                raise LocalInstallError("该步骤超过限定时间，已停止；现有 Python 未卸载，下载缓存已保留")
            try:
                line = output.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                ended = True
                continue
            parsed = parse_progress(line)
            if parsed:
                emit(progress, phase, parsed[0], parsed[1], detail)
            else:
                lines.append(line)
                lines = lines[-500:]
                log(line + "\n")
                if line.strip():
                    detail = line.strip()
                    emit(progress, phase, detail=detail)
        return process.wait(), "\n".join(lines)
    except Exception:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        raise
    finally:
        process.stdout.close()


def download_commands(python, directory, requests):
    return [(name, [str(python), "-s", "-u", "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
                    "--disable-pip-version-check", "--no-input", "--progress-bar", "raw", "--retries", "2",
                    "--timeout", "20", "--dest", str(directory), "--index-url", mirror, *requests])
            for name, mirror in PYPI_MIRRORS]


def inspect_wheels(directory, requests):
    expected = dict(request.split("==", 1) for request in requests)
    found = {}
    for wheel in Path(directory).glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise LocalInstallError("依赖包元数据不完整：" + wheel.name)
            text = archive.read(metadata_names[0]).decode("utf-8")
            name = re.search(r"^Name: (.+)$", text, re.M)
            version = re.search(r"^Version: (.+)$", text, re.M)
            if not name or not version:
                raise LocalInstallError("依赖包缺少版本：" + wheel.name)
            name, version = normalized(name[1].strip()), version[1].strip()
            if protected(name) or name not in expected or expected[name] != version or name in found:
                raise LocalInstallError("依赖包与待修复清单不一致：" + wheel.name)
            if archive.testzip() is not None:
                raise LocalInstallError("依赖包下载损坏：" + wheel.name)
            found[name] = wheel
    if set(found) != set(expected):
        raise LocalInstallError("依赖包尚未全部下载完成，现有环境未修改")
    return list(found.values())


def repair_ordinary_dependencies(root, progress=None, cancel=None, log=None):
    from launcher_repair import diagnose_environment, atomic_json
    root = Path(root).resolve()
    log = log or (lambda message: None)
    emit(progress, "1/4 检测当前环境", detail="检查 GPU、导入和版本要求；可能需要一两分钟")
    before = diagnose_environment(root)
    check_cancel(cancel)
    requests = repair_requests(before)
    if not requests:
        return {"message": "没有检测到可自动修复的普通核心依赖；环境报告已保存。", "report": before}
    emit(progress, "2/4 准备国内源下载", detail="、".join(requests))
    python = root / "python_embeded/python.exe"
    site = root / "python_embeded/Lib/site-packages"
    # Portable Python must enable site initialization for the repair overlay.
    pth_configs = list(python.parent.glob("python*._pth"))
    if pth_configs and not any(re.search(r"(?m)^\s*import site\s*$", path.read_text(encoding="utf-8")) for path in pth_configs):
        raise LocalInstallError("此内置 Python 未启用 site，无法安全加载修复包；请从下载包恢复")
    transaction = str(time.time_ns())
    work = root / "_launcher_dependency_repairs" / transaction
    cache = root / "_launcher_wheel_cache" / __import__("hashlib").sha256("\n".join(requests).encode()).hexdigest()[:16]
    cache.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    atomic_json(work / "before.json", before)
    atomic_json(work / "requests.json", requests)
    final_error = ""
    for name, command in download_commands(python, cache, requests):
        code, output = run_visible(command, root, "2/4 下载依赖（" + name + "）", progress, cancel, log)
        if code == 0:
            break
        final_error = output[-2200:]
    else:
        raise LocalInstallError("国内源下载未完成，旧依赖保持原样。可重试，或从下载包恢复。\n" + final_error)
    wheels = inspect_wheels(cache, requests)
    atomic_json(work / "wheels.json", [{"name": path.name, "sha256": sha_file(path, cancel)} for path in wheels])
    check_cancel(cancel)
    stage = python.parent / "_launcher_packages" / ("repair_" + transaction)
    command = [str(python), "-s", "-u", "-m", "pip", "install", "--no-deps", "--no-index", "--no-compile",
               "--no-warn-script-location", "--target", str(stage), *map(str, wheels)]
    code, output = run_visible(command, root, "3/4 安装修复包到独立目录", progress, cancel, log)
    if code:
        raise LocalInstallError("修复包未安装完成，原 Python 依赖未修改：\n" + output[-1800:])
    check_cancel(cancel)
    activate = site / "zz_drowm_repair.pth"
    previous = activate.read_bytes() if activate.exists() else None
    work.joinpath("previous-pth.txt").write_bytes(previous or b"")
    line = '\nimport os,sys; sys.path.insert(0, os.path.join(os.path.dirname(sys.executable), "_launcher_packages", "repair_' + transaction + '"))\n'
    temporary = activate.with_name(activate.name + ".tmp")
    temporary.write_bytes((previous or b"") + line.encode("utf-8"))
    os.replace(temporary, activate)
    try:
        emit(progress, "4/4 检查修复结果", detail="重新检查实际导入和 GPU，未通过会撤回本次修复")
        after = diagnose_environment(root)
        atomic_json(work / "after.json", after)
        check_cancel(cancel)
        if after["code"] not in ("ok", "dependency_conflict") or after.get("environment", {}).get("requirements_issues"):
            raise LocalInstallError("修复后仍有依赖错误，本次修复已撤回。请查看报告或从下载包恢复完整 Python 环境")
        return {"message": f"已从国内源修复 {len(requests)} 个普通依赖，GPU 和核心模块复查通过。", "report": after}
    except Exception:
        if previous is None:
            activate.unlink(missing_ok=True)
        else:
            temporary.write_bytes(previous)
            os.replace(temporary, activate)
        raise


def open_dependency_tools(app, module):
    from tkinter import filedialog
    from launcher_repair import diagnose_environment, present_report, ui_log
    root = app._resolve_comfy_root()
    if not root:
        module.msgbox.showinfo("请先指定路径", "请先安装或指定 ComfyUI 便携版目录。", parent=app)
        return
    if getattr(app, "comfy_process", None):
        module.msgbox.showinfo("请先停止服务", "请先停止 ComfyUI，再检测或修复依赖。", parent=app)
        return
    window = module.ctk.CTkToplevel(app)
    window.title("检测与修复依赖")
    window.geometry("640x420")
    window.transient(app)
    module.ctk.CTkLabel(window, text="检测与修复依赖", font=("Microsoft YaHei", 20)).pack(pady=(24, 12))
    module.ctk.CTkLabel(window, text="普通依赖使用国内源，下载完成后再启用修复包。\nTorch 缺失或损坏时，可直接从下载包恢复完整 Python 环境。", wraplength=580).pack(pady=8)
    def completed(result, detail):
        if result.get("report"):
            present_report(app, result["report"])
        detail.configure(text=result["message"])
        app.set_status(result["message"])
        ui_log(app, result["message"] + "\n")
    def diagnose():
        window.destroy()
        def worker(progress, cancel):
            emit(progress, "检查 GPU 和依赖", detail="检查实际运行情况，请稍候……")
            report = diagnose_environment(root)
            check_cancel(cancel)
            return {"message": report["message"], "report": report}
        task_window(app, module, "检测环境", worker, completed)
    def repair():
        window.destroy()
        task_window(app, module, "国内源修复普通依赖", lambda progress, cancel: repair_ordinary_dependencies(
            root, progress, cancel, lambda text: ui_log(app, text)), completed)
    def restore():
        core = filedialog.askopenfilename(parent=window, title="选择网盘下载好的 comfyui_core.zip", filetypes=[("ComfyUI 核心压缩包", "*.zip")],
                                          initialdir=app.settings.get("manual_download_dir") or app.base_dir)
        if not core:
            return
        window.destroy()
        def worker(progress, cancel):
            result = restore_python(core, root, progress, cancel)
            emit(progress, "Python 已恢复，检查实际运行环境", detail="模型和工作流保持原位置；旧 Python 已备份")
            report = diagnose_environment(root)
            result.update(report=report, message="内置 Python 已恢复；" + report["message"])
            return result
        task_window(app, module, "从下载包恢复 Python 环境", worker, completed)
    for text, command in (("检测环境 / 导出报告", diagnose), ("国内源修复普通依赖", repair), ("从下载包恢复 Python 环境（含 Torch）", restore)):
        module.ctk.CTkButton(window, text=text, height=42, width=500, command=command).pack(pady=8)
    module.ctk.CTkLabel(window, text="每一步都显示状态和耗时；下载有进度，等待响应有提示，可停止。", wraplength=580).pack(pady=10)
    window.after(100, window.lift)
