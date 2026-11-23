"""ffmpeg / ffprobe 定位、媒体探测、字幕探测。

设计要点：Mac 上 ffmpeg 来源很杂（Homebrew / MacPorts / 官方静态包 / 内置），
所以这里做多级回退，并且允许用户在设置页手动指定。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .utils import COVER_EXTS, SUB_EXTS, is_macos, is_windows

CREATE_NO_WINDOW = 0x08000000 if is_windows() else 0


class FFmpegError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 二进制定位
# --------------------------------------------------------------------------- #

_MAC_SEARCH_DIRS = [
    "/opt/homebrew/bin",          # Apple Silicon Homebrew
    "/usr/local/bin",             # Intel Homebrew / 手动安装
    "/opt/local/bin",             # MacPorts
    "/usr/bin",
]


def _bundled_dir() -> Path:
    """打包进 .app / .exe 的 bin 目录（PyInstaller 解包后会落到 _MEIPASS）。

    注意：Windows 下的文件名带 .exe 后缀，所以两种命名都要认。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = Path(base) / "bin"
        if cand.is_dir():
            return cand
    # 兼容打包后 exe 同级目录手工放置的 bin/
    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
    here = Path(__file__).resolve()
    roots = [p for p in ([exe_dir] if exe_dir else []) + list(here.parents) if p]
    for parent in roots:
        cand = parent / "bin"
        if not cand.is_dir():
            continue
        if (cand / "ffmpeg").is_file() or (cand / "ffmpeg.exe").is_file():
            return cand
    return Path("/nonexistent")


def _candidates(name: str, user_path: str | None = None) -> list[Path]:
    exe = f"{name}.exe" if is_windows() else name
    out: list[Path] = []

    if user_path:
        out.append(Path(user_path))

    env = os.environ.get(f"XIAOWAN_{name.upper()}")
    if env:
        out.append(Path(env))

    out.append(_bundled_dir() / exe)

    which = shutil.which(name)
    if which:
        out.append(Path(which))

    if is_macos():
        for d in _MAC_SEARCH_DIRS:
            out.append(Path(d) / exe)

    # 开发期便利：imageio-ffmpeg 自带的静态 ffmpeg（注意：它不含 ffprobe，
    # 所以这个兜底只能用于 ffmpeg 本体，否则会把 ffmpeg 误当成 ffprobe）
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            out.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
        except Exception:
            pass

    return out



def _probe_failure_reason(stderr: str) -> str:
    """从试编 stderr 里提取用户能看懂的失败原因。

    已知案例：NVENC 驱动过旧时 stderr 里有
    「The minimum required Nvidia driver for nvenc is 610.00 or newer」。
    """
    # 第一优先：编码器自己说的「不支持 X」。这类句子是唯一真正解释起因的，
    # 且都在 stderr **最前面**；后面那些 "Error while opening encoder" /
    # "Nothing was written into output file" 只是善后噪音（实测 h264_amf
    # 编 10bit：起因在第 1 行，最后一行完全没有信息量）
    for line in stderr.splitlines():
        low = line.lower()
        if "not supported" in low or "unsupported" in low:
            return _humanize_probe_line(line)
    for line in stderr.splitlines():
        low = line.lower()
        if "minimum required" in low and "driver" in low:
            # NVENC 驱动版本要求：给出人话
            num = ""
            for token in line.split():
                if token.replace(".", "").isdigit():
                    num = token
                    break
            return (f"NVIDIA 驱动过旧（需要 {num} 或更新），"
                    "升级显卡驱动后即可用")
        if "driver does not support" in low:
            return "NVIDIA 驱动版本与该 ffmpeg 的 NVENC 要求不匹配，升级显卡驱动后即可用"
        # AMD AMF 的 AV1 有一条专属失败：显卡是 GCN/RDNA1/RDNA2 时会报
        # 「CreateComponent(AMFVideoEncoderHW_AV1) failed with error 30」
        # —— 实测本机 AMD Radeon 核显（驱动 32.0.11024.2）就是这条。
        # 必须单独说清楚，否则用户会以为是驱动没装好，白折腾一遍。
        if "amfvideoencoderhw_av1" in low and "failed" in low:
            return ("这块 AMD 显卡不支持 AV1 硬件编码（需要 RDNA3 及更新的型号，"
                    "如 RX 7000 系 / Ryzen 7040 之后的核显）。"
                    "可以改用 H.264 / H.265 的 AMD 加速，或 AV1 软件编码")
        if "failed with error" in low and "amf" in low:
            return "AMD AMF 初始化失败（驱动或显卡不支持该编码）"
        if "init" in low and "failed" in low:
            return _humanize_probe_line(line)
    # 没命中已知案例时也别返回空：取第一条非空输出（起因总在前面，
    # 末尾那几行"没写出任何包"对用户毫无意义）
    for line in stderr.splitlines():
        if line.strip():
            return _humanize_probe_line(line)
    return ""


