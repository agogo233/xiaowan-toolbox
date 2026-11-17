#!/usr/bin/env python3
"""打包专用启动器（launcher）—— spec 的入口就是这个文件，不是 run.py。

为什么必须有它（AGENTS.md 的硬性要求）：

1. 🔴 **数据目录必须先固定到用户目录，再导入主程序**。
   打包后 `sys._MEIPASS` 指向临时目录（macOS 上是
   `/var/folders/.../T/_MEIxxxxx`，退出即删）。若配置/日志按
   `__file__` 相对定位，就会写进那个临时目录 —— 用户每次启动都是
   "全新安装"：主题、上次选的预设、任务队列全丢。
   这里在 import app 之前把环境钉死，保证 `app.core.utils.data_dir()`
   落在 `~/Library/Application Support/XiaoWanToolbox/`。

2. 🔴 **windowed（无控制台）形态下 stdout/stderr 是 None**。
   Python 的 `print()` / 第三方库的 `sys.stderr.write()` 会直接抛
   AttributeError，表现成"双击没反应"。这里换成写日志文件的对象。

3. 🔴 **致命错误必须弹系统对话框**，不许静默退出。
   Windows 用 `MessageBoxW`，macOS 用 `osascript`（.app 双击时终端是关着的，
   写 stderr 没人看得见）。

4. 捕获对象必须带 `isatty()`：很多库（含 tqdm、colorama、以及本项目的
   兼容补丁）会调 `sys.stdout.isatty()`，缺了这个方法同样 AttributeError。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

APP_NAME = "XiaoWanToolbox"
APP_TITLE = "小丸工具箱"


# --------------------------------------------------------------------------- #
# 1) 数据目录：必须在任何 app 模块 import 之前钉死
# --------------------------------------------------------------------------- #
def _user_data_dir() -> Path:
    """与 app/core/utils.data_dir() 完全一致的规则（此处不能 import 它）。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME",
                                   str(Path.home() / ".config"))) / APP_NAME
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


DATA_DIR = _user_data_dir()
LOG_PATH = DATA_DIR / "launcher.log"


# --------------------------------------------------------------------------- #
# 2) stdout / stderr 兜底（windowed 下是 None）
# --------------------------------------------------------------------------- #
class _LogStream:
    """替代 sys.stdout / sys.stderr 的最小对象：写文件 + 提供 isatty。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, text: str) -> int:
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        # 🔴 必须提供：没有它，任何调 isatty() 的库都会 AttributeError
        return False

    # 少数库会读这些属性
    encoding = "utf-8"
    errors = "replace"

    def fileno(self) -> int:
        raise OSError("no fileno")


def _install_stream_fallbacks() -> None:
    """stdout/stderr 为 None 或不可写时换成日志流。"""
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name, None)
        broken = cur is None
        if not broken:
            try:
                cur.write("")
            except Exception:  # noqa: BLE001
                broken = True
        if broken:
            setattr(sys, name, _LogStream(LOG_PATH))


# --------------------------------------------------------------------------- #
# 3) 致命错误：写日志 + 弹系统对话框
# --------------------------------------------------------------------------- #
def _fatal(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        (DATA_DIR / "crash.log").write_text(text, encoding="utf-8")
    except OSError:
        pass

    tail = text[-1500:]
    if sys.platform == "darwin":
        # .app 双击时没有终端，只能靠系统对话框
        try:
            import subprocess

            script = (
                'display alert "小丸工具箱 启动失败" message "'
                + tail.replace("\\", "\\\\").replace('"', '\\"')
                       .replace("\n", "\\n")[:1200]
                + '\\n\\n日志：' + str(DATA_DIR / "crash.log") + '" as critical'
            )
            subprocess.run(["osascript", "-e", script], check=False,
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
    elif sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None, tail, "小丸工具箱 · 启动失败", 0x10)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    _install_stream_fallbacks()

    # Qt 高分屏（必须在 import PySide6 之前）
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    try:
        from app.main import main as app_main
    except BaseException as exc:  # noqa: BLE001
        _fatal(exc)
        return 1

    try:
        return app_main()
    except BaseException as exc:  # noqa: BLE001
        _fatal(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
