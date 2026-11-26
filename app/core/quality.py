"""预检阶段：画质评估与分辨率推荐。

解决的问题：目标体积"数学上可行"不代表"结果能看"。
2 小时 1080p 压到 50MB，视频码率只有 60kbps，编码会自动降到低于 MIN_VIDEO_KBPS
被拒；但 200kbps 这种"能编出来但满屏色块"的目标，以前没有任何提示。

判据用 bits-per-pixel-per-frame（bpp）：
    bpp = 视频码率(bps) / (宽 × 高 × 帧率)

实测校准（1920x1080@30，x264 veryfast，与原片比 SSIM）：

    bpp 0.0048 → SSIM 0.941     bpp 0.0193 → SSIM 0.978
    bpp 0.0096 → SSIM 0.964     bpp 0.0402 → SSIM 0.987
                                bpp 0.0804 → SSIM 0.994

注意 SSIM 对低码率偏乐观（人眼对块效应更敏感），所以评级线定得比 SSIM
数值更保守一些。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ffmpeg import MediaInfo

# 画质等级阈值（bpp 下限，从高到低匹配）
# 参考上表实测：0.02 左右是"够用"的下限，0.01 以下肉眼可见劣化
QUALITY_LADDER = (
    (0.060, "excellent", "优秀"),
    (0.030, "good", "良好"),
    (0.018, "fair", "一般"),
    (0.010, "poor", "明显劣化"),
    (0.000, "bad", "严重劣化"),
)

# 推荐分辨率候选（按高度），与 UI 的分辨率选项保持一致
CANDIDATE_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240)

# 认为"可以接受"的最低 bpp；低于它就建议降分辨率
ACCEPTABLE_BPP = 0.018


class QualityLevel(Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    BAD = "bad"

    @property
    def label(self) -> str:
        return {"excellent": "优秀", "good": "良好", "fair": "一般",
                "poor": "明显劣化", "bad": "严重劣化"}[self.value]

    @property
    def is_acceptable(self) -> bool:
        return self in (QualityLevel.EXCELLENT, QualityLevel.GOOD,
                        QualityLevel.FAIR)


@dataclass
class QualityAssessment:
    bpp: float = 0.0
    level: QualityLevel = QualityLevel.FAIR
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_kbps: int = 0

    # 推荐方案（None = 不建议调整）
    suggest_height: int = 0
    suggest_kbps: int = 0
    suggest_bpp: float = 0.0
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def needs_action(self) -> bool:
        return bool(self.suggest_height) and self.suggest_height != self.height


def measure_bpp(video_kbps: int, width: int, height: int, fps: float) -> float:
    if not (width and height and fps) or video_kbps <= 0:
        return 0.0
    return video_kbps * 1000.0 / (width * height * fps)


def classify(bpp: float) -> QualityLevel:
    for threshold, key, _label in QUALITY_LADDER:
        if bpp >= threshold:
            return QualityLevel(key)
    return QualityLevel.BAD


# AV1 的编码效率远高于 H.264：同画质只需约 62% 的码率（实测：
# 1708x1212 / 9s 真实素材，同 SSIM 下 AV1 软编与 NVENC 硬编都约为
# x264 medium 的 0.62 倍码率，软硬编几乎一致）。
# 阈值表按 H.264 校准，所以 AV1 的 bpp 要**除以** 0.62（即等效于
# x264 在 1.61 倍码率下的观感），否则画质提示会系统性偏悲观。
# ⚠️ av1_amf 用的是同一个 0.62：AV1 的效率优势来自编码标准本身，
# 与哪家硬件实现无关（NVENC 与 SVT-AV1 软编实测几乎一致就是证据）。
# 这个数字没在 A 卡上单独测过 —— 手上没有 RDNA3+ 的机器。
AV1_BPP_RATIO = 0.62
_AV1_CODECS = {"libsvtav1", "av1_nvenc", "av1_amf", "av1_qsv", "av1_vaapi"}


def assess(info: MediaInfo | None, video_kbps: int,
           scale_height: int = 0, codec: str = "") -> QualityAssessment:
    """评估当前码率下的画质，并在必要时给出降分辨率方案。

    scale_height：用户在分辨率下拉里选的高度（0 = 保持原始分辨率）。
    🔴 必须传进来 —— 评估的是"这次编码真的会发生的事"，用户已经手动选了
    480p 时，还按源的 1280p 算 bpp 就是在评估一个不会发生的编码
    （实测：选 480p 后上方提示仍停留在 1280p 的结论上）。

    只缩不放与编码行为一致：用户选的高度比源高时，实际输出仍是源高度。
    """
    if not info or not info.video:
        return QualityAssessment(video_kbps=video_kbps)

    width = info.video.width or 0
    height = info.video.height or 0
    if scale_height and 0 < scale_height < height:
        # 跟随编码行为的换算：等比缩放，宽取偶
        aspect = (width / height) if height else 0.0
        height = scale_height
        width = int(height * aspect) // 2 * 2 if aspect else 0
    fps = info.fps or 30.0
    bpp = measure_bpp(video_kbps, width, height, fps)
    # AV1 同 bpp 的观感更好：折算成"x264 等效 bpp"再套阈值 ——
    # 否则阈值表（按 H.264 校准）会对 AV1 系统性偏悲观
    if codec in _AV1_CODECS:
        bpp = bpp / AV1_BPP_RATIO

    result = QualityAssessment(bpp=bpp, level=classify(bpp), width=width,
                               height=height, fps=fps, video_kbps=video_kbps)

    if result.level.is_acceptable:
        return result

    # 画质不够：找一个能让 bpp 达标的更低分辨率
    best = _pick_height(info, video_kbps, ACCEPTABLE_BPP)
    if best and best[0] < height:
        new_h, new_kbps, new_bpp = best
        result.suggest_height = new_h
        result.suggest_kbps = new_kbps
        result.suggest_bpp = new_bpp
        result.reason = (
            "%dp 下码率仅 %d kbps（bpp %.3f，低于 %.3f），画面会出现明显色块；"
            "降到 %dp 后同等体积下 bpp 升到 %.3f，观感明显更好"
            % (height, video_kbps, bpp, ACCEPTABLE_BPP, new_h, new_bpp))
    else:
        # 连最低档也救不了：算出"要多少码率才够"，并换算成还差多少倍体积
        need_kbps = int(ACCEPTABLE_BPP * width * height * fps / 1000)
        ratio = need_kbps / video_kbps if video_kbps else 0
        result.suggest_kbps = need_kbps
        # 最低档从 CANDIDATE_HEIGHTS 取，别硬编码 —— 加了 360/240 之后
        # 这里还写 480p 就在说谎
        lowest = CANDIDATE_HEIGHTS[-1]
        result.reason = (
            "%dp 下码率仅 %d kbps（bpp %.3f），画质损失严重；"
            "即使降到 %dp 也达不到可接受水平，建议把目标提高到约 %.1f 倍"
            % (height, video_kbps, bpp, lowest, ratio))
    return result


def _pick_height(info: MediaInfo, video_kbps: int, target_bpp: float):
    """在候选高度里挑一个 bpp 达标且分辨率最高的。

    码率不变、只降分辨率时 bpp 会升高（像素少了），
    所以这里算的是"同一码率下"的 bpp。

    用户已手动选了某个分辨率时（scale_height），建议仍然可以比它低：
    用户的目标是「装进目标体积且能看」，当前选择达不到时，给出更低的
    选项是帮他，不是推翻他。
    """
    src_h = info.video.height or 0
    src_w = info.video.width or 0
    if not (src_h and src_w):
        return None
    fps = info.fps or 30.0
    aspect = src_w / src_h

    for h in CANDIDATE_HEIGHTS:
        if h >= src_h:
            continue                      # 不放大
        w = int(h * aspect) // 2 * 2
        bpp = measure_bpp(video_kbps, w, h, fps)
        if bpp >= target_bpp:
            return h, video_kbps, bpp
    return None


def describe(assessment: QualityAssessment) -> str:
    """给界面用的一行摘要。"""
    return ("预计画质：%s（%dp · %d kbps · bpp %.3f）"
            % (assessment.level.label, assessment.height,
               assessment.video_kbps, assessment.bpp))
