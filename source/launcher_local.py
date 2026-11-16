"""Baidu manual-download UI, portable ZIP installation and safe model placement."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
import webbrowser
import zipfile

SHARE_URL = "https://pan.baidu.com/s/11QUPcDYLSrRuY9ELTZzgcg?pwd=n35a"
SHARE_CODE = "n35a"
CHUNK = 2 * 1024 * 1024


class LocalInstallError(RuntimeError):
    pass


class TaskCancelled(LocalInstallError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise TaskCancelled("操作已停止，源文件和已完成的文件已保留，可以再次安装继续")


def emit(progress, phase, done=None, total=None, detail=""):
    if progress:
        progress(phase, done, total, detail)


def safe_relative(value):
    value = str(value).replace("\\", "/").rstrip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ":" in value or ".." in path.parts:
        raise LocalInstallError("文件路径不合法：" + value)
    for part in path.parts:
        if part.endswith((".", " ")) or re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise LocalInstallError("Windows 不支持该文件路径：" + value)
    return path


def assert_plain_path(path):
    """Reject existing junctions and symlinks in either source or destination."""
    path = Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LocalInstallError("目录包含链接或联接点，请选择普通文件夹：" + str(ancestor))


def sha_file(path, cancel=None, progress=None):
    digest = hashlib.sha256()
    size, count = Path(path).stat().st_size, 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            check_cancel(cancel)
            digest.update(chunk)
            count += len(chunk)
            emit(progress, "校验文件", count, size, Path(path).name)
    return digest.hexdigest()


def portable_root(directory):
    directory = Path(directory)
    candidates = [directory, directory / "ComfyUI_windows_portable"]
    if directory.name.lower() == "comfyui":
        candidates.append(directory.parent)
    return next((path for path in candidates if (path / "ComfyUI/main.py").is_file()
                 and (path / "python_embeded/python.exe").is_file()), None)


def zip_members(archive):
    seen, files = set(), []
    for item in archive.infolist():
        if not item.filename.rstrip("/\\"):
            continue
        relative = safe_relative(item.filename)
        normalized = str(relative).casefold()
        if normalized in seen:
            raise LocalInstallError("压缩包含有重复路径：" + item.filename)
        seen.add(normalized)
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise LocalInstallError("压缩包含有符号链接")
        if item.flag_bits & 1:
            raise LocalInstallError("暂不支持带密码的 ZIP，请先解压到普通文件夹")
        if not item.is_dir():
            files.append((item, relative))
    names = {str(relative).casefold() for _, relative in files}
    for name in names:
        if any(str(parent).casefold() in names for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise LocalInstallError("压缩包中的文件和目录路径冲突")
    return files


def core_layout(archive):
    files = zip_members(archive)
    names = {str(relative): item for item, relative in files}
    roots = []
    suffix = "ComfyUI/main.py"
    for name in names:
        if name == suffix or name.endswith("/" + suffix):
            prefix = name[:-len(suffix)]
            if prefix + "python_embeded/python.exe" in names:
                roots.append(prefix)
    if len(roots) != 1:
        raise LocalInstallError("压缩包中应包含一套完整的 ComfyUI 和 python_embeded")
    return roots[0], files


def extract_zip(source, target, progress=None, cancel=None, only_python=False):
    target = Path(target)
    assert_plain_path(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        prefix, files = core_layout(archive)
        selected = []
        for item, relative in files:
            name = str(relative)
            if not name.startswith(prefix):
                continue
            name = name[len(prefix):]
            if only_python and not name.startswith("python_embeded/"):
                continue
            if name:
                selected.append((item, safe_relative(name)))
        size = sum(item.file_size for item, _ in selected)
        if shutil.disk_usage(target).free < size + 256 * 1024**2:
            raise LocalInstallError(f"解压空间不足，需要至少 {size / 1024**3:.1f} GB")
        count = 0
        for item, relative in selected:
            check_cancel(cancel)
            destination = target.joinpath(*relative.parts)
            assert_plain_path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise LocalInstallError("解压目标已有文件，请选择新的安装文件夹")
            with archive.open(item) as stream, destination.open("xb") as output:
                while True:
                    check_cancel(cancel)
                    chunk = stream.read(CHUNK)  # ZipExtFile verifies the CRC at EOF.
                    if not chunk:
                        break
                    output.write(chunk)
                    count += len(chunk)
                    emit(progress, "解压 Python 环境" if only_python else "解压 ComfyUI", count, size, str(relative))
    return target


def find_downloads(directory):
    directory = Path(directory).resolve()
    assert_plain_path(directory)
    if not directory.is_dir():
        raise LocalInstallError("请选择下载好的“云端备份下载”文件夹")
    candidates = [directory]
    for _ in range(2):
        next_level = []
        for parent in candidates[-200:]:
            try:
                next_level.extend(path for path in parent.iterdir() if path.is_dir() and path.name.lower() != "models")
            except OSError:
                continue
        candidates.extend(next_level[:200])
    cores = sorted({path / "comfyui_core.zip" for path in candidates if (path / "comfyui_core.zip").is_file()})
    if len(cores) != 1:
        raise LocalInstallError("没有找到唯一的 comfyui_core.zip。请选同时包含 comfyui_core.zip 和 models 的文件夹")
    core = cores[0]
    models = core.parent / "models"
    if not models.is_dir():
        raise LocalInstallError("没有找到 models 文件夹，请把网盘中的 models 整个下载，保持里面的分类目录")
    return core, models


def list_models(models):
    models = Path(models)
    assert_plain_path(models)
    files = []
    for parent, dirs, names in os.walk(models, followlinks=False):
        for directory in dirs:
            assert_plain_path(Path(parent) / directory)
        for name in names:
            path = Path(parent) / name
            assert_plain_path(path)
            relative = path.relative_to(models)
            safe_relative(relative.as_posix())
            if name.lower().endswith((".part", ".tmp", ".baiduyun", ".aria2", ".downloading", ".crdownload")):
                raise LocalInstallError("模型还没有下载完成：" + name)
            if len(relative.parts) < 2 and path.suffix.lower() in (".safetensors", ".ckpt", ".pt", ".pth", ".bin"):
                raise LocalInstallError("模型分类目录已丢失，无法根据文件后缀区分 checkpoint、LoRA 和 VAE；请完整下载 models 文件夹")
            if path.suffix.lower() in (".safetensors", ".ckpt", ".pt", ".pth", ".bin") and path.stat().st_size == 0:
                raise LocalInstallError("模型为空文件：" + str(relative))
            files.append((path, relative))
    if not files:
        raise LocalInstallError("models 文件夹为空，请先完成网盘下载")
    return files


def copy_models(source, destination, progress=None, cancel=None):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        return {"copied": 0, "skipped": len(list_models(source)), "conflicts": []}
    files = list_models(source)
    assert_plain_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    # Check required copy capacity before making changes.
    capacity = sum(path.stat().st_size for path, relative in files if not (destination / relative).exists())
    if shutil.disk_usage(destination).free < capacity + 256 * 1024**2:
        raise LocalInstallError(f"复制模型空间不足，还需约 {capacity / 1024**3:.1f} GB")
    result = {"copied": 0, "skipped": 0, "conflicts": []}
    for index, (path, relative) in enumerate(files, 1):
        check_cancel(cancel)
        target = destination / relative
        assert_plain_path(target)
        size = path.stat().st_size
        phase = f"复制模型 {index}/{len(files)}"
        if target.exists():
            if target.is_file() and target.stat().st_size == size and sha_file(path, cancel, progress) == sha_file(target, cancel, progress):
                result["skipped"] += 1
                continue
            result["conflicts"].append(str(relative))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".launcher-copy.part")
        assert_plain_path(temporary)
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as stream, temporary.open("wb") as output:
            for chunk in iter(lambda: stream.read(CHUNK), b""):
                check_cancel(cancel)
                output.write(chunk)
                digest.update(chunk)
                count += len(chunk)
                emit(progress, phase, count, size, str(relative))
        if count != size or sha_file(temporary, cancel, progress) != digest.hexdigest():
            raise LocalInstallError("模型复制校验失败：" + str(relative))
        check_cancel(cancel)
        if target.exists():
            result["conflicts"].append(str(relative))
            continue
        # Windows rename refuses to replace an existing target.
        temporary.rename(target)
        result["copied"] += 1
    return result


def install_downloads(download_directory, target_directory, progress=None, cancel=None):
    from launcher_repair import atomic_json
    core, models = find_downloads(download_directory)
    list_models(models)  # Verify classification before extracting gigabytes.
    target = Path(target_directory).resolve()
    assert_plain_path(target)
    if target == core.parent or core.parent in target.parents or target in core.parents:
        raise LocalInstallError("安装目录与下载目录请分开放置，避免重复复制")
    target.mkdir(parents=True, exist_ok=True)
    root = portable_root(target)
    if root is None:
        final = target / "ComfyUI_windows_portable"
        if final.exists():
            raise LocalInstallError("安装目录中已有不完整环境，请选择新目录；之前的暂存文件已保留")
        staging = target / ("_manual_install_" + str(time.time_ns()))
        extract_zip(core, staging, progress, cancel)
        check_cancel(cancel)
        if portable_root(staging) is None:
            raise LocalInstallError("解压后未找到 ComfyUI 主程序或内置 Python")
        staging.rename(final)
        root = final
    result = copy_models(models, root / "ComfyUI/models", progress, cancel)
    result.update(comfy_root=str(root), source=str(core.parent))
    atomic_json(root / "manual-install-report.json", result)
    if result["conflicts"]:
        raise LocalInstallError("同名文件内容不同，已保留现有文件，安装未标记完成：\n" + "\n".join(result["conflicts"][:12]))
    return result


def restore_python(core, root, progress=None, cancel=None):
    """Restore the matched package's entire runtime, leaving ComfyUI/models alone."""
    root = Path(root).resolve()
    core = Path(core).resolve()
    assert_plain_path(root)
    if not (root / "ComfyUI/main.py").is_file():
        raise LocalInstallError("请先指定现有 ComfyUI 便携版目录")
    with zipfile.ZipFile(core) as archive:
        prefix, files = core_layout(archive)
        names = {str(relative): item for item, relative in files}
        for relative in ("ComfyUI/requirements.txt", "ComfyUI/main.py"):
            local = root / relative
            item = names.get(prefix + relative)
            if not local.is_file() or item is None or local.read_bytes().replace(b"\r\n", b"\n") != archive.read(item).replace(b"\r\n", b"\n"):
                raise LocalInstallError("现有 ComfyUI 与压缩包版本不同，请使用“安装下载好的文件”在新目录安装整套包")
    token = str(time.time_ns())
    staging = root / ("_python_restore_" + token)
    extract_zip(core, staging, progress, cancel, only_python=True)
    check_cancel(cancel)
    current = root / "python_embeded"
    backup = root / ("python_embeded.backup-" + token)
    if current.exists():
        current.rename(backup)  # Fails cleanly if another Python process still uses it.
    try:
        (staging / "python_embeded").rename(current)
    except Exception:
        if backup.exists() and not current.exists():
            backup.rename(current)
        raise
    if not any(staging.iterdir()):
        staging.rmdir()
    emit(progress, "内置 Python 已恢复", 1, 1, "模型、工作流和旧 Python 备份已保留")
    return {"comfy_root": str(root), "backup": str(backup) if backup.exists() else ""}


