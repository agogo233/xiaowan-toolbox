# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows · **单文件 onefile**）。

用法（项目根目录）：
    python packaging/build_windows.py            # 默认走这个 spec

产物：
    dist/小丸工具箱.exe      单个文件，双击即用（约 460 MB，含 ffmpeg）

为什么是 onefile（AGENTS.md 的既定偏好）：
    Windows 端用户要"一个文件就能发"，目录式得拷一整个文件夹。
    代价是每次启动都要把运行时解压到临时目录 —— 内置 ffmpeg+ffprobe
    就有 460 MB，实测启动明显变慢，这是明确的取舍，不是疏漏。

🔴 单文件下 `sys._MEIPASS` 是**临时目录**，退出即删。所以：
    - 配置/日志必须写用户目录（app/core/utils.data_dir() 已按 APPDATA 走，
      不依赖 _MEIPASS），否则每次启动都是"全新安装"。
    - 任何"记住上次状态"的文件都不能落在 exe 旁边。

目录式（启动快）见 packaging/xiaowan_win_onedir.spec。
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # packaging/ 的上一级
bin_dir = ROOT / "bin"
ffmpeg = bin_dir / "ffmpeg.exe"
ffprobe = bin_dir / "ffprobe.exe"
ico = ROOT / "packaging" / "icon.ico"

datas = [(str(ROOT / "app" / "resources" / "icon.png"), "app/resources"),
        (str(ROOT / "app" / "resources" / "qrcode.png"), "app/resources")]

# 是否内置 ffmpeg。默认内置，可用**环境变量**关掉，避免为次需求再维护
# 一份 spec（两份 spec 迟早会漂移，改了一处忘另一处更难查）：
#     PowerShell:  $env:XIAOWAN_NO_FFMPEG=1; python -m PyInstaller ...
#     bash:        XIAOWAN_NO_FFMPEG=1 python -m PyInstaller ...
# 关掉后产物约 40 MB，用户需自备 ffmpeg（「功能设置」里指定路径或放 PATH）。
# 注意 build_windows.py --no-ffmpeg 就是设置这个变量后再调用 PyInstaller。
_NO_FFMPEG = os.environ.get("XIAOWAN_NO_FFMPEG", "").strip() not in ("", "0", "false", "False")
if not _NO_FFMPEG:
    if ffmpeg.is_file():
        datas.append((str(ffmpeg), "bin"))
    if ffprobe.is_file():
        datas.append((str(ffprobe), "bin"))

# 只保留真正用到的 Qt 模块，其余整块排除，能砍掉上百 MB
EXCLUDES = [
    "tkinter", "matplotlib", "numpy", "scipy", "pandas",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D", "PySide6.QtTest", "PySide6.QtSql",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtNetworkAuth", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtWebSockets",
    "PySide6.QtWebChannel", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.Qt3DCore",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtDBus",
    "PySide6.QtConcurrent", "PySide6.QtXml", "PySide6.QtSvgWidgets",
    "PySide6.QtLinguistics", "PySide6.QtPrintSupport",
    "imageio_ffmpeg",                   # 仅开发期兜底用，打包后走内置 bin/ffmpeg.exe
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

# onefile：不设 exclude_binaries、不建 COLLECT，所有依赖塞进同一个 exe
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="小丸工具箱",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                          # upx 压 Qt 的 DLL 容易出问题，关掉
    runtime_tmpdir=None,
    console=False,                      # 关键：不弹黑色命令行窗口
    disable_windowed_traceback=False,
    icon=str(ico) if ico.is_file() else None,
)
