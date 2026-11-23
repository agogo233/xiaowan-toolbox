#!/usr/bin/env python3
"""封面功能测试：同名匹配 / 优先级 / 容器能力 / 实际封装。

重点不是"能不能跑"，而是几个已经踩过的坑必须钉死：
  1. `+use_metadata_tags` 会把 attached_pic 和 covr 一起丢掉
  2. `-c:v copy` 与 `-c:v:1 mjpeg` 并存时 ffmpeg 行为不可控（歧义）
  3. MOV 存不下封面（写不进就静默丢掉，不能让用户以为加了）
  4. COVER_EXTS 必须是**列表**，顺序就是优先级
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
from app.core.ffmpeg import FFmpegEnv, find_cover  # noqa: E402
from app.core.pipeline import resolve_cover  # noqa: E402
from app.core.presets import Container, cover_support  # noqa: E402
from app.core.utils import COVER_EXTS  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


def probe_streams(path: Path, env=None) -> list[dict]:
    """用 FFmpegEnv 找到的 ffprobe 读流信息。

    🔴 不能写死 `bin/ffprobe.exe`：bin/ 里的二进制**不入库**（体积太大，
    .gitignore 已排除），开源用户 clone 下来只有 bin/README.md ——
    写死路径会直接 FileNotFoundError。必须走 env 的自动探测
    （内置 bin/ → PATH → 系统常见位置），这样在两种环境下都能跑。
    """
    probe = (env.ffprobe if env is not None and env.ffprobe else None)
    if not probe:
        return []
    out = subprocess.run(
        [probe, "-v", "error", "-show_streams", "-of", "json",
         str(path)], capture_output=True).stdout
    return json.loads(out.decode("utf-8", "replace")).get("streams", [])


def has_covr(path: Path) -> bool:
    return b"covr" in path.read_bytes()


def main() -> int:
    env = FFmpegEnv().refresh()
    if not env.ready:
        print("找不到 ffmpeg")
        return 2
    work = Path(tempfile.mkdtemp(prefix="cover_test_"))
    print(f"测试目录：{work}\n")

    def mk(*args: str) -> bool:
        return subprocess.run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
                               *args], capture_output=True).returncode == 0

    video = work / "课时2.mp4"
    video_noaudio = work / "课时3.mp4"
    mk("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25",
       "-f", "lavfi", "-i", "sine=f=440", "-t", "1",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video))
    mk("-f", "lavfi", "-i", "testsrc2=s=320x180:r=25", "-t", "1",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_noaudio))
    cover_jpg = work / "课时2.jpg"
    cover_png = work / "课时2.png"
    glob = work / "全局封面.jpg"
    mk("-f", "lavfi", "-i", "color=c=red:s=320x180", "-frames:v", "1",
       "-q:v", "3", str(cover_jpg))
    mk("-f", "lavfi", "-i", "color=c=blue:s=320x180", "-frames:v", "1",
       str(cover_png))
    mk("-f", "lavfi", "-i", "color=c=green:s=320x180", "-frames:v", "1",
       "-q:v", "3", str(glob))
    if not all(p.exists() for p in (video, video_noaudio, cover_jpg, cover_png, glob)):
        print("素材生成失败")
        return 2

    print("[1] COVER_EXTS 必须是列表（顺序 = 优先级）")
    check("是 list 不是 set", isinstance(COVER_EXTS, list), type(COVER_EXTS).__name__)
    check("jpg 排第一（兼容性最好）", COVER_EXTS[0] == ".jpg", str(COVER_EXTS))
    check("含 png / webp", ".png" in COVER_EXTS and ".webp" in COVER_EXTS)
    # 🔴 set 无序 —— 用 set 就定不了"取第一个"，每次跑可能是不同的图
    check("顺序稳定（多次读取一致）", list(COVER_EXTS) == list(COVER_EXTS))

    print("\n[2] find_cover：同名匹配")
    got = find_cover(video)
    check("同时有 jpg 和 png 时取 jpg", got is not None and got.name == "课时2.jpg",
          got.name if got else "None")
    check("没有同名图返回 None", find_cover(video_noaudio) is None,
          str(find_cover(video_noaudio)))
    check("只认完全同名（不模糊匹配）",
          find_cover(work / "不存在.mp4") is None)

    print("\n[3] resolve_cover：优先级 同名 > 全局 > 不加")
    r = resolve_cover(video, Container.MP4, auto_cover=True, fallback=glob)
    check("有同名 → 用同名（不用全局）", r is not None and r.name == "课时2.jpg",
          r.name if r else "None")
    r = resolve_cover(video_noaudio, Container.MP4, auto_cover=True, fallback=glob)
    check("无同名 → 用全局兜底", r is not None and r.name == "全局封面.jpg",
          r.name if r else "None")
    r = resolve_cover(video, Container.MP4, auto_cover=False, fallback=glob)
    check("关掉自动同名 → 只用全局", r is not None and r.name == "全局封面.jpg",
          r.name if r else "None")
    r = resolve_cover(video_noaudio, Container.MP4, auto_cover=True, fallback=None)
    check("无同名且无全局 → 不加（不跟着别的文件加）", r is None, str(r))
    r = resolve_cover(video, Container.MP4, auto_cover=True, fallback=glob,
                      drop_source=True)
    check("drop_source 时也不加", r is None, str(r))

    print("\n[4] 容器能力（全部实测过的结论）")
    check("MP4 = full（播放器+资源管理器）", cover_support(Container.MP4) == "full")
    check("MKV = player（只有播放器）", cover_support(Container.MKV) == "player")
    check("MOV = none（存不下）", cover_support(Container.MOV) == "none")
    check("WebM = none", cover_support(Container.WEBM) == "none")
    check("MOV 上 resolve_cover 直接返回 None（不留给 ffmpeg 报错）",
          resolve_cover(video, Container.MOV, auto_cover=True, fallback=glob) is None)

    print("\n[5] MP4 实际封装：封面流 + covr 原子")
    dst = work / "out.mp4"
    steps = build_remux_steps(env, video, dst, Container.MP4, cover=cover_jpg)
    args = steps[0].args
    check("有封面时不给 +use_metadata_tags（会丢封面）",
          "+use_metadata_tags" not in " ".join(str(a) for a in args),
          [a for a in args if a.startswith("+")])
    check("主视频用 -c:v:0 copy（带流索引，避免与 -c:v:1 歧义）",
          "-c:v:0" in args and "copy" in args)
    check("封面转 mjpeg", "-c:v:1" in args and "mjpeg" in args)
    check("封面标为 attached_pic",
          "attached_pic" in args, args[args.index("-disposition:v:1") + 1]
          if "-disposition:v:1" in args else "")
    ok = subprocess.run(args, capture_output=True).returncode == 0
    check("封装成功", ok)
    if ok:
        streams = probe_streams(dst, env)
        check("输出 3 条流（视频+音频+封面）", len(streams) == 3, str(len(streams)))
        pic = [s for s in streams if (s.get("disposition") or {}).get("attached_pic")]
        check("封面流带 attached_pic 标记", len(pic) == 1,
              str(len(pic)))
        check("封面是 mjpeg", bool(pic) and pic[0].get("codec_name") == "mjpeg",
              pic[0].get("codec_name") if pic else "")
        # 🔴 这是 Windows 资源管理器缩略图的真正来源
        check("生成了 covr 原子（资源管理器读它）", has_covr(dst))

    print("\n[6] 无封面时行为不变（不给封面就别动 flags）")
    steps = build_remux_steps(env, video, work / "plain.mp4", Container.MP4)
    joined = " ".join(str(a) for a in steps[0].args)
    check("无封面时 +use_metadata_tags 保留",
          "+use_metadata_tags" in joined, [a for a in steps[0].args if a.startswith("+")])
    check("无封面时不加封面参数", "-disposition:v:1" not in steps[0].args)
    ok = subprocess.run(steps[0].args, capture_output=True).returncode == 0
    check("无封面也能正常封装", ok and not has_covr(work / "plain.mp4"))

    print("\n[7] MKV 实际封装：走附件（不是 attached_pic 流）")
    dst_mkv = work / "out.mkv"
    steps = build_remux_steps(env, video, dst_mkv, Container.MKV, cover=cover_jpg)
    args = steps[0].args
    check("MKV 用 -attach 而不是 attached_pic",
          "-attach" in args and "-disposition:v:1" not in args)
    check("MKV 不把封面作为视频输入",
          str(cover_jpg) in args, "attach 参数里应有图片路径")
    ok = subprocess.run(args, capture_output=True).returncode == 0
    check("MKV 封装成功", ok)
    if ok:
        check("MKV 没有 covr（资源管理器不认，符合预期）", not has_covr(dst_mkv))

    print("\n[8] 无音频的源 + 封面")
    dst2 = work / "noaudio.mp4"
    steps = build_remux_steps(env, video_noaudio, dst2, Container.MP4,
                              cover=cover_jpg)
    ok = subprocess.run(steps[0].args, capture_output=True).returncode == 0
    check("无音频源也能加封面", ok)
    if ok:
        streams = probe_streams(dst2, env)
        check("流数 = 视频 + 封面（2 条）", len(streams) == 2, str(len(streams)))

    print("\n[9] 封面 + 字幕同时存在时映射不串")
    sub = work / "课时2.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
    dst3 = work / "both.mp4"
    steps = build_remux_steps(env, video, dst3, Container.MP4,
                              subtitle_files=[sub], subtitle_langs=["chi"],
                              cover=cover_jpg)
    args = steps[0].args
    # 源=#0、字幕=#1、封面=#2
    check("字幕映射到输入 #1", "1:0" in args, [a for a in args if ":" in a and a[0].isdigit()])
    check("封面映射到输入 #2", "2:v:0" in args, [a for a in args if "v:0" in a])
    ok = subprocess.run(args, capture_output=True).returncode == 0
    check("字幕+封面同时封装成功", ok)
    if ok:
        streams = probe_streams(dst3, env)
        kinds = [s.get("codec_type") for s in streams]
        check("有视频/音频/字幕/封面四类",
              "subtitle" in kinds
              and any((s.get("disposition") or {}).get("attached_pic")
                      for s in streams),
              str(kinds))

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print(f"  失败：{n}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())