def task_window(app, module, title, worker, complete=None):
    """Show honest per-file progress and a heartbeat when total work is unknown."""
    if getattr(app, "_launcher_task_cancel", None) is not None:
        module.msgbox.showinfo("操作进行中", "请等待当前操作结束，或在进度窗口点击停止。", parent=app)
        return
    if getattr(app, "_launcher_install_busy", False):
        module.msgbox.showinfo("安装进行中", "请等待当前在线安装结束，再处理本地文件或修复依赖。", parent=app)
        return
    window = module.ctk.CTkToplevel(app)
    window.title(title)
    window.geometry("580x300")
    window.transient(app)
    window.resizable(False, False)
    label = module.ctk.CTkLabel(window, text=title, font=("Microsoft YaHei", 18), wraplength=530)
    label.pack(padx=22, pady=(22, 10))
    detail = module.ctk.CTkLabel(window, text="准备中……", wraplength=530, height=52)
    detail.pack(padx=22, fill="x")
    bar = module.ctk.CTkProgressBar(window, width=530, mode="indeterminate")
    bar.pack(padx=22, pady=12)
    bar.start()
    status = module.ctk.CTkLabel(window, text="已用时 0 秒")
    status.pack()
    cancel = threading.Event()
    app._launcher_task_cancel = cancel
    app._launcher_task_window = window
    began = time.monotonic()
    current = {"phase": title, "done": None, "total": None, "detail": "准备中……", "changed": began, "speed": 0}
    lock = threading.Lock()
    finished = threading.Event()
    def progress(phase, done=None, total=None, message=""):
        with lock:
            now = time.monotonic()
            same_file = current["phase"] == phase and current["detail"] == message
            rate = current["speed"] if same_file else 0
            if same_file and done is not None and current["done"] is not None and done >= current["done"] and now > current["changed"]:
                sample = (done - current["done"]) / (now - current["changed"])
                rate = 0.8 * rate + 0.2 * sample if rate else sample
            current.update(phase=phase, done=done, total=total, detail=message, changed=now, speed=rate)
    def stop():
        cancel.set()
        cancel_button.configure(state="disabled", text="正在停止……")
    cancel_button = module.ctk.CTkButton(window, text="停止", command=stop)
    cancel_button.pack(pady=(12, 18))
    window.protocol("WM_DELETE_WINDOW", stop)
    app.show_console()
    def tick():
        if finished.is_set():
            return
        with lock:
            value = dict(current)
        elapsed = int(time.monotonic() - began)
        idle = int(time.monotonic() - value["changed"])
        waiting = f"　等待响应 {idle} 秒" if idle >= 10 else ""
        label.configure(text=value["phase"])
        detail.configure(text=value["detail"][-220:])
        if value["total"] and value["done"] is not None:
            bar.stop()
            bar.configure(mode="determinate")
            bar.set(min(1, value["done"] / value["total"]))
            amount = f"{value['done'] / 1024**2:.1f} / {value['total'] / 1024**2:.1f} MB"
            if value["total"] <= 1:
                amount = "已完成"
            speed = f"　{value['speed'] / 1024**2:.1f} MB/s" if value["speed"] and idle < 10 else ""
            status.configure(text=f"{amount}{speed}　已用时 {elapsed} 秒{waiting}")
        else:
            bar.configure(mode="indeterminate")
            bar.start()
            status.configure(text=f"已用时 {elapsed} 秒{waiting}")
        app.set_status(value["phase"] + f"（{elapsed} 秒）")
        app.after(350, tick)
    def finish(result=None, error=None):
        from launcher_repair import ui_log
        app._launcher_task_cancel = None
        app._launcher_task_window = None
        bar.stop()
        cancel_button.configure(state="normal", text="关闭", command=window.destroy)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        if error:
            label.configure(text="已停止" if isinstance(error, TaskCancelled) else "操作未完成")
            detail.configure(text=str(error)[:360])
            status.configure(text=f"用时 {int(time.monotonic() - began)} 秒；原下载文件已保留")
            app.set_status("操作未完成，请查看进度窗口和控制台")
            ui_log(app, str(error) + "\n")
        else:
            bar.configure(mode="determinate")
            bar.set(1)
            label.configure(text="操作完成")
            status.configure(text=f"用时 {int(time.monotonic() - began)} 秒")
            detail.configure(text="文件处理完成。")
            if complete:
                complete(result, detail)
    def run():
        try:
            result = worker(progress, cancel)
        except Exception as error:
            finished.set()
            app.after(0, finish, None, error)
        else:
            finished.set()
            app.after(0, finish, result)
    tick()
    threading.Thread(target=run, daemon=True).start()
    window.after(100, window.lift)
    return window


