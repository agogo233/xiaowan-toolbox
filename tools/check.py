#!/usr/bin/env python3
"""项目体检：在提交/打包前跑一遍，检查那些"靠人记不住"的项目约定。

目前检查：
  * 换行：所有已跟踪文本文件必须是 LF（仓库统一 LF）

用法：
    python tools/check.py            # 只检查，有问题退出码 1
    python tools/check.py --fix      # 顺手修掉能自动修的
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, **kw)


def ensure_hooks_path() -> None:
    """新克隆的仓库没有 core.hooksPath（它是本地配置，不进仓库）。"""
    cur = run(["git", "config", "core.hooksPath"]).stdout.decode().strip()
    if cur == ".githooks":
        return
    r = run(["git", "config", "core.hooksPath", ".githooks"])
    if r.returncode == 0:
        print("✔ 已启用 git hook（core.hooksPath = .githooks）")
    else:
        print("⚠ 启用 git hook 失败，换行守卫不会自动运行", file=sys.stderr)


def check_eol(fix: bool) -> bool:
    args = [sys.executable, str(ROOT / ".githooks" / "normalize_eol.py")]
    if not fix:
        args.append("--check")
    r = run(args)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    if out:
        print(out)
    return r.returncode == 0


def main() -> int:
    ensure_hooks_path()
    ok = check_eol("--fix" in sys.argv)
    print("✔ 换行检查通过" if ok else "✘ 有文件换行不是 LF")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