def _humanize_probe_line(line: str) -> str:
    """把 ffmpeg 的 `[模块 @ 0x...] 正文` 剥成正文，并去掉对象地址噪音。"""
    text = line.strip()
    if text.startswith("[") and "]" in text:
        text = text.split("]", 1)[1].strip()
    return text[:120]


#: 试编 stderr 里算"真失败"的关键词。不能用「stderr 非空」来判断：
#: SVT-AV1 成功时也会往 stderr 打一大段 Svt[info] 横幅（实测），
#: 那样会把好编码器误判成不可用（libsvtav1 就是这么被漏判的）。
_FAIL_MARKERS = ("error", "failed", "invalid", "unable", "no capable",
                 "not support", "unsupported", "cannot")


def _looks_failed(stderr: str) -> bool:
    low = stderr.lower()
    return any(m in low for m in _FAIL_MARKERS)


def find_binary(name: str, user_path: str | None = None) -> str | None:
    """返回可用的可执行文件路径，找不到返回 None。"""
    seen: set[str] = set()
    for cand in _candidates(name, user_path):
        try:
            key = str(cand)
            if key in seen:
                continue
            seen.add(key)
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:
            continue
    return None


@dataclass
class FFmpegEnv:
    """当前生效的 ffmpeg 环境。"""

    ffmpeg: str | None = None
    ffprobe: str | None = None
    version: str = ""
    user_ffmpeg: str = ""
    _encoders: list[str] = field(default_factory=list, repr=False)
    _usable: dict[str, bool] = field(default_factory=dict, repr=False)
    _unusable_reason: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def ready(self) -> bool:
        return bool(self.ffmpeg)

    @property
    def has_probe(self) -> bool:
        # 兜底校验：路径里必须真的含 "ffprobe"，防止把 ffmpeg 本体误当成 ffprobe
        return bool(self.ffprobe) and "ffprobe" in Path(self.ffprobe).name.lower()

    def refresh(self, user_ffmpeg: str = "", user_ffprobe: str = "") -> "FFmpegEnv":
        self.user_ffmpeg = user_ffmpeg
        self._encoders = []
        self._usable = {}
        self.ffmpeg = find_binary("ffmpeg", user_ffmpeg)

        # ffprobe 优先找与 ffmpeg 同目录的那一个，再退回全局搜索
        probe_exe = "ffprobe.exe" if is_windows() else "ffprobe"
        self.ffprobe = None
        if self.ffmpeg:
            sibling = Path(self.ffmpeg).with_name(probe_exe)
            if sibling.is_file() and os.access(sibling, os.X_OK):
                self.ffprobe = str(sibling)
        if not self.ffprobe:
            self.ffprobe = find_binary("ffprobe", user_ffprobe)

        self.version = ""
        if self.ffmpeg:
            try:
                res = subprocess.run(
                    [self.ffmpeg, "-version"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                first = (res.stdout or res.stderr or "").splitlines()
                if first:
                    self.version = first[0].strip()
            except Exception:
                pass
        return self

    def encoder_list(self) -> list[str]:
        """ffmpeg 编译进来的编码器名字列表（只查一次，之后的调用走缓存）。"""
        if not self._encoders:
            names: list[str] = []
            if self.ffmpeg:
                try:
                    res = subprocess.run(
                        [self.ffmpeg, "-hide_banner", "-encoders"],
                        capture_output=True, text=True, timeout=20,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    for line in (res.stdout or "").splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and parts[0] and parts[0][0] in "VAS":
                            names.append(parts[1])
                except Exception:
                    pass
            self._encoders = names
        return self._encoders

    def encoder_available(self, encoder: str) -> bool:
        """ffmpeg 是否编译了该编码器。"""
        return bool(self.ffmpeg) and encoder in self.encoder_list()

    def unusable_reason(self, encoder: str) -> str:
        """硬件编码器不可用的人话原因（探测时从 stderr 提取）。

        编码器实际可用或尚未探测时返回空串。
        """
        return self._unusable_reason.get(encoder, "")

    def usable_reason(self, encoder: str,
                      extra: list[str] | None = None) -> str:
        """带附加参数那次试编的失败原因（如 10 位色深探测）。"""
        key = encoder if not extra else f"{encoder}|{' '.join(extra)}"
        return self._unusable_reason.get(key, "")

    #: 10 位色深探测用的附加参数
    TEN_BIT_ARGS = ["-pix_fmt", "yuv420p10le"]

    def encoder_usable_10bit(self, encoder: str) -> bool:
        """该编码器能不能真的编 10 位色深（yuv420p10le）。

        **必须试编，不能看声明**：ffmpeg 报的像素格式列表不可信 ——
        h264_amf 的 `-h encoder` 里明明白白列着 p010le，实际编码直接失败
        （实测 2026-11-11）；libx264 有没有 10 位也取决于构建。
        结果按编码器缓存，和 8 位那次探测互不覆盖。
        """
        return self.encoder_usable(encoder, self.TEN_BIT_ARGS)

    def ten_bit_reason(self, encoder: str) -> str:
        """不支持 10 位色深时的人话原因（支持的返回空串）。"""
        return self.usable_reason(encoder, self.TEN_BIT_ARGS)

    def encoder_usable(self, encoder: str, extra: list[str] | None = None) -> bool:
        """真机试编 1 帧，确认硬件编码器真的能用。

        「编译进去了」和「本机能跑」是两回事：h264_qsv / h264_amf 常年出现在
        -encoders 列表里，但没有对应显卡或驱动时一跑就报错。所以这里必须实测。

        结果按编码器缓存，避免反复试编拖慢界面。
        """
        key = encoder if not extra else f"{encoder}|{' '.join(extra)}"
        if key in self._usable:
            return self._usable[key]
        ok = False
        reason = ""
        if self.encoder_available(encoder):
            try:
                res = subprocess.run(
                    [self.ffmpeg or "ffmpeg", "-hide_banner", "-loglevel", "error",
                     # 256x144：AMF 硬件编码器有最小分辨率限制（实测 192x108 会
                     # Init 失败 error 5，256x144 起正常），探测统一用这个尺寸
                     "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=25",
                     "-frames:v", "3", "-an", "-c:v", encoder, *(extra or []),
                     "-f", "null", "-"],
                    capture_output=True, timeout=12,
                    creationflags=CREATE_NO_WINDOW,
                )
                stderr = (res.stderr or b"").decode("utf-8", "replace")
                # 不能用「stderr 非空」判失败：SVT-AV1 成功时也会打一大段
                # Svt[info] 横幅（实测），那会把好编码器误判成不可用
                ok = res.returncode == 0 and not _looks_failed(stderr)
                if not ok:
                    # 失败原因要留给用户看（如 NVENC 的驱动版本要求），
                    # 否则界面只会说"不可用"，用户不知道该怎么办
                    reason = _probe_failure_reason(stderr) or ""
            except Exception:
                ok = False
        self._usable[key] = ok
        # 原因按**完整 key** 存：带附加参数的那次探测（如 10 位色深）不能把
        # 8 位那次的原因（如「驱动过旧」）覆盖掉，两者是两回事
        self._unusable_reason[key] = reason
        return ok


# --------------------------------------------------------------------------- #
# 媒体探测
# --------------------------------------------------------------------------- #

@dataclass
class StreamInfo:
    index: int = 0
    codec_type: str = ""
    codec_name: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bit_rate: int = 0
    channels: int = 0
    channel_layout: str = ""      # stereo / 5.1 / 5.1(side) / 7.1 …
    sample_rate: int = 0
    pix_fmt: str = ""
    language: str = ""
    title: str = ""
    duration: float = 0.0

    # ---- 媒体信息页用的补充字段（ffprobe 直接提供） ----
    profile: str = ""             # High / Main 10 / LC …
    level: int = 0                # 视频 level（40 = L4.0），0 = 未知
    codec_tag: str = ""           # avc1 / mp4a / hvc1 …
    bit_depth: int = 0            # 视频读 bits_per_raw_sample，音频读 bits_per_sample
    color_range: str = ""         # tv / pc
    color_space: str = ""         # bt709 / bt601 …
    color_primaries: str = ""
    color_transfer: str = ""
    field_order: str = ""         # progressive / tt / bb …
    sample_aspect_ratio: str = "" # 1:1
    display_aspect_ratio: str = ""# 45:52
    nb_frames: int = 0
    sample_fmt: str = ""          # fltp / s16 …
    creation_time: str = ""       # tags.creation_time 原样
    is_default: bool = False      # disposition.default
    #: 平均帧率与 r_frame_rate 各自保留，用来判断恒定/可变
    r_fps: float = 0.0

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "—"

    @property
    def chroma_text(self) -> str:
        """从 pix_fmt 推导色度抽样（4:2:0 / 4:2:2 / 4:4:4）。

        🔴 ffprobe **没有**独立的色度抽样字段，只能从 pix_fmt 推。
        注意半平面格式：nv12 / nv21 是 4:2:0，nv16 是 4:2:2 ——
        实测很容易漏掉，nv12 会被误算成 4:4:4。
        """
        pf = (self.pix_fmt or "").lower()
        if pf in ("nv12", "nv21") or "420" in pf:
            return "4:2:0"
        if pf in ("nv16",) or "422" in pf:
            return "4:2:2"
        if "440" in pf:
            return "4:4:0"
        if "411" in pf:
            return "4:1:1"
        if pf.startswith(("rgb", "bgr", "gbr", "yuv444", "yuva444", "gbrap",
                          "argb", "abgr")):
            return "4:4:4"
        return ""

    @property
    def color_space_text(self) -> str:
        """色彩空间：从 pix_fmt 的家族推导（YUV / RGB / 灰度）。"""
        pf = (self.pix_fmt or "").lower()
        if not pf:
            return ""
        if "gray" in pf:
            return "灰度"
        if pf.startswith(("rgb", "bgr", "gbr", "argb", "abgr", "gbrap")):
            return "RGB"
        if pf.startswith(("yuv", "yuva", "nv", "p0")):
            return "YUV"
        return ""

    @property
    def aspect_ratio_text(self) -> str:
        """显示宽高比（老版那个 0.865），从 display_aspect_ratio 换算。

        ⚠️ ffprobe 给的是 `45:52` 这种冒号写法，
        `Fraction("45:52")` 会直接抛 ValueError —— 必须自己拆。
        """
        raw = (self.display_aspect_ratio or "").strip()
        if raw and ":" in raw:
            num, _, den = raw.partition(":")
            try:
                d = float(den)
                return f"{float(num) / d:.3f}" if d else ""
            except ValueError:
                return ""
        if self.width and self.height:
            return f"{self.width / self.height:.3f}"
        return ""

    @property
    def pixels_per_frame(self) -> float:
        """Bits/(Pixel*Frame)，老版的 0.043 就是这个。"""
        if not (self.width and self.height and self.fps and self.bit_rate):
            return 0.0
        return self.bit_rate / (self.width * self.height * self.fps)

    @property
    def level_text(self) -> str:
        """level 整数转 L4.1 这种写法。

        🔴 两种编码器的换算**完全不同**，套错就会显示成 L5.3 这种不存在的等级：
          · H.264：level_idc = major×10 + minor  →  40 = L4.0，41 = L4.1
          · HEVC ：level_idc = major×30 + minor×3 →  153 = L5.1，156 = L5.2

        实测 HEVC L5.1 的 level_idc 就是 153（用 x265 的
        `-x265-params level-idc=153` 可复现），按 H.264 的 /10 算会得到 L15.3。
        """
        if not self.level:
            return ""
        n = self.level
        if n >= 90:                       # HEVC：major×30 + minor×3
            major, rem = divmod(n, 30)
            return f"L{major}.{rem // 3}"
        major, minor = divmod(n, 10)      # H.264 / H.263 / AV1 之前的通用写法
        return f"L{major}.{minor}"

    @property
    def bit_depth_text(self) -> str:
        """位深度。视频用 bits_per_raw_sample，音频用 bits_per_sample。"""
        if self.bit_depth:
            return f"{self.bit_depth} bits"
        # 兜底：从 pix_fmt 的 p10/p12/p16 之类后缀推
        pf = (self.pix_fmt or "").lower()
        for n in (16, 12, 10, 9):
            if f"p{n}" in pf or f"{n}le" in pf or f"{n}be" in pf:
                return f"{n} bits"
        return ""

    @property
    def field_order_text(self) -> str:
        fo = (self.field_order or "").lower()
        if fo in ("progressive",):
            return "逐行扫描"
        if fo.startswith(("tt", "bb", "tb", "bt")):
            return "隔行扫描（交错）"
        if fo in ("unknown", ""):
            return ""
        return fo

    @property
    def color_range_text(self) -> str:
        cr = (self.color_range or "").lower()
        if cr in ("tv", "limited"):
            return "有限 (tv)"
        if cr in ("pc", "full", "jpeg"):
            return "全范围 (pc)"
        return ""

    @property
    def fps_mode_text(self) -> str:
        """恒定（CFR）/ 可变（VFR）。ffprobe 只给 avg 和 r 两个帧率。

        两者相等 → 恒定；不等 → 多半是可变帧率（手机录屏、VFR 转码常见）。
        没有 r_frame_rate 数据时不下结论，返回空串而不是假装恒定。
        """
        if not self.fps or not self.r_fps:
            return ""
        return "恒定" if abs(self.fps - self.r_fps) < 0.01 else "可变"

    @property
    def creation_text(self) -> str:
        """'2020-04-11T23:01:50.000000Z' → '2020-04-11 23:01'（UTC 原样）。"""
        raw = (self.creation_time or "").strip()
        if len(raw) < 16:
            return ""
        return f"{raw[0:10]} {raw[11:16]}"

    @property
    def channel_text(self) -> str:
        """声道的人话描述，优先用布局名。"""
        lay = self.channel_layout.strip().lower()
        if lay in ("mono", "1.0") or self.channels == 1:
            return "单声道"
        if lay in ("stereo", "2.0") or self.channels == 2:
            return "立体声"
        if lay:
            return lay
        return f"{self.channels}ch" if self.channels else ""

    @property
    def label(self) -> str:
        parts = [f"#{self.index}", self.codec_name or "?"]
        if self.codec_type == "video":
            parts.append(self.resolution)
            if self.fps:
                parts.append(f"{self.fps:g}fps")
        elif self.codec_type == "audio":
            ch = self.channel_text
            if ch:
                parts.append(ch)
            if self.sample_rate:
                parts.append(f"{self.sample_rate / 1000:g}kHz")
        if self.language:
            parts.append(self.language)
        if self.title:
            parts.append(self.title)
        return " · ".join(parts)


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    size: int = 0
    format_name: str = ""
    bit_rate: int = 0
    video: StreamInfo | None = None
    audios: list[StreamInfo] = field(default_factory=list)
    subtitles: list[StreamInfo] = field(default_factory=list)
    error: str = ""
    #: 容器长名（QuickTime / MOV、Matroska / WebM …），媒体信息页显示用
    format_long_name: str = ""
    #: tags.encoder —— 老版叫「写入程序」
    encoder: str = ""
    #: tags.creation_time（容器级）
    creation_time: str = ""

    def stream_bytes(self, stream: StreamInfo) -> int:
        """估算某一路流占的字节数。

        🔴 ffprobe **不给**"流大小"（MediaInfo 库才给 Stream size）。
        用 `bit_rate × duration ÷ 8` 估算 —— 实测误差在 1% 内
        （老版 6.46 MiB / 97%，同算法算出来一样）。
        流的码率缺失时返回 0，调用方据此不显示。
        """
        br = stream.bit_rate or 0
        if not br:
            return 0
        dur = stream.duration or self.duration or 0
        if not dur:
            return 0
        return int(br * dur / 8)

    def stream_share(self, stream: StreamInfo) -> float:
        """该流占总大小的比例（0~1）。总大小为 0 时返回 0。"""
        if not self.size:
            return 0.0
        return self.stream_bytes(stream) / self.size

    @property
    def ok(self) -> bool:
        return not self.error and (self.video is not None or bool(self.audios))

    @property
    def is_video(self) -> bool:
        return self.video is not None

    @property
    def resolution(self) -> str:
        return self.video.resolution if self.video else "—"

    @property
    def fps(self) -> float:
        return self.video.fps if self.video else 0.0

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if self.video:
            bits = [self.video.codec_name.upper(), self.video.resolution]
            if self.video.fps:
                bits.append(f"{self.video.fps:g}fps")
            if self.audios:
                a = self.audios[0]
                audio = a.codec_name.upper()
                lay = a.channel_layout.strip().lower()
                # 立体声是默认形态不用标；5.1 / 7.1 这类多声道必须亮出来
                if lay and lay not in ("stereo", "2.0", "mono", "1.0"):
                    audio += f" · {lay}"
                elif a.channels > 2:
                    audio += f" · {a.channels}ch"
                bits.append(audio)
            return " · ".join(bits)
        if self.audios:
            a = self.audios[0]
            bits = [a.codec_name.upper()]
            ch = a.channel_text
            if ch:
                bits.append(ch)
            if a.sample_rate:
                bits.append(f"{a.sample_rate / 1000:g}kHz")
            return " · ".join(bits)
        return "未知"


def _parse_fps(rate: str | None) -> float:
    if not rate:
        return 0.0
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


#: ffprobe 里表示"没写这个标签"的占位值
_TAG_PLACEHOLDERS = {"und", "unknown", "", "none", "n/a"}


def _tag(tags: dict, key: str) -> str:
    """从 tags 里取一个值，并把 und/unknown 这类占位值当成"没有"。

    ffprobe 经常给 `language: "und"`（undefined）——直接显示会变成
    「语言：und」，用户看不懂也没意义。统一在解析层就清掉。
    """
    raw = (tags or {}).get(key, "") or ""
    raw = str(raw).strip()
    if raw.lower() in _TAG_PLACEHOLDERS:
        return ""
    return raw


def probe_with_ffprobe(env: FFmpegEnv, path: Path) -> MediaInfo:
    cmd = [
        env.ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-i", str(path),
    ]
    res = subprocess.run(
        cmd, capture_output=True, timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    raw = res.stdout.decode("utf-8", "replace") or "{}"
    data = json.loads(raw)

    fmt = data.get("format", {}) or {}
    fmt_tags = fmt.get("tags", {}) or {}
    info = MediaInfo(
        path=path,
        duration=_to_float(fmt.get("duration")),
        size=_to_int(fmt.get("size")) or (path.stat().st_size if path.exists() else 0),
        format_name=(fmt.get("format_name") or "").split(",")[0],
        bit_rate=_to_int(fmt.get("bit_rate")),
        format_long_name=fmt.get("format_long_name", "") or "",
        encoder=_tag(fmt_tags, "encoder"),
        creation_time=_tag(fmt_tags, "creation_time"),
    )

    for st in data.get("streams", []) or []:
        kind = st.get("codec_type", "")
        tags = st.get("tags", {}) or {}
        disp = st.get("disposition", {}) or {}
        s = StreamInfo(
            index=_to_int(st.get("index")),
            codec_type=kind,
            codec_name=st.get("codec_name", "") or "",
            width=_to_int(st.get("width")),
            height=_to_int(st.get("height")),
            fps=_parse_fps(st.get("avg_frame_rate") or st.get("r_frame_rate")),
            bit_rate=_to_int(st.get("bit_rate")),
            channels=_to_int(st.get("channels")),
            channel_layout=st.get("channel_layout", "") or "",
            sample_rate=_to_int(st.get("sample_rate")),
            pix_fmt=st.get("pix_fmt", "") or "",
            language=_tag(tags, "language"),
            title=_tag(tags, "title"),
            duration=_to_float(st.get("duration")),
            # ---- 媒体信息页字段 ----
            profile=st.get("profile", "") or "",
            level=_to_int(st.get("level")),
            codec_tag=st.get("codec_tag_string", "") or "",
            # 视频用 bits_per_raw_sample，音频用 bits_per_sample（实测两者不通用）
            bit_depth=_to_int(st.get("bits_per_raw_sample")
                              or st.get("bits_per_sample")),
            color_range=st.get("color_range", "") or "",
            color_space=st.get("color_space", "") or "",
            color_primaries=st.get("color_primaries", "") or "",
            color_transfer=st.get("color_transfer", "") or "",
            field_order=st.get("field_order", "") or "",
            sample_aspect_ratio=st.get("sample_aspect_ratio", "") or "",
            display_aspect_ratio=st.get("display_aspect_ratio", "") or "",
            nb_frames=_to_int(st.get("nb_frames")),
            sample_fmt=st.get("sample_fmt", "") or "",
            creation_time=_tag(tags, "creation_time"),
            is_default=bool(disp.get("default")),
            r_fps=_parse_fps(st.get("r_frame_rate")),
        )
        if kind == "video" and not s.codec_name.startswith("mjpeg"):
            if info.video is None:
                info.video = s
        elif kind == "audio":
            info.audios.append(s)
        elif kind == "subtitle":
            info.subtitles.append(s)

    if not info.duration:
        cands = [s.duration for s in info.audios + info.subtitles if s.duration]
        if info.video and info.video.duration:
            cands.append(info.video.duration)
        if cands:
            info.duration = max(cands)
    return info


_FFMPEG_INFO_KEYS = {
    "Duration": "duration", "bitrate": "bit_rate", "Stream #": "stream",
}

# 布局名 → 声道数（用于 `ffmpeg -i` 文本解析，也是识别 5.1／7.1 的依据）
_LAYOUT_CHANNELS = {
    "mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "3.0(back)": 3,
    "quad": 4, "quad(side)": 4, "4.0": 4,
    "5.0": 5, "5.0(side)": 5, "5.1": 6, "5.1(side)": 6,
    "6.0": 6, "6.0(front)": 6, "hexagonal": 6,
    "6.1": 7, "6.1(back)": 7, "6.1(front)": 7,
    "7.0": 7, "7.0(front)": 7,
    "7.1": 8, "7.1(wide)": 8, "7.1(wide-side)": 8, "octagonal": 8,
}


def parse_layout(raw: str) -> tuple[str, int]:
    """把 ffmpeg 文本里的声道描述拆成 (布局名, 声道数)。

    布局名认不出来时返回 ("", 声道数)，交给上层按声道数兜底。
    """
    import re as _re

    text = (raw or "").strip().lower()
    if not text:
        return "", 0
    m = _re.match(r"^(\d+)\s*(?:channels?|ch)$", text)
    if m:
        return "", int(m.group(1))
    if text in _LAYOUT_CHANNELS:
        return text, _LAYOUT_CHANNELS[text]
    m = _re.match(r"^([\d.]+)", text)          # 形如 5.1(back) 的变体
    if m:
        prefix = m.group(1)
        if prefix in _LAYOUT_CHANNELS:
            return text, _LAYOUT_CHANNELS[prefix]
    return "", 0


def probe_with_ffmpeg(env: FFmpegEnv, path: Path) -> MediaInfo:
    """没有 ffprobe 时的降级方案：解析 `ffmpeg -i` 的 stderr。"""
    import re

    res = subprocess.run(
        [env.ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW,
    )
    text = res.stderr.decode("utf-8", "replace")

    info = MediaInfo(
        path=path,
        size=path.stat().st_size if path.exists() else 0,
        format_name=path.suffix.lstrip("."),
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if m:
        info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        info.bit_rate = int(m.group(1)) * 1000

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Stream #"):
            continue
        sm = re.match(r"Stream #(\d+):(\d+)", line)
        idx = int(sm.group(1)) if sm else 0
        if "Video:" in line:
            v = StreamInfo(index=idx, codec_type="video")
            cm = re.search(r"Video:\s*([a-zA-Z0-9_]+)", line)
            v.codec_name = cm.group(1) if cm else ""
            rm = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if rm:
                v.width, v.height = int(rm.group(1)), int(rm.group(2))
            fm = re.search(r"([\d.]+)\s*fps", line)
            if fm:
                v.fps = _to_float(fm.group(1))
            if info.video is None:
                info.video = v
        elif "Audio:" in line:
            a = StreamInfo(index=idx, codec_type="audio")
            cm = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", line)
            a.codec_name = cm.group(1) if cm else ""
            hm = re.search(r"(\d+)\s*Hz", line)
            if hm:
                a.sample_rate = int(hm.group(1))
            # 采样率之后紧跟的就是声道布局，例如 "48000 Hz, 5.1(side), fltp"
            lm = re.search(r"Hz,\s*([^,]+)", line)
            if lm:
                a.channel_layout, a.channels = parse_layout(lm.group(1))
            if not a.channels:
                if "stereo" in line:
                    a.channels, a.channel_layout = 2, "stereo"
                elif "mono" in line:
                    a.channels, a.channel_layout = 1, "mono"
            info.audios.append(a)
        elif "Subtitle:" in line:
            s = StreamInfo(index=idx, codec_type="subtitle")
            cm = re.search(r"Subtitle:\s*([a-zA-Z0-9_]+)", line)
            s.codec_name = cm.group(1) if cm else ""
            info.subtitles.append(s)
    return info


def _binary_failure_hint(env: FFmpegEnv, exc: Exception) -> str:
    """探测**执行阶段**失败的人话原因（不是 ffmpeg 找不到）。

    🔴 这条报错只在一种情况下出现：`refresh()` 当时检查过 `env.ffmpeg` 存在
    且可执行，真正要执行时那个文件已经不在了。所以必须把**当时的路径**和
    **winerror** 一起报出来 —— 光说「系统找不到指定的文件」，用户和我们
    都无从判断是哪个文件、什么时候没的。

    实测复现：启动时 is_file/可执行检查全过，`_MEI` 临时目录被清掉后
    立刻变成 `[WinError 2]`。其他错误码各自的含义也已实测区分：
    5=拒绝访问、193/216=不是有效程序（损坏/架构不符）、206=路径过长。
    区分它们很重要：报 193 时提示「重新检测」是没用的，文件还在但坏了。
    """
    path = env.ffmpeg or "(未设置)"
    code = getattr(exc, "winerror", None)
    if code == 2 or isinstance(exc, FileNotFoundError):
        return (f"探测失败：ffmpeg 已不在（{path}）"
                "—— 可能是杀毒软件隔离或临时目录被清理，"
                "请点「功能设置 → 重新检测」。")
    if code == 5 or isinstance(exc, PermissionError):
        return f"探测失败：无权限运行 ffmpeg（{path}），请点「功能设置 → 重新检测」。"
    if code in (193, 216):
        return (f"探测失败：ffmpeg 无法运行（{path}）"
                "—— 文件损坏或架构不符，建议重新安装本软件。")
    if code == 206:
        return f"探测失败：ffmpeg 路径过长（{path}），请换到更短的目录。"
    return f"探测失败：{exc}（ffmpeg：{path}）"


def probe(env: FFmpegEnv, path: Path) -> MediaInfo:
    if not env.ready:
        return MediaInfo(path=path, error="未找到 ffmpeg")
    if env.has_probe:
        try:
            info = probe_with_ffprobe(env, path)
            if info.ok:
                return info
        except Exception:  # noqa: BLE001
            pass  # ffprobe 不可用或输出异常，转用 ffmpeg 兜底
    try:
        return probe_with_ffmpeg(env, path)
    except Exception as exc:  # noqa: BLE001
        return MediaInfo(path=path, error=_binary_failure_hint(env, exc))


def find_subtitles(path: Path) -> list[Path]:
    """查找与视频同名的外挂字幕。"""
    out: list[Path] = []
    for ext in SUB_EXTS:
        cand = path.with_suffix(ext)
        if cand.exists():
            out.append(cand)
    return out


def find_cover(path: Path) -> Path | None:
    """查找与视频同名的封面图片，找到返回第一个，没有返回 None。

    和字幕同一套思路（`path.with_suffix(ext)` 逐个试探），但只能返回一张 ——
    所以按 COVER_EXTS 的**顺序**取第一个，而不是把所有命中的都收下。
    实测一个目录里同时有 课时2.jpg 和 课时2.png 是很常见的事，
    不按固定顺序会出现"同一个文件这次用 jpg、下次用 png"的随机行为。

    🔴 只认**完全同名**的图片：不做模糊匹配、不扫任意名字的图片、
    不递归子目录。一个文件夹里几十张图，程序猜错比不加更糟。
    """
    for ext in COVER_EXTS:
        cand = path.with_suffix(ext)
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None
