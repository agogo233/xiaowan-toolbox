"""通用工具：体积/时长格式化、路径处理、平台目录。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "XiaoWanToolbox"
APP_TITLE = "小丸工具箱"

VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".rmvb", ".3gp", ".ogv",
}
AUDIO_EXTS = {
    ".mp3", ".aac", ".m4a", ".flac", ".wav", ".ape", ".ogg", ".opus",
    ".wma", ".alac", ".aiff", ".ac3", ".dts", ".amr",
}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

#: 封面图扩展名。🔴 用**列表**而不是 set —— 顺序就是优先级。
#: 同名图片可能有好几张（课时2.jpg + 课时2.png），封面只能选一张，
#: 必须有确定的"第一个"。字幕用 set 是因为它**全都要嵌**，顺序无所谓。
#: jpg 排第一：它是资源管理器/播放器兼容性最好的格式（实测 foobar2000
#: 写进 MP4 covr 原子里的就是 JPEG，格式码 0x0D）。
COVER_EXTS = [".jpg", ".jpeg", ".png", ".webp"]


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def platform_label() -> str:
    """界面文案里的平台名：'Mac' / 'Windows' / 'Linux'。"""
    if is_macos():
        return "Mac"
    if is_windows():
        return "Windows"
    return "Linux"


def file_manager_label() -> str:
    """系统文件管理器的中文名。"""
    if is_macos():
        return "访达"
    if is_windows():
        return "资源管理器"
    return "文件管理器"


def ffmpeg_install_hint() -> str:
    """按平台给出安装 / 指定 ffmpeg 的提示。"""
    if is_macos():
        return "brew install ffmpeg"
    if is_windows():
        return "到 gyan.dev 或 BtbN 的 GitHub 发布页下载 ffmpeg，再把 bin\\ffmpeg.exe 填进来"
    return "用包管理器安装，例如 sudo apt install ffmpeg"


def human_size(num: float | int | None) -> str:
    """1536 -> '1.50 KB'"""
    if num is None:
        return "—"
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "—"
    if num < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(num)} B"
    return f"{num:.2f} {units[idx]}"


def human_duration(seconds: float | None) -> str:
    """3725 -> '01:02:05'"""
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_bitrate(bps: float | None) -> str:
    if not bps or bps <= 0:
        return "—"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    return f"{bps / 1000:.0f} kbps"


def parse_bitrate(text: str | int | None) -> int:
    """'128k' / '2M' / 128000 -> bps"""
    if text is None:
        return 0
    if isinstance(text, int):
        return text
    t = str(text).strip().lower()
    try:
        if t.endswith("k"):
            return int(float(t[:-1]) * 1000)
        if t.endswith("m"):
            return int(float(t[:-1]) * 1_000_000)
        return int(float(t))
    except ValueError:
        return 0


def data_dir() -> Path:
    """平台标准配置目录。"""
    if is_macos():
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif is_windows():
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return data_dir() / "config.json"


def unique_path(path: Path) -> Path:
    """若目标已存在，追加 _1 / _2 … 避免覆盖。"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    idx = 1
    while True:
        cand = parent / f"{stem}_{idx}{suffix}"
        if not cand.exists():
            return cand
        idx += 1


def open_in_file_manager(path: Path) -> None:
    """在访达 / 资源管理器中显示文件或目录。"""
    p = str(path)
    try:
        if is_macos():
            if Path(p).is_file():
                subprocess.Popen(["open", "-R", p])
            else:
                subprocess.Popen(["open", p])
        elif is_windows():
            if Path(p).is_file():
                subprocess.Popen(["explorer", "/select,", p])
            else:
                os.startfile(p)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(Path(p).parent if Path(p).is_file() else p)])
    except Exception:
        pass


