#!/usr/bin/env python3
"""生成「Mac 上一键打包」的分发 zip。

在 **Windows 上**执行（用 Windows 打出来的 zip 由 mac 端解压），产物：

    dist_mac/小丸工具箱-mac打包.zip

里面是完整的项目源码 + 「打包小丸工具箱.command」，
mac 用户解压后双击那个 .command 就能得到 .app。

为什么由 Windows 来打这个包（而不是在 mac 上 git clone）：
    用户的 mac 上不一定有 git/网络，把源码打包发过去最省事。

🔴 zip 里 .command 的权限必须是 0o755，否则 mac 上双击会提示
   "无法打开，因为它来自身份不明的开发者"或干脆没反应。
   直接传 .app / .command 文件夹也会丢执行权限 —— 所以必须压 zip，
   且用 external_attr 显式写权限位（Python 的 zipfile 默认不写）。
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "dist_mac"
ZIP_NAME = "小丸工具箱-mac打包.zip"

# 只收源码与构建脚本；排除体积大/平台专属/构建产物
EXCLUDE_DIRS = {
    ".git", ".venv", ".buildenv", "build", "dist", "dist_mac",
    "__pycache__", ".pytest_cache", "_shots", "_shots_install",
    "_reference", "tests",
    # 开发期专用，mac 使用者不需要，带上只会让人困惑
    ".agents", ".githooks", "tools",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
EXCLUDE_NAMES = {
    ".DS_Store", "Thumbs.db", "desktop.ini", "icon.ico",
    # 仓库内部的协作/规范文件，对打包者无用
    "AGENTS.md", ".gitattributes", ".gitignore",
}

#: 需要打成 0o755 的条目（zip 里要写权限）
EXECUTABLE = {"打包小丸工具箱.command", "build_macos.sh", "run.py"}

# bin/ 里的 Windows exe 不能带进 mac 包
BIN_SKIP = {"ffmpeg.exe", "ffprobe.exe"}

# bin_mac/ 里的 mac 二进制**必须带上**（这样 mac 用户不用装 ffmpeg），
# 且在 zip 里要有可执行权限，否则解压后跑不了
MAC_BIN_DIR = "bin_mac"

# wheels_mac/ 里的离线依赖**必须带上** —— 这样 mac 端 pip 完全不用联网。
# 缺了它 .command 会退回联网安装，慢且容易因版本不兼容失败。
MAC_WHEEL_DIR = "wheels_mac"


def should_include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return False
    if path.name in EXCLUDE_NAMES:
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    if rel.parts and rel.parts[0] == "bin" and path.name in BIN_SKIP:
        return False
    # 根目录的测试素材
    if len(rel.parts) == 1 and path.suffix.lower() in (".mp4", ".mkv", ".mov"):
        return False
    return path.is_file()


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    zip_path = OUT_DIR / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    files = sorted(p for p in ROOT.rglob("*") if should_include(p))
    print(f"收集到 {len(files)} 个文件")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            rel = f.relative_to(ROOT)
            # mac 解压后应得到一个顶层目录，避免文件散落
            arcname = f"xiaowan-toolbox/{rel.as_posix()}"
            info = zipfile.ZipInfo.from_file(f, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 🔴 显式写权限：Python 的 ZipInfo 默认把 external_attr 留空，
            # mac 解压后所有文件都是 644 —— .command 双击就废了，
            # bin_mac/ 里的 ffmpeg 也会变成"不可执行"。
            is_mac_bin = rel.parts[0] == MAC_BIN_DIR
            if is_mac_bin:
                mode = 0o755
            elif f.name in EXECUTABLE:
                mode = 0o755
            elif rel.parts[0] == "packaging" and f.suffix == ".sh":
                mode = 0o755
            else:
                mode = 0o644
            info.external_attr = mode << 16
            info.create_system = 3          # 3 = Unix，mac 才会读权限位
            with open(f, "rb") as fh:
                zf.writestr(info, fh.read())

    size_mb = zip_path.stat().st_size / 1048576
    print(f"\n✓ {zip_path}")
    print(f"  {size_mb:.1f} MB / {len(files)} 个文件")

    mac_bins = [p.name for p in files if p.relative_to(ROOT).parts[0] == MAC_BIN_DIR]
    if mac_bins:
        print(f"  内置 mac 版 ffmpeg/ffprobe：{', '.join(sorted(mac_bins))}")
        print("  → mac 用户无需安装 ffmpeg / Homebrew")
    else:
        print("  ⚠️ 没有 bin_mac/，mac 用户需要自行安装 ffmpeg（brew install ffmpeg）")

    mac_wheels = [p.name for p in files
                  if p.relative_to(ROOT).parts[0] == MAC_WHEEL_DIR]
    if mac_wheels:
        wsize = sum((ROOT / MAC_WHEEL_DIR / n).stat().st_size
                    for n in mac_wheels) / 1048576
        print(f"  内置离线 Python 依赖：{len(mac_wheels)} 个 wheel，{wsize:.0f} MB")
        print("  → mac 端 pip 完全不用联网（--no-index --find-links）")
    else:
        print("  ⚠️ 没有 wheels_mac/，mac 端需要联网装依赖（慢，且可能版本不兼容）")
        print("     先生成：python packaging/fetch_mac_wheels.py")

    print("\nmac 端用法：")
    print("  1. 解压得到 xiaowan-toolbox/ 文件夹")
    print("  2. 双击里面的「打包小丸工具箱.command」")
    print("  3. 等它跑完（全程离线，约 3~8 分钟），得到 dist/小丸工具箱.app 与 .zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
