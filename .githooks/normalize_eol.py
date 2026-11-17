#!/usr/bin/env python3
"""提交前把工作区里已跟踪文本文件的 CRLF 归一成 LF。

起因：Python 的 `Path.write_text()` 在 Windows 上默认把 `\\n` 写成 `\\r\\n`。
本项目所有文本文件在仓库里都是 LF，所以一次 write_text 就能让「只改一行」
的提交在 `git diff` 里显示为整篇重写（MEMORY.md 中过两次）。

为什么不能只看"暂存的文件"：`.gitattributes` 里的 `text=auto eol=lf` 会让
`git add` 时就把 CRLF 转成 LF，于是 git 认为"内容没变"、暂存区里根本没有这个
文件——可工作区磁盘上仍然是 CRLF。所以必须**直接扫工作区的已跟踪文件**。

二进制、以及显式声明要 CRLF 的扩展名一律跳过。

用法：
    .githooks/pre-commit                        # 由 git 自动调用（修完继续提交）
    python .githooks/normalize_eol.py --check   # 只检查不改，有 CRLF 则退出码 1
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 显式需要 CRLF 的扩展名（Windows 批处理；macOS 的 .command 必须 LF）
CRLF_OK = {".bat", ".cmd"}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".7z", ".zip", ".gz", ".tar", ".exe", ".dll", ".so", ".dylib",
    ".mp4", ".mkv", ".mov", ".webm", ".mp3", ".m4a", ".flac", ".wav",
    ".pyc", ".pdf", ".woff", ".woff2", ".ttf", ".otf",
}

MAX_SIZE = 2 * 1024 * 1024      # 超过 2MB 的"文本"大概率不是文本，跳过


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    return [Path(p.decode("utf-8")) for p in out.split(b"\x00") if p]


def normalize(path: Path) -> bool:
    """把文件里的 CRLF 换成 LF；真的改了才返回 True。"""
    try:
        if path.stat().st_size > MAX_SIZE:
            return False
        raw = path.read_bytes()
    except OSError:
        return False
    if b"\r\n" not in raw:
        return False
    # 没扩展名或扩展名不认识的，用 NUL 字节兜底判断二进制
    if b"\x00" in raw[:8192]:
        return False
    path.write_bytes(raw.replace(b"\r\n", b"\n"))
    return True


def main() -> int:
    check_only = "--check" in sys.argv
    fixed: list[Path] = []

    for path in tracked_files():
        ext = path.suffix.lower()
        if ext in BINARY_EXT or ext in CRLF_OK:
            continue
        if not path.is_file():
            continue
        if check_only:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\r\n" in raw and b"\x00" not in raw[:8192]:
                fixed.append(path)
        elif normalize(path):
            fixed.append(path)

    if not fixed:
        return 0

    verb = "换行不是 LF" if check_only else "已把换行归一为 LF"
    print(f"{verb}（仓库统一 LF）：")
    for p in fixed:
        print(f"  {p}")
    return 1 if check_only else 0


if __name__ == "__main__":
    raise SystemExit(main())
