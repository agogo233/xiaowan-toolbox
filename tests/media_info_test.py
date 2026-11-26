#!/usr/bin/env python3
"""媒体信息页测试：字段解析 / 推导 / 文本输出 / 页面接线。

这一页的核心风险不是"画得好不好看"，而是**推导出来的数字对不对**：
老版 MediaInfo 的 0.865 画面比例、97% 流占比、4:2:0 色度抽样
都是算出来的，算错用户不会发现，只会觉得软件不准。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.ffmpeg import MediaInfo, StreamInfo  # noqa: E402
from app.ui.pages.media_info import (  # noqa: E402
    CODEC_DISPLAY, INFO_EXTS, _codec_display, _fmt_bitrate, _fmt_duration,
    _fmt_size, build_info_text, display_width, pad_label)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    print("[1] 色度抽样：从 pix_fmt 推导（ffprobe 不给这个字段）")
    cases = [
        ("yuv420p", "4:2:0"), ("yuv420p10le", "4:2:0"),
        ("nv12", "4:2:0"), ("nv21", "4:2:0"),      # 半平面，最容易漏
        ("yuv422p", "4:2:2"), ("nv16", "4:2:2"),
        ("yuv444p", "4:4:4"), ("rgb24", "4:4:4"),
    ]
    for fmt, want in cases:
        got = StreamInfo(pix_fmt=fmt).chroma_text
        check(f"  {fmt} → {want}", got == want, got or "(空)")
    check("  gray 不该报色度", StreamInfo(pix_fmt="gray").chroma_text == "",
          StreamInfo(pix_fmt="gray").chroma_text)

    print("\n[2] 色彩空间：YUV / RGB / 灰度")
    for fmt, want in [("yuv420p", "YUV"), ("nv12", "YUV"),
                      ("rgb24", "RGB"), ("gray", "灰度"),
                      ("p010le", "YUV")]:
        got = StreamInfo(pix_fmt=fmt).color_space_text
        check(f"  {fmt} → {want}", got == want, got or "(空)")

    print("\n[3] 画面比例：45:52 → 0.865（老版数字）")
    s = StreamInfo(display_aspect_ratio="45:52", width=1080, height=1248)
    check("  换算为 0.865", s.aspect_ratio_text == "0.865", s.aspect_ratio_text)
    # 🔴 Fraction('45:52') 会抛 ValueError —— 这里就是防它回潮
    check("  不抛异常（冒号写法）", True)
    check("  16:9 → 1.778",
          StreamInfo(display_aspect_ratio="16:9").aspect_ratio_text == "1.778",
          StreamInfo(display_aspect_ratio="16:9").aspect_ratio_text)
    check("  无 DAR 时用宽高比兜底",
          StreamInfo(width=1920, height=1080).aspect_ratio_text == "1.778",
          StreamInfo(width=1920, height=1080).aspect_ratio_text)

    print("\n[4] 位深度：视频读 raw_sample，音频读 bits_per_sample")
    check("  8 bits", StreamInfo(bit_depth=8).bit_depth_text == "8 bits")
    check("  10 bits", StreamInfo(bit_depth=10).bit_depth_text == "10 bits")
    check("  缺失时从 pix_fmt 推 p10",
          StreamInfo(pix_fmt="yuv420p10le").bit_depth_text == "10 bits",
          StreamInfo(pix_fmt="yuv420p10le").bit_depth_text)
    check("  纯 8bit pix_fmt 不误报",
          StreamInfo(pix_fmt="yuv420p").bit_depth_text == "",
          StreamInfo(pix_fmt="yuv420p").bit_depth_text)

    print("\n[5] level：H.264 /10，HEVC /30")
    check("  H.264 level 40 → L4.0", StreamInfo(level=40).level_text == "L4.0",
          StreamInfo(level=40).level_text)
    check("  H.264 level 41 → L4.1", StreamInfo(level=41).level_text == "L4.1")
    check("  HEVC level 153 → L5.1", StreamInfo(level=153).level_text == "L5.1",
          StreamInfo(level=153).level_text)
    check("  无 level 返回空", StreamInfo(level=0).level_text == "")

    print("\n[6] 帧率模式：恒定 vs 可变")
    check("  avg==r → 恒定",
          StreamInfo(fps=30.0, r_fps=30.0).fps_mode_text == "恒定")
    check("  avg!=r → 可变",
          StreamInfo(fps=29.97, r_fps=30.0).fps_mode_text == "可变",
          StreamInfo(fps=29.97, r_fps=30.0).fps_mode_text)
    check("  缺 r_fps 不下结论（不假装恒定）",
          StreamInfo(fps=30.0).fps_mode_text == "",
          StreamInfo(fps=30.0).fps_mode_text)

    print("\n[7] 流大小 / 占比：老版 6.46 MiB (97%)")
    info = MediaInfo(
        path=Path("car.mp4"), duration=31.0, size=int(6.66 * 1048576),
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        bit_rate=1790000,
        video=StreamInfo(index=0, codec_type="video", codec_name="h264",
                         width=1080, height=1248, fps=30.0, r_fps=30.0,
                         bit_rate=1743000, duration=31.0),
    )
    info.audios.append(StreamInfo(index=1, codec_type="audio",
                                 codec_name="aac", channels=2,
                                 channel_layout="stereo", sample_rate=44100,
                                 bit_rate=48000, duration=31.0))
    vbytes = info.stream_bytes(info.video)
    check("  视频流约 6.4 MiB",
          6.0 * 1048576 < vbytes < 6.9 * 1048576,
          f"{vbytes / 1048576:.2f} MiB")
    share = info.stream_share(info.video)
    check("  视频占比约 97%", 0.94 < share < 0.99, f"{share:.0%}")
    ashare = info.stream_share(info.audios[0])
    check("  音频占比约 3%", 0.01 < ashare < 0.06, f"{ashare:.0%}")
    check("  无码率时不瞎算",
          info.stream_bytes(StreamInfo(bit_rate=0, duration=31.0)) == 0)

    print("\n[8] 显示宽度对齐：中文算 2 格")
    check("  '文件名' = 6", display_width("文件名") == 6, str(display_width("文件名")))
    check("  'abc' = 3", display_width("abc") == 3)
    check("  pad 到 10", display_width(pad_label("文件名")) == 10,
          repr(pad_label("文件名")))
    # 半角括号只占 1 格：'音频(1)' = 2+2+1+1+1 = 7 格，需要再补 3 个空格。
    # 🔴 这里曾经用 ljust(10) —— 它按字符数补齐，'音频(1)' 是 6 个字符，
    # 补 4 个空格后显示宽度变成 11 格，整块右移一格。
    check("  '音频(1)' = 7 格", display_width("音频(1)") == 7,
          str(display_width("音频(1)")))
    check("  '音频(1)' 补齐到 10 格", display_width(pad_label("音频(1)")) == 10,
          repr(pad_label("音频(1)")))
    check("  标签宽度一致",
          {display_width(pad_label(x)) for x in
           ("文件名", "总码率", "音频(1)", "视频(1)")} == {10})

    print("\n[9] 数字格式化")
    check("  1790000 → 1.79 Mbps", _fmt_bitrate(1790000) == "1.79 Mbps",
          _fmt_bitrate(1790000))
    check("  48000 → 48 kb/s", _fmt_bitrate(48000) == "48 kb/s",
          _fmt_bitrate(48000))
    check("  0 返回空", _fmt_bitrate(0) == "")
    check("  31.208 → 31 s 208 ms",
          _fmt_duration(31.208) == "31 s 208 ms", _fmt_duration(31.208))
    check("  211.16 → 3 min 31 s 160 ms",
          _fmt_duration(211.16) == "3 min 31 s 160 ms", _fmt_duration(211.16))
    check("  6.46MiB 用 KiB/MiB 单位（老版口径）",
          _fmt_size(int(6.46 * 1048576)) == "6.46 MiB",
          _fmt_size(int(6.46 * 1048576)))
    check("  _fmt_size 不用人类 KB",
          "KB" not in _fmt_size(185 * 1024), _fmt_size(185 * 1024))

    print("\n[10] 编码器显示名：h264 → AVC（老版写法）")
    check("  h264 → AVC", _codec_display("h264") == "AVC")
    check("  hevc → HEVC", _codec_display("hevc") == "HEVC")
    check("  aac → AAC", _codec_display("aac") == "AAC")
    check("  subrip → SRT", _codec_display("subrip") == "SRT")
    check("  未知编码器回退大写", _codec_display("libx265") == "LIBX265",
          _codec_display("libx265"))
    check("  空返回空", _codec_display("") == "")

    print("\n[11] 纯文本输出")
    txt = build_info_text(info)
    lines = txt.splitlines()
    check("  首行是文件名", lines[0].startswith("文件名") and "car.mp4" in lines[0],
          lines[0])
    check("  含容器行", any(l.startswith("容器") for l in lines))
    check("  含 AVC（不是 H264）", "AVC" in txt and "H264" not in txt)
    check("  含画面比例 0.865", "0.865" in txt)
    check("  含流占比", "97%" in txt, [l for l in lines if "97%" in l])
    check("  含音频段", any("音频(1)" in l for l in lines))
    # 对齐验证：每个带标签的行，值都从第 10 格开始。
    # ⚠️ 不能检查"前 10 个字符的宽度"—— 标签补到 10 格后，
    # 第 10 个字符可能已经是值的一部分，宽度自然 >10。要看的是**前缀**。
    labeled = [l for l in lines if l and l[0] not in " " and l[0] != "#"]
    ok_align = True
    for l in labeled:
        # 取前 9 格内容，确认它是"标签 + 空格"，即值在第 10 格起
        prefix, w = "", 0
        for ch in l:
            if w >= 10:
                break
            prefix += ch
            w += 2 if ord(ch) > 0x2E80 else 1
        # 前缀必须恰好占满 10 格，且最后一个字符是空格（标签后有留白）
        if w != 10 or not prefix.endswith(" "):
            ok_align = False
            print(f"      对齐异常: {l[:30]!r} 前缀={prefix!r}")
    check("  行首标签对齐（值从第 10 格开始）", ok_align,
          f"检查了 {len(labeled)} 行")
    check("  结尾无多余空行", txt.endswith("\n") and not txt.endswith("\n\n"))

    print("\n[12] 纯音频（没有视频段）")
    aonly = MediaInfo(path=Path("b.flac"), duration=211.16,
                      size=int(25.7 * 1048576), format_name="flac")
    aonly.audios.append(StreamInfo(index=0, codec_type="audio",
                                   codec_name="flac", channels=2,
                                   channel_layout="stereo", sample_rate=44100,
                                   bit_rate=1003000, duration=211.16,
                                   bit_depth=16))
    atxt = build_info_text(aonly)
    check("  不出现视频段", "视频" not in atxt)
    check("  出现音频段", "音频(1)" in atxt)
    check("  位深度 16 bits", "16 bits" in atxt)
    check("  FLAC 容器", "FLAC" in atxt)

    print("\n[13] 接受的文件类型（老版能读纯音频）")
    check("  含视频扩展", ".mp4" in INFO_EXTS and ".mkv" in INFO_EXTS)
    check("  含音频扩展", ".flac" in INFO_EXTS and ".mp3" in INFO_EXTS)

    print("\n[14] 无数据时不瞎编")
    empty = MediaInfo(path=Path("x.mp4"))
    etxt = build_info_text(empty)
    check("  空 info 不崩溃", isinstance(etxt, str) and len(etxt) > 0)
    check("  没有视频时不写视频段", "视频(" not in etxt)
    check("  没有色彩字段时不出空标签",
          StreamInfo().color_range_text == "" and StreamInfo().field_order_text == "")

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print(f"  失败：{name}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())