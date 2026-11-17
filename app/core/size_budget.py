"""严格目标体积：可行性预检、体积预算、码率求解。

这个模块是「按文件大小压缩」页面的专用引擎，与 commands.py 里原有的
指定体积路径完全独立——那边保持原样不动，避免影响「按常用预设」页签等既有功能。

设计依据（实测数据）：
    一轮修正即可把超标 45% 收敛到 -2.2%（x264/x265 均验证），
    因此策略是「首次按公式估算 + 留 2% 余量 → 输出后按实得体积外推一次」。
    修正外推必须用「本轮码率 ↔ 本轮实得体积」配对，否则会发散。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .ffmpeg import MediaInfo

# ── 容器封装开销 ──────────────────────────────────────────────────────
# 2026-11-09 实测（320x180@15，200k 视频 + 64k 音频）：
#
#   容器    5s      30s     120s    600s
#   mp4    +17.9%  +2.32%  +1.51%  +1.44%
#   mkv    +17.6%  +2.30%  +1.54%  +1.48%
#
# 规律：索引区字节数基本固定（约 28~29KB），所以短视频开销比例极高，
# 长视频收敛到容器结构本身的 ~1.4%。固定 2% 对 5 秒视频会低估 9 倍。
# 用 "固定字节 + 比例" 两段式建模，比单看比例准得多。
CONTAINER_FIXED_BYTES = {
    # moov 索引 + faststart 重排 + metadata
    "mp4": 30000,
    "mov": 30000,
    "mkv": 29000,      # cluster 头 + segment info + cues
    "webm": 20000,
}
CONTAINER_RATIO = {
    # 长视频稳定后的容器占比（结构开销，与内容无关）
    "mp4": 0.014,
    "mov": 0.014,
    "mkv": 0.015,
    "webm": 0.012,
}
DEFAULT_FIXED_BYTES = 25000
DEFAULT_RATIO = 0.014

# 音频实际输出相对标称码率的膨胀系数（实测 AAC 96k ≈ +1.9%）
AUDIO_OVERHEAD = 1.03

# 视频码率低于此值基本不可用（再低就是满屏色块）
MIN_VIDEO_KBPS = 50


class SizePolicy(Enum):
    """目标体积策略。两档共享同一条铁律：输出绝不大于目标。"""

    STRICT = "strict"   # 严格不超过：两遍 ABR，编码后必测，超标就修正重跑（默认）
    FAST = "fast"       # 快速单遍：不跑第一遍分析，预算码率打折起编（见 FAST_MARGIN）


# 超标判定容差：0 —— 多一个字节就修。
# 它只决定"要不要跑修正轮"，**不是"允许超标 N%"**：曾经的实现是
# limit = target × (1 + tolerance)，等于允许超 2%，与"不超过"直接矛盾。
OVERFLOW_TOLERANCE = 0.0

# 快速单遍的起步折扣：预算码率先打这个折扣再编（VBV 会自然贴着上限走）。
# 实测（噪声素材，最坏情况）单遍贴 cap 时可能超出预算 1~4%，所以 3% 折扣
# 是"一次成功率"与"体积利用率"的折中。
FAST_MARGIN = 0.03

# 欠标时是否追加编码把剩余空间用满（默认不要：实测普通素材两遍 ABR 已到 98%）
FILL_TRIGGER = 0.05

# 允许的修正轮数：够收敛，又不至于无限重跑
MAX_ROUNDS = 3

# 音频降级阶梯（kbps）：预算不够时依次尝试
AUDIO_LADDER = (128, 96, 64, 48)


class Feasibility(Enum):
    OK = "ok"                       # 可以做到
    TIGHT = "tight"                 # 勉强，画质会很差
    AUDIO_WINS = "audio_wins"       # 音频就吃掉大半预算，需允许压缩音频
    IMPOSSIBLE = "impossible"       # 数学上做不到


@dataclass
class SizeBudget:
    """一次「目标体积」的完整预算方案。"""

    target_bytes: int
    duration: float
    container: str

    audio_kbps: int = 0             # 实际采用的音频码率
    audio_streams: int = 1          # 实际保留的音轨数
    video_kbps: int = 0
    overhead_fixed: int = DEFAULT_FIXED_BYTES   # 索引/头部的固定字节
    overhead_ratio: float = DEFAULT_RATIO       # 容器结构占比

    feasibility: Feasibility = Feasibility.OK
    reason: str = ""                # 不可行/勉强时的说明
    suggestions: list[str] = field(default_factory=list)

    @property
    def audio_bytes(self) -> int:
        """音频占用字节。

        实测 AAC 的实际输出比标称码率多约 1.9%（帧对齐 + 容器头），
        这里按 1.03 预留，避免"预算说装得下、实际超一点"。
        """
        nominal = self.audio_kbps * 1000 / 8 * self.duration * self.audio_streams
        return int(nominal * AUDIO_OVERHEAD)

    @property
    def overhead_bytes(self) -> int:
        """容器开销字节：固定索引 + 结构占比。短视频以固定部分为主。"""
        return int(self.overhead_fixed + self.target_bytes * self.overhead_ratio)

    @property
    def overhead(self) -> float:
        """等效开销比例（给 UI 显示与旧逻辑兼容）。"""
        return self.overhead_bytes / self.target_bytes if self.target_bytes else 0.0

    @property
    def usable_bytes(self) -> int:
        """扣掉封装开销后真正能给音视频的字节。"""
        return max(0, self.target_bytes - self.overhead_bytes)

    @property
    def video_bytes(self) -> int:
        return max(0, self.usable_bytes - self.audio_bytes)

    @property
    def ok(self) -> bool:
        return self.feasibility in (Feasibility.OK, Feasibility.TIGHT)


def _min_feasible_mb(budget: SizeBudget, duration: float) -> float:
    """算出"装得下音频 + 最低视频码率"所需的最小目标 MB（含容器开销）。"""
    stream = (budget.audio_bytes
              + MIN_VIDEO_KBPS * 1000 / 8 * duration) * AUDIO_OVERHEAD
    total = (stream + budget.overhead_fixed) / (1.0 - budget.overhead_ratio)
    return total / 1024 / 1024


def _target_bytes(target_mb: float) -> int:
    """MB 按 1024 进制理解为字节（与网盘/资源管理器显示一致）。"""
    return int(round(target_mb * 1024 * 1024))


def container_overhead(container: str, target_mb: float) -> tuple[int, float]:
    """容器开销：返回 (字节, 占目标的比例)。

    不依赖素材，所以界面在还没拖入文件时也能显示
    "这个容器会吃掉目标里多少" —— 只是把公式拿出来共用，别在 UI 里重算一遍。
    """
    target_bytes = _target_bytes(target_mb)
    fixed = CONTAINER_FIXED_BYTES.get(container, DEFAULT_FIXED_BYTES)
    ratio = CONTAINER_RATIO.get(container, DEFAULT_RATIO)
    cost = int(fixed + target_bytes * ratio)
    return cost, (cost / target_bytes if target_bytes else 0.0)


def _audio_kbps_of(info: MediaInfo | None) -> int:
    """源音频码率；copy 时用它，缺失时按声道数估。"""
    if not info or not info.audios:
        return 0
    total = sum(a.bit_rate for a in info.audios if a.bit_rate > 0)
    if total > 0:
        return int(total / 1000)
    # 探测不到码率时按声道数粗估
    a = info.audios[0]
    return 192 if (a.channels or 0) > 2 else 128


def solve(info: MediaInfo | None, target_mb: float, container: str,
          audio_mode: str = "auto", audio_kbps: int = 128,
          allow_audio_shrink: bool = False) -> SizeBudget:
    """给定目标体积，算出一套可执行的预算。

    audio_mode:
        "copy"  保留原始音轨（码率取源实测）
        "none"  去音轨，全部预算给视频
        其它值（aac / mp3 / opus …）一律按「重编码到 audio_kbps」处理 ——
        具体用哪个编码器由 size_service 按容器决定。
        注意：**降不降音频码率不由这里决定**，只看 allow_audio_shrink。
    allow_audio_shrink:
        为 True 时，预算不够会沿 AUDIO_LADDER 自动降音频码率，
        而不是报「做不到」。
    """
    target_bytes = _target_bytes(target_mb)
    duration = info.duration if info else 0.0
    fixed = CONTAINER_FIXED_BYTES.get(container, DEFAULT_FIXED_BYTES)
    ratio = CONTAINER_RATIO.get(container, DEFAULT_RATIO)
    budget = SizeBudget(target_bytes=target_bytes, duration=max(duration, 0.001),
                        container=container, overhead_fixed=fixed,
                        overhead_ratio=ratio)

    if not info or duration <= 0:
        budget.feasibility = Feasibility.IMPOSSIBLE
        budget.reason = "读不到视频时长，无法按体积推算码率"
        return budget

    has_audio = bool(info.audios) and audio_mode != "none"
    src_audio_kbps = _audio_kbps_of(info) if has_audio else 0
    streams = len(info.audios) if (audio_mode == "copy" and has_audio) else (1 if has_audio else 0)

    # ---- 音频码率与是否降级 ----
    if not has_audio:
        budget.audio_kbps = 0
        budget.audio_streams = 0
    elif audio_mode == "copy":
        budget.audio_kbps = src_audio_kbps or 128
        budget.audio_streams = streams
    else:
        budget.audio_kbps = audio_kbps
        budget.audio_streams = 1

    video_bits = budget.video_bytes * 8
    video_kbps = video_bits / 1000 / duration

    # ---- 预算不够：按阶梯降音频（仅在允许时） ----
    if video_kbps < MIN_VIDEO_KBPS and has_audio and allow_audio_shrink:
        original_audio = budget.audio_kbps
        for candidate in AUDIO_LADDER:
            if candidate >= original_audio:
                continue
            budget.audio_kbps = candidate
            budget.audio_streams = 1
            trial = budget.video_bytes * 8 / 1000 / duration
            if trial >= MIN_VIDEO_KBPS:
                budget.reason = "目标偏小，音频已从 %d 降到 %d kbps" % (
                    original_audio, candidate)
                budget.feasibility = Feasibility.TIGHT if trial < MIN_VIDEO_KBPS * 4 else Feasibility.OK
                break
        video_kbps = budget.video_bytes * 8 / 1000 / duration

    budget.video_kbps = max(1, int(video_kbps))

    # ---- 可行性判定 ----
    multi = budget.audio_streams > 1
    if budget.video_bytes <= 0:
        budget.feasibility = Feasibility.IMPOSSIBLE
        need_mb = _min_feasible_mb(budget, duration)
        if multi:
            budget.reason = "%d 条音轨共需 %.1f MB，已超过目标 %.1f MB" % (
                budget.audio_streams, budget.audio_bytes / 1024 / 1024, target_mb)
            budget.suggestions = [
                "把目标提高到至少 %.1f MB" % need_mb,
                "或改用 AAC 重编码音频（自动只保留第 1 条音轨）",
                "或选择「移除音轨」把空间全部给画面",
            ]
        else:
            budget.reason = "音频单独就要 %.1f MB，已超过目标 %.1f MB" % (
                budget.audio_bytes / 1024 / 1024, target_mb)
            budget.suggestions = [
                "把目标提高到至少 %.1f MB" % need_mb,
                "或勾选「允许压缩音频」，让音轨降码率腾出空间",
                "或改用 AAC 重编码音频（体积小很多）",
            ]
    elif budget.video_kbps < MIN_VIDEO_KBPS:
        budget.feasibility = Feasibility.AUDIO_WINS
        need_mb = _min_feasible_mb(budget, duration)
        if multi:
            budget.reason = "%d 条音轨占了 %.1f MB，视频只剩 %d kbps" % (
                budget.audio_streams, budget.audio_bytes / 1024 / 1024,
                budget.video_kbps)
            budget.suggestions = [
                "把目标提高到至少 %.1f MB" % need_mb,
                "或改用 AAC 重编码（只保留第 1 条音轨，体积小很多）",
                "或选择「移除音轨」",
            ]
        else:
            budget.reason = "视频只剩 %d kbps，画质会严重崩坏（低于 %d kbps 不建议）" % (
                budget.video_kbps, MIN_VIDEO_KBPS)
            budget.suggestions = [
                "把目标提高到至少 %.1f MB" % need_mb,
                "或勾选「允许压缩音频」",
                "或勾选「去除音轨」把全部空间给画面",
            ]

    # ---- 源文件本来就比目标小：没必要压 ----
    if info.size and info.size <= target_bytes:
        budget.suggestions.insert(0, "源文件 %.1f MB 已经小于目标 %.1f MB，无需压缩" % (
            info.size / 1024 / 1024, target_mb))

    return budget


def correct_kbps(budget: SizeBudget, kbps_used: int, achieved_bytes: int,
                 history: list[tuple[int, int]] | None = None) -> int:
    """首遍超差后，用实得体积外推新码率。

    必须用「本轮码率 ↔ 本轮实得体积」配对，否则会发散——这一点实测过：
    拿旧码率配新体积，第二轮会从 -2% 直接跳到 +48%。

    这个公式同时用于两个方向：
    * 超标（achieved > 目标）→ 降码率；
    * 欠标 → 升码率（仅当调用方决定要补跑时）。
    只是 ratio 被钳在合理范围内，避免极端素材把码率拉到离谱值。

    history: 之前的 (码率, 实得字节) 观测。短视频上码率↔体积并不线性 ——
        实测在 3183 kbps→86%、3526 kbps→105% 之间线性外推会在两点间来回
        震荡（3183→3543→3214→…），而**在跨越目标的两点之间做对数插值**
        每次都收敛到 ~3400。所以有历史时优先插值，没有才退回线性外推。
    """
    if achieved_bytes <= 0 or kbps_used <= 0 or budget.duration <= 0:
        return kbps_used
    audio_bytes = budget.audio_bytes
    got_video = max(1, achieved_bytes - audio_bytes)
    want_video = max(1, budget.video_bytes)
    ratio = want_video / got_video
    # 限制单次调整幅度，避免极端素材（全黑片尾）把码率拉到离谱值
    ratio = max(0.5, min(2.5, ratio))
    linear = max(MIN_VIDEO_KBPS, int(kbps_used * ratio))

    if not history:
        return linear

    # 找一对「一个欠标、一个超标」的观测，在它们之间按 log(码率) 插值。
    # 用体积比例（而非绝对字节）插值，这样目标变了也成立。
    pts = [(k, v / want_video) for k, v in history if k > 0 and v > 0]
    pts.append((kbps_used, got_video / want_video))
    below = [p for p in pts if p[1] < 1.0]
    above = [p for p in pts if p[1] >= 1.0]
    if not below or not above:
        return linear
    lo = max(below, key=lambda p: p[1])      # 最接近目标的欠标点
    hi = min(above, key=lambda p: p[1])      # 最接近目标的超标点
    if hi[0] <= lo[0] or hi[1] <= lo[1]:
        return linear
    t = (1.0 - lo[1]) / (hi[1] - lo[1])
    interp = math.exp(math.log(lo[0]) + t * (math.log(hi[0]) - math.log(lo[0])))
    return max(MIN_VIDEO_KBPS, int(round(interp)))


def needs_fill(budget: SizeBudget, achieved_bytes: int, fill_enabled: bool) -> bool:
    """欠标是否要补跑把空间用满（只在用户勾了「尽量用满」时）。

    默认不补跑，因为实测普通素材两遍 ABR 已经到目标的 96~98%，为剩下那几
    个百分点再跑一轮不划算。但有些素材会明显欠标（实测 63 Mbps 噪声素材
    严格档只出 41%），这种浪费用户看得出来，所以要给他一个开关。

    「填不满」的两类素材（实测，见 fill_hopeless）：
      * 纯色/静止：抬到 12000k 体积仍是 5 KB，纹丝不动；
      * 纯渐变：抬到 5000k 到 93% 就封顶，再抬也不涨。
    所以补跑必须带「涨幅 <5% 就停手」的判据，否则会在死循环里空转。
    """
    if not fill_enabled or budget.target_bytes <= 0:
        return False
    return achieved_bytes < budget.target_bytes * (1.0 - FILL_TRIGGER)


def fill_hopeless(budget: SizeBudget, prev_bytes: int, now_bytes: int) -> bool:
    """上一轮加了码率但体积几乎没涨 → 内容撑不满，停止补跑。"""
    if prev_bytes <= 0:
        return False
    growth = (now_bytes - prev_bytes) / prev_bytes
    return growth < 0.05


def estimate_output_bytes(budget: SizeBudget) -> int:
    """按预算估算最终体积（用于 UI 显示）。"""
    if not budget.duration:
        return 0
    stream_bytes = (budget.video_kbps + budget.audio_kbps * budget.audio_streams) * 1000 / 8 * budget.duration
    # 两段式开销下不能简单除，需反解 target = stream + fixed + target*ratio
    return int(stream_bytes + budget.overhead_fixed
               + (stream_bytes * budget.overhead_ratio / (1.0 - budget.overhead_ratio)))


def describe_budget(budget: SizeBudget) -> str:
    """给界面用的一行摘要。"""
    parts = ["视频 %d kbps" % budget.video_kbps]
    if budget.audio_kbps:
        parts.append("音频 %d kbps×%d" % (budget.audio_kbps, budget.audio_streams))
    else:
        parts.append("无音轨")
    parts.append("预计 %.2f MB" % (estimate_output_bytes(budget) / 1024 / 1024))
    return " · ".join(parts)
