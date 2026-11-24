#!/usr/bin/env python3
"""抽取拆分测试：多流勾选 / 命名 / 扩展名映射 / 字幕 / copy 无损。

这一组钉的是"拆出来的东西对不对"：
  1. 勾 N 条流 → N 个任务，**每个任务只产出一个文件**（任务模型不用改）
  2. 文件名必须是 `源名_画面/音轨N/字幕N.扩展名` —— 不能退化成 _1 / _2
     （那样用户分不清哪个是中文音轨、哪个是英语音轨）
  3. 音频扩展名必须匹配源编码（AAC 装 .m4a、AC3 装 .ac3），
     因为我们是 copy，容器和编码对不上就是坏文件
  4. 字幕：文本转 srt；PGS 走 .sup（实测 sup muxer 只认 hdmv_pgs_subtitle）
  5. 拆出来的各部分大小之和 ≈ 源文件（证明是 copy 不是重编码）
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

from app.core.commands import (audio_ext_for, build_extract_steps,  # noqa: E402
                               subtitle_ext_for)
from app.core.config import AppConfig  # noqa: E402
from app.core.ffmpeg import FFmpegEnv, probe  # noqa: E402
from app.core.pipeline import build_extract_tasks  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    env = FFmpegEnv().refresh()
    if not env.ready:
        print("找不到 ffmpeg")
        return 2
    work = Path(tempfile.mkdtemp(prefix="extract_test_"))
    print(f"测试目录：{work}\n")

    def mk(*args):
        return subprocess.run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
                               *args], capture_output=True)

    print("[1] 音频扩展名映射（必须匹配源编码，因为是 copy）")
    cases = [("aac", ".m4a"), ("mp3", ".mp3"), ("ac3", ".ac3"), ("eac3", ".eac3"),
             ("dts", ".dts"), ("flac", ".flac"), ("opus", ".opus"),
             ("vorbis", ".ogg"), ("alac", ".m4a"), ("truehd", ".thd"),
             ("pcm_s16le", ".wav"), ("pcm_s24le", ".wav")]
    for codec, want in cases:
        got = audio_ext_for(codec)
        check(f"  {codec} → {want}", got == want, got)
    check("  未知编码 → .mka（什么都能装）", audio_ext_for("weird_codec") == ".mka",
          audio_ext_for("weird_codec"))

    print("\n[2] 字幕扩展名映射")
    for codec, want in (("subrip", ".srt"), ("ass", ".srt"), ("ssa", ".srt"),
                        ("webvtt", ".srt"), ("mov_text", ".srt")):
        check(f"  文本 {codec} → {want}", subtitle_ext_for(codec) == want,
              subtitle_ext_for(codec))
    # 🔴 PGS 是位图，不能转文字；sup muxer 只认 hdmv_pgs_subtitle
    check("  PGS → .sup（专用容器）",
          subtitle_ext_for("hdmv_pgs_subtitle") == ".sup",
          subtitle_ext_for("hdmv_pgs_subtitle"))
    check("  DVD 图形字幕 → .mkv（没有单独 muxer）",
          subtitle_ext_for("dvd_subtitle") == ".mkv",
          subtitle_ext_for("dvd_subtitle"))
    check("  DVB 图形字幕 → .mkv",
          subtitle_ext_for("dvb_subtitle") == ".mkv",
          subtitle_ext_for("dvb_subtitle"))

    # 造一个多流源：视频 + 2 音轨（不同语言）+ 1 字幕
    base = work / "b.mkv"
    mk("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25",
       "-f", "lavfi", "-i", "sine=f=440",
       "-f", "lavfi", "-i", "sine=f=880", "-t", "4",
       "-map", "0:v", "-map", "1:a", "-map", "2:a",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(base))
    sub_file = work / "s.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试字幕\n",
                        encoding="utf-8")
    multi = work / "课件.mkv"
    mk("-i", str(base), "-i", str(sub_file), "-map", "0", "-map", "1",
       "-c", "copy", "-c:s", "srt",
       "-metadata:s:a:0", "language=chi",
       "-metadata:s:a:1", "language=eng", str(multi))

    info = probe(env, multi)
    check("素材就绪（1 视频 + 2 音轨 + 1 字幕）",
          info.ok and bool(info.video) and len(info.audios) == 2
          and len(info.subtitles) == 1,
          f"v={bool(info.video)} a={len(info.audios)} s={len(info.subtitles)}")

    cfg = AppConfig(output_mode="custom", output_dir=str(work),
                    filename_suffix="")
    Ctx = type("Ctx", (), {"env": env, "config": cfg})

    print("\n[3] 勾选全部流 → 生成 4 个任务，命名带类型和序号")
    selections = [("video", info.video.index)]
    selections += [("audio", s.index) for s in info.audios]
    selections += [("subtitle", s.index) for s in info.subtitles]
    tasks = build_extract_tasks(Ctx(), multi, info, selections, suffix="")
    check("任务数 = 勾选流数（不是一个任务多产物）", len(tasks) == len(selections),
          f"{len(tasks)} / {len(selections)}")
    names = sorted(t.dst.name for t in tasks)
    check("画面命名带「_画面」", any("_画面." in n for n in names), str(names))
    check("两条音轨分别叫 _音轨1 / _音轨2",
          any("_音轨1." in n for n in names) and any("_音轨2." in n for n in names),
          str(names))
    check("字幕叫 _字幕1", any("_字幕1." in n for n in names), str(names))
    # 🔴 不能出现 _1 / _2 这种分不清的名字
    check("没有退化成 _1 / _2", not any(n.endswith("_1.m4a") for n in names),
          str(names))

    print("\n[4] 实际执行：每个任务产出一个文件，内容正确")
    ok_all = True
    for t in tasks:
        r = subprocess.run(t.steps[0].args, capture_output=True)
        if r.returncode != 0 or not t.dst.exists():
            ok_all = False
            check(f"  {t.dst.name} 产出成功", False,
                  (r.stderr or b"")[-100:].decode("utf-8", "replace").strip())
    if ok_all:
        check("4 个文件全部产出", True, ", ".join(n for n in names))

    # 字幕内容原样
    srt = next((t.dst for t in tasks if "_字幕" in t.dst.name), None)
    if srt and srt.exists():
        check("字幕内容正确（中文没坏）",
              "测试字幕" in srt.read_text(encoding="utf-8"),
              srt.read_text(encoding="utf-8").strip()[:30])

    print("\n[5] copy 无损：各流大小之和 ≈ 源文件")
    pic = next((t.dst for t in tasks if "_画面" in t.dst.name), None)
    auds = [t.dst for t in tasks if "_音轨" in t.dst.name]
    if pic and auds:
        total = pic.stat().st_size + sum(a.stat().st_size for a in auds)
        src_size = multi.stat().st_size
        ratio = total / src_size
        check("画面+音轨之和 ≈ 源文件（copy 的证明）",
              0.85 <= ratio <= 1.05, f"{total}/{src_size} = {ratio:.2f}")

    print("\n[6] 多文件批量：按序号复制到每个文件")
    multi2 = work / "课件2.mkv"
    shutil.copy2(multi, multi2)
    tasks2 = build_extract_tasks(Ctx(), multi2, probe(env, multi2),
                                 selections, suffix="")
    check("第二个文件也生成 4 个任务", len(tasks2) == 4, str(len(tasks2)))
    check("命名用第二个文件的名字",
          all(t.dst.name.startswith("课件2_") for t in tasks2),
          str([t.dst.name for t in tasks2][:2]))

    print("\n[7] 只勾一部分")
    only_audio = [s for s in selections if s[0] == "audio"]
    # 🔴 用一个干净的输出目录：第 [4] 步已经产出了同名文件，
    # 在同一个目录里再跑会撞名 → unique_path 追加 _1，
    # 那样测的就不是"命名规则"而是"防撞名逻辑"了（两件事混在一起）。
    sub_dir = work / "sub7"
    sub_dir.mkdir()
    cfg7 = AppConfig(output_mode="custom", output_dir=str(sub_dir),
                     filename_suffix="")
    Ctx7 = type("Ctx7", (), {"env": env, "config": cfg7})
    tasks3 = build_extract_tasks(Ctx7(), multi, info, only_audio, suffix="")
    check("只勾 2 条音轨 → 2 个任务", len(tasks3) == 2, str(len(tasks3)))
    check("仍然叫 _音轨1 / _音轨2（序号按源顺序，不按勾选顺序）",
          sorted(t.dst.name for t in tasks3)
          == sorted([f"课件_音轨1.m4a", f"课件_音轨2.m4a"]),
          str([t.dst.name for t in tasks3]))

    print("\n[7b] 输出目录里已有同名文件 → 自动加 _1，不覆盖")
    tasks3b = build_extract_tasks(Ctx(), multi, info, only_audio, suffix="")
    names_b = [t.dst.name for t in tasks3b]
    check("撞名时加 _1（不覆盖已有文件）",
          all("_1.m4a" in n for n in names_b), str(names_b))

    print("\n[8] 纯音频源：只有音轨，没有画面项")
    pure_audio = work / "纯音频.m4a"
    mk("-f", "lavfi", "-i", "sine=f=440", "-t", "2", "-c:a", "aac", str(pure_audio))
    info_a = probe(env, pure_audio)
    check("纯音频没有视频流", info_a.video is None)
    sel_a = [("audio", s.index) for s in info_a.audios]
    tasks4 = build_extract_tasks(Ctx(), pure_audio, info_a, sel_a, suffix="")
    check("生成 1 个任务", len(tasks4) == 1, str(len(tasks4)))
    check("扩展名跟着源编码（AAC → .m4a）",
          tasks4[0].dst.suffix == ".m4a", tasks4[0].dst.name)

    print("\n[9] 命令结构：显式 copy，不带重编码参数")
    st = build_extract_steps(env, multi, work / "x.mkv", "video",
                             info.video.index, "h264")[0].args
    check("画面用 -c:v copy", "copy" in st and "-c:v" in st)
    check("画面不带 -an/-sn 之外的其它流",
          "-an" in st and "-sn" in st, str([a for a in st if a.startswith("-a")]))
    st_a = build_extract_steps(env, multi, work / "x.m4a", "audio",
                               info.audios[0].index, "aac")[0].args
    check("音轨用 -c:a copy", "copy" in st_a)
    check("音轨不带 -b:a（copy 时无意义）",
          "-b:a" not in st_a, str([a for a in st_a if a.startswith("-b")]))

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print(f"  失败：{n}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())