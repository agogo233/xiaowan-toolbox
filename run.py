#!/usr/bin/env python3
"""小丸工具箱 · 启动脚本。

用法：
    python run.py          # 系统 Python 也能用，会自动切到项目 .venv
    pythonw run.py         # 不弹控制台窗口

依赖装在项目内的 .venv（Python 3.12）。若用系统 Python 直接运行，
本脚本会检测到缺依赖并自动用 .venv 的解释器重启自己。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 让 Qt 在 Retina / 高分屏上正确渲染
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

# 重启标记：防止「系统 Python → venv Python」无限套娃
_RELAUNCH_FLAG = "XIAOWAN_VENV_RELAUNCH"


def _venv_python() -> Path | None:
    """项目虚拟环境的解释器路径（不存在则返回 None）。"""
    base = ROOT / ".venv"
    for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


def _running_inside_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    except OSError:
        return False


def _relaunch_in_venv() -> int | None:
    """依赖不在当前解释器里、但项目有 .venv 时，用 venv 的解释器重启自己。

    返回子进程退出码；不需要重启则返回 None。
    """
    try:
        import PySide6  # noqa: F401

        return None                       # 依赖齐全，直接跑
    except ImportError:
        pass

    if os.environ.get(_RELAUNCH_FLAG) == "1":
        return None                       # 已经重启过一次，别再循环
    if _running_inside_venv():
        return None                       # venv 里也缺依赖，交给下面正常报错
    venv_py = _venv_python()
    if venv_py is None:
        return None

    env = dict(os.environ, **{_RELAUNCH_FLAG: "1"})
    try:
        # 用 python.exe 而不是 pythonw.exe：终端里能看到输出
        code = subprocess.call(
            [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]], env=env
        )
    except OSError:
        return None
    return code


def _crash_report(exc: BaseException) -> None:
    """打包成无控制台的 exe 后，异常默认是"静默退出"。

    这里把堆栈落盘并在 Windows 上弹一个原生错误框，
    避免用户双击后"什么都没发生"却无从排查。
    """
    import traceback

    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log: Path | None = None
    try:
        from app.core.utils import data_dir

        log = data_dir() / "crash.log"
        log.write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    sys.stderr.write(text)
    if sys.platform.startswith("win"):
        try:
            import ctypes

            body = f"{text}\n\n日志已保存到：{log}" if log else text
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None, body[-1800:], "小丸工具箱 · 启动失败", 0x10
            )
        except Exception:  # noqa: BLE001
            pass


def _missing_dependency(exc: ImportError) -> int:
    """缺依赖时给出可照做的提示（含项目 .venv 的用法）。"""
    venv_py = _venv_python()
    print("依赖缺失：", exc)
    if venv_py is not None:
        print("项目虚拟环境已存在，请用它启动：")
        print(f'    "{venv_py}" "{Path(__file__).resolve()}"')
    else:
        print("请先创建虚拟环境并安装依赖：")
        print("    python -m venv .venv")
        print(f'    "{ROOT / ".venv" / "Scripts" / "python.exe"}" -m pip install -r requirements.txt')
    return 1


def main() -> int:
    relaunched = _relaunch_in_venv()
    if relaunched is not None:
        return relaunched

    try:
        from app.main import main as app_main
    except ImportError as exc:  # pragma: no cover
        return _missing_dependency(exc)

    try:
        return app_main()
    except BaseException as exc:  # noqa: BLE001
        _crash_report(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
