"""严格目标体积：命令构造与任务装配。

与 commands.py 的 build_compress_steps 完全独立——那边保持原样，
这里只服务「按文件大小压缩」页面。可复用的底层辅助（_base/_tail/
_speed_args/_scale_filter）从 commands 导入，不复制代码。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .commands import (CommandStep, _base, _is_windows, _speed_args, _tail,
                       _thread_args, can_cqp, cleanup_passlogs, cqp_args,
                       qp_max)
from .ffmpeg import FFmpegEnv, MediaInfo
from .presets import (Container, VideoSettings, is_hw_encoder, resolve_codec)
from .size_budget import (FAST_MARGIN, MAX_ROUNDS, MIN_VIDEO_KBPS,
                          OVERFLOW_TOLERANCE, SizeBudget, SizePolicy,
                          correct_kbps, estimate_output_bytes, solve)
from .task import Task, TaskKind

_HEVC_ENCODERS = {"libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf",
                  "hevc_videotoolbox", "hevc_vaapi"}

# 见 commands._HEVC_TAG_CONTAINERS 的说明：MOV 也默认写 hev1，必须一起改。
_HEVC_TAG_CONTAINERS = {Container.MP4, Container.MOV}

_WEBM_VIDEO = {"libvpx-vp9", "libvpx", "libaom-av1", "libsvtav1",
                "av1_nvenc", "av1_amf", "av1_qsv", "av1_vaapi"}

# 各容器能承载的音频编码器。Opus 放 MP4/MOV 里播放器支持太差，
# 所以不列进去 —— 界面会按容器过滤掉 Opus 选项，避免"选了却不生效"。
_CONTAINER_AUDIO = {
    Container.WEBM: {"libopus", "libvorbis"},
    Container.MP4: {"aac", "libmp3lame", "ac3"},
    Container.MOV: {"aac", "libmp3lame", "ac3"},
    Container.MKV: {"aac", "libopus", "libmp3lame", "ac3", "flac"},
}

# 没有特别指定时的默认音频编码器
_DEFAULT_AUDIO = {
    Container.WEBM: "libopus",
    Container.MKV: "aac",
    Container.MP4: "aac",
    Container.MOV: "aac",
}


def _pick_codecs(env: FFmpegEnv, s: VideoSettings) -> tuple[str, str]:
    """按容器决定真正使用的视频/音频编码器。

    WebM 只认 VP9/VP8/AV1 + Opus/Vorbis，选 x264 或 AAC 会直接报
    「Could not write header」。这里统一收敛，不要指望用户自己选对。
    """
    video = resolve_codec(s.codec, env)
    if video == "copy":
        raise ValueError("copy 编码器不能用于目标体积模式")

    if s.container == Container.WEBM and video not in _WEBM_VIDEO:
        video = "libvpx-vp9"

    allowed = _CONTAINER_AUDIO.get(s.container, {"aac"})
    wanted = (s.audio_codec or "").strip()
    audio = wanted if wanted in allowed else _DEFAULT_AUDIO.get(s.container, "aac")
    return video, audio


def _video_args(codec: str, s: VideoSettings, kbps: int) -> list[str]:
    """视频编码参数：两遍 ABR + VBV 约束。"""
    args = ["-c:v", codec]
    if codec in ("libx264", "libx265"):
        args += ["-preset", s.preset]
        if s.tune:
            args += ["-tune", s.tune]
    elif codec == "libvpx-vp9":
        # VP9 的速度档是 0~8，和 x264 的命名不一样
        args += ["-cpu-used", _vp9_speed(s.preset), "-row-mt", "1"]
    else:
        args += _speed_args(codec, s.preset)

    args += ["-b:v", f"{kbps}k"]
    # 本机这版 libsvtav1 不吃 -maxrate/-bufsize（传任何值都报 Invalid argument），
    # 两遍 ABR 的体积精度靠 passlog 统计，不依赖 VBV，跳过即可
    if codec != "libsvtav1":
        args += ["-maxrate", f"{int(kbps * 1.4)}k",
                 "-bufsize", f"{int(kbps * 2.8)}k"]
    if s.pix_fmt:
        args += ["-pix_fmt", s.pix_fmt]
    if codec in _HEVC_ENCODERS and s.container in _HEVC_TAG_CONTAINERS:
        args += ["-tag:v", "hvc1"]
    return args


def _vp9_speed(preset: str) -> str:
    """把 x264 的速度档映射到 VP9 的 cpu-used（0 最慢，8 最快）。"""
    table = {"ultrafast": "8", "superfast": "8", "veryfast": "6", "faster": "5",
             "fast": "4", "medium": "3", "slow": "2", "slower": "1",
             "veryslow": "0"}
    return table.get(preset, "3")


def _audio_args(audio_codec: str, budget: SizeBudget,
                audio_mode: str) -> tuple[list[str], list[str]]:
    """返回 (音频参数, 音频 map 参数)。"""
    if budget.audio_streams == 0 or audio_mode == "none":
        return ["-an"], []
    if audio_mode == "copy":
        maps = ["-map", "0:a"] if budget.audio_streams > 1 else ["-map", "0:a:0"]
        return ["-c:a", "copy"], maps
    maps = ["-map", "0:a:0"]
    from .commands import _fdk_he_args
    return (["-c:a", audio_codec, "-b:a", f"{budget.audio_kbps}k"]
            + _fdk_he_args(audio_codec, budget.audio_kbps)), maps


def _scale_filter(s: VideoSettings, info: MediaInfo | None) -> str | None:
    """只缩小不放大：源本身比目标矮就保持原样。"""
    if not s.scale_height or not info or not info.video:
        return None
    if info.video.height <= s.scale_height:
        return None
    return f"scale=-2:{s.scale_height}"


def _input_args(env: FFmpegEnv, src: Path,
                clip: tuple[float, float] | None = None,
                threads: int | None = None,
                codec: str | None = None) -> list[str]:
    """`-i` 及其前后的参数。试压时 clip=(起始秒, 时长)。

    截取放在 `-i` **之前**（输入侧 seek）：ffmpeg 直接跳关键帧，不会先解码
    前面那几分钟再丢弃 —— 试压要的就是快。

    🔴 threads 放在 `-i` **之后**：那时它才作用于编码器。
    放前面只限解码线程，对 x264 的编码线程数毫无影响（实测同值同素材
    前后两个位置：放前面 CPU 峰 99%，放后面降到 52%）。
    """
    args = _base(env)
    ss, dur = clip if clip else (0.0, 0.0)
    if ss > 0:
        args += ["-ss", f"{ss:.3f}"]
    args += ["-i", str(src)]
    if dur > 0:
        args += ["-t", f"{dur:.3f}"]
    args += _thread_args(threads, codec)
    return args


def build_size_steps(env: FFmpegEnv, src: Path, dst: Path, s: VideoSettings,
                     budget: SizeBudget, work_dir: Path,
                     audio_mode: str = "auto",
                     info: MediaInfo | None = None,
                     clip: tuple[float, float] | None = None,
                     threads: int | None = None) -> list[CommandStep]:
    """按预算生成两遍编码步骤。

    目标体积模式一律走两遍 ABR，第一遍输出到空设备，第二遍写到 dst。
    clip 见 _input_args（试压用）。
    """
    video_codec, audio_codec = _pick_codecs(env, s)
    log = work_dir / f"{dst.stem}_pass"
    null_out = "NUL" if _is_windows() else "/dev/null"

    v_args = _video_args(video_codec, s, budget.video_kbps)
    a_args, audio_map = _audio_args(audio_codec, budget, audio_mode)

    filters: list[str] = []
    sc = _scale_filter(s, info)
    if sc:
        filters.append(sc)
    if s.fps:
        filters.append(f"fps={s.fps}")   # s.fps 已是 ffmpeg 可用的字符串

    m_args: list[str] = []
    if s.container == Container.MP4 and s.faststart:
        m_args += ["-movflags",
                   "+faststart+use_metadata_tags" if s.keep_metadata
                   else "+faststart"]

    maps = ["-map", "0:v:0"] + audio_map + ["-map_metadata", "0"]

    def _common() -> list[str]:
        args = _input_args(env, src, clip, threads, video_codec) + maps + v_args
        if filters:
            args += ["-vf", ",".join(filters)]
        return args

    # 🔴 第一遍必须带上音频参数（不能 -an）：新版 ffmpeg 的两遍日志按"输出流"
    # 分文件写（视频 -0.log、音频 -1.log），pass 2 会读所有流的日志。
    # pass 1 用 -an 的话音频日志不存在，pass 2 直接报
    # "Error reading log file ...-1.log"（旧版 ffmpeg 不写音频日志，所以没暴露）。
    # 音频是轻量编码，第一遍带上它成本几乎不变。
    p1 = _common() + ["-pass", "1", "-passlogfile", str(log)] + a_args
    p1 += ["-f", "null"] + _tail() + [null_out]

    p2 = (_common() + ["-pass", "2", "-passlogfile", str(log)]
          + a_args + m_args + _tail() + [str(dst)])

    return [CommandStep(p1, 0.45, "第一遍分析"),
            CommandStep(p2, 0.55, f"第二遍编码（{budget.video_kbps} kbps）")]


# --------------------------------------------------------------------------- #
# 硬件编码器：固定 QP（CQP）+ 二分搜索
# --------------------------------------------------------------------------- #
# 为什么硬件编码不能走上面那套两遍 ABR：passlog 是 x264/x265 写出的统计文件，
# 硬件编码器**不产生**（实测 h264_amf / h264_nvenc 的 pass 1 退出码 0、日志 0 字节），
# 于是"两遍"名存实亡 —— pass 2 拿不到统计，体积按它自己的 ABR 走，实测超
# 1.2~2.4 倍。CQP 反而可控：QP 与体积单调（实测 AMF 每档约 13%、NVENC 跨度更大），
# 画质也够（同体积 NVENC QP30 的 SSIM 0.9749，软编两遍 0.9742）。
#
# 代价：整数 QP 的分辨率有限，相邻档可能差 15 个百分点，所以**撞不准目标**是
# 常态（实测目标 1MB：QP34 → 102.6%、QP35 → 87.7%）。二分只保证"不超目标"，
# 不保证"用满"。

#: 二分搜索的 QP 起点（越大体积越小）。
#: H.264/HEVC 是 0~51 刻度，30 是中间偏质量；AV1 NVENC 是 0~255 刻度
#: 且明显平缓（QP 30 在 1708x1212 上产出 6.7 倍体积），80 才是中间档。
CQP_START = 30
CQP_START_AV1 = 80


def build_cqp_steps(env: FFmpegEnv, src: Path, dst: Path, s: VideoSettings,
                    budget: SizeBudget, qp: int, audio_mode: str = "auto",
                    info: MediaInfo | None = None,
                    clip: tuple[float, float] | None = None,
                    threads: int | None = None) -> list[CommandStep]:
    """硬件编码：单遍固定 QP。"""
    video_codec, audio_codec = _pick_codecs(env, s)
    a_args, audio_map = _audio_args(audio_codec, budget, audio_mode)

    filters: list[str] = []
    sc = _scale_filter(s, info)
    if sc:
        filters.append(sc)
    if s.fps:
        filters.append(f"fps={s.fps}")   # s.fps 已是 ffmpeg 可用的字符串

    m_args: list[str] = []
    if s.container == Container.MP4 and s.faststart:
        m_args += ["-movflags",
                   "+faststart+use_metadata_tags" if s.keep_metadata
                   else "+faststart"]

    maps = ["-map", "0:v:0"] + audio_map + ["-map_metadata", "0"]
    args = (_input_args(env, src, clip, threads, video_codec) + maps
            + ["-c:v", video_codec] + _speed_args(video_codec, s.preset)
            + cqp_args(video_codec, qp)
            + (["-pix_fmt", s.pix_fmt] if s.pix_fmt else [])
            + (["-vf", ",".join(filters)] if filters else [])
            + a_args + m_args + _tail() + [str(dst)])
    return [CommandStep(args, 1.0, f"硬件编码（QP {qp}）")]


#: AV1 的 QP 刻度比 H.264 平得多：实测 +6 QP 只减 ~25% 体积，
#: +16 QP 才接近减半（H.264 的 +6 ≈ 减半）。步长必须按编码器分。
#: av1_amf 与 av1_nvenc 量程相同（0~255），沿用同一套步长。
AV1_QP_STEP = 16
#: 用 AV1 刻度（QP 0~255、步长 16）的编码器
_AV1_CQP_ENCODERS = {"av1_nvenc", "av1_amf"}


def build_cqp_correction_steps(env: FFmpegEnv, src: Path, dst: Path,
                               s: VideoSettings, budget: SizeBudget,
                               qp_used: int, achieved_bytes: int,
                               audio_mode: str = "auto",
                               info: MediaInfo | None = None,
                               clip: tuple[float, float] | None = None,
                               threads: int | None = None
                               ) -> tuple[list[CommandStep], int]:
    """按实得体积调 QP：超标升 QP、欠标降 QP。

    QP 与体积单调递减，所以按比例换算成 QP 步进。步长按编码器分：
    H.264/HEVC 每 +6 QP ≈ 体积减半；AV1 NVENC 的刻度平缓，+16 QP 才
    接近减半（实测：QP 80→96 体积 224%→163%，QP 96→112 → 114%）。
    """
    import math

    audio_bytes = budget.audio_bytes
    got_video = max(1, achieved_bytes - audio_bytes)
    want_video = max(1, budget.video_bytes)
    ratio = max(0.4, min(2.5, got_video / want_video))
    # 实得比目标大（ratio>1）→ 要升 QP 压体积
    # 🔴 必须用**解析后**的编码器名判断 —— s.codec 是哨兵（auto_hw_av1），
    # 直接比较永远不成立，步长/上限会错成 H.264 的（实测 QP 被钳到 51）
    video_codec, _ = _pick_codecs(env, s)
    step = AV1_QP_STEP if video_codec in _AV1_CQP_ENCODERS else 6
    max_qp = qp_max(video_codec)
    delta = math.log2(ratio) * step
    new_qp = max(0, min(max_qp, int(round(qp_used + delta))))
    if new_qp == qp_used:
        return [], 0

    steps = build_cqp_steps(env, src, dst, s, budget, new_qp,
                            audio_mode=audio_mode, info=info)
    return steps, new_qp


def build_fast_steps(env: FFmpegEnv, src: Path, dst: Path, s: VideoSettings,
                     budget: SizeBudget, audio_mode: str = "auto",
                     info: MediaInfo | None = None,
                     clip: tuple[float, float] | None = None,
                     threads: int | None = None) -> list[CommandStep]:
    """快速单遍：不跑第一遍分析，预算码率打 FAST_MARGIN 折扣直接编。

    与两遍的区别：
    * 只有一步，耗时约两遍的一半；
    * 体积精度靠「起步折扣 + 超标修正循环」保证，而不是第一遍的统计；
    * 修正只重编这一步（没有 passlog 可复用，但本来也只有一步）。
    """
    video_codec, audio_codec = _pick_codecs(env, s)
    kbps = max(MIN_VIDEO_KBPS, int(budget.video_kbps * (1 - FAST_MARGIN)))

    v_args = _video_args(video_codec, s, kbps)
    a_args, audio_map = _audio_args(audio_codec, budget, audio_mode)

    filters: list[str] = []
    sc = _scale_filter(s, info)
    if sc:
        filters.append(sc)
    if s.fps:
        filters.append(f"fps={s.fps}")   # s.fps 已是 ffmpeg 可用的字符串

    m_args: list[str] = []
    if s.container == Container.MP4 and s.faststart:
        m_args += ["-movflags",
                   "+faststart+use_metadata_tags" if s.keep_metadata
                   else "+faststart"]

    maps = ["-map", "0:v:0"] + audio_map + ["-map_metadata", "0"]
    args = (_input_args(env, src, clip, threads, video_codec) + maps + v_args
            + (["-vf", ",".join(filters)] if filters else [])
            + a_args + m_args + _tail() + [str(dst)])
    return [CommandStep(args, 1.0, "快速编码（单遍）")]


def build_fast_correction_steps(env: FFmpegEnv, src: Path, dst: Path,
                                s: VideoSettings, budget: SizeBudget,
                                kbps_used: int, achieved_bytes: int,
                                audio_mode: str = "auto",
                                info: MediaInfo | None = None,
                                history: list[tuple[int, int]] | None = None,
                                clip: tuple[float, float] | None = None,
                                threads: int | None = None,
                                ) -> tuple[list[CommandStep], int]:
    """快速模式的修正：按实得体积外推新码率，整步重编（单遍，代价小）。"""
    new_kbps = correct_kbps(budget, kbps_used, achieved_bytes, history)
    if abs(new_kbps - kbps_used) <= max(2, int(kbps_used * 0.01)):
        return [], 0

    video_codec, audio_codec = _pick_codecs(env, s)
    v_args = _video_args(video_codec, s, new_kbps)
    a_args, audio_map = _audio_args(audio_codec, budget, audio_mode)

    filters: list[str] = []
    sc = _scale_filter(s, info)
    if sc:
        filters.append(sc)
    if s.fps:
        filters.append(f"fps={s.fps}")   # s.fps 已是 ffmpeg 可用的字符串

    m_args: list[str] = []
    if s.container == Container.MP4 and s.faststart:
        m_args += ["-movflags",
                   "+faststart+use_metadata_tags" if s.keep_metadata
                   else "+faststart"]

    maps = ["-map", "0:v:0"] + audio_map + ["-map_metadata", "0"]
    args = (_input_args(env, src, clip, threads, video_codec) + maps + v_args
            + (["-vf", ",".join(filters)] if filters else [])
            + a_args + m_args + _tail() + [str(dst)])
    return [CommandStep(args, 1.0, f"快速修正（{new_kbps} kbps）")], new_kbps


def build_correction_steps(env: FFmpegEnv, src: Path, dst: Path,
                           s: VideoSettings, budget: SizeBudget,
                           kbps_used: int, achieved_bytes: int,
                           work_dir: Path, audio_mode: str = "auto",
                           info: MediaInfo | None = None,
                           history: list[tuple[int, int]] | None = None,
                           clip: tuple[float, float] | None = None,
                           threads: int | None = None,
                           ) -> tuple[list[CommandStep], int]:
    """超标则降码率、欠标则升码率，只重跑第二遍（passlog 还在）。

    返回 (步骤列表, 新码率)；若已收敛则返回 ([], 0)。
    """
    new_kbps = correct_kbps(budget, kbps_used, achieved_bytes, history)
    if abs(new_kbps - kbps_used) <= max(2, int(kbps_used * 0.01)):
        return [], 0

    video_codec, audio_codec = _pick_codecs(env, s)
    log = work_dir / f"{dst.stem}_pass"

    corrected = SizeBudget(
        target_bytes=budget.target_bytes, duration=budget.duration,
        container=budget.container, audio_kbps=budget.audio_kbps,
        audio_streams=budget.audio_streams, video_kbps=new_kbps,
        overhead_fixed=budget.overhead_fixed, overhead_ratio=budget.overhead_ratio,
        feasibility=budget.feasibility, reason=budget.reason,
    )

    v_args = _video_args(video_codec, s, new_kbps)
    a_args, audio_map = _audio_args(audio_codec, corrected, audio_mode)

    filters: list[str] = []
    sc = _scale_filter(s, info)
    if sc:
        filters.append(sc)
    if s.fps:
        filters.append(f"fps={s.fps}")   # s.fps 已是 ffmpeg 可用的字符串

    m_args = (["-movflags",
               "+faststart+use_metadata_tags" if s.keep_metadata
               else "+faststart"] if (
        s.container == Container.MP4 and s.faststart) else [])

    maps = ["-map", "0:v:0"] + audio_map + ["-map_metadata", "0"]
    args = _input_args(env, src, clip, threads, video_codec) + maps + v_args
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-pass", "2", "-passlogfile", str(log)]
    args += a_args + m_args + _tail() + [str(dst)]

    return [CommandStep(args, 1.0, f"重新适配（{new_kbps} kbps）")], new_kbps


def build_size_task(ctx, src: Path, info: MediaInfo | None, s: VideoSettings,
                    target_mb: float, audio_mode: str = "auto",
                    allow_audio_shrink: bool = False,
                    policy: str = "strict",
                    fill_target: bool = False,
                    suffix: str | None = None,
                    clip: tuple[float, float] | None = None,
                    threads: int | None = None) -> Task:
    """装配一个「目标体积」任务。

    可行性判定不在这里抛异常——把结论放进 Task，让 UI 提前显示，
    用户看到「做不到」就不用白等一轮编码。

    policy:
        "strict" 严格不超过：两遍 ABR，编码后必测，一超标就修正重跑（默认）
        "fast"   快速单遍：省掉第一遍分析（耗时约一半）起步打 3% 折扣，
                 超标同样修正重跑，代价是体积利用率略低

    fill_target: 欠标时补跑把目标用满。默认关——普通素材两遍 ABR 已到
        96~98%，为剩下几个百分点多跑一轮不划算；但有些素材会明显欠标
        （实测 63 Mbps 噪声只出 41%），勾上才补。

    clip: (起始秒, 时长)，试压用。🔴 调用方必须**同时**把 info 换成截断版
        （`commands.truncated_info()`）—— 只传 clip 而 info 仍是完整时长，
        预算会按完整片算，样本体积必然对不上，补跑循环还会把样本往死里冲。
    """
    from .pipeline import make_output_path

    cfg = ctx.config
    if suffix is None:
        suffix = cfg.filename_suffix
    dst = make_output_path(cfg, src, s.container.value, suffix, force_suffix=True)

    budget = solve(info, target_mb, s.container.value, audio_mode=audio_mode,
                   audio_kbps=s.audio_bitrate_kbps,
                   allow_audio_shrink=allow_audio_shrink)

    task = Task(kind=TaskKind.COMPRESS, src=src, dst=dst, info=info,
                clip=clip,
                is_sample=clip is not None,
                threads=threads,
                size_target_bytes=budget.target_bytes,
                size_policy=policy,
                size_tolerance=OVERFLOW_TOLERANCE,
                size_max_rounds=MAX_ROUNDS,
                audio_shrink=allow_audio_shrink,
                audio_mode=audio_mode,
                # 快速单遍不补跑：它起手只给 97% 码率，实测常落在 93~94%，
                # 刚好低于 5% 触发线 → 几乎每次都补一轮，耗时翻倍只换 3 个百分点，
                # 把"快速"变成"两遍"。这里强制关掉，UI 上也已禁用。
                fill_target=fill_target and policy != SizePolicy.FAST.value,
                budget=budget,
                encoding_settings=s)

    if not budget.ok:
        task.message = budget.reason
        return task

    # 硬件编码器：passlog 是 x264/x265 才写的统计文件，硬件编码不产生
    # （实测 pass 1 退出码 0、日志 0 字节），两遍 ABR 会产出超目标 1.2~2.4 倍的
    # 文件。所以这类编码器改走固定 QP + 二分；不能走 CQP 的（QSV 只有 min/max_qp、
    # VideoToolbox 只有 qscale）直接拒绝，不要给出一个注定不准的承诺。
    video_codec, _ = _pick_codecs(ctx.env, s)
    if is_hw_encoder(video_codec):
        if not can_cqp(video_codec):
            task.message = (
                f"{video_codec} 无法精确控制体积：它不支持固定 QP 模式，"
                "目标体积页请改用软编或 NVENC/AMF 硬件编码")
            return task
        start = CQP_START_AV1 if video_codec in _AV1_CQP_ENCODERS else CQP_START
        task.steps = build_cqp_steps(ctx.env, src, dst, s, budget, start,
                                     audio_mode=audio_mode, info=info,
                                     clip=clip, threads=threads)
        task.qp_used = start
        # AV1 NVENC 的 QP 刻度平缓（+16 QP ≈ 体积 -27%），从远处收敛需要
        # 更多轮 —— 硬件编码每轮都很快，多几轮的代价可以接受
        task.size_max_rounds = max(MAX_ROUNDS, 5)
        task.expected_bytes = estimate_output_bytes(budget)
        task.message = "等待开始（硬件编码 · 固定 QP）"
        return task

    if policy == SizePolicy.FAST.value:
        # 快速单遍：预算码率打折扣起编，没有第一遍可跑
        task.steps = build_fast_steps(ctx.env, src, dst, s, budget,
                                      audio_mode=audio_mode, info=info,
                                      clip=clip, threads=threads)
        task.expected_bytes = int(budget.target_bytes * (1 - FAST_MARGIN))
        task.video_kbps_used = max(MIN_VIDEO_KBPS,
                                   int(budget.video_kbps * (1 - FAST_MARGIN)))
        task.message = "等待开始（快速单遍）"
        return task

    # 前缀必须落在 cleanup_passlogs 认的 "xiaowan_2pass_" 家族里，否则
    # 每跑一次目标体积就在临时目录留一份 passlog（实测攒过 363 个 ≈ 1.2 GB）
    work_dir = Path(tempfile.mkdtemp(prefix="xiaowan_2pass_size_"))
    task.work_dir = work_dir
    task.steps = build_size_steps(ctx.env, src, dst, s, budget, work_dir,
                                  audio_mode=audio_mode, info=info,
                                  clip=clip, threads=threads)
    task.message = "等待开始"
    task.expected_bytes = estimate_output_bytes(budget)
    task.video_kbps_used = int(budget.video_kbps)
    return task


def discard_task_files(task: Task) -> None:
    """任务被拒绝时清掉占用的临时目录。"""
    cleanup_passlogs(task.work_dir)
