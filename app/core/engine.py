"""任务执行引擎。

要点：
* 每个任务跑在独立的 Python 线程里，通过 Qt 信号（跨线程自动排队）回主线程刷新界面；
* ffmpeg 的 `-progress pipe:1` 输出在 stdout，逐行解析出进度/速度/剩余时间；
* stderr 单独线程消费，避免管道写满造成死锁，同时保留最后若干行用于报错提示。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .commands import cleanup_passlogs
from .ffmpeg import FFmpegEnv
from .task import Task, TaskStatus
from .utils import ffmpeg_install_hint, is_windows, human_duration

CREATE_NO_WINDOW = 0x08000000 if is_windows() else 0


def _set_background_priority(proc) -> None:
    """把已启动的 ffmpeg 进程降到「低于正常」优先级（后台运行）。

    为什么不减线程：减线程是**直接牺牲吞吐**换 CPU 空闲，而调低优先级是
    **让操作系统在别人需要时把它挪开** —— 没人抢 CPU 时它照样全速跑。
    同样是"别卡我"，后者不付代价。

    Windows：SetPriorityClass(BELOW_NORMAL_PRIORITY_CLASS = 0x4000)
      · 不用 IDLE：那是"只在完全空闲时运行"，压片会慢到夸张
    POSIX：os.nice +10 只能改**当前进程**，所以那边不在这里做，
      而在 Popen 的 preexec_fn 里改（见 _background_preexec）。

    全部失败静默 —— 这是锦上添花的优化，拿不到就让进程按默认优先级跑，
    绝不该因此让任务失败。
    """
    if not is_windows():
        return
    try:
        import ctypes

        PROCESS_SET_INFORMATION = 0x0200
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_SET_INFORMATION, False, proc.pid)
        if h:
            ctypes.windll.kernel32.SetPriorityClass(
                h, BELOW_NORMAL_PRIORITY_CLASS)
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass


def _background_preexec():
    """POSIX 用：在 fork 之后、exec 之前把**子进程**的 nice 调高。

    🔴 不能在父进程里 os.nice() —— 那会把整个 GUI 一起降级，
    界面反而变卡。必须只在子进程里做。
    """
    if is_windows():
        return None

    def _fn() -> None:
        try:
            os.nice(10)
        except Exception:  # noqa: BLE001
            pass

    return _fn

# 欠标多少才提示「可以勾选尽量用满」。
# 取 15%：普通素材两遍 ABR 稳定在 96~98%，差几个百分点不值得让用户多跑一轮；
# 而明显欠标（实测有素材只出 41%）时，用户看到这句才知道有开关可用。
FILL_HINT_GAP = 0.15


def _policy_label(task: Task) -> str:
    """体积校验日志里的模式名。硬件编码走固定 QP，说"严格"会误导。"""
    if task.qp_used >= 0:
        return "硬件编码 · 固定 QP"
    if task.size_policy == "fast":
        return "快速单遍"
    return "严格"

_ERR_HINTS = [
    (re.compile(r"Invalid data found when processing input", re.I),
     "源文件损坏或不是 ffmpeg 支持的格式"),
    (re.compile(r"No such file or directory", re.I), "找不到输入或输出文件路径"),
    (re.compile(r"Permission denied", re.I), "没有写入权限，换个输出目录试试"),
    (re.compile(r"Unknown encoder '([^']+)'", re.I),
     f"当前 ffmpeg 没有编译该编码器（{ffmpeg_install_hint()}）"),
    (re.compile(r"Error initializing output stream", re.I),
     "输出参数不被该容器接受，试着换 MKV 容器或改用 AAC 音频"),
    (re.compile(r"moov atom not found", re.I), "MP4 文件不完整（缺少 moov），无法处理"),
    (re.compile(r"height not divisible by 2", re.I),
     "分辨率必须为偶数，请把缩放高度改为偶数"),
    (re.compile(r"Conversion failed", re.I), "编码失败，详情见下方日志"),
    (re.compile(r"Invalid argument", re.I), "参数无效，可能源文件缺少可用音视频轨"),
]


def friendly_error(log: str) -> str:
    for pattern, hint in _ERR_HINTS:
        if pattern.search(log):
            return hint
    # 退回到最后一条 Error 行
    for line in reversed(log.splitlines()):
        if "error" in line.lower() or "invalid" in line.lower():
            return line.strip()[:200]
    return "编码失败，详情见日志"


class _Bridge(QObject):
    """工作线程 -> 主线程的信号桥。"""

    progress = Signal(str)          # task_id
    logLine = Signal(str, str)      # task_id, line
    state = Signal(str)             # task_id


class _Canceled(Exception):
    pass


class QueueManager(QObject):
    """队列调度 + 并发控制。"""

    taskAdded = Signal(str)
    taskUpdated = Signal(str)
    taskRemoved = Signal(str)
    queueEmpty = Signal()

    def __init__(self, env_provider, config, parent=None):
        super().__init__(parent)
        self._env_provider = env_provider          # callable -> FFmpegEnv
        self._config = config                      # callable -> AppConfig
        self._tasks: list[Task] = []
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._paused = False

        self.bridge = _Bridge()
        self.bridge.progress.connect(self._on_progress)
        self.bridge.logLine.connect(self._on_log)
        self.bridge.state.connect(self._on_state)

    # ------------------------------------------------------------------ #
    # 队列操作
    # ------------------------------------------------------------------ #
    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks)

    @property
    def paused(self) -> bool:
        return self._paused

    def add(self, task: Task) -> Task:
        with self._lock:
            self._tasks.append(task)
        self.taskAdded.emit(task.id)
        self._dispatch()
        return task

    def add_many(self, tasks: list[Task]) -> None:
        with self._lock:
            self._tasks.extend(tasks)
        for t in tasks:
            self.taskAdded.emit(t.id)
        self._dispatch()

    def remove(self, task_id: str) -> None:
        task = self.get(task_id)
        if task and task.is_active:
            self.cancel(task_id)
        with self._lock:
            self._tasks = [t for t in self._tasks if t.id != task_id]
        self.taskRemoved.emit(task_id)

    def clear_finished(self) -> None:
        with self._lock:
            keep = [t for t in self._tasks if not t.is_finished]
            gone = [t.id for t in self._tasks if t.is_finished]
            self._tasks = keep
        for tid in gone:
            self.taskRemoved.emit(tid)

    def clear_all(self) -> None:
        for t in self.tasks:
            if t.is_active:
                self.cancel(t.id)
        with self._lock:
            gone = [t.id for t in self._tasks]
            self._tasks = []
        for tid in gone:
            self.taskRemoved.emit(tid)

    def get(self, task_id: str) -> Task | None:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def start(self) -> None:
        self._paused = False
        self._dispatch()

    def pause(self) -> None:
        """暂停派发新任务（已运行的任务会跑完，避免产生半截文件）。"""
        self._paused = True

    def cancel(self, task_id: str) -> None:
        task = self.get(task_id)
        if not task:
            return
        task.cancel_requested = True
        proc = task.process
        if proc is not None and proc.poll() is None:
            _terminate(proc)
        elif task.status == TaskStatus.WAITING:
            task.status = TaskStatus.CANCELED
            task.message = "已取消"
            task.finished = time.time()
            self.taskUpdated.emit(task.id)

    def retry(self, task_id: str) -> None:
        task = self.get(task_id)
        if not task or not task.is_finished:
            return
        task.status = TaskStatus.WAITING
        task.progress = 0.0
        task.speed = ""
        task.eta = -1.0
        task.error = ""
        task.message = ""
        task.log.clear()
        task.started = task.finished = 0.0
        task.cancel_requested = False
        self.taskUpdated.emit(task.id)
        self._dispatch()

    def running_count(self) -> int:
        return sum(1 for t in self._tasks if t.status == TaskStatus.RUNNING)

    def waiting_count(self) -> int:
        return sum(1 for t in self._tasks if t.status == TaskStatus.WAITING)

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #
    def _dispatch(self) -> None:
        cfg = self._config()
        limit = max(1, int(cfg.concurrency))
        with self._lock:
            if self._paused:
                return
            while self.running_count() < limit:
                nxt = next((t for t in self._tasks if t.status == TaskStatus.WAITING
                            and not t.cancel_requested), None)
                if nxt is None:
                    break
                nxt.status = TaskStatus.RUNNING
                nxt.started = time.time()
                nxt.message = "启动中…"
                th = threading.Thread(target=self._run_task, args=(nxt,), daemon=True)
                self._threads[nxt.id] = th
                self.taskUpdated.emit(nxt.id)
                th.start()

    def _on_task_finished(self, task: Task) -> None:
        with self._lock:
            self._threads.pop(task.id, None)
        self.taskUpdated.emit(task.id)
        self._dispatch()
        if not any(t.is_active for t in self._tasks):
            self.queueEmpty.emit()

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _run_task(self, task: Task) -> None:
        env: FFmpegEnv = self._env_provider()
        try:
            if not env.ready:
                raise RuntimeError("未找到 ffmpeg，请在「设置」中指定路径")
            if task.cancel_requested:
                raise _Canceled()

            # 严格目标体积：开跑前先拒绝数学上做不到的目标，别让用户白等
            # 目标体积任务先做可行性预检；不可行直接拒绝，不浪费一轮编码。
            # 注意：两档都要预检——"做不到"与策略无关。
            if task.size_policy and not self._size_precheck(task):
                return

            weights = [s.weight for s in task.steps] or [1.0]
            total_weight = sum(weights)
            done_weight = 0.0

            for step in task.steps:
                if task.cancel_requested:
                    raise _Canceled()
                task.message = step.label or "处理中"
                self.bridge.state.emit(task.id)
                code = self._run_step(task, step, done_weight, total_weight)
                if task.cancel_requested:
                    raise _Canceled()
                if code != 0:
                    raise RuntimeError(friendly_error("\n".join(task.log)))
                done_weight += step.weight

            # 严格目标体积：校验实际体积，超标就按实得结果修正
            if task.size_policy:
                done_weight = self._size_verify_and_correct(task, done_weight, total_weight)
                if task.cancel_requested:
                    raise _Canceled()

            # 收尾
            if task.dst.exists():
                task.out_size = task.dst.stat().st_size
            # 成功路径也要清临时件（.best 已被 os.replace 吃掉，passlog 还在），
            # 但**保留产物**
            _cleanup_artifacts(task, keep_dst=True)
            task.progress = 1.0
            task.status = TaskStatus.DONE
            task.message = self._done_message(task)
            task.eta = 0.0

        except _Canceled:
            _cleanup_artifacts(task)
            task.status = TaskStatus.CANCELED
            task.message = "已取消"
        except Exception as exc:  # noqa: BLE001
            _cleanup_artifacts(task)
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task.message = "失败"
        finally:
            task.finished = time.time()
            task.process = None
            self._on_task_finished(task)

    # ------------------------------------------------------------------ #
    # 严格目标体积
    # ------------------------------------------------------------------ #
    @staticmethod
    def _size_precheck(task: Task) -> bool:
        """开跑前判定目标是否可行；不可行直接置 FAILED，不浪费一轮编码。"""
        budget = task.budget
        if budget is None or getattr(budget, "ok", True):
            return True
        # 这里是 return 而不是抛异常，所以不走下面的 except 分支 ——
        # 临时件要自己清。**不删 dst**：任务还没开始编码，那个路径上可能是
        # 用户已存在的同名文件，删了就是误伤。
        _safe_unlink(_best_path(task.dst))
        cleanup_passlogs(task.work_dir)
        task.status = TaskStatus.FAILED
        task.error = getattr(budget, "reason", "目标体积不可行")
        task.message = "目标不可行"
        hints = getattr(budget, "suggestions", None) or []
        for line in hints:
            task.append_log("💡 " + line)
        return False

    def _size_verify_and_correct(self, task: Task, done_weight: float,
                                 total_weight: float) -> float:
        """比对实际输出体积，超标降码率、欠标（fill 模式）升码率。

        修正公式的配对规则（实测验证）：新码率必须由「本轮码率 ↔ 本轮实得体积」
        推导。若拿旧码率配新体积，误差会从上轮直接翻倍发散。

        重要：判定和修正必须用同一套基准。这里用 task.video_kbps_used（本轮真正
        编码用的码率）而不是 budget.video_kbps——降音频等路径会让两者不同。

        末轮兜底：短视频上码率↔体积不是严格线性（实测在 3225~3977 kbps 之间
        来回震荡、3 轮都撞不上目标），所以轮数用完后若最后一份产物超标，
        必须回退到本轮里**体积最接近目标且不超标**的那一份。否则
        「绝不超目标」这条铁律会被补跑打破。
        """
        from .size_budget import FILL_TRIGGER, fill_hopeless, needs_fill
        from .size_service import (build_correction_steps,
                           build_cqp_correction_steps,
                           build_fast_correction_steps)

        budget = task.budget
        if budget is None or not task.dst.exists():
            return done_weight

        # 硬件编码走 CQP：没有"码率"这个概念，用 QP 当基准
        is_cqp = task.qp_used >= 0
        if not is_cqp:
            if not task.video_kbps_used:
                task.video_kbps_used = int(getattr(budget, "video_kbps", 0) or 0)
            if task.video_kbps_used <= 0:
                return done_weight

        # 两档的共同铁律：**实际体积永远不许超过目标**，容差为 0。
        # 曾经的 loose 档（2% 容差、且被调用方排除在校验之外）已删除：
        # 它花同样的两遍时间，却放弃了唯一的硬保证。
        target = task.size_target_bytes
        # 「加码率有没有用」只能拿**上一轮补跑**的结果比。不能拿任何上一轮的
        # 结果比：超标那轮会降码率、体积随之变小，再跟它比就成了"负增长"，
        # 补跑会被误判成"撑不满"而提前放弃（实测：0.65MB → 1.21MB → 0.82MB
        # 这条路径上就是这么停手的）。
        fill_prev_bytes = 0
        # 每轮产物都写到同一个 dst 上，后一轮会覆盖前一轮。想在末轮回退就得
        # 边跑边留一份「目前最好的、不超标」的副本（拷贝比重新编码便宜得多）。
        best_copy: Path | None = None
        best_size = 0
        # (码率, 实得字节) 观测，供对数插值收敛（线性外推在短视频上会震荡）
        history: list[tuple[int, int]] = []

        for _round in range(task.size_max_rounds):
            actual = task.dst.stat().st_size

            # 留一份「目前最好的、不超标」的副本：后一轮会覆盖 dst，
            # 万一最后一轮冲过目标，就靠它回退（拷贝远比重新编码便宜）
            history.append((task.video_kbps_used, actual))
            if actual <= target and actual > best_size:
                if best_copy is not None:
                    _safe_unlink(best_copy)
                best_copy = _best_path(task.dst)
                try:
                    shutil.copyfile(task.dst, best_copy)
                    best_size = actual
                except OSError:
                    best_copy, best_size = None, 0

            # ---- 超过目标：两档都必须修 ----
            if actual > target:
                reason = "超出目标 %.2f MB" % ((actual - target) / 1024 / 1024)
                fill_prev_bytes = 0        # 这轮不是补跑，不能当补跑的基线
            # ---- 欠标：只有用户勾了「尽量用满」才补跑 ----
            elif needs_fill(budget, actual, task.fill_target):
                if fill_hopeless(budget, fill_prev_bytes, actual):
                    task.append_log(
                        "⚠ 内容撑不满目标（加码率也无增长），停在 %.2f MB"
                        % (actual / 1024 / 1024))
                    return done_weight
                reason = "低于目标 %.2f MB，尝试用满" % (
                    (target - actual) / 1024 / 1024)
            else:
                task.append_log(
                    "✔ 体积校验通过：%.2f MB / 目标 %.2f MB（%s）"
                    % (actual / 1024 / 1024, target / 1024 / 1024,
                       _policy_label(task)))
                # 明显欠标却没勾「尽量用满」：别只说"校验通过"，那会让人
                # 以为这就是最优结果（用户就是被这句困惑过——报了 41% 也算通过）。
                if (not task.fill_target and target > 0
                        and actual < target * (1 - FILL_HINT_GAP)):
                    task.append_log(
                        "💡 内容比预期简单，只用到目标的 %.0f%%；"
                        "勾选「尽量用满目标」可再压一次把空间用掉"
                        % (actual / target * 100))
                return done_weight

            if task.cancel_requested:
                return done_weight

            if is_cqp:
                # 硬件编码：调 QP（没有码率可调）
                steps, new_val = build_cqp_correction_steps(
                    self._env_provider(), task.src, task.dst,
                    task.encoding_settings, budget, task.qp_used,
                    actual, audio_mode=task.audio_mode, info=task.info,
                    clip=task.clip, threads=task.threads)
            elif task.size_policy == "fast":
                # 快速单遍没有 passlog，修正就是整步重编（单遍，代价小）
                steps, new_val = build_fast_correction_steps(
                    self._env_provider(), task.src, task.dst,
                    task.encoding_settings, budget, task.video_kbps_used,
                    actual, audio_mode=task.audio_mode, info=task.info,
                    history=history, clip=task.clip, threads=task.threads)
            else:
                steps, new_val = build_correction_steps(
                    self._env_provider(), task.src, task.dst, task.encoding_settings,
                    budget, task.video_kbps_used, actual, task.work_dir,
                    audio_mode=task.audio_mode, info=task.info,
                    history=history, clip=task.clip, threads=task.threads)
            if not steps or new_val <= 0:
                task.append_log("⚠ 已到可调边界，停止调整")
                return done_weight
            task.append_log(
                "⚠ %s，%s（第 %d 轮）"
                % (reason,
                   f"QP {task.qp_used} → {new_val}" if is_cqp
                   else f"码率 {task.video_kbps_used} → {new_val} kbps",
                   task.correction_round + 1))
            task.correction_round += 1
            # 只记「补跑前的体积」，供下一轮判断加码率是否有效
            fill_prev_bytes = actual if reason.startswith("低于目标") else 0

            extra = sum(s.weight for s in steps)
            total_weight = done_weight + extra
            for st in steps:
                if task.cancel_requested:
                    return done_weight
                task.message = st.label or "修正重跑"
                self.bridge.state.emit(task.id)
                code = self._run_step(task, st, done_weight, total_weight)
                if code != 0:
                    raise RuntimeError(friendly_error("\n".join(task.log)))
                done_weight += st.weight
            if is_cqp:
                task.qp_used = new_val
            else:
                task.video_kbps_used = new_val

        # 轮数用完：**必须把最后一轮的结果也量一遍**。
        # 原来循环一结束就直接返回，最后一轮修正的产物从没被校验过 ——
        # 那正是最可能超标的一轮（每次修正都在往目标推），"绝不超目标"
        # 的保证在这里会漏掉。
        final = task.dst.stat().st_size
        if final <= target:
            task.append_log(
                "✔ 修正 %d 轮后停在 %.2f MB / 目标 %.2f MB"
                % (task.correction_round, final / 1024 / 1024,
                   target / 1024 / 1024))
            return done_weight

        # 末轮超标：短视频上码率↔体积不是严格线性，实测会在 3225~3977 kbps
        # 之间来回震荡、3 轮都撞不上目标。这时不能就这么交出去 ——
        # 回退到本轮里体积最大的**不超标**那份副本，铁律优先于"用满"。
        if best_copy is None or not best_copy.exists():
            # 一轮都没达标过（极少见）：保留现状并明说，不谎报成功
            task.append_log(
                "⚠ 已修正 %d 轮仍超出目标：%.2f MB / %.2f MB"
                % (task.correction_round, final / 1024 / 1024,
                   target / 1024 / 1024))
            return done_weight

        try:
            os.replace(best_copy, task.dst)
            task.append_log(
                "⚠ 最后一轮超出目标（%.2f MB），回退到本轮最接近的 %.2f MB"
                % (final / 1024 / 1024, best_size / 1024 / 1024))
        except OSError as exc:
            task.append_log(f"⚠ 回退失败（{exc}），保留 %.2f MB"
                            % (final / 1024 / 1024))
            _safe_unlink(best_copy)
        return done_weight

    @staticmethod
    def _done_message(task: Task) -> str:
        # 目标体积的两档都会实测体积，所以两档都把结果写进完成提示
        if not task.size_target_bytes:
            return "已完成"
        actual = task.out_size or (task.dst.stat().st_size if task.dst.exists() else 0)
        text = "%s / 目标 %s" % (_mb(actual), _mb(task.size_target_bytes))
        if task.correction_round:
            text += "（修正 %d 次）" % task.correction_round
        return "已完成：" + text

    def _run_step(self, task: Task, step, done_weight: float, total_weight: float) -> int:
        cmd = [str(a) for a in step.args]
        # 原子输出：把命令里的最终产物路径换成 `.part<扩展名>`，跑完再 replace。
        # 这样目标路径只可能"不存在"或"完整"，不会出现半截文件。
        # 只替换**完全等于** dst 的参数，passlog 路径、NUL 之类不受影响。
        part = _part_path(task.dst)
        writes_dst = any(a == str(task.dst) for a in cmd)
        if writes_dst:
            cmd = [str(part) if a == str(task.dst) else a for a in cmd]

        # 只走 emit 一条路：_on_log 会 append 到 task.log 并刷新界面。
        # 原来这里又 append_log 又 emit，同一条命令在日志里出现两遍
        # （_short() 对 <90 字符的参数是恒等变换，两行字节完全相同）。
        self.bridge.logLine.emit(task.id, f"$ {' '.join(_short(cmd))}")

        # 后台运行：每个任务启动时读一次配置（改设置在下一个任务生效，
# 已在跑的任务不受影响 —— 这也是我们期望的语义）
        background = bool(getattr(self._config(), "background_mode", False))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
                # POSIX 上"后台运行"必须在 fork 后、exec 前改 nice，
                # 不能动父进程（见 _background_preexec 的说明）
                preexec_fn=_background_preexec() if background else None,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"无法启动 ffmpeg：{exc}") from exc

        if background:
            # Windows 走 SetPriorityClass；POSIX 已由 preexec_fn 处理（此处空转）
            _set_background_priority(proc)

        task.process = proc

        # 取消请求可能落在 Popen 尚未返回的窗口期（此时 task.process 还是 None，
        # cancel() 无从下手），所以进程创建完成后必须再补查一次
        if task.cancel_requested:
            _terminate(proc)

        err_buf: deque[str] = deque(maxlen=400)
        err_thread = threading.Thread(
            target=_drain_stderr, args=(task, proc, err_buf), daemon=True
        )
        err_thread.start()

        duration = task.duration
        last_emit = 0.0
        timeline = 0.0
        speed_val = 0.0

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time_us" or key == "out_time_ms":
                try:
                    secs = int(value) / 1_000_000
                except ValueError:
                    continue
                timeline = max(timeline, secs)
                if duration > 0:
                    frac = min(1.0, timeline / duration)
                    task.progress = min(1.0, (done_weight + step.weight * frac) / total_weight)
                if speed_val > 0 and duration > 0:
                    remain = max(0.0, duration - timeline) / speed_val
                    task.eta = remain
                now = time.time()
                if now - last_emit > 0.12:
                    last_emit = now
                    self.bridge.progress.emit(task.id)
            elif key == "speed":
                m = re.match(r"([\d.]+)", value.strip())
                if m:
                    try:
                        speed_val = float(m.group(1))
                        task.speed = f"{speed_val:.2f}x"
                    except ValueError:
                        speed_val = 0.0
            elif key == "total_size":
                pass
            elif key == "progress" and value.strip() == "end":
                if duration > 0:
                    task.progress = min(
                        1.0, (done_weight + step.weight) / total_weight)
                self.bridge.progress.emit(task.id)

        code = proc.wait()
        err_thread.join(timeout=3)
        task.process = None
        # 直接把 stderr 尾部**同步**写进 task.log：
        # logLine 是跨线程排队投递的，任务一结束上层就会读日志（拼错误信息、
        # 判断能否识别），这时最后几行可能还没到 —— 实测失败时 task.log 里
        # 看不到 "Unknown encoder" 这种关键行，用户只看到"详情见日志"。
        #
        # 这里**不再 emit** 这批：_drain_stderr 只负责缓冲、不实时投递，
        # 所以不存在"已经投递过"的重复；界面需要的实时刷新由下一条命令开始时
        # 的 emit 与 taskUpdated 覆盖，末尾这几行本来就只对事后诊断有意义。
        for entry in list(err_buf)[-6:]:
            task.append_log(entry)

        # 原子收尾：成功才把 .part 换成正式文件名。
        # 失败/取消时 .part 由 _cleanup_artifacts 清掉，目标路径始终干净。
        if writes_dst:
            if code == 0 and part.exists():
                try:
                    os.replace(part, task.dst)
                except OSError as exc:
                    task.append_log(f"⚠ 无法重命名输出文件：{exc}")
                    code = code or 1
            else:
                _safe_unlink(part)
        return code

    # ------------------------------------------------------------------ #
    # 信号槽
    # ------------------------------------------------------------------ #
    def _on_progress(self, task_id: str) -> None:
        self.taskUpdated.emit(task_id)

    def _on_log(self, task_id: str, line: str) -> None:
        task = self.get(task_id)
        if task:
            task.append_log(line)
            self.taskUpdated.emit(task_id)

    def _on_state(self, task_id: str) -> None:
        self.taskUpdated.emit(task_id)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _drain_stderr(task: Task, proc, buf: deque) -> None:
    """持续读取 stderr，防止管道写满；同时保留最后若干行。"""
    stream = proc.stderr
    if stream is None:
        return
    try:
        for raw in stream:
            line = raw.rstrip()
            if line:
                buf.append(line)
    except (ValueError, OSError):
        pass


def _terminate(proc) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def _best_path(dst: Path) -> Path:
    """末轮兜底副本的路径（与目标同目录，加 .best 后缀）。

    收敛过程中每轮都会覆盖 dst，所以要留一份"目前最好的、不超标"的副本，
    万一最后一轮冲过目标就换回来（拷贝远比重新编码便宜）。
    """
    return dst.with_name(dst.name + ".best")


def _part_path(dst: Path) -> Path:
    """编码中间产物的路径：`<名字>.part<扩展名>`。

    **必须保留扩展名**：ffmpeg 靠扩展名选容器（`-f` 没显式指定时），
    写成 `a.mkv.part` 会报"Unable to find a suitable output format"。

    为什么要有这一层：直接写 dst 的话，进程被杀 / 断电会在**目标路径**上留一个
    截断文件——用户双击打开是坏的，而且分不清"没做完"和"做坏了"。改成写
    `.part`，成功后 `os.replace` 一次性换上，目标路径要么不存在、要么完整。
    """
    return dst.with_name(f"{dst.stem}.part{dst.suffix}")


def _cleanup_artifacts(task: Task, keep_dst: bool = False) -> None:
    """清掉任务产生的临时件与半成品。

    🔴 必须在**设置 task.status 之前**调用：外部（UI / 测试）一看到
    status=DONE/FAILED/CANCELED 就会去检查文件，清理放在 status 之后
    会留下一个"已经说失败、但垃圾还在"的竞态窗口（实测踩过）。

    keep_dst=True 时保留产物本身（成功路径），只清临时件。
    """
    if not keep_dst:
        _safe_unlink(task.dst)
    _safe_unlink(_best_path(task.dst))
    _safe_unlink(_part_path(task.dst))
    cleanup_passlogs(task.work_dir)


def _safe_unlink(path: Path) -> None:
    """取消/失败时清掉半成品。"""
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        pass


def _mb(num_bytes: int) -> str:
    """字节转 MB 文本（严格体积任务的完成提示用）。"""
    return "%.2f MB" % (num_bytes / 1024 / 1024)


def _short(cmd: list[str]) -> list[str]:
    """日志里省略超长参数。"""
    out = []
    for part in cmd:
        out.append(part if len(part) < 90 else part[:87] + "…")
    return out
