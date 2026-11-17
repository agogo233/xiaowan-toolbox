#!/usr/bin/env python3
"""预下载 mac 打包所需的 Python 依赖（wheel），供**离线**安装。

在 Windows 上执行，产物进 `wheels_mac/`，随后被打进 mac 分发包。
mac 用户双击 .command 时用 `--no-index --find-links` 从本地装，
**完全不需要联网**。

## 为什么不能直接 `pip download`
pip 默认下**当前平台**（Windows）的 wheel，mac 上装不了。
必须显式给 `--platform` / `--abi`，而各包支持的平台标签又不一致：
    PySide6 / PyInstaller → macosx_*_universal2（arm64+x86_64 通吃）
    imageio-ffmpeg        → 分架构发布（arm64 / x86_64 各一个）
所以按「多平台依次尝试，拿到就停」的方式兜底。

## 版本下限为什么是 3.10
    imageio-ffmpeg 0.8.1 要求 Python >= 3.10（0.6.0 才支持 3.9）
    PySide6 6.9 支持 3.9~3.13
取 3.10 的交集，既能拿到新包，又覆盖了绝大多数 mac。
mac 自带的 /usr/bin/python3 若是 3.9，脚本会提示装 Homebrew Python。

## 两个架构都下
PySide6/PyInstaller 是 universal2，一份通吃；但 imageio-ffmpeg 必须两份。
只下一个的话另一种 Mac 会装不上。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "wheels_mac"

#: 目标 Python 版本（决定能选到哪些 wheel）
PY_VER = "3.10"
ABI = "cp310"

#: 依次尝试的平台标签 —— 找到兼容的就停。
#: 顺序有讲究：先 universal2（覆盖最广），再分架构兜底。
PLATFORMS = [
    "macosx_12_0_universal2",
    "macosx_11_0_universal2",
    "macosx_12_0_arm64",
    "macosx_10_13_x86_64",
    "macosx_10_9_x86_64",
]

#: 要下载的包。**不含 pillow** —— 它只有 Windows 的 make_ico.py 用，
#: mac 端 make_icon.py 走 PySide6，多下一个白占 5MB。
#:
#: 第二项 True = 必须**两种架构各下一份**（通用 wheel 不会，分架构的才会）。
#:
#: 🔴 macholib 必须显式列出（PyInstaller 在 mac 上的依赖）。
#: `pip download --platform` 只按目标平台挑 **wheel**，但依赖的**环境标记**
#: 仍按当前机器算 —— 实测（Windows 上跑）：PyInstaller 的依赖里
#:   · pywin32-ctypes（标记 win32）→ 成立，下了（mac 上根本用不到）
#:   · macholib（标记 darwin）→ 不成立，**被跳过** ← mac 端装 PyInstaller
#:     时就会报「Could not find a version ... macholib>=1.8」
#: 用户的 mac 就是这么失败的。所以 darwin 专属依赖必须手动点名。
TARGETS = [
    ("PySide6-Essentials>=6.9,<6.10", False),   # universal2
    ("shiboken6>=6.9,<6.10", False),            # universal2
    ("imageio-ffmpeg>=0.4.9", True),            # 分架构发布！
    ("pyinstaller", False),                     # universal2
    ("macholib>=1.8", False),                   # PyInstaller 在 mac 上的依赖
]

#: 明确不要的：Windows 专用（pip 解析 PyInstaller 依赖时会带出来）
SKIP_NAMES = ("pywin32", "pefile", "pywin32-ctypes")


def pip_download(spec: str, platform: str) -> tuple[bool, str]:
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--dest", str(OUT),
        "--only-binary=:all:",
        "--platform", platform,
        "--python-version", PY_VER,
        "--implementation", "cp",
        "--abi", ABI,
        spec,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode == 0:
        return True, ""
    # 只保留最后一条错误（pip 的报错很长）
    err = (r.stderr or "").strip().splitlines()
    return False, err[-1] if err else f"exit {r.returncode}"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    print("=" * 68)
    print(f"  预下载 mac 依赖（Python {PY_VER}+ / 离线安装用）")
    print("=" * 68)

    failed = []
    for spec, need_both_arch in TARGETS:
        if need_both_arch:
            # 必须两种架构各下一份：分架构发布的包（imageio-ffmpeg）
            # 只下一份的话，另一种 Mac 会「找不到匹配的版本」。
            got = {}
            for plat, arch in (("macosx_12_0_arm64", "arm64"),
                               ("macosx_10_13_x86_64", "x86_64"),
                               ("macosx_10_9_x86_64", "x86_64")):
                if arch in got:
                    continue            # 该架构已拿到
                ok, err = pip_download(spec, plat)
                if ok:
                    print(f"  ✓ {spec:<32} ← {plat}")
                    got[arch] = plat
                else:
                    last_err = err
            if not got:
                print(f"  ✗ {spec:<32} 两种架构都失败")
                print(f"       {last_err}")
                failed.append(spec)
            elif len(got) < 2:
                print(f"  ✗ {spec:<32} 只拿到 {list(got)}，缺另一种架构")
                failed.append(spec)
        else:
            ok_any = False
            last_err = ""
            for plat in PLATFORMS:
                ok, err = pip_download(spec, plat)
                if ok:
                    print(f"  ✓ {spec:<32} ← {plat}")
                    ok_any = True
                    break
                last_err = err
            if not ok_any:
                print(f"  ✗ {spec:<32} 所有平台都失败")
                print(f"       {last_err}")
                failed.append(spec)

    # 清掉 Windows 专用包
    removed = []
    for w in OUT.glob("*.whl"):
        if any(s in w.name.lower() for s in SKIP_NAMES):
            w.unlink()
            removed.append(w.name)
    if removed:
        print(f"\n  清掉 Windows 专用包：{', '.join(removed)}")

    whls = sorted(OUT.glob("*.whl"))
    total = sum(w.stat().st_size for w in whls) / 1048576
    print("\n" + "=" * 68)
    print(f"  共 {len(whls)} 个 wheel，{total:.1f} MB")
    for w in whls:
        print(f"    {w.name:<62} {w.stat().st_size/1048576:>6.1f} MB")

    # 校验：不能混进非 mac 的 wheel
    bad = [w.name for w in whls
           if not ("macosx" in w.name or w.name.endswith("-none-any.whl"))]
    if bad:
        print(f"\n  ✗ 混进了非 mac 的 wheel：{bad}")
        return 1

    if failed:
        print(f"\n  ✗ 失败：{failed}")
        return 1

    # 关键校验：两个架构的 imageio-ffmpeg 都要在（PySide6 是 universal2）
    names = " ".join(w.name for w in whls)
    has_arm = "imageio_ffmpeg" in names and "arm64" in names
    has_intel = "imageio_ffmpeg" in names and ("x86_64" in names or "intel" in names)
    print(f"\n  imageio-ffmpeg arm64: {'✓' if has_arm else '✗'}   "
          f"x86_64: {'✓' if has_intel else '✗'}")
    if not (has_arm and has_intel):
        print("  ✗ 缺少某个架构的 imageio-ffmpeg，另一种 Mac 会装不上")
        return 1

    # 🔴 必需 wheel 白名单 —— macholib 就是这么丢的（环境标记按当前机器
    # 算，darwin 专属依赖被静默跳过），所以这里按名字硬性核对。
    REQUIRED = ("pyside6_essentials", "shiboken6", "pyinstaller",
                "pyinstaller_hooks_contrib", "altgraph", "macholib",
                "packaging", "setuptools")
    missing = [n for n in REQUIRED
               if not any(n.lower() in w.name.lower() for w in whls)]
    if missing:
        print(f"\n  ✗ 缺少必需的 wheel：{missing}")
        print("     （PyInstaller 在 mac 上装时会用到它们）")
        return 1
    print("  必需 wheel 全部齐备：", ", ".join(REQUIRED))

    print("\n  ✓ 全部就绪（mac 端可完全离线安装）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())