# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（macOS · 目录式 onedir → .app）。

用法（项目根目录）：
    bash packaging/build_macos.sh                    # 推荐：一键（venv/图标/签名）
    pyinstaller packaging/xiaowan.spec --noconfirm   # 手动

产物：
    dist/小丸工具箱.app      可双击的独立应用

🔴 入口是 packaging/launcher.py 而不是 run.py —— 见该文件的说明：
   它先把数据目录钉到 ~/Library/Application Support/XiaoWanToolbox/，
   再导入主程序；并兜住 windowed 形态下 stdout/stderr 为 None 的情况。
   spec 里必须用 launcher，否则临时目录会把配置写丢。

形态选择（AGENTS.md）：mac 端**目录式优先**（启动快）。用户口中的"单文件"
指"给用户的东西只有一个 .app 图标"，不是技术上的 onefile。
dist/ 里与 .app 同名的普通文件夹是 COLLECT 中间产物，可删。

如果仓库根目录存在 bin/ffmpeg 与 bin/ffprobe，会被一并打进 .app，
实现"开箱即用、无需 brew install ffmpeg"。
"""

import re
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # packaging/ 的上一级
bin_dir = ROOT / "bin"
ffmpeg = bin_dir / "ffmpeg"
ffprobe = bin_dir / "ffprobe"
icon = ROOT / "packaging" / "icon.icns"
LAUNCHER = ROOT / "packaging" / "launcher.py"

# 版本号与 app/__init__.py 保持同步，避免 .app 里写死旧版本
_version = "1.0.0"
try:
    _m = re.search(r'__version__\s*=\s*"([^"]+)"',
                   (ROOT / "app" / "__init__.py").read_text(encoding="utf-8"))
    if _m:
        _version = _m.group(1)
except Exception:
    pass

datas = [(str(ROOT / "app" / "resources" / "icon.png"), "app/resources"),
        (str(ROOT / "app" / "resources" / "qrcode.png"), "app/resources")]

# ffmpeg 来源优先级：
#   1. bin/ffmpeg（.command 已经把 bin_mac/ 里对应架构的那份复制过来）
#   2. bin_mac/ffmpeg-{arm64,x64}（直接跑 spec 时的兜底，按本机架构挑）
# 这样无论走 .command 还是手动 pyinstaller，都能带上 ffmpeg。
def _pick_mac_bin(name: str) -> Path | None:
    direct = bin_dir / name
    if direct.is_file():
        return direct
    import platform
    suffix = {"arm64": "arm64", "x86_64": "x64"}.get(platform.machine())
    if suffix:
        cand = ROOT / "bin_mac" / f"{name}-{suffix}"
        if cand.is_file():
            return cand
    return None


_ff = _pick_mac_bin("ffmpeg")
_fp = _pick_mac_bin("ffprobe")
if _ff is not None:
    datas.append((str(_ff), "bin"))
if _fp is not None:
    datas.append((str(_fp), "bin"))

a = Analysis(
    [str(LAUNCHER)],
    pathex=[str(ROOT)],                  # launcher 里 import app.* 需要它
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.distutils"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="小丸工具箱",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                      # 关键：不弹终端窗口
    disable_windowed_traceback=False,
    icon=str(icon) if icon.is_file() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="小丸工具箱",
)

app = BUNDLE(
    coll,
    name="小丸工具箱.app",
    icon=str(icon) if icon.is_file() else None,
    bundle_identifier="com.xiaowan.toolbox",
    info_plist={
        "CFBundleName": "小丸工具箱",
        "CFBundleDisplayName": "小丸工具箱",
        "CFBundleShortVersionString": _version,
        "CFBundleVersion": _version,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "基于 FFmpeg，遵循 LGPL/GPL 许可分发",
        "NSPrincipalClass": "NSApplication",
        "LSApplicationCategoryType": "public.app-category.video",
    },
)