def open_manual_download(app, module):
    from tkinter import filedialog
    from launcher_repair import ui_log
    window = module.ctk.CTkToplevel(app)
    window.title("手动下载")
    window.geometry("620x420")
    window.transient(app)
    module.ctk.CTkLabel(window, text="手动下载 ComfyUI + 配套模型", font=("Microsoft YaHei", 20)).pack(pady=(24, 12))
    module.ctk.CTkLabel(window, text="先通过百度网盘下载，再由启动器解压并放置模型。\n请同时下载 comfyui_core.zip 和整个 models 文件夹。", wraplength=560).pack(pady=8)
    address = module.ctk.CTkEntry(window, width=560)
    address.pack(pady=8)
    address.insert(0, SHARE_URL)
    address.configure(state="readonly")
    module.ctk.CTkLabel(window, text="提取码：" + SHARE_CODE).pack(pady=4)
    actions = module.ctk.CTkFrame(window, fg_color="transparent")
    actions.pack(pady=10)
    def copy_code():
        app.clipboard_clear()
        app.clipboard_append(SHARE_CODE)
        app.set_status("已复制百度网盘提取码")
    module.ctk.CTkButton(actions, text="打开百度网盘", command=lambda: webbrowser.open(SHARE_URL)).pack(side="left", padx=8)
    module.ctk.CTkButton(actions, text="复制提取码", command=copy_code).pack(side="left", padx=8)
    def choose_files():
        download = filedialog.askdirectory(parent=window, title="选择包含 comfyui_core.zip 和 models 的下载文件夹")
        if not download:
            return
        destination = filedialog.askdirectory(parent=window, title="选择安装位置（与下载文件夹分开）")
        if not destination:
            return
        if getattr(app, "comfy_process", None):
            module.msgbox.showinfo("请先停止服务", "请先停止 ComfyUI，再安装或整理模型。", parent=window)
            return
        window.destroy()
        def complete(result, detail):
            app.comfy_dir = result["comfy_root"]
            app.settings["comfy_dir"] = result["comfy_root"]
            app.settings["last_install_dir"] = destination
            app.settings["manual_download_dir"] = download
            app.save_settings()
            message = f"ComfyUI 已解压，复制 {result['copied']} 个模型文件，已有相同文件 {result['skipped']} 个。\n已自动设置启动路径，可点击“一键启动”。"
            detail.configure(text=message)
            app.set_status("ComfyUI 和配套模型已安装；启动前将检测本机环境")
            ui_log(app, message + "\n安装位置：" + result["comfy_root"] + "\n")
        task_window(app, module, "安装手动下载的文件", lambda progress, cancel: install_downloads(download, destination, progress, cancel), complete)
    module.ctk.CTkButton(window, text="安装下载好的文件（自动解压并放置模型）", height=42, width=450, command=choose_files).pack(pady=(16, 10))
    module.ctk.CTkLabel(window, text="下载文件会保留；同名且内容不同的模型不会被覆盖。", wraplength=550).pack(pady=6)
    window.after(100, window.lift)
