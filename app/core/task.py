"""任务模型。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .commands import CommandStep
from .ffmpeg import MediaInfo


class TaskKind(str, Enum):
    COMPRESS = "compress"
    AUDIO = "audio"
    REMUX = "remux"
    DOWNMIX = "downmix"


KIND_LABEL = {
    TaskKind.COMPRESS: "视频压缩",
    TaskKind.AUDIO: "音频提取",
    TaskKind.REMUX: "封装混流",
    TaskKind.DOWNMIX: "声道下混",
}


class TaskStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


STATUS_LABEL = {
    TaskStatus.WAITING: "等待中",
    TaskStatus.RUNNING: "进行中",
    TaskStatus.DONE: "已完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELED: "已取消",
}


@dataclass
class Task:
    kind: TaskKind
    src: Path
    dst: Path
    steps: list[CommandStep] = field(default_factory=list)
    info: MediaInfo | None = None
    work_dir: Path | None = None
    # 多文件合并出来的任务：src 只是代表项，真正的输入都在 sources 里
    sources: list[Path] = field(default_factory=list)
    label: str = ""                  # 覆盖默认显示名（合并任务用）

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.WAITING
    progress: float = 0.0            # 0.0 ~ 1.0
    speed: str = ""
    eta: float = -1.0                # 剩余秒数，-1 未知
    message: str = ""
    error: str = ""

    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0

    out_size: int = 0
    log: list[str] = field(default_factory=list)
    process: object | None = None    # subprocess.Popen
    cancel_requested: bool = False

    # ---- 严格目标体积（仅「按文件大小压缩」页面使用） ----
    # 其它页面的任务不设这些字段，默认值即可，行为完全不受影响。
    size_policy: str = ""            # "" = 不校验；"strict" / "fast" = 目标体积两档
    size_target_bytes: int = 0       # 目标体积（字节）
    size_tolerance: float = 0.02     # 允许超出比例（判定用；目标体积两档都是 0）
    size_max_rounds: int = 2         # 最多修正轮数
    correction_round: int = 0        # 已修正次数
    video_kbps_used: int = 0         # 当前轮使用的视频码率，供外推
    qp_used: int = -1                # 硬件编码 CQP 模式当前使用的 QP（-1 = 非 CQP）
    expected_bytes: int = 0          # 开跑前的估算体积（UI 显示）
    audio_shrink: bool = False       # 是否允许自动降音频
    audio_mode: str = "auto"         # copy / aac / none / auto
    fill_target: bool = False        # 欠标时补跑把目标用满（用户勾选）
    budget: object | None = None     # SizeBudget，避免循环导入用 object
    encoding_settings: object | None = None   # VideoSettings，修正时要用

    # ---- 试压（只压一段看效果，产物不进正常流程） ----
    # (起始秒, 时长)。设了它，所有修正重跑也必须带上，否则"试压"会在
    # 第一轮超标后突然全片重编 —— 那正是最贵的一步。
    clip: tuple[float, float] | None = None
    is_sample: bool = False          # UI 用它标出"这是试压产物"

    # ---- 编码线程上限 ----
    # 目前恒为 None（交给 ffmpeg 自动）。保留字段是为了"以后真要限线程"时
    # 修正重跑也能带上，不用再改一遍调用链。见 commands._threads_for_one_job。
    threads: int | None = None

    # ---- 封面（封装混流用） ----
    #: 这个任务实际会嵌入的封面图；None = 不加。队列页显示"封面来源"靠它，
    #: 而不是在 UI 里再算一遍 —— 算两遍必然出现"界面说加了、实际没加"。
    cover: Path | None = None

    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return self.label or self.src.name

    @property
    def source_tooltip(self) -> str:
        paths = self.sources or [self.src]
        if len(paths) == 1:
            return str(paths[0])
        return f"{len(paths)} 个文件合成：\n" + "\n".join(str(p) for p in paths)

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, "任务")

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, "未知")

    @property
    def duration(self) -> float:
        return self.info.duration if self.info else 0.0

    @property
    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        end = self.finished or time.time()
        return max(0.0, end - self.started)

    @property
    def ratio(self) -> float:
        """压缩比：输出 / 输入。"""
        if self.out_size and self.src.exists():
            try:
                return self.out_size / max(self.src.stat().st_size, 1)
            except OSError:
                return 0.0
        return 0.0

    @property
    def is_finished(self) -> bool:
        return self.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELED)

    @property
    def is_active(self) -> bool:
        return self.status in (TaskStatus.RUNNING, TaskStatus.WAITING)

    def append_log(self, line: str, limit: int = 500) -> None:
        self.log.append(line)
        if len(self.log) > limit:
            del self.log[: len(self.log) - limit]

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.log[-n:])
