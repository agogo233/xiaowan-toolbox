"""编码预设与参数模型。

预设命名参考小丸工具箱的习惯：以「用途 + 画质倾向」组织，
让不懂编码参数的人也能直接选一个就用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .utils import is_macos, is_windows


class RateMode(str, Enum):
    CRF = "crf"           # 恒定质量（推荐）
    BITRATE = "bitrate"   # 目标码率（单遍）
    TWOPASS = "2pass"     # 二次编码，体积最可控
    TARGET_SIZE = "size"  # 目标体积


class Container(str, Enum):
    MP4 = "mp4"
    MKV = "mkv"
    MOV = "mov"
    WEBM = "webm"


CONTAINER_LABEL = {
    Container.MP4: "MP4（兼容性最好）",
    Container.MKV: "MKV（支持多音轨/字幕）",
    Container.MOV: "MOV（剪辑友好）",
    Container.WEBM: "WebM（网页）",
}


@dataclass
class VideoSettings:
    codec: str = "libx264"                  # libx264 / libx265 / h264_videotoolbox / hevc_videotoolbox
    rate_mode: RateMode = RateMode.CRF
    crf: float = 23.0                       # 1.0–63.0，一位小数；libx264 会取整并钳制到 51
    bitrate_kbps: int = 2000
    preset: str = "medium"                  # x264/x265 speed preset
    tune: str = ""                          # film / animation / grain / stillimage / fastdecode
    profile: str = ""                       # 留空 = auto
    scale_height: int = 0                   # 0 = 保持原始高度
    fps: float = 0.0                        # 0 = 原始帧率
    audio_codec: str = "copy"               # copy / aac / mp3 / none
    audio_bitrate_kbps: int = 128
    audio_channels: int = 0                 # 0 = 原始
    container: Container = Container.MP4
    faststart: bool = True
    pix_fmt: str = ""                       # 留空 = auto
    keep_metadata: bool = True              # 保留拍摄日期/设备/GPS 等（写入 MP4 iTunes tag）
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Preset:
    """一键预设。"""

    key: str
    name: str
    tagline: str
    detail: str
    video: VideoSettings


def _v(**kw) -> VideoSettings:
    base = VideoSettings()
    for k, val in kw.items():
        setattr(base, k, val)
    return base


# --------------------------------------------------------------------------- #
# 硬件编码器：不写死型号，运行时按平台 + 真机可用性决定
# --------------------------------------------------------------------------- #

HW_H264 = "auto_hw"          # 占位符，提交任务前解析成真正的编码器名
HW_H265 = "auto_hw_hevc"
HW_AV1 = "auto_hw_av1"

_HW_TABLE = {
    HW_H264: {
        "mac": ["h264_videotoolbox"],
        "win": ["h264_nvenc", "h264_qsv", "h264_amf"],
        "linux": ["h264_nvenc"],
    },
    HW_H265: {
        "mac": ["hevc_videotoolbox"],
        "win": ["hevc_nvenc", "hevc_qsv", "hevc_amf"],
        "linux": ["hevc_nvenc"],
    },
    # AV1 硬件编码：只有 NVIDIA NVENC 支持（RTX 40 系起）；
    # macOS 没有 AV1 硬编（VideoToolbox 不提供），Intel/AMD 的走 QSV/AMF
    # 的话另算，目前先只接 NVENC —— 本机就是 N 卡。
    HW_AV1: {
        "win": ["av1_nvenc"],
        "linux": ["av1_nvenc"],
    },
}

HW_ENCODER_LABEL = {
    "h264_videotoolbox": "Apple VideoToolbox",
    "hevc_videotoolbox": "Apple VideoToolbox",
    "h264_nvenc": "NVIDIA NVENC",
    "hevc_nvenc": "NVIDIA NVENC",
    "av1_nvenc": "NVIDIA NVENC",
    "h264_qsv": "Intel Quick Sync",
    "hevc_qsv": "Intel Quick Sync",
    "h264_amf": "AMD AMF",
    "hevc_amf": "AMD AMF",
    "h264_vaapi": "VAAPI",
    "hevc_vaapi": "VAAPI",
}

# 试编时要额外加的参数
_HW_PROBE_EXTRA = {
    "h264_videotoolbox": ["-allow_sw", "1"],
    "hevc_videotoolbox": ["-allow_sw", "1"],
}


def _platform_key() -> str:
    if is_macos():
        return "mac"
    if is_windows():
        return "win"
    return "linux"


def hw_candidates(codec: str = HW_H264) -> list[str]:
    """当前平台的硬件编码器候选（优先 → 次选）。"""
    return list(_HW_TABLE.get(codec, {}).get(_platform_key(), []))


def resolve_hw_encoder(codec: str, env=None) -> str | None:
    """在本机挑一个真能用的硬件编码器，没有则返回 None。"""
    if env is None or not getattr(env, "ffmpeg", None):
        return None
    for name in hw_candidates(codec):
        if env.encoder_usable(name, _HW_PROBE_EXTRA.get(name)):
            return name
    return None


def resolve_codec(codec: str, env=None) -> str:
    """把 auto_hw* 哨兵解析成真正的编码器名；解析不出就回退软编。"""
    if codec == HW_H264:
        return resolve_hw_encoder(HW_H264, env) or "libx264"
    if codec == HW_H265:
        return resolve_hw_encoder(HW_H265, env) or "libx265"
    if codec == HW_AV1:
        # AV1 的软编回退是 libsvtav1（不是 libx264），保证"选了 AV1 就还是 AV1"
        return resolve_hw_encoder(HW_AV1, env) or "libsvtav1"
    return codec


_HW_NAMES = {
    "h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv", "h264_amf", "hevc_amf",
    "h264_videotoolbox", "hevc_videotoolbox", "h264_vaapi", "hevc_vaapi",
    "av1_nvenc",
}


def is_hw_encoder(codec: str) -> bool:
    return codec in _HW_NAMES


def hw_unusable_reason(sentinel: str, env=None) -> str:
    """硬件家族挑不出可用编码器时，探测到的原因（人话，如「NVIDIA 驱动过旧」）。

    一次都没探测过、或 ffmpeg 里根本没编这个编码器时返回空串 —— 那时
    「本机不可用」这种模糊说法反而更准。
    """
    for cand in hw_candidates(sentinel):
        if env is not None and env.encoder_available(cand):
            return env.unusable_reason(cand)
    return ""


def codec_available(codec: str, env=None) -> tuple[bool, str]:
    """这个编码器项在本机能不能**真的用上**；不能时给出原因。

    🔴 硬件项必须按**家族**判断，不能拿 `resolve_codec()` 的结果去问 ——
    挑不出硬件时它就是返回 "libx264"（静默回退软编），照它判断会把用不了的
    「H.265 硬件加速」显示成「可用」，用户选了以为在跑硬件，实际出片慢好几倍。

    没有 ffmpeg 时一律返回可用（与色深下拉同一约定）：交给编码时报错，
    别把整个下拉灰成一片。
    """
    if codec in ("copy", "none") or env is None or not getattr(env, "ready", False):
        return True, ""
    if codec in _HW_TABLE:
        if resolve_hw_encoder(codec, env):
            return True, ""
        return False, hw_unusable_reason(codec, env) or "本机没有可用的硬件编码器"
    ok = env.encoder_usable(codec)
    return ok, "" if ok else (env.unusable_reason(codec) or "本机不可用")


def codec_labels(env=None) -> list[tuple[str, str]]:
    """编码器下拉项：按平台与本机硬件动态生成。"""
    out = [
        ("libx264", "H.264 / AVC（兼容性最好）"),
        ("libx265", "H.265 / HEVC（同画质体积更小）"),
    ]
    for sentinel, family in ((HW_H264, "H.264"), (HW_H265, "H.265")):
        real = resolve_hw_encoder(sentinel, env)
        if real:
            label = HW_ENCODER_LABEL.get(real, real)
            out.append((sentinel, f"{family} 硬件加速（{label}）"))
        else:
            # 不可用时把探测到的原因带上（如"NVIDIA 驱动过旧"），
            # 否则用户只能看到"不可用"，不知道该做什么。
            # 🔴 原因不要自己带前导逗号：外面已经有一对括号了
            # （踩过：拼出「H.264 硬件加速（，NVIDIA 驱动过旧）」，那个逗号很显眼）
            detail = hw_unusable_reason(sentinel, env) or "本机不可用，将回退软编"
            out.append((sentinel, f"{family} 硬件加速（{detail}）"))

    # AV1 硬件加速：只有 RTX 40 系起的 N 卡有 AV1 编码单元。
    # 软件列表里已有 AV1 软编（libsvtav1），这里加的是"快一个数量级"的硬编。
    real = resolve_hw_encoder(HW_AV1, env)
    if real:
        label = HW_ENCODER_LABEL.get(real, real)
        out.append((HW_AV1, f"AV1 硬件加速（{label} · 最快）"))
    else:
        detail = hw_unusable_reason(HW_AV1, env) or "本机不可用"
        out.append((HW_AV1, f"AV1 硬件加速（{detail}）"))
    # AV1：同画质下比 H.265 再省 36% 左右（实测七段真实素材求交，平均 35.9%），
    # 且不像 x265 那样随画质提高而衰减。但较新设备才能硬解。
    # 内置 ffmpeg 带 libsvtav1，始终可提供（软编）。
    out.insert(2, ("libsvtav1",
                   "AV1 / SVT-AV1（体积最小，需较新设备播放）"))
    out.append(("copy", "不重新编码（仅换容器）"))
    return out


PRESETS: list[Preset] = [
    Preset(
        key="fast",
        name="极速压缩",
        tagline="体积优先",
        detail="H.264 · CRF 28 · veryfast · 适合快速分享，画质有损失",
        video=_v(codec="libx264", crf=28, preset="veryfast", audio_codec="aac",
                 audio_bitrate_kbps=96),
    ),
    Preset(
        key="balanced",
        name="均衡 1080p",
        tagline="推荐",
        detail="H.264 · CRF 23 · medium · 通用场景的最佳平衡点",
        video=_v(codec="libx264", crf=23, preset="medium", audio_codec="aac",
                 audio_bitrate_kbps=128),
    ),
    Preset(
        key="quality",
        name="高质量收藏",
        tagline="画质优先",
        # 容器用 MP4 而不是 MKV：MKV 浏览器（<video> / MSE）根本不认，
        # 想「自己存着也想偶尔在网页里放一下」就必须是 MP4 + hvc1。
        # ⚠️ 这里的压缩率说法被实测改过两次，别凭印象再改回去：
        # 原来无条件写「省三成」→ 实测（2026-11-12，七段真实素材同 SSIM 求交）
        # 只有 13~42%，平均 27.7%，而且**画质越高省得越少**：
        #   大运动01: 31.3%(低) → 28.2%(中) → 8.0%(高)
        #   换装:     21.3%      → 10.5%      → 8.4%
        #   实拍01:   41.5%      → 32.5%      → 25.0%
        # 本预设是 CRF 20（高画质段），恰好踩在最不准的位置，所以文案
        # 必须把衰减说出来，不能只写"省两三成"。
        detail="H.265 · CRF 20 · slow · 比 H.264 省一到三成，画质越高省得越少；"
               "网页可能播不了",
        video=_v(codec="libx265", crf=20, preset="slow", audio_codec="aac",
                 audio_bitrate_kbps=192, container=Container.MP4),
    ),
    Preset(
        key="tiny",
        name="极限小体积",
        tagline="体积最小",
        detail="AV1 · CRF 38 · preset 4 · 10bit · 同等画质下比 H.265 更小、"
               "编码更快（SVT-AV1 并行优化）；需较新设备播放",
        video=_v(codec="libsvtav1", crf=38, preset="slower",
                 audio_codec="libopus", audio_bitrate_kbps=96,
                 container=Container.MKV, pix_fmt="yuv420p10le"),
    ),
    Preset(
        key="hevc_compat",
        # 🔴 名称 + 标签必须塞进卡片内宽（168-22=146px）：
        # 「H.265 兼容压缩」(107px) + 「老设备友好」(67px) 要 182px，
        # 名称会被省略成「H.265 …」——标题直接看不见。现在 130px，余 16px。
        # 改这两行前先跑 tests/preset_chip_test.py。
        name="H.265 兼容",
        # 标签不能写「老设备」—— 那恰好是 H.265 最播不了的地方（见下）。
        # 「省体积」才是它相对 H.264 的真实优势，且宽 45px 放得下。
        tagline="省体积",
        # 🔴 这里曾经写「老设备也能硬解」，是**错的**（2026-11-12 核实）：
        # 浏览器不做 HEVC 软解，只把解码委托给系统/GPU，链路一断就黑屏 ——
        # 老设备恰恰是 H.265 最播不了的地方。真正的"老设备友好"是 H.264。
        # 这个预设现在的定位是：同画质比 H.264 小、又比 AV1 播得开（新设备）。
        detail="H.265 · CRF 28 · veryslow · 比 AV1 播得开（2018 年后设备基本能硬解），"
               "体积比 H.264 小；老设备和网页仍可能播不了",
        video=_v(codec="libx265", crf=28, preset="veryslow", audio_codec="aac",
                 audio_bitrate_kbps=96, container=Container.MP4),
    ),
    Preset(
        key="social",
        name="社交平台 720p",
        tagline="微信",
        detail="H.264 · CRF 25 · 720p，微信直接发送不卡顿",
        video=_v(codec="libx264", crf=25, preset="fast", scale_height=720,
                 audio_codec="aac", audio_bitrate_kbps=96),
    ),
    Preset(
        key="hw",
        name="硬件加速",
        tagline="最快出片",
        detail="H.264 硬件编码（自动挑 NVENC/QSV/AMF 里可用的），出片最快；"
               "要同体积画质更好，可手选 AV1 / H.265 的硬件加速",
        video=_v(codec=HW_H264, rate_mode=RateMode.BITRATE,
                 bitrate_kbps=6000, audio_codec="aac", audio_bitrate_kbps=128),
    ),
    Preset(
        key="anim",
        name="动漫番剧",
        tagline="二次元",
        detail="H.264 · CRF 21 · tune animation · 保留线条与大色块",
        video=_v(codec="libx264", crf=21, preset="slow", tune="animation",
                 audio_codec="aac", audio_bitrate_kbps=128),
    ),
    Preset(
        key="remux_copy",
        name="只换壳",
        tagline="不重编码",
        detail="流复制，秒级完成，画质零损失（仅换容器）",
        video=_v(codec="copy", audio_codec="copy"),
    ),
]

PRESETS_BY_KEY = {p.key: p for p in PRESETS}

# 编码器下拉项请用 codec_labels(env) —— 它会在真机上探测硬件编码器是否可用。

X264_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
]

TUNE_OPTIONS = [
    ("", "不指定"),
    ("film", "实拍影片"),
    ("animation", "动画 / 动漫"),
    ("grain", "保留颗粒感"),
    ("stillimage", "静态画面为主"),
    ("fastdecode", "优先解码速度"),
]

SCALE_OPTIONS = [
    (0, "保持原始分辨率"),
    (2160, "2160p (4K)"),
    (1440, "1440p (2K)"),
    (1080, "1080p (全高清)"),
    (720, "720p (高清)"),
    (480, "480p (标清)"),
    (360, "360p (流畅)"),
    (240, "240p (极小体积)"),
]

# 色深：10bit 对 8bit 源也能减少色带（实测纯渐变画面灰阶级数 34→40 级）。
# 注意收益量级：同体积下真实素材只有 ΔSSIM +0.0003~0.0009，
# 所以只作为可选项，不进任何预设的默认值（AV1 预设除外）。
PIX_FMT_OPTIONS = [
    ("", "8bit（默认）"),
    ("yuv420p10le", "10bit（色带更少）"),
]


def ten_bit_note(codec: str) -> str:
    """10 位色深在当前编码器下的**播放**兼容性（与"能不能编"是两件事）。

    H.264 的 10bit 就是 Hi10P —— 到 2026 年仍然没有任何设备能硬解；
    H.265 / AV1 的 10bit 反而是主流格式。两者混成一句"兼容性稍差"会误导用户。
    """
    if codec == "libx264" or codec.startswith("h264_"):
        return ("H.264 的 10bit 就是 Hi10P：至今没有设备能硬解，手机 / 电视 / "
                "网页播放器基本解不动，只适合在电脑上软解观看。")
    if codec == "libvpx-vp9":
        return ("VP9 的 10bit 是 Profile 2：Chrome / 安卓能解，老设备多数只支持 8bit。")
    return ("H.265 / AV1 的 10bit 是主流格式：2018 年后的手机、电视、显卡基本都能"
            "硬解；Windows 自带播放器需另装「HEVC 视频扩展」。")

FPS_OPTIONS = [
    (0.0, "保持原始帧率"),
    (60.0, "60 fps"),
    (30.0, "30 fps"),
    (24.0, "24 fps"),
]

AUDIO_CODEC_OPTIONS = [
    ("copy", "直接复制（不重编码）"),
    ("aac", "AAC（通用）"),
    ("libmp3lame", "MP3"),
    ("libopus", "Opus（体积最小）"),
    ("none", "移除音轨"),
]

# --------------------------------------------------------------------------- #
# 音频提取
# --------------------------------------------------------------------------- #

AUDIO_FORMATS = [
    ("mp3", "MP3", "libmp3lame", "320"),
    ("aac", "AAC / M4A", "aac", "256"),
    ("flac", "FLAC（无损）", "flac", ""),
    ("wav", "WAV（无损未压缩）", "pcm_s16le", ""),
    ("opus", "Opus", "libopus", "128"),
    ("m4a", "M4A（Apple 原生）", "aac", "256"),
    ("copy", "原样复制音轨", "copy", ""),
]

AUDIO_BITRATES = ["96", "128", "192", "256", "320"]

SAMPLE_RATES = [
    (0, "保持原始采样率"),
    (48000, "48 kHz"),
    (44100, "44.1 kHz"),
    (32000, "32 kHz"),
    (22050, "22.05 kHz"),
]

CHANNEL_OPTIONS = [
    (0, "保持原始声道"),
    (1, "单声道 Mono"),
    (2, "立体声 Stereo"),
]

# --------------------------------------------------------------------------- #
# 封装混流
# --------------------------------------------------------------------------- #

REMUX_CONTAINERS = [
    (Container.MP4, "MP4", "兼容性最好，适合手机/网页播放"),
    (Container.MKV, "MKV", "支持任意音轨/字幕数量，推荐归档"),
    (Container.MOV, "MOV", "Final Cut / Premiere 剪辑用"),
    (Container.WEBM, "WebM", "网页嵌入，仅支持 VP9/Opus"),
]

# --------------------------------------------------------------------------- #
# 声道下混：5.1 / 7.1 等多声道 → 立体声 / 单声道
# --------------------------------------------------------------------------- #

class DownmixMode(str, Enum):
    STANDARD = "standard"   # ITU-R BS.775 标准下混
    DIALOG = "dialog"       # 对白优先（中置加权 + 动态压缩）
    SURROUND = "surround"   # 保留环绕氛围，低音炮一并混入
    NIGHT = "night"         # 夜间小声看：压动态 + 统一响度
    MONO = "mono"           # 直接塌成单声道


@dataclass
class DownmixSettings:
    """多声道下混参数。增益一律用 dB，负值表示衰减。

    系数上：中置/环绕默认都取 -3dB，这是 ITU-R BS.775 给 5.1→2.0 的
    推荐值（0.707 倍），既能保留声场又不容易削波。
    """

    target_channels: int = 2          # 1 = 单声道 / 2 = 立体声
    center_gain_db: float = -3.0      # 中置声道（对白主要来源）
    surround_gain_db: float = -3.0    # 环绕声道（左后/右后/侧环绕）
    include_lfe: bool = False         # 是否把低音炮（LFE）混进来
    lfe_gain_db: float = -6.0
    dialog_compress: bool = False     # 动态压缩：小声也能听清对白
    normalize_loudness: bool = False  # EBU R128 响度标准化
    loudness_lufs: float = -16.0
    prevent_clip: bool = True         # 末尾加限幅器，防止叠加后爆音
    container: str = "auto"           # auto = 跟随源文件
    audio_bitrate_kbps: int = 192
    sample_rate: int = 0              # 0 = 保持原始
    mode_key: str = DownmixMode.STANDARD.value


@dataclass
class DownmixPreset:
    """下混一键方案。属性名与 Preset 对齐，可直接复用预设卡片。"""

    key: str
    name: str
    tagline: str
    detail: str
    settings: DownmixSettings


def _d(**kw) -> DownmixSettings:
    base = DownmixSettings()
    for k, val in kw.items():
        setattr(base, k, val)
    return base


DOWNMIX_PRESETS: list[DownmixPreset] = [
    DownmixPreset(
        key="standard",
        name="标准立体声",
        tagline="通用",
        detail="中置与环绕各 -3dB，低音炮丢弃 · ITU 标准下混，最不容易出错",
        settings=_d(mode_key=DownmixMode.STANDARD.value),
    ),
    DownmixPreset(
        key="dialog",
        name="对白清晰",
        tagline="看剧 / 访谈",
        detail="中置不衰减 + 动态压缩 · 人声更靠前，小声台词也听得清",
        settings=_d(mode_key=DownmixMode.DIALOG.value,
                    center_gain_db=0.0, surround_gain_db=-6.0,
                    dialog_compress=True),
    ),
    DownmixPreset(
        key="surround",
        name="环绕氛围",
        tagline="电影感",
        detail="环绕不衰减、低音炮混入 -6dB · 保留声场与低频冲击",
        settings=_d(mode_key=DownmixMode.SURROUND.value,
                    center_gain_db=-3.0, surround_gain_db=0.0,
                    include_lfe=True, lfe_gain_db=-6.0),
    ),
    DownmixPreset(
        key="night",
        name="夜间小声",
        tagline="深夜不吵人",
        detail="压平动态 + 响度统一到 -20 LUFS · 爆炸声不吓人，对白照样清楚",
        settings=_d(mode_key=DownmixMode.NIGHT.value,
                    center_gain_db=0.0, surround_gain_db=-6.0,
                    include_lfe=True, lfe_gain_db=-12.0,
                    dialog_compress=True,
                    normalize_loudness=True, loudness_lufs=-20.0),
    ),
    DownmixPreset(
        key="mono",
        name="塌成单声道",
        tagline="最省空间",
        detail="合并为 1.0 单声道 · 老旧音箱、收音机、语音素材适用",
        settings=_d(mode_key=DownmixMode.MONO.value, target_channels=1),
    ),
]

DOWNMIX_PRESETS_BY_KEY = {p.key: p for p in DOWNMIX_PRESETS}

# 输出格式：auto 会跟随源文件（视频源就保留视频流）
DOWNMIX_FORMATS = [
    ("auto", "自动（跟随原文件）", "视频保留画面，容器与源文件一致"),
    ("mp4", "MP4（视频／音频）", "兼容性最好，手机与网页通吃"),
    ("mkv", "MKV（视频／音频）", "多音轨、字幕友好，适合归档"),
    ("m4a", "M4A / AAC（仅音频）", "Apple 生态友好"),
    ("mp3", "MP3（仅音频）", "最通用的音频格式"),
    ("ac3", "AC3（仅音频）", "老式家庭影院功放友好"),
    ("flac", "FLAC（仅音频，无损）", "体积大但音质无损"),
    ("wav", "WAV（仅音频，未压缩）", "给剪辑软件用"),
]

# 格式 → (音频编码器, 默认码率 kbps)，码率 0 表示无损不设码率
FORMAT_AUDIO_CODEC = {
    "mp4": ("aac", 192),
    "mkv": ("aac", 192),
    "mov": ("aac", 192),
    "m4a": ("aac", 256),
    "mp3": ("libmp3lame", 320),
    "ac3": ("ac3", 448),
    "eac3": ("eac3", 640),
    "flac": ("flac", 0),
    "wav": ("pcm_s16le", 0),
    "opus": ("libopus", 160),
}

# 能装视频流的容器
VIDEO_CONTAINERS = {"mp4", "mkv", "mov", "webm", "avi", "ts", "flv"}

# 无损编码器：不设码率
LOSSLESS_AUDIO_CODECS = {"flac", "pcm_s16le", "pcm_s24le", "alac"}

DOWNMIX_BITRATES = ["96", "128", "160", "192", "256", "320", "448", "640"]


def estimate_audio_mb(duration_s: float, codec: str, bitrate_kbps: int,
                      channels: int = 2, sample_rate: int = 48000) -> float:
    """估算「只输出音频」时的文件体积（MB）。

    有损按码率直接算；无损按 PCM 码率算（FLAC/ALAC 再折算典型压缩率）。
    用来在界面上回答「为什么输出这么小」——体积由码率决定，与声道数无关。
    """
    if not duration_s or duration_s <= 0:
        return 0.0
    if codec in LOSSLESS_AUDIO_CODECS:
        bits = 24 if codec == "pcm_s24le" else 16
        pcm_kbps = (sample_rate or 48000) * bits * max(channels, 1) / 1000.0
        if codec == "flac":
            kbps = pcm_kbps * 0.6
        elif codec == "alac":
            kbps = pcm_kbps * 0.7
        else:
            kbps = pcm_kbps
    else:
        kbps = float(bitrate_kbps or 192)
    return duration_s * kbps / 8.0 / 1024.0


def format_size_mb(mb: float) -> str:
    """把 MB 数写成「125 MB」/「1.4 GB」这种人话。"""
    if mb <= 0:
        return ""
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    if mb >= 100:
        return f"{mb:.0f} MB"
    return f"{mb:.1f} MB"

DOWNMIX_SAMPLE_RATES = [
    (0, "保持原始采样率"),
    (48000, "48 kHz"),
    (44100, "44.1 kHz"),
    (32000, "32 kHz"),
]

# --------------------------------------------------------------------------- #
# 多轨合并：把 N 个「一路一条声道」的文件合成一条多声道，再下混成一条输出
# --------------------------------------------------------------------------- #

# 文件数 → 默认声道布局。取值对齐 ffmpeg 自带的默认布局表，
# 也正是「从多声道素材拆出 N 条单声道」最常见的拆分结果。
AUTO_MERGE_LAYOUT = {
    1: "mono", 2: "stereo", 3: "3.0", 4: "quad",
    5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1",
}

# 下拉里可选的具体布局，括号里就是文件该按什么顺序排
MERGE_LAYOUTS = [
    ("auto", "自动（按文件数推断）"),
    ("stereo", "2.0 立体声：前左 / 前右"),
    ("3.0", "3.0：前左 / 前右 / 中置"),
    ("quad", "4.0：前左 / 前右 / 后左 / 后右"),
    ("5.0", "5.0：前左 / 前右 / 中置 / 后左 / 后右"),
    ("5.1", "5.1：前左 / 前右 / 中置 / 低音炮 / 后左 / 后右"),
    ("7.1", "7.1：前左 / 前右 / 中置 / 低音炮 / 后左 / 后右 / 侧左 / 侧右"),
]

# 声道角色 → 中文名，用来告诉用户第几个文件该放什么
ROLE_NAME = {
    "FL": "前左", "FR": "前右", "FC": "中置", "LFE": "低音炮",
    "BL": "后左", "BR": "后右", "SL": "侧左", "SR": "侧右",
    "BC": "后中", "FLC": "左中", "FRC": "右中",
}
