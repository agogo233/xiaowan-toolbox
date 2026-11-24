#!/usr/bin/env python3
"""额外音频测试：四种对齐模式 / 输入索引 / 容器不影响视频时长。

这一组钉的都是**实测踩出来的坑**，每一个都会毁掉输出：
  1. `-shortest` + `-stream_loop -1` + `attached_pic` 共存 → 输出 0.02 秒
     （封面那 1 帧被当成"最短的流"）
  2. `-af apad` 是全局滤镜，会被应用到 `-c:a copy` 的原声上 →
     ffmpeg 报「Filtergraph 'apad' was specified, but codec copy was selected」
  3. `-c:a:-1` 负索引不被支持 → 「Trailing garbage at the end of a stream specifier」
  4. 没有 `-t` 时 apad / stream_loop 会**无限写盘**（实测写了 781 MB）
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.commands import build_remux_steps  # noqa: E402
from app.core.config import AppConfig  # noqa: E402
from app.core.ffmpeg import FFmpegEnv, probe  # noqa: E402
from app.core.pipeline import build_remux_task  # noqa: E402
from app.core.presets import Container  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


#: 由 main() 里用 FFmpegEnv 填好，避免写死 bin/ 路径
#: 🔴 bin/ 里的二进制**不入库**（体积太大），开源用户 clone 下来只有
#: bin/README.md —— 写死 "bin/ffprobe.exe" 会直接 FileNotFoundError。
_PROBE: str | None = None


def probe_all(path: Path) -> dict:
    if not _PROBE:
        return {}
    out = subprocess.run(
        [_PROBE, "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)], capture_output=True).stdout
    return json.loads(out.decode("utf-8", "replace"))


def duration_of(path: Path) -> float:
    try:
        return float(probe_all(path)["format"]["duration"])
    except Exception:
        return 0.0


def audio_tracks(path: Path) -> int:
    return len([s for s in probe_all(path).get("streams", [])
                if s.get("codec_type") == "audio"])


def main() -> int:
    env = FFmpegEnv().refresh()
    if not env.ready:
        print("找不到 ffmpeg")
        return 2
    # 让 probe_all 用 env 找到的 ffprobe（可能是内置 bin/、也可能是 PATH）
    global _PROBE
    _PROBE = env.ffprobe
    if not _PROBE:
        print("找不到 ffprobe")
        return 2
    work = Path(tempfile.mkdtemp(prefix="extra_audio_"))
    print(f"测试目录：{work}\n")

    def mk(*args):
        return subprocess.run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
                               *args], capture_output=True)

    # 视频 8s（1 条音轨）、短音频 3s、长音频 15s、封面
    video = work / "v.mp4"
    mk("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25",
       "-f", "lavfi", "-i", "sine=f=440", "-t", "8",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video))
    short = work / "短.mp3"
    mk("-f", "lavfi", "-i", "sine=f=880", "-t", "3",
       "-c:a", "libmp3lame", str(short))
    long_a = work / "长.m4a"
    mk("-f", "lavfi", "-i", "sine=f=660", "-t", "15",
       "-c:a", "aac", str(long_a))
    cover = work / "v.jpg"
    mk("-f", "lavfi", "-i", "color=c=red:s=320x180", "-frames:v", "1",
       str(cover))
    sub = work / "v.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:02,000\nhi\n", encoding="utf-8")

    info = probe(env, video)
    check("素材就绪", info.ok and abs(info.duration - 8) < 0.2,
          f"{info.duration}s")

    cfg = AppConfig(output_dir=str(work), filename_suffix="_out")
    # 类体里不能直接读闭包变量（Python 的作用域规则），用 type() 造
    Ctx = type("Ctx", (), {"env": env, "config": cfg})

    def run(label: str, expect_tracks: int, **kw) -> Path | None:
        task = build_remux_task(Ctx(), video, info, Container.MP4, **kw)
        r = subprocess.run(task.steps[0].args, capture_output=True, timeout=180)
        if r.returncode != 0:
            check(f"{label} 编码成功", False,
                  (r.stderr or b"")[-120:].decode("utf-8", "replace").strip())
            return None
        check(f"{label} 编码成功", True)
        # 🔴 所有模式都不许改变视频时长
        dur = duration_of(task.dst)
        check(f"{label} 时长保持 8 秒", abs(dur - 8.0) < 0.4, f"{dur:.2f}s")
        check(f"{label} 音轨 {expect_tracks} 条", audio_tracks(task.dst) == expect_tracks,
              str(audio_tracks(task.dst)))
        return task.dst

    print("[1] 替换原声 + 循环铺满（音频比视频短）")
    p_loop = run("替换+loop", 1, extra_audio=short, extra_audio_replace=True,
                 extra_audio_fill="loop")
    if p_loop:
        # 后段应该还有声（循环铺满）
        r = subprocess.run([env.ffmpeg, "-hide_banner", "-ss", "6", "-t", "1",
                            "-i", str(p_loop), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True)
        err = (r.stderr or b"").decode("utf-8", "replace")
        vals = [l.split("mean_volume:")[1].strip() for l in err.splitlines()
                if "mean_volume" in l]
        loud = vals and float(vals[0].split()[0]) > -60
        check("loop 模式下后段还有声（循环生效）", loud,
              vals[0] if vals else "无读数")

    print("\n[2] 替换原声 + 播放一次后静音")
    # 🔴 先跑 once，再检查 once 的产物 —— 不能拿上一次 loop 的结果去测
    p_once = run("替换+once", 1, extra_audio=short, extra_audio_replace=True,
                 extra_audio_fill="once")
    if p_once:
        # 音频 3 秒，取 6-7 秒应该纯静音（ffmpeg 读数是 -91 dB 左右）
        r = subprocess.run([env.ffmpeg, "-hide_banner", "-ss", "6", "-t", "1",
                            "-i", str(p_once), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True)
        err = (r.stderr or b"").decode("utf-8", "replace")
        vals = [l.split("mean_volume:")[1].strip() for l in err.splitlines()
                if "mean_volume" in l]
        quiet = vals and float(vals[0].split()[0]) < -70
        check("once 模式下后段是静音", quiet, vals[0] if vals else "无读数")

    print("\n[3] 替换原声 + 长音频截断")
    run("替换+trim(长)", 1, extra_audio=long_a, extra_audio_replace=True,
        extra_audio_fill="trim")

    print("\n[4] 追加为第 2 条")
    run("追加+loop", 2, extra_audio=short, extra_audio_replace=False,
        extra_audio_fill="loop")

    print("\n[5] 与封面、字幕共存（输入索引最容易错）")
    run("追加+once+字幕", 2, extra_audio=short, extra_audio_replace=False,
        extra_audio_fill="once", embed_subtitles=True)
    p2 = run("替换+loop+封面+字幕", 1, extra_audio=short,
             extra_audio_replace=True, extra_audio_fill="loop",
             auto_cover=True, embed_subtitles=True)
    if p2:
        d = probe_all(p2)
        pics = [s for s in d["streams"]
                if (s.get("disposition") or {}).get("attached_pic")]
        check("封面流还在（没被音频挤掉）", len(pics) == 1, str(len(pics)))
        check("covr 原子还在", b"covr" in p2.read_bytes())

    print("\n[6] 没有额外音频时行为不变（回归）")
    run("无额外音频", 1)

    print("\n[7] 三种 fill 模式的命令差异（静态检查）")
    base = dict(extra_audio=short, src_audio_count=1, timeline_seconds=8.0)
    loop_args = build_remux_steps(env, video, work / "x1.mp4", Container.MP4,
                                 extra_audio_fill="loop", **base)[0].args
    once_args = build_remux_steps(env, video, work / "x2.mp4", Container.MP4,
                                 extra_audio_fill="once", **base)[0].args
    trim_args = build_remux_steps(env, video, work / "x3.mp4", Container.MP4,
                                 extra_audio_fill="trim", **base)[0].args

    check("loop 用 -stream_loop -1", "-stream_loop" in loop_args)
    check("once/trim 不用 stream_loop",
          "-stream_loop" not in once_args and "-stream_loop" not in trim_args)
    # 🔴 必须用 -filter:a:N 而不是全局 -af
    check("once 用按流索引的 filter:a:N（不是全局 -af）",
          any(str(a).startswith("-filter:a:") for a in once_args)
          and "-af" not in once_args,
          [a for a in once_args if str(a).startswith(("-af", "-filter"))])
    check("trim 也用按流索引的 filter",
          any(str(a).startswith("-filter:a:") for a in trim_args))
    # 🔴 必须用 -t 而不是 -shortest
    check("三种模式都用 -t 收尾（不用 -shortest）",
          all("-t" in args and "-shortest" not in args
              for args in (loop_args, once_args, trim_args)),
          "loop/once/trim 都要有 -t")

    print("\n[8] 缺时长时必须拒绝生成（防止无限写盘）")
    try:
        build_remux_steps(env, video, work / "x4.mp4", Container.MP4,
                          extra_audio=short, extra_audio_fill="loop",
                          src_audio_count=1, timeline_seconds=0.0)
        check("缺 timeline_seconds 抛异常", False, "没抛")
    except ValueError as exc:
        check("缺 timeline_seconds 抛异常", "timeline_seconds" in str(exc), str(exc)[:60])

    print("\n[9] 追加模式缺 src_audio_count 也必须拒绝")
    try:
        build_remux_steps(env, video, work / "x5.mp4", Container.MP4,
                          extra_audio=short, extra_audio_replace=False,
                          timeline_seconds=8.0, src_audio_count=None)
        check("缺 src_audio_count 抛异常", False, "没抛")
    except ValueError as exc:
        check("缺 src_audio_count 抛异常", "src_audio_count" in str(exc), str(exc)[:60])

    print("\n[10] 音频模式 none：额外音频不参与")
    args = build_remux_steps(env, video, work / "x6.mp4", Container.MP4,
                             audio_mode="none", extra_audio=short,
                             src_audio_count=0, timeline_seconds=8.0)[0].args
    check("audio_mode=none 时没有额外音频的 map",
          "2:a:0" not in args and not any("apad" in str(a) for a in args),
          [a for a in args if "a:" in str(a)][:4])

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print(f"  失败：{n}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())