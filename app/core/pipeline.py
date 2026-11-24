"""把界面上的参数装配成一个可执行的 Task。"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .commands import (build_compress_steps, build_downmix_steps,
                       build_extract_audio_steps, build_extract_steps,
                       build_merge_downmix_steps, build_remux_steps,
                       audio_ext_for, resolve_downmix_format,
                       subtitle_ext_for, _threads_for_one_job)
from .ffmpeg import MediaInfo, StreamInfo
from .presets import (LOSSLESS_AUDIO_CODECS, VIDEO_CONTAINERS, Container,
                      DownmixSettings, VideoSettings, cover_support)
from .task import Task, TaskKind
from .utils import safe_stem, unique_path


def make_output_path(cfg, src: Path, ext: str, suffix: str = "",
                     force_suffix: bool = False,
                     stem_override: str | None = None) -> Path:
    """按配置推导输出路径，必要时自动改名避免覆盖。

    stem_override 用于「多文件合并」：输出名不该沿用某一个输入文件的名字。
    """
    outdir = cfg.resolved_output_dir(src)
    stem = safe_stem(Path(stem_override)) if stem_override else safe_stem(src)
    if suffix or force_suffix:
        stem = f"{stem}{suffix}"
    target = outdir / f"{stem}.{ext}"
    try:
        same_as_source = target.resolve() == src.resolve()
    except OSError:
        same_as_source = False
    if same_as_source or (not cfg.overwrite and target.exists()):
        target = unique_path(target)
    return target


# --------------------------------------------------------------------------- #
# 视频压缩
# --------------------------------------------------------------------------- #

def build_compress_task(ctx, src: Path, info: MediaInfo | None,
                        settings: VideoSettings, target_size_mb: float = 0.0,
                        suffix: str | None = None,
                        clip: tuple[float, float] | None = None) -> Task:
    """clip: (起始秒, 时长)，试压用（见 build_compress_steps）。"""
    cfg = ctx.config
    if suffix is None:
        suffix = cfg.filename_suffix
    # force_suffix：试压产物必须带后缀，否则会和正常输出撞名、互相覆盖
    dst = make_output_path(cfg, src, settings.container.value, suffix,
                           force_suffix=clip is not None)

    work_dir: Path | None = None
    from .presets import RateMode
    if settings.rate_mode in (RateMode.TWOPASS, RateMode.TARGET_SIZE) or target_size_mb > 0:
        work_dir = Path(tempfile.mkdtemp(prefix="xiaowan_2pass_"))

    # 默认不限制线程（见 _threads_for_one_job 的实测结论）
    threads = _threads_for_one_job(cfg.concurrency)
    steps = build_compress_steps(
        ctx.env, src, dst, settings, info,
        target_size_mb=target_size_mb, work_dir=work_dir, clip=clip,
        threads=threads,
    )
    return Task(kind=TaskKind.COMPRESS, src=src, dst=dst, steps=steps,
                info=info, work_dir=work_dir,
                clip=clip, is_sample=clip is not None, threads=threads)


# --------------------------------------------------------------------------- #
# 音频提取
# --------------------------------------------------------------------------- #

def build_extract_tasks(ctx, src: Path, info: MediaInfo | None,
                        selections: list[tuple[str, int]],
                        suffix: str | None = None) -> list[Task]:
    """一个源文件 + 一组勾选的流 → **多条** Task（每条产出一个文件）。

    `selections` 是 `[(kind, stream_index), ...]`，kind ∈ video/audio/subtitle；
    stream_index 是**源的绝对流序号**（ffprobe 的 index）。

    🔴 这里的关键设计：**复选框 = 展开成多条任务**，而不是"一个任务产出
    多个文件"。后者要改 Task 模型（一个 dst → 多个 dst）、队列页、
    体积统计、失败处理，全都要动；前者在现有模型里就是天然成立的
    ——每个 task 仍然只产出一个文件。

    命名规则（必须带类型和序号，否则用户分不清哪个是哪个）：
        课时2_画面.mp4 / 课时2_音轨1.m4a / 课时2_音轨2.ac3 / 课时2_字幕.srt
    """
    cfg = ctx.config
    if suffix is None:
        suffix = cfg.filename_suffix        # 抽取页一般传 ""（不加后缀）

    # 统计各类流出现的次序，用来生成「音轨1 / 音轨2」这种稳定编号。
    # 🔴 按**源里的序号升序**编号，不受勾选顺序影响 —— 否则用户
    # 先勾第 2 条再勾第 1 条，产出的文件名会跟界面上的行号对不上。
    ordered = sorted(selections, key=lambda kv: (kv[1],))
    counters = {"video": 0, "audio": 0, "subtitle": 0}
    tasks: list[Task] = []

    for kind, stream_index in ordered:
        counters[kind] += 1
        n = counters[kind]

        # 扩展名
        if kind == "video":
            ext = src.suffix.lstrip(".").lower() or "mp4"
            codec = _stream_codec(info, kind, stream_index)
        elif kind == "audio":
            codec = _stream_codec(info, kind, stream_index)
            ext = audio_ext_for(codec).lstrip(".")
        elif kind == "subtitle":
            codec = _stream_codec(info, kind, stream_index)
            ext = subtitle_ext_for(codec).lstrip(".")
        else:
            continue

        # 文件名后缀：画面 / 音轨N / 字幕N
        if kind == "video":
            tag = "画面"
        elif kind == "audio":
            tag = f"音轨{n}"
        else:
            tag = f"字幕{n}"

        dst = make_output_path(cfg, src, ext, suffix,
                               stem_override=f"{src.stem}_{tag}")
        steps = build_extract_steps(ctx.env, src, dst, kind, stream_index, codec)
        tasks.append(Task(kind=TaskKind.AUDIO, src=src, dst=dst,
                          steps=steps, info=info,
                          label=f"{tag} · {stream_index}"))
    return tasks


def _stream_codec(info: MediaInfo | None, kind: str, stream_index: int) -> str:
    """从 MediaInfo 里捞某条流的编码名（拿不到就返回空串）。

    拿不到时不猜：build_extract_steps 会按 kind 给安全的默认
    （视频 copy、字幕转 srt、音频走 .mka）。
    """
    if info is None:
        return ""
    if kind == "video":
        if info.video and info.video.index == stream_index:
            return info.video.codec_name
        return info.video.codec_name if info.video else ""
    pool = info.audios if kind == "audio" else info.subtitles
    for s in pool:
        if s.index == stream_index:
            return s.codec_name
    return ""


def build_audio_task(ctx, src: Path, info: MediaInfo | None, ext: str,
                     codec: str, bitrate_kbps: int, sample_rate: int = 0,
                     channels: int = 0, suffix: str | None = None) -> Task:
    cfg = ctx.config
    if suffix is None:
        suffix = cfg.filename_suffix
    dst = make_output_path(cfg, src, ext, suffix)
    steps = build_extract_audio_steps(
        ctx.env, src, dst, codec, bitrate_kbps, sample_rate, channels)
    return Task(kind=TaskKind.AUDIO, src=src, dst=dst, steps=steps, info=info)


# --------------------------------------------------------------------------- #
# 封装混流
# --------------------------------------------------------------------------- #

def build_remux_task(ctx, src: Path, info: MediaInfo | None,
                     container: Container, audio_mode: str = "all",
                     audio_codec: str = "copy", audio_bitrate_kbps: int = 192,
                     audio_sample_rate: int = 0, audio_channels: int = 0,
                     embed_subtitles: bool = False, faststart: bool = True,
                     suffix: str | None = None,
                     auto_cover: bool = False,
                     fallback_cover: Path | None = None,
                     drop_source_cover: bool = False,
                     extra_audio: Path | None = None,
                     extra_audio_replace: bool = True,
                     extra_audio_fill: str = "loop",
                     extra_audio_lang: str = "") -> Task:
    cfg = ctx.config
    if suffix is None:
        suffix = cfg.filename_suffix
    dst = make_output_path(cfg, src, container.value, suffix)

    sub_files: list[Path] = []
    sub_langs: list[str] = []
    if embed_subtitles:
        from .ffmpeg import find_subtitles
        from .utils import SUB_EXTS
        for cand in find_subtitles(src):
            sub_files.append(cand)
            sub_langs.append(_guess_lang(cand))
        if not sub_files and info:
            for i, st in enumerate(info.subtitles):
                sub_langs.append(st.language or "")

    cover = resolve_cover(src, container, auto_cover=auto_cover,
                          fallback=fallback_cover,
                          drop_source=drop_source_cover)

    # 追加音轨时，新音轨在输出里的序号依赖源的音轨条数 ——
    # 这个数字必须从 info 里拿，不能让 build_remux_steps 去猜。
    src_audio_count = len(info.audios) if info else 0
    # 只保留第一条时源音轨也就 1 条
    if audio_mode == "first":
        src_audio_count = min(src_audio_count, 1)
    elif audio_mode == "none":
        src_audio_count = 0
    # 有额外音频时，输出长度由这个值决定（不能用 -shortest，见 commands.py）
    timeline = info.duration if (extra_audio is not None and info) else 0.0

    steps = build_remux_steps(
        ctx.env, src, dst, container,
        audio_mode=audio_mode, audio_codec=audio_codec,
        audio_bitrate_kbps=audio_bitrate_kbps,
        audio_sample_rate=audio_sample_rate,
        audio_channels=audio_channels,
        subtitle_files=sub_files, subtitle_langs=sub_langs,
        faststart=faststart, cover=cover,
        extra_audio=extra_audio,
        extra_audio_replace=extra_audio_replace,
        extra_audio_fill=extra_audio_fill,
        extra_audio_lang=extra_audio_lang,
        src_audio_count=src_audio_count,
        timeline_seconds=timeline,
    )
    return Task(kind=TaskKind.REMUX, src=src, dst=dst, steps=steps, info=info,
                cover=cover, extra_audio=extra_audio)


def resolve_cover(src: Path, container: Container, *, auto_cover: bool = False,
                  fallback: Path | None = None,
                  drop_source: bool = False) -> Path | None:
    """决定这个任务用哪张封面。返回 None 表示不加封面。

    优先级（用户拍板的规则）：
        1. **同名图片** —— 和字幕同一套思路，每个文件各用各的
        2. **全局封面** —— 只在没匹配到同名图时兜底
        3. **不加** —— 都没找到就不动它，不会因为别的文件有封面就跟着加

    🔴 容器不支持的直接返回 None（MOV / WebM）：宁可静默不加，也不要让
    ffmpeg 在封装时报一个用户看不懂的错。界面上已经提前禁用并说明了原因。
    """
    if cover_support(container) == "none":
        return None
    # 🔴 drop_source 表示「去掉封面」，必须**最先**判断 ——
    # 它排在同名匹配之后的话，文件名旁边有同名图时仍然会把封面加回去，
    # 用户选了"去掉封面"却还是加了，是最难解释的那种 bug。
    if drop_source:
        return None
    if auto_cover:
        from .ffmpeg import find_cover
        found = find_cover(src)
        if found is not None:
            return found
    return fallback


def _guess_lang(path: Path) -> str:
    """从文件名尾巴猜语言，如 xxx.chs.srt -> chi"""
    stem = path.stem.lower()
    mapping = {
        "chs": "chi", "cht": "chi", "chi": "chi", "zh": "chi", "sc": "chi", "tc": "chi",
        "eng": "eng", "en": "eng", "jpn": "jpn", "jp": "jpn", "ja": "jpn",
        "kor": "kor", "kr": "kor",
    }
    token = stem.replace("_", ".").replace("-", ".").split(".")[-1]
    return mapping.get(token, "")


# --------------------------------------------------------------------------- #
# 声道下混（5.1 / 7.1 → 立体声 / 单声道）
# --------------------------------------------------------------------------- #

def build_downmix_task(ctx, src: Path, info: MediaInfo | None,
                       settings: DownmixSettings,
                       stream_index: int | None = None,
                       suffix: str | None = None) -> Task:
    cfg = ctx.config
    ext, codec, default_br = resolve_downmix_format(settings.container, src, info)

    if suffix is None:
        suffix = cfg.filename_suffix or (
            "_立体声" if settings.target_channels == 2 else "_单声道")
    dst = make_output_path(cfg, src, ext, suffix)

    has_video = bool(info and info.video) and ext in VIDEO_CONTAINERS
    bitrate = 0 if codec in LOSSLESS_AUDIO_CODECS else int(
        settings.audio_bitrate_kbps or default_br or 192)

    steps = build_downmix_steps(
        ctx.env, src, dst, settings, info,
        audio_codec=codec, audio_bitrate_kbps=bitrate,
        stream_index=stream_index, keep_video=has_video,
        container=ext, faststart=True,
    )
    return Task(kind=TaskKind.DOWNMIX, src=src, dst=dst, steps=steps, info=info)


# --------------------------------------------------------------------------- #
# 多轨合并下混：N 个单声道文件 → 一条立体声
# --------------------------------------------------------------------------- #

_CHANNEL_TAIL = re.compile(
    r"[ _\-.]*(\d*)(fl|fr|fc|lfe|sl|sr|bl|br|ls|rs|l|r|c|sw)[ _\-.]*$", re.I)


def merge_output_stem(paths: list[Path]) -> str:
    """给合并输出挑个名字。

    movie_FL.wav / movie_FR.wav … 剥掉尾部的声道标记后是同一个名字，
    就直接用它；名字对不上（各文件命名很乱）就退回目录名。
    """
    if not paths:
        return "output"

    def strip(stem: str) -> str:
        out, prev = stem, None
        while out and out != prev:
            prev = out
            out = _CHANNEL_TAIL.sub("", out)
        return out.strip(" _-.")

    stems = [strip(p.stem) for p in paths]
    cleaned = [s for s in stems if s]
    if cleaned and len({s.lower() for s in cleaned}) == 1:
        return cleaned[0]

    parents = {str(p.parent.resolve()) for p in paths}
    if len(parents) == 1:
        folder = paths[0].parent.name
        if folder:
            return folder
    return cleaned[0] if cleaned else paths[0].stem


def build_merge_downmix_task(
    ctx,
    srcs: list[Path],
    infos: list[MediaInfo | None],
    roles: list[str],
    settings: DownmixSettings,
    suffix: str | None = None,
) -> Task | None:
    """把多个单声道文件合成一条多声道再下混，只产出一个文件。"""
    if len(srcs) < 1 or len(roles) != len(srcs):
        return None

    cfg = ctx.config
    first = srcs[0]
    ext, codec, default_br = resolve_downmix_format(settings.container, first, None)
    # 「自动」跟随源文件在这条路径上没意义（源是一堆单声道文件，
    # 而且可能扩展名各不相同），统一落到 AAC/M4A。
    if (settings.container or "auto").lower() == "auto":
        ext, codec, default_br = "m4a", "aac", 256

    if suffix is None:
        suffix = cfg.filename_suffix or (
            "_立体声" if settings.target_channels == 2 else "_单声道")
    stem = merge_output_stem(srcs)
    dst = make_output_path(cfg, first, ext, suffix, stem_override=stem)

    durations = [info.duration if info else 0.0 for info in infos]
    known = [d for d in durations if d > 0]
    duration = max(known) if known else 0.0

    rates = [info.audios[0].sample_rate for info in infos
             if info and info.audios and info.audios[0].sample_rate]
    rate = int(settings.sample_rate or (max(rates) if rates else 48000))

    bitrate = 0 if codec in LOSSLESS_AUDIO_CODECS else int(
        settings.audio_bitrate_kbps or default_br or 192)

    steps = build_merge_downmix_steps(
        ctx.env, srcs, dst, roles, settings,
        audio_codec=codec, audio_bitrate_kbps=bitrate,
        sample_rate=rate, durations=durations, container=ext,
    )
    if not steps:
        return None

    # 造一个「合成后」的媒体信息：队列靠它的时长算进度
    merged = MediaInfo(
        path=dst, duration=duration, size=sum(
            (info.size if info else 0) for info in infos),
        audios=[StreamInfo(index=0, codec_type="audio", codec_name=codec,
                           channels=len(srcs), sample_rate=rate)],
    )
    label = f"{stem}（{len(srcs)} 路声道合并）"
    return Task(kind=TaskKind.DOWNMIX, src=first, dst=dst, steps=steps,
                info=merged, label=label, sources=list(srcs))
