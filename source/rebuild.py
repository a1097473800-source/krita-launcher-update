"""Rebuild against the user's original EXE using the matching Python 3.10 ABI.

Usage: python rebuild.py ORIGINAL.exe [OUTPUT.exe]
No pip packages, global installation or original Python source are required.
"""
import hashlib
import json
import marshal
import pathlib
import struct
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from archive_tools import LauncherArchive
import launcher_build as build
from launcher_updates import executable_identity, file_hash, version_key


def rebuild(original, output):
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError("请使用 Python 3.10 构建，以匹配原 EXE 的字节码版本")
    directory = pathlib.Path(__file__).resolve().parent
    version_key(build.APP_VERSION)
    archive = LauncherArchive(original)
    if archive.python_version != 310:
        raise RuntimeError("此修复层仅验证过 Python 3.10 打包的原启动器")
    pyz = archive.read("PYZ.pyz")
    toc = marshal.loads(pyz[int.from_bytes(pyz[8:12], "big"):])
    items = list(toc.items()) if isinstance(toc, dict) else list(toc)
    if "launcher_legacy" in dict(items) or "launcher_repair" in dict(items):
        raise RuntimeError("输入已经是修复版；请使用保留的原始启动器重建")
    manifest = json.loads((directory / "course-core.json").read_text(encoding="utf-8"))
    patch = (directory / "launcher_repair.py").read_text(encoding="utf-8")
    patch += "\nCOURSE_CORE = " + repr(manifest) + "\n"
    shim = "from launcher_updates import handle_headless_arguments\nhandle_headless_arguments()\nfrom launcher_legacy import *\nimport launcher_legacy as _legacy\nfrom launcher_repair import apply_patch\nLauncherApp = apply_patch(_legacy)\nrun_apply_update_mode = _legacy.run_apply_update_mode\n"
    payload = bytearray(pyz[:12])
    rebuilt_toc = []
    for name, (kind, start, length) in items:
        renamed = "launcher_legacy" if name == "launcher_main" else name
        raw = pyz[start:start + length]
        rebuilt_toc.append((renamed, (kind, len(payload), len(raw))))
        payload.extend(raw)
    sources = [("launcher_main", shim), ("launcher_repair", patch)]
    sources += [(name, (directory / (name + ".py")).read_text(encoding="utf-8"))
                for name in ("launcher_build", "launcher_updates", "archive_tools", "launcher_local")]
    dependencies = (directory / "launcher_dependencies.py").read_text(encoding="utf-8")
    dependencies += "\nBASELINE_PACKAGES = " + repr(json.loads((directory / "course-packages.json").read_text(encoding="utf-8"))) + "\n"
    sources.append(("launcher_dependencies", dependencies))
    for name, source in sources:
        raw = zlib.compress(marshal.dumps(compile(source, name + ".py", "exec", dont_inherit=True)), 9)
        rebuilt_toc.append((name, (0, len(payload), len(raw))))
        payload.extend(raw)
    struct.pack_into("!I", payload, 8, len(payload))
    payload.extend(marshal.dumps(rebuilt_toc))
    archive.rebuild(output, {"PYZ.pyz": bytes(payload)})
    rebuilt = LauncherArchive(output)
    for entry in archive.entries:
        if entry["name"] != "PYZ.pyz":
            assert archive.read(entry["name"]) == rebuilt.read(entry["name"]), entry["name"]
    new_pyz = rebuilt.read("PYZ.pyz")
    lookup = dict(marshal.loads(new_pyz[int.from_bytes(new_pyz[8:12], "big"):]))
    for name, (_, start, length) in items:
        _, new_start, new_length = lookup["launcher_legacy" if name == "launcher_main" else name]
        assert zlib.decompress(pyz[start:start + length]) == zlib.decompress(new_pyz[new_start:new_start + new_length]), name
    identity = executable_identity(output)
    if identity["version"] != build.APP_VERSION or identity["product_id"] != build.PRODUCT_ID:
        raise RuntimeError("输出 EXE 的实际版本未通过验收")
    return {"version": identity["version"], "product_id": identity["product_id"],
            "original_sha256": hashlib.sha256(pathlib.Path(original).read_bytes()).hexdigest(),
            "preview_sha256": hashlib.sha256(pathlib.Path(output).read_bytes()).hexdigest(),
            "retained_modules": len(items), "retained_archive_entries": len(archive.entries) - 1,
            "modified_entries": ["PYZ.pyz"], "output": str(pathlib.Path(output).resolve())}


def release_manifest(executable):
    """Generate from the finished EXE, never from a separately entered version."""
    identity = executable_identity(executable)
    if identity["version"] != build.APP_VERSION or identity["product_id"] != build.PRODUCT_ID:
        raise RuntimeError("EXE 版本与 launcher_build.py 不一致，不能生成发布清单")
    return {"version": identity["version"], "product_id": build.PRODUCT_ID,
            "package_type": "launcher", "size": pathlib.Path(executable).stat().st_size,
            "sha256": file_hash(executable),
            "url": "https://github.com/a1097473800-source/krita-launcher-update/releases/download/"
                   + identity["version"] + "/KritaLauncherSetup.exe",
            "notes": "增加百度网盘手动下载、解压及模型归位、国内源依赖修复与进度显示；保留更新和断点续传修复。",
            "force": False, "channel": build.CHANNEL, "mirrors": []}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) not in (2, 3):
        raise SystemExit("用法：Python 3.10 rebuild.py 原启动器.exe [修复预览.exe]")
    source_path = pathlib.Path(sys.argv[1]).resolve()
    target = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) == 3 else source_path.with_name("Krita一键启动器-修复预览.exe")
    if target == source_path:
        raise SystemExit("输出路径不能覆盖原启动器")
    report = rebuild(source_path, target)
    manifest_path = target.with_name("version.preview.json" if build.CHANNEL == "preview" else "version.json")
    manifest_path.write_text(json.dumps(release_manifest(target), ensure_ascii=False, indent=2), encoding="utf-8")
    report["manifest"] = str(manifest_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