def collect_media_files(paths: list[str], exts: set[str], recursive: bool = False) -> list[Path]:
    """展开目录、按扩展名过滤、去重并保持顺序。"""
    result: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = str(p.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        result.append(p)

    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for child in sorted(it):
                if child.is_file() and child.suffix.lower() in exts:
                    _add(child)
        elif p.is_file():
            _add(p)
    return result


def safe_stem(path: Path) -> str:
    """去掉文件名里对 ffmpeg 不友好的字符。"""
    bad = '<>:"/\\|?*\n\r\t'
    name = "".join("_" if c in bad else c for c in path.stem).strip()
    return name or "output"


def resource_path(*parts: str) -> Path:
    """应用内资源路径（打包后从 _MEIPASS 读，源码运行时从 app/resources 读）。

    放在这里而不是 app/main.py：界面层（侧栏徽标等）也要用，反向 import main 会循环。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base).joinpath("app", "resources", *parts)
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parents[1] / "resources" / Path(*parts)


# --------------------------------------------------------------------------- #
# 关机（任务队列跑完后的可选动作）
# --------------------------------------------------------------------------- #

def shutdown_command(delay_seconds: int = 0) -> list[str]:
    """跨平台的「关机」命令。delay_seconds 交给系统自己倒计时。

    🔴 一律用系统自带的关机命令，不自己拼平台细节之外的逻辑：
      · Windows: `shutdown /s /t N`（系统会显示自己的倒计时）
      · macOS  : `sudo shutdown -h +N` 需要密码 → 改用 `osascript` 走
                  System Events（会弹一次授权，或已授权则直接执行）
      · Linux  : `shutdown -h now` / `+N`（多数发行版需 root，桌面环境通常已授权）

    ⚠️ Windows 的 `/t 0` 会**立刻**关机，连保存机会都不给，所以调用方
    必须自己先倒计时（我们用的是应用内 60 秒倒计时 + 1 秒缓冲）。
    """
    delay = max(0, int(delay_seconds))
    if is_windows():
        return ["shutdown", "/s", "/t", str(delay)]
    if is_macos():
        # System Events 的 shut down 不接受延迟参数，延迟由应用侧做
        return ["osascript", "-e",
                'tell application "System Events" to shut down']
    return ["shutdown", "-h", f"+{delay // 60}" if delay else "now"]


def cancel_shutdown_command() -> list[str]:
    """取消系统层面已经排上的关机（用户在我们的倒计时里点取消、但
    Windows 那边已经用 /t N 排上了时，需要再撤一次）。"""
    if is_windows():
        return ["shutdown", "/a"]
    if is_macos():
        # System Events 排不出可撤销的延迟关机，无需处理
        return []
    return ["shutdown", "-c"]


def power_off(delay_seconds: int = 0) -> tuple[bool, str]:
    """执行关机。返回 (成功, 人话说明)。

    失败不抛异常 —— 关机失败不该把界面搞崩，用户还在等着看结果。
    """
    import subprocess

    cmd = shutdown_command(delay_seconds)
    if not cmd:
        return False, "当前平台不支持自动关机"
    # 0x08000000 = CREATE_NO_WINDOW。这里写字面量而不是引常量，
    # 是为了让 utils 保持零内部依赖（它是整个 core 的最底层）。
    no_window = 0x08000000 if is_windows() else 0
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15,
                             creationflags=no_window)
    except Exception as exc:  # noqa: BLE001
        return False, f"执行关机命令失败：{exc}"
    if res.returncode != 0:
        err = (res.stderr or b"").decode("utf-8", "replace").strip()
        return False, err or f"关机命令返回 {res.returncode}"
    return True, "关机命令已下达"


def should_power_off(tasks, user_canceled: bool = False) -> tuple[bool, str]:
    """队列跑完后该不该关机。返回 (要不要关, 人话理由)。

    🔴 关机不可逆，所以这里是一道**闸门**，默认一切异常情况都不关：

    | 情况 | 结果 | 为什么 |
    | --- | --- | --- |
    | 没有任务 | 不关 | 空队列触发关机是"偷袭" |
    | 有任务失败 | 不关 | 用户回来发现文件没出来、电脑却关了，无从判断 |
    | 用户点过取消 | 不关 | "我取消了，你却把电脑关了" |
    | 全部成功 | **关** | 这才是用户要的场景 |

    只接受 counts 形式的输入（[成功数, 失败数, 取消数]）或 Task 列表，
    方便 UI 直接传 `queue.tasks()`。
    """
    from .task import TaskStatus

    items = list(tasks or [])
    if not items:
        return False, "队列里没有任务"

    total = len(items)
    done = sum(1 for t in items if t.status == TaskStatus.DONE)
    failed = sum(1 for t in items if t.status == TaskStatus.FAILED)
    canceled = sum(1 for t in items if t.status == TaskStatus.CANCELED)

    if user_canceled:
        # 用户主动表态「我不想跑了」——那更不该关机（否则等于"我取消了，
        # 你却把电脑关了"）。这条要单独说，不能复用下面的计数文案。
        return False, "你取消过任务，不关机"
    if canceled:
        return False, f"有 {canceled} 个任务被取消，不关机"
    if failed:
        return False, f"有 {failed} 个任务失败，不关机（避免回来发现文件没出来）"
    if done == 0:
        return False, "没有成功完成的任务，不关机"
    if done < total:
        return False, f"只有 {done}/{total} 完成，不关机"
    return True, f"全部 {total} 个任务已完成"
