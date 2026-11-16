# Krita + ComfyUI 启动器

当前正式版本：**1.2.3**。

[下载启动器](https://github.com/a1097473800-source/krita-launcher-update/releases/download/1.2.3/KritaLauncherSetup.exe) · [发布说明](https://github.com/a1097473800-source/krita-launcher-update/releases/tag/1.2.3)

现有学员可以在“环境维护 → 一键更新启动器”检测更新。下载的 EXE 是独立启动器，放在普通文件夹运行即可。需要原 Krita AI 安装入口时，将它放在原启动器目录，与“安装环境包(必留)”并列。

## 本版功能

- “软件安装 → 手动下载”：打开课程百度网盘，提取码 **n35a**。下载 comfyui_core.zip 和整个 models 文件夹，保持分类目录；再由启动器自动解压和放置模型。
- “环境维护 → 检测 / 修复依赖”：普通依赖优先使用清华源，阿里云源备用；显示下载进度、速度、耗时和等待提示。
- 可从同版压缩包恢复内置 Python / Torch，保留旧 Python 备份，模型和工作流留在原处。
- 在线资源下载保留断点并校验；更新校验包身份、备份原文件并确认新版启动。

本仓库和 Release 不包含大型课程模型。显卡驱动或 GPU 架构不兼容仍需使用适配环境；检测报告用于定位原因，不能用重装普通依赖代替驱动兼容处理。

## 源码与构建

原始完整 Python 源码已丢失。source 目录保存本次可编辑修复层及测试，界面仍依赖原 EXE 中的编译模块。这份备份不能视为完整原始源码。

使用 **Python 3.10**（只需标准库）执行：

```powershell
python -X utf8 -B source/rebuild.py ORIGINAL.exe KritaLauncherSetup.exe
```

构建输入必须是未修改的原始 1.2.1 启动器：

- [原始构建基准](https://github.com/a1097473800-source/krita-launcher-update/releases/download/v1.2.1/KritaLauncherSetup.exe)
- SHA256：8eec43719347e82f4480c9786827e1988b05f457621c8f42c3c4b80d9b4b653a

后续版本统一修改 source/launcher_build.py；构建脚本从完成的 EXE 自动生成 version.json 的版本、长度和哈希。请先上传并验证同版 EXE，再更新 main 的 version.json。不要复用已发布版本号上传不同内容。

在 source 目录运行测试（默认寻找上一级预览文件名，可指定正式 EXE）：

```powershell
$env:KRITA_UPDATE_TEST_EXE = (Resolve-Path ../KritaLauncherSetup.exe).Path
python -X utf8 -B -m unittest discover -s tests -v
```

本版 57 项测试通过，并在隔离目录验证了新协议及旧 1.2.1/1.2.2 参数更新、备份、配置保留和正确路径重启。尚未在所有学员故障电脑上完成实际课程生图验收。
