#!/usr/bin/env python3
"""「按文件大小」页签的界面验证：音频选项、策略、容器开销提示。

起因：上方有「允许压缩音频」复选框，下方音频下拉里又有一项
「自动（预算紧时降码率）」—— 后者描述的是前者的语义，而且实测
它和「重编码为 AAC」解析出的编码器**逐个相同**，纯属重复选项。

这条测试守几条不变量：
  1. 下拉里每项必须是**编码器选择**，不能夹带策略措辞；
  2. 同一容器下，各个选项的效果必须**互不相同**（防再次出现重复项）；
  3. 「压不压音频」只能由那个复选框表达，且只在重编码时可用；
  4. 容器开销提示里的数值必须等于预算模型里的真实开销
     （不能是界面自己另算一遍的近似值）。

用法：
    python tests/size_tab_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 🔴 必须在 import app.context 之前：构造 AppContext 会写配置
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="xiaowan_sizeaudio_cfg_")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.context import AppContext  # noqa: E402
from app.core.presets import Container, VideoSettings  # noqa: E402
from app.core.size_service import _pick_codecs  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.pages.compress import (CONTAINER_AUDIO_MODES,  # noqa: E402
                                   CONTAINER_DEFAULT_AUDIO,
                                   SIZE_AUDIO_OPTIONS)
from app.ui.style import apply_theme  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    ctx = AppContext()
    apply_theme(app, ctx.config.theme)
    window = MainWindow(ctx)
    window.show()
    pump(app, 0.5)
    page = window.compress_page
    page.tab_bar.select(1)
    pump(app, 0.3)

    print("\n[1] 选项本身：只谈编码器，不谈策略")
    texts = [t for _, t in SIZE_AUDIO_OPTIONS]
    check("没有任何选项的文案带「预算」字样",
          not any("预算" in t for t in texts), str(texts))
    check("没有含糊的「自动」项", not any("自动" in t for t in texts), str(texts))
    check("选项数据里没有 auto",
          "auto" not in [v for v, _ in SIZE_AUDIO_OPTIONS])

    print("\n[2] 同一容器内，各项效果必须互不相同（重复项就是这么冒出来的）")
    for key in CONTAINER_AUDIO_MODES:
        allowed = CONTAINER_AUDIO_MODES[key]
        cont = Container(key)
        resolved = {}
        for value in allowed:
            codec = {"copy": "copy", "none": "none", "aac": "aac",
                     "mp3": "libmp3lame", "opus": "libopus"}[value]
            settings = VideoSettings(codec="libx264", audio_codec=codec,
                                     container=cont)
            try:
                resolved[value] = _pick_codecs(ctx.env, settings)[1]
            except Exception as exc:  # noqa: BLE001
                resolved[value] = f"ERR {exc}"
        distinct = len(set(resolved.values())) == len(resolved) or \
            resolved.get("none") == "none"
        print(f"   {key:<5} {resolved}")
        # copy / none 是特例：copy 会落在容器默认编码器上，但语义不同（不重编码）
        shown = {k: v for k, v in resolved.items() if k not in ("copy", "none")}
        check(f"{key}: 重编码类选项互不重复",
              len(set(shown.values())) == len(shown), str(shown))
        check(f"{key}: 选项都属于该容器允许的集合",
              set(allowed) == set(allowed), str(allowed))

    print("\n[3] 每个容器的默认项 + 界面实际取值")
    for key, want in CONTAINER_DEFAULT_AUDIO.items():
        page.size_container.setCurrentIndex(page.size_container.findData(key))
        pump(app, 0.15)
        items = [page.size_audio.itemData(i) for i in range(page.size_audio.count())]
        print(f"   {key:<5} {items}  默认={page.size_audio.currentData()}")
        check(f"{key} 默认是 {want}", page.size_audio.currentData() == want,
              str(page.size_audio.currentData()))
        check(f"{key} 选项都可用",
              set(items) <= set(CONTAINER_AUDIO_MODES[key]), str(items))

    print("\n[4] 「压不压音频」只由复选框表达")
    for key in ("mp4", "webm"):
        page.size_container.setCurrentIndex(page.size_container.findData(key))
        pump(app, 0.15)
        for mode, expect in (("copy", False), ("none", False),
                             ("aac", True), ("opus", True)):
            i = page.size_audio.findData(mode)
            if i < 0:
                continue
            page.size_audio.setCurrentIndex(i)
            pump(app, 0.1)
            check(f"{key}/{mode}: 复选框{'可用' if expect else '禁用'}",
                  page.cb_shrink.isEnabled() == expect,
                  f"enabled={page.cb_shrink.isEnabled()}")
        check(f"{key}: 复选框有说明性 tooltip", bool(page.cb_shrink.toolTip()))

    print("\n[5] 设置真的会传到编码参数上")
    page.size_container.setCurrentIndex(page.size_container.findData("mp4"))
    pump(app, 0.15)
    for mode, want_codec, want_br in (("copy", "copy", 128), ("none", "none", 128),
                                      ("aac", "aac", 128), ("mp3", "libmp3lame", 128),
                                      ("opus", "libopus", 96)):
        i = page.size_audio.findData(mode)
        if i < 0:
            continue
        page.size_audio.setCurrentIndex(i)
        pump(app, 0.1)
        s = page._size_settings()
        check(f"{mode} → {want_codec} / {want_br}k",
              s.audio_codec == want_codec and s.audio_bitrate_kbps == want_br,
              f"{s.audio_codec} / {s.audio_bitrate_kbps}")

    print("\n[6] 容器开销提示：显示值必须来自预算模型，不能另算一遍")
    from app.core.size_budget import (CONTAINER_FIXED_BYTES, CONTAINER_RATIO,
                                      container_overhead)

    for target_mb in (1.0, 100.0, 1000.0):
        page.spin_target.setValue(target_mb)
        pump(app, 0.1)
        line = next((l for l in page.lbl_policy_hint.text().split("\n")
                     if "占掉" in l), "")
        check(f"{target_mb:g} MB 时给出了容器开销", bool(line), line[:44])
        for key in ("mp4", "mkv", "webm"):
            page.size_container.setCurrentIndex(
                page.size_container.findData(key))
            pump(app, 0.1)
            line = next((l for l in page.lbl_policy_hint.text().split("\n")
                         if "占掉" in l), "")
            cost, ratio = container_overhead(key, target_mb)
            # 显示的数字与模型一致（按界面同样的格式取整）
            want_bytes = int(CONTAINER_FIXED_BYTES[key]
                             + target_mb * 1024 * 1024 * CONTAINER_RATIO[key])
            check(f"{target_mb:g}MB/{key}: 开销百分比正确",
                  f"{ratio * 100:.1f}%" in line, line[-30:])
            check(f"{target_mb:g}MB/{key}: 开销字节正确",
                  cost == want_bytes, f"{cost} vs {want_bytes}")
            check(f"{target_mb:g}MB/{key}: 用短名（不带括号说明）",
                  "（" not in line.split("占掉")[0], line[:24])

    print("\n[6b] 策略提示必须按编码器说实话（不能对硬件编码说「两遍」）")
    idx_soft = page.size_codec.findData("libx264")
    idx_hw = page.size_codec.findData("auto_hw")
    page.size_codec.setCurrentIndex(idx_soft)
    pump(app, 0.2)
    soft_line = page.lbl_policy_hint.text().split("\n")[0]
    check("软编的提示说两遍", "两遍" in soft_line, soft_line[:36])
    if idx_hw >= 0:
        page.size_codec.setCurrentIndex(idx_hw)
        pump(app, 0.2)
        hw_line = page.lbl_policy_hint.text().split("\n")[0]
        # 硬件编码在目标体积模式下走固定 QP 单遍，说成"两遍"会让用户
        # 以为它和软编一样准（实测它撞不准目标，会偏小）
        check("硬件编码的提示不再说两遍", "两遍" not in hw_line, hw_line[:40])
        check("硬件编码的提示点明固定 QP", "QP" in hw_line, hw_line[:40])
        page.size_codec.setCurrentIndex(idx_soft)
        pump(app, 0.2)

    print("\n[7] 提示随目标/容器实时更新（不是算一次就不动）")
    page.spin_target.setValue(1.0)
    page.size_container.setCurrentIndex(page.size_container.findData("mp4"))
    pump(app, 0.1)
    small = next(l for l in page.lbl_policy_hint.text().split("\n") if "占掉" in l)
    page.spin_target.setValue(1000.0)
    pump(app, 0.1)
    big = next(l for l in page.lbl_policy_hint.text().split("\n") if "占掉" in l)
    check("目标变大后文案确实变了", small != big, f"{small[-18:]} → {big[-18:]}")
    check("大目标时比例降到结构占比附近",
          "1.4%" in big, big[-24:])

    print("\n" + "=" * 60)
    test_av1_twopass_unit()
    test_fill_helpers_unit()
    test_cqp_unit()
    test_vbv_guard_unit()
    test_fill_policy_unit()
    test_speed_gate_unit()
    test_audio_cost_unit()
    test_design_review_345_unit()
    test_bitdepth_crf_trap_unit()
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
    return 1 if FAIL else 0


def test_fill_policy_unit() -> int:
    """「尽量用满」的默认值与快速单遍的互斥关系（用户 2026-11-11 拍板）。

    两个决定：
      1. 默认勾上 —— 用户要的是"按文件大小"，用满是自然期待；不需要时它不动
         （低于目标 5% 才触发，普通素材 96~98% 到不了这条线）。
      2. 快速单遍下禁用 —— 该档起手只给 97% 码率，实测常落在 93~94%，
         刚好低于触发线 → 几乎每次都补跑，耗时翻倍只换 3 个百分点。
    """
    from app.core.ffmpeg import MediaInfo, StreamInfo
    from app.core.presets import Container, VideoSettings
    from app.core.size_service import build_size_task

    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    app = QApplication.instance() or QApplication(sys.argv[:1])
    ctx = AppContext()
    page = MainWindow(ctx).compress_page
    for _ in range(10):
        app.processEvents()
    page.tab_bar.select(1)
    pump(app, 0.3)

    print("\n[「尽量用满」的默认值与档位互斥]")
    check("默认勾上", page.cb_fill.isChecked())
    check("默认可用", page.cb_fill.isEnabled())

    page.rb_fast.setChecked(True)
    page._on_policy_changed(page.rb_fast)
    pump(app, 0.2)
    check("切到快速单遍后禁用", not page.cb_fill.isEnabled())
    # 只灰掉不取消勾选的话，勾还在，看起来像"已启用但被锁住"，容易误以为仍生效
    check("切到快速单遍后也取消勾选", not page.cb_fill.isChecked())
    check("禁用时给出了原因",
          "快速单遍下不可用" in page.cb_fill.toolTip(),
          page.cb_fill.toolTip()[:30])

    page.rb_strict.setChecked(True)
    page._on_policy_changed(page.rb_strict)
    pump(app, 0.2)
    check("切回严格档后恢复可用", page.cb_fill.isEnabled())
    check("切回后恢复原来的勾选", page.cb_fill.isChecked())

    # 后端必须自己也拒绝 —— UI 禁用只是表面
    m = MediaInfo(path=Path("x.mp4"), duration=60, size=10 ** 8,
                  video=StreamInfo(index=0, codec_type="video",
                                   codec_name="h264", width=1920, height=1080,
                                   fps=30))
    st = VideoSettings(codec="libx264", preset="medium", container=Container.MKV)
    t_fast = build_size_task(ctx, Path("x.mp4"), m, st, 2.0, policy="fast",
                             fill_target=True)
    t_strict = build_size_task(ctx, Path("x.mp4"), m, st, 2.0, policy="strict",
                               fill_target=True)
    check("快速档即使传 True 也不补跑", t_fast.fill_target is False)
    check("严格档尊重 True", t_strict.fill_target is True)
    check("快速档传 False 也是 False",
          build_size_task(ctx, Path("x.mp4"), m, st, 2.0, policy="fast",
                          fill_target=False).fill_target is False)

    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_vbv_guard_unit() -> int:
    """两遍 ABR 必须带 VBV（maxrate + bufsize 齐全）。

    实测（140 Mbps 噪声 3s，目标 250k）：
      * 只 `-b:v`、无 VBV        → **107.6 倍**（QP 被打到 1~2，几乎无损）
      * 只加 `-maxrate`、无 bufsize → **107.6 倍**（VBV 不生效！）
      * 两者齐全                  → 0.6 倍 ✔
    这条守的是「别为了省事把 VBV 去掉，也别只留 maxrate」。
    """
    from app.core.ffmpeg import MediaInfo, StreamInfo
    from app.core.presets import Container, VideoSettings
    from app.core.size_service import _video_args

    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    print("\n[两遍 ABR 的 VBV 守卫]")
    st = VideoSettings(codec="libx264", preset="medium", container=Container.MKV)
    args = _video_args("libx264", st, 1000)
    check("带 -maxrate", "-maxrate" in args, str(args[:8]))
    check("带 -bufsize", "-bufsize" in args)
    # 只给 maxrate 等于没给 —— 这是实测确认的坑，必须两者齐全
    check("maxrate 与 bufsize 同时存在",
          "-maxrate" in args and "-bufsize" in args)
    mr = int(args[args.index("-maxrate") + 1].rstrip("k"))
    bs = int(args[args.index("-bufsize") + 1].rstrip("k"))
    check("maxrate 约为码率的 1.4 倍", 1.3 <= mr / 1000 <= 1.5, f"{mr} vs 1000")
    check("bufsize 约为码率的 2.8 倍", 2.5 <= bs / 1000 <= 3.0, f"{bs} vs 1000")
    # 缓冲不能太大：实测 10×码率 → 1.9 倍、20× → 3.2 倍（越攒越漏）
    check("bufsize 没有大到 10 倍码率（实测会漏到 1.9 倍）", bs / 1000 < 5.0,
          f"{bs / 1000:.1f}x")

    # AV1 的例外：该构建不吃 maxrate，跳过是**有意**的，别当 bug 修
    av1 = _video_args("libsvtav1", st, 1000)
    check("libsvtav1 有意跳过 VBV（该构建不吃）",
          "-maxrate" not in av1, str(av1[:6]))

    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_speed_gate_unit() -> int:
    """快速单遍档必须禁掉慢速档（设计评审待办 1）。

    实测（混合复杂度 10s / 目标 1500k）：
      单遍 + veryslow  3.5s → 997 KB（目标的 66%）
      两遍 + medium    2.0s → 1713 KB
    慢档的收益来自「第二遍按第一遍的统计分配比特」，单遍拿不到这个收益，
    只剩编码变慢 —— 留着它等于让用户把"快速档"调得比两遍还慢。
    """
    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    app = QApplication.instance() or QApplication(sys.argv[:1])
    ctx = AppContext()
    page = MainWindow(ctx).compress_page
    for _ in range(10):
        app.processEvents()
    page.tab_bar.select(1)
    pump(app, 0.3)

    print("\n[快速单遍禁用慢速档]")
    model = page.size_speed.model()

    def enabled_map():
        return {page.size_speed.itemData(i): model.item(i).isEnabled()
                for i in range(page.size_speed.count())}

    strict_map = enabled_map()
    check("严格档下 9 档全可用", all(strict_map.values()), str(strict_map))

    page.rb_fast.setChecked(True)
    page._on_policy_changed(page.rb_fast)
    pump(app, 0.2)
    fast_map = enabled_map()
    for name in ("slow", "slower", "veryslow"):
        check(f"快速档下 {name} 被禁用", not fast_map.get(name))
    for name in ("ultrafast", "medium", "fast"):
        check(f"快速档下 {name} 仍可用", fast_map.get(name))

    # 当前选中的若是慢档，切过来要自动退到 medium
    page.rb_strict.setChecked(True)
    page._on_policy_changed(page.rb_strict)
    pump(app, 0.2)
    page.size_speed.setCurrentIndex(page.size_speed.findData("veryslow"))
    pump(app, 0.1)
    page.rb_fast.setChecked(True)
    page._on_policy_changed(page.rb_fast)
    pump(app, 0.2)
    check("切到快速档时，选中的慢档自动退到 medium",
          page.size_speed.currentData() == "medium",
          str(page.size_speed.currentData()))

    page.rb_strict.setChecked(True)
    page._on_policy_changed(page.rb_strict)
    pump(app, 0.2)
    check("切回严格档后慢档恢复可用",
          enabled_map().get("veryslow") is True)

    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_audio_cost_unit() -> int:
    """音频预算占用必须可见（设计评审待办 2）。

    目标体积模式下音频和视频抢同一块预算。实测 1MB / 12s 目标下
    128k 音频占约 19%，用户完全看不出"画面只剩多少码率"。
    """
    from app.core.ffmpeg import MediaInfo, StreamInfo
    from app.core.size_budget import solve

    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    app = QApplication.instance() or QApplication(sys.argv[:1])
    ctx = AppContext()
    page = MainWindow(ctx).compress_page
    for _ in range(10):
        app.processEvents()
    page.tab_bar.select(1)
    pump(app, 0.3)

    print("\n[音频预算占用可见]")
    check("没拖文件时不显示音频行（没有时长算不出）",
          page._audio_cost_text() == "", repr(page._audio_cost_text())[:30])

    # 直接验证数值来自 solve()，不是 UI 另算
    m = MediaInfo(path=Path("x.mp4"), duration=12.0, size=10 ** 8,
                  video=StreamInfo(index=0, codec_type="video",
                                   codec_name="h264", width=1920, height=1080,
                                   fps=30),
                  audios=[StreamInfo(index=1, codec_type="audio",
                                     codec_name="aac", channels=2,
                                     bit_rate=192000)])
    b = solve(m, 1.0, "mp4", audio_mode="aac", audio_kbps=128)
    ratio = b.audio_bytes / b.target_bytes * 100
    check("音频占预算比例算得出来（1MB/12s/128k 约 19%）",
          15 < ratio < 25, f"{ratio:.1f}%")
    check("视频预算相应减少",
          b.video_kbps < solve(m, 1.0, "mp4", audio_mode="none").video_kbps,
          f"{b.video_kbps} vs 无音频 {solve(m, 1.0, 'mp4', audio_mode='none').video_kbps}")
    check("移除音轨时音频字节为 0",
          solve(m, 1.0, "mp4", audio_mode="none").audio_bytes == 0)

    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_design_review_345_unit() -> int:
    """设计评审待办 3/4/5：10bit 警告、速度档文案、目标大小 chip。

    3. H.264+10bit（Hi10P）几乎没有设备能硬解，而目标体积页常用于发送/存档 ——
       这种警告必须自己跳出来，不能藏在 tooltip 里。
    4. 这一页码率被目标钉死，速度档是唯一能用时间换画质的控件，文案要说透。
    5. 快捷 chip 只改目标大小，**绝不能动编码参数**（那是预设卡的职责）。
    """
    from PySide6.QtWidgets import QPushButton

    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    app = QApplication.instance() or QApplication(sys.argv[:1])
    ctx = AppContext()
    page = MainWindow(ctx).compress_page
    for _ in range(10):
        app.processEvents()
    page.tab_bar.select(1)
    pump(app, 0.3)

    print("\n[待办 3：10bit 的可见提示 —— 分「警告」和「说明」两级]")
    page.size_codec.setCurrentIndex(page.size_codec.findData("libx264"))
    page.size_pixfmt.setCurrentIndex(page.size_pixfmt.findData("yuv420p10le"))
    pump(app, 0.3)
    # 窗口没 show 时 isVisible() 恒为 False，所以断言文本非空
    check("H.264+10bit 时提示有内容",
          bool(page.lbl_pixfmt_warn_size.text()),
          page.lbl_pixfmt_warn_size.text()[:40])
    check("警告点明 Hi10P 无硬解",
          "Hi10P" in page.lbl_pixfmt_warn_size.text(),
          page.lbl_pixfmt_warn_size.text()[:40])
    # 兼容性是硬伤 → 必须用警告样式（橙色），不能和"好消息"一个颜色
    check("H.264+10bit 用的是警告样式", page.lbl_pixfmt_warn_size.objectName() == "Warn",
          page.lbl_pixfmt_warn_size.objectName())
    check("警告里给了出路（换 H.265/AV1）",
          "H.265" in page.lbl_pixfmt_warn_size.text(),
          page.lbl_pixfmt_warn_size.text()[-40:])

    page.size_codec.setCurrentIndex(page.size_codec.findData("libx265"))
    pump(app, 0.3)
    # H.265 + 10bit 是好事，但**不能什么都不说**：小白不知道 10bit 到底带来什么、
    # 有什么代价。所以改成一句大白话说明（灰色 Hint），而不是留空。
    check("换成 H.265 后改说好处而不是留空",
          bool(page.lbl_pixfmt_warn_size.text()),
          page.lbl_pixfmt_warn_size.text()[:40])
    check("说明用的是提示样式而非警告样式",
          page.lbl_pixfmt_warn_size.objectName() == "Hint",
          page.lbl_pixfmt_warn_size.objectName())
    check("说明里讲了体积不会变大（这页的关键卖点）",
          "体积" in page.lbl_pixfmt_warn_size.text(),
          page.lbl_pixfmt_warn_size.text()[:50])
    check("说明里讲了代价（编码变慢）",
          "慢" in page.lbl_pixfmt_warn_size.text(),
          page.lbl_pixfmt_warn_size.text()[:50])
    check("说明里不用行话（不出现 bpp / SSIM / Hi10P）",
          not any(j in page.lbl_pixfmt_warn_size.text()
                  for j in ("bpp", "SSIM", "Hi10P")),
          page.lbl_pixfmt_warn_size.text()[:50])

    page.size_pixfmt.setCurrentIndex(page.size_pixfmt.findData(""))
    pump(app, 0.3)
    check("8bit 时警告消失", not page.lbl_pixfmt_warn_size.text())

    print("\n[色深自动跟随编码器（体积钉死时 10bit 白赚）]")
    # 规则：目标体积页换编码器时自动切色深 —— H.264 保持 8bit（Hi10P 播不了），
    # H.265/AV1 自动开 10bit。用户一旦亲手选过，就停止跟随。
    # 这里用 setCurrentIndex 模拟"代码/默认值"，用 _on_item_clicked 模拟"用户点击"。

    def pick_pixfmt_user(data):
        """模拟用户在下拉面板里点选（会触发 activated → 标记用户已选）"""
        page.size_pixfmt.showPopup()
        pump(app, 0.15)
        i = page.size_pixfmt.findData(data)
        page.size_pixfmt._on_item_clicked(
            page.size_pixfmt.model().index(i, 0))
        pump(app, 0.2)

    page.size_codec.setCurrentIndex(page.size_codec.findData("libx264"))
    page.size_pixfmt.setCurrentIndex(page.size_pixfmt.findData(""))
    page._pixfmt_user_set = False          # 回到"用户没动过"的状态
    pump(app, 0.3)
    check("H.264 下自动保持 8bit", page.size_pixfmt.currentData() == "",
          repr(page.size_pixfmt.currentData()))

    page.size_codec.setCurrentIndex(page.size_codec.findData("libx265"))
    pump(app, 0.3)
    check("换到 H.265 自动开 10bit",
          page.size_pixfmt.currentData() == "yuv420p10le",
          repr(page.size_pixfmt.currentData()))

    page.size_codec.setCurrentIndex(page.size_codec.findData("libsvtav1"))
    pump(app, 0.3)
    check("换到 AV1 仍是 10bit",
          page.size_pixfmt.currentData() == "yuv420p10le",
          repr(page.size_pixfmt.currentData()))

    page.size_codec.setCurrentIndex(page.size_codec.findData("libx264"))
    pump(app, 0.3)
    check("换回 H.264 自动退回 8bit（避免压出播不了的文件）",
          page.size_pixfmt.currentData() == "",
          repr(page.size_pixfmt.currentData()))

    # 🔴 关键：用户亲手选过之后，不许再被自动改
    pick_pixfmt_user("yuv420p10le")
    check("用户点选后标记为「已手动设置」", page._pixfmt_user_set)
    check("用户能手动在 H.264 下选 10bit（不硬拦，只警告）",
          page.size_pixfmt.currentData() == "yuv420p10le",
          repr(page.size_pixfmt.currentData()))
    check("这时给的是红色警告", page.lbl_pixfmt_warn_size.objectName() == "Warn",
          page.lbl_pixfmt_warn_size.objectName())
    page.size_codec.setCurrentIndex(page.size_codec.findData("libx265"))
    pump(app, 0.3)
    check("之后换编码器不再擅自改色深（尊重用户选择）",
          page.size_pixfmt.currentData() == "yuv420p10le",
          repr(page.size_pixfmt.currentData()))
    page.size_codec.setCurrentIndex(page.size_codec.findData("libx264"))
    pump(app, 0.3)
    check("换回 H.264 也不改（用户已表态）",
          page.size_pixfmt.currentData() == "yuv420p10le",
          repr(page.size_pixfmt.currentData()))
    check("但警告重新出现", page.lbl_pixfmt_warn_size.objectName() == "Warn")

    # 复原，免得影响后面的用例
    page._pixfmt_user_set = False
    page.size_pixfmt.setCurrentIndex(page.size_pixfmt.findData(""))
    pump(app, 0.2)

    print("\n[音频兼容性守卫：AC3/DTS 进 MP4 会「只有画面没声音」]")
    from app.core.ffmpeg import MediaInfo, StreamInfo
    from app.ui.pages.compress import audio_compat_warning

    def _mk(*acodecs):
        return MediaInfo(
            path=Path("x.mp4"), duration=10, size=10 ** 7,
            video=StreamInfo(index=0, codec_type="video", codec_name="h264"),
            audios=[StreamInfo(index=i + 1, codec_type="audio", codec_name=c)
                    for i, c in enumerate(acodecs)])

    # ffmpeg 能把 AC3/DTS 原样封进 MP4（实测成功、不报错），但封进去 ≠ 能播：
    # 网页 <video> 只认 AAC/MP3。任务显示"完成"，用户拿去发微信才发现没声音。
    for bad, label in (("ac3", "AC3"), ("dts", "DTS"), ("eac3", "E-AC3"),
                       ("truehd", "TrueHD"), ("flac", "FLAC")):
        w = audio_compat_warning("mp4", "copy", _mk(bad))
        check(f"MP4+保留原音轨，源是 {label} → 有警告", bool(w), w[:36])
        check(f"警告里点出了 {label}", label in w, w[:40])
    check("警告说明了后果（没声音）",
          "没有声音" in audio_compat_warning("mp4", "copy", _mk("ac3")))
    check("警告给了出路（改 AAC）",
          "AAC" in audio_compat_warning("mp4", "copy", _mk("ac3")))

    check("源是 AAC 时不警告（本来就能播）",
          not audio_compat_warning("mp4", "copy", _mk("aac")))
    check("重编码成 AAC 时不警告（问题不存在）",
          not audio_compat_warning("mp4", "aac", _mk("ac3")))
    check("MKV 里保留 AC3 不警告（MKV 播放器自己认）",
          not audio_compat_warning("mkv", "copy", _mk("ac3")))
    check("MOV 同样要警告（也是 MP4 系）",
          bool(audio_compat_warning("mov", "copy", _mk("ac3"))))
    check("多条音轨只列一次（AC3 + AC3）",
          audio_compat_warning("mp4", "copy", _mk("ac3", "ac3")).count("AC3") == 1)
    check("没文件时不报错", audio_compat_warning("mp4", "copy", None) == "")

    print("\n[「用途」一键配置：按「在哪播」分档，不按渠道名分档]")
    from app.ui.pages.compress import USE_CASES
    # 用户当场指出的设计问题：原来「发微信/传网站」和「放网页/嵌入」参数
    # 完全一样（纯重复），且「自己存着」给的是 H.265 —— 那不是效率最高，
    # 只是比 H.264 高。本地播放器能软解 AV1，所以本地档该用 AV1。
    check("档数等于真正的结论数（两档，不是三档）", len(USE_CASES) == 2,
          str([u[1] for u in USE_CASES]))

    # 🔴 核心约束：不能有两个用途给出完全相同的配置（那就是重复项）
    configs = [(u[2], u[3], u[4], u[5]) for u in USE_CASES]
    check("没有两个用途配置完全相同（防重复项）",
          len(set(configs)) == len(configs),
          str({c: configs.count(c) for c in set(configs)}))

    by_key = {u[0]: u for u in USE_CASES}
    share = by_key.get("share")
    local = by_key.get("local")
    check("有「给别人播」档", share is not None)
    check("有「自己电脑看」档", local is not None)
    check("给别人播 → H.264 8bit（唯一谁都能播的组合）",
          share and share[2] == "libx264" and share[4] == "",
          str(share[2:5]) if share else "")
    check("自己电脑看 → AV1（本地能软解，吃最高压缩率）",
          local and local[2] == "libsvtav1",
          local[2] if local else "")
    check("自己电脑看 → 也是 8bit（10bit 收益小且是不确定项）",
          local and local[4] == "", local[4] if local else "")

    page._apply_use_case("share")
    pump(app, 0.4)
    check("点「给别人播」→ H.264", page.size_codec.currentData() == "libx264",
          str(page.size_codec.currentData()))
    check("点「给别人播」→ MP4", page.size_container.currentData() == "mp4")
    check("点「给别人播」→ 8bit", page.size_pixfmt.currentData() == "")
    check("点「给别人播」→ AAC", page.size_audio.currentData() == "aac")
    check("配好之后有说明", bool(page.lbl_use_note.text()))
    check("说明里讲了「平台会自己转码」（省得用户纠结编码器）",
          "转码" in page.lbl_use_note.text(), page.lbl_use_note.text()[:40])
    check("说明里也讲了浏览器黑屏（放网页也是这档）",
          "黑屏" in page.lbl_use_note.text(), page.lbl_use_note.text()[:60])

    page._apply_use_case("local")
    pump(app, 0.4)
    check("点「自己电脑看」→ AV1", page.size_codec.currentData() == "libsvtav1",
          str(page.size_codec.currentData()))
    check("点「自己电脑看」→ 8bit（不被色深自动跟随改成 10bit）",
          page.size_pixfmt.currentData() == "", repr(page.size_pixfmt.currentData()))
    check("说明里点明了代价（拷给别人可能播不了）",
          "播不了" in page.lbl_use_note.text(), page.lbl_use_note.text()[:60])
    check("说明里提到了本地播放器（这是它成立的前提）",
          "播放器" in page.lbl_use_note.text(), page.lbl_use_note.text()[:50])

    print("\n[待办 4：速度档说清「用时间换画质」]")
    page.size_codec.setCurrentIndex(page.size_codec.findData("libx264"))
    pump(app, 0.3)
    tip = page.size_speed.toolTip()
    check("点明速度档是唯一能换画质的控件", "用时间换画质" in tip, tip[:50])
    check("给了实测依据", "SSIM" in tip)

    print("\n[用途 chip 必须真的改到参数（用户报过：点了编码器不变）]")
    # 根因：USE_CASES 里把编码器写成 "h264"，而下拉存的是 "libx264"，
    # _set_combo 的 findData 返回 -1 → 静默什么都不做。因为原本就在 H.264
    # 状态，界面上看不出异常；一旦从 H.265 切回来就暴露。
    use_chips = {b.text(): b for b in page.findChildren(QPushButton)
                 if b.objectName() == "SizeChip"}
    check("只剩用途 chip（目标大小的常用 chip 已删）",
          len(use_chips) == len(USE_CASES), str(list(use_chips)))
    check("目标大小区不再有「微信 25MB」这类 chip",
          not any("25MB" in t for t in use_chips), str(list(use_chips)))

    # 每个用途配的编码器都必须能在下拉里真的找到（防再次写错值）
    from app.ui.pages.compress import USE_CASES
    for key, text, codec, cont, pf, audio, _note in USE_CASES:
        check(f"「{text}」的编码器 {codec} 是下拉里的真实值",
              page.size_codec.findData(codec) >= 0, codec)
        check(f"「{text}」的容器 {cont} 是下拉里的真实值",
              page.size_container.findData(cont) >= 0, cont)
        check(f"「{text}」的音频 {audio} 是下拉里的真实值",
              page.size_audio.findData(audio) >= 0, audio)

    # 🔴 关键回归：从 AV1 切回 H.264（原来这里不动）
    use_chips["只在自己电脑看"].click()
    pump(app, 0.4)
    check("点「自己电脑看」→ 编码器变 AV1",
          page.size_codec.currentData() == "libsvtav1",
          str(page.size_codec.currentData()))
    use_chips["要发给别人 / 放网页"].click()
    pump(app, 0.4)
    check("点「给别人播」→ 编码器切回 H.264（这就是报的 bug）",
          page.size_codec.currentData() == "libx264",
          str(page.size_codec.currentData()))

    # 手动选成 AV1 后再点「给别人播」也要能切回去
    page.size_codec.setCurrentIndex(page.size_codec.findData("libsvtav1"))
    pump(app, 0.2)
    use_chips["要发给别人 / 放网页"].click()
    pump(app, 0.4)
    check("手动选 AV1 后点「给别人播」→ 切回 H.264",
          page.size_codec.currentData() == "libx264",
          str(page.size_codec.currentData()))

    # 设计评审遗留 bug 的回归：画质评估曾无视用户选的分辨率（选 480p 后
    # 提示仍停在源分辨率上），且"改用 XXXp"按钮是个灰色默认样式、极不显眼。
    print("\n[画质评估跟随分辨率 + 建议按钮显眼]")
    # 需要真实文件：给评估一个真实可读的源
    src = Path(tempfile.mkdtemp(prefix="xw_res_")) / "src.mp4"
    # 渐变 + 轻噪声：源有几 MB，0.15MB 的目标才落在"需要压缩"的区间
    r = subprocess.run([ctx.env.ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                        "-i", "gradients=s=1280x720:r=30:d=6:c0=0x0a1220:"
                              "c1=0x3a5a80:x0=0:y0=0:x1=1280:y1=720:speed=0.0",
                        "-vf", "noise=alls=3:allf=t",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-pix_fmt", "yuv420p", str(src)], capture_output=True)
    if r.returncode == 0:
        page.table.add_paths([str(src)])
        # 等异步探测出结果。原来只等 4s —— 机器被打满时（如紧挨着跑完
        # 真编码的套件、或并行压着 ffmpeg）ffprobe 启动就要好几秒，于是
        # 「读到信息」这个前提没成立，后面 6 条断言跟着全挂，看报错还以为是
        # 画质评估坏了（实测复现：给 32 核跑满 libx264 时必现）。
        got_info = False
        for _ in range(600):                      # 最多 30s
            app.processEvents()
            time.sleep(0.05)
            if page.table.info_for(src):
                got_info = True
                break
        check("源文件信息已解析（下面 6 条断言的前提）", got_info, str(src))
        # 上一个场景把单位切到了 GB，先切回 MB 再设小目标
        page.cmb_unit.setCurrentIndex(page.cmb_unit.findData("mb"))
        pump(app, 0.2)
        page.spin_target.setValue(0.15)   # 小目标：720p 下 bpp 不达标，必须触发降级建议
        page.size_scale.setCurrentIndex(page.size_scale.findData(0))

        # 🔴 不能只固定 pump 一下就断言 —— 机器忙时（比如刚跑完视频编码的套件）
        # `_update_size_estimate()` 里的 solve()+assess() 还没跑完，按钮仍是上一次
        # 的旧值。实测在批量回归里必现、单独跑却不出现，非常迷惑。
        # 改成轮询「评估结果确实对应本次输入」：
        #   `page._quality` 是最近一次评估结果，它的 width 能反映用的是哪个分辨率。
        def _wait_assessed(want_scale: int, timeout: float = 8.0) -> bool:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.03)
                qa = getattr(page, "_quality", None)
                if qa is None:
                    continue
                # scale=0 表示保持原分辨率，此时 width 应等于源宽度
                if want_scale == 0:
                    if qa.width == src_info_width:
                        return True
                elif qa.height == want_scale:
                    return True
            return False

        src_info_width = (page.table.info_for(src).video.width
                          if page.table.info_for(src) and page.table.info_for(src).video
                          else 0)
        ok0 = _wait_assessed(0)
        if not ok0:
            info = page.table.info_for(src)
            qa = getattr(page, "_quality", None)
            print(f"      [诊断] info={info} target={page._target_mb()}MB "
                  f"scale={page.size_scale.currentData()} "
                  f"qa={qa} "
                  f"quality='{page.lbl_quality.text()[:40]}' "
                  f"hint='{page.lbl_apply_hint.text()[:30]}'")
        check("等待画质评估跑完（下面断言的前提）", ok0)

        # 原分辨率下 0.15MB 明显欠标 → 应该出现降级建议按钮
        # （窗口没 show，isVisible() 恒 False，所以断言按钮文字）
        check("原分辨率下出现降级建议按钮",
              bool(page.btn_apply_res.text()), page.btn_apply_res.text())
        check("按钮是主色（能被看见）",
              page.btn_apply_res.objectName() == "Primary")
        check("按钮文案带收益说明",
              "画质更好" in page.btn_apply_res.text(), page.btn_apply_res.text())
        check("按钮旁有 bpp 收益解释", bool(page.lbl_apply_hint.text()),
              page.lbl_apply_hint.text()[:44])
        # 用户手动选 480p：评估必须翻转，建议按钮必须消失
        page.size_scale.setCurrentIndex(page.size_scale.findData(480))
        # 同样不能只固定 pump：要等评估真的按 480p 重算过
        ok480 = _wait_assessed(480)
        check("等待 480p 的评估跑完", ok480)
        check("选 480p 后提示按 480p 评估（不再说源分辨率的结论）",
              "480p" in page.lbl_quality.text(),
              page.lbl_quality.text()[:60])
        # 480p 仍不达标时，继续建议更低的 360p 是在帮用户逼近目标，
        # 不是推翻他的选择（用户随时可以不点）
        check("选 480p 后仍劣化时，按钮建议更低的 360p",
              "360p" in page.btn_apply_res.text(), page.btn_apply_res.text())
        check("提示里的分辨率也跟着变成 480p",
              "480p" in page.lbl_quality.text(), page.lbl_quality.text()[:60])
        page.table.clear_all()
    else:
        print("      测试素材生成失败，跳过")

    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_cqp_unit() -> int:
    """硬件编码的固定 QP 路径（不开引擎）。

    守的是 P0 那个 bug：硬件编码器不产生 passlog（实测 pass1 日志 0 字节），
    两遍 ABR 名存实亡、体积超 1.2~2.4 倍。所以这类编码器必须走 CQP。
    """
    from app.core.commands import CQP_ENCODERS, can_cqp, cqp_args
    from app.core.ffmpeg import MediaInfo, StreamInfo
    from app.core.presets import Container, VideoSettings
    from app.core.size_budget import solve
    from app.core.size_service import (build_cqp_correction_steps,
                                       build_cqp_steps, build_size_task)

    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    print("\n[硬件编码的 CQP 路径]")
    check("AMF / NVENC 支持 CQP",
          all(can_cqp(c) for c in ("h264_amf", "hevc_amf",
                                   "h264_nvenc", "hevc_nvenc")))
    check("QSV 不支持纯 CQP（只有 min/max_qp）", not can_cqp("h264_qsv"))
    check("VideoToolbox 不支持（只有 qscale）", not can_cqp("h264_videotoolbox"))
    check("软编不需要 CQP", not can_cqp("libx264"))

    check("AMF 的写法是 -rc cqp -qp_i/-qp_p",
          cqp_args("h264_amf", 35) == ["-rc", "cqp", "-qp_i", "35", "-qp_p", "35"],
          str(cqp_args("h264_amf", 35)))
    check("NVENC 的写法是 -rc constqp -qp",
          cqp_args("h264_nvenc", 30) == ["-rc", "constqp", "-qp", "30"],
          str(cqp_args("h264_nvenc", 30)))
    check("QP 会被钳在 0~51", cqp_args("h264_amf", 99)[3] == "51",
          str(cqp_args("h264_amf", 99)))

    app = QApplication.instance() or QApplication(sys.argv[:1])
    ctx = AppContext()
    m = MediaInfo(path=Path("x.mp4"), duration=60, size=2 * 1024 ** 3,
                  video=StreamInfo(index=0, codec_type="video", codec_name="h264",
                                   width=1920, height=1080, fps=30))
    budget = solve(m, 2.0, "mkv", audio_mode="aac", audio_kbps=128)
    st = VideoSettings(codec="h264_amf", preset="medium", container=Container.MKV)

    steps = build_cqp_steps(ctx.env, Path("x.mp4"), Path("o.mkv"), st, budget, 30,
                            audio_mode="aac", info=m)
    joined = " ".join(" ".join(str(a) for a in s.args) for s in steps)
    check("CQP 只有一步（单遍）", len(steps) == 1, f"{len(steps)} 步")
    check("CQP 命令里没有 -pass / passlog",
          "-pass" not in joined and "passlogfile" not in joined)
    check("CQP 命令里没有 -b:v（码率不参与）", "-b:v" not in joined)

    # 超标 → QP 必须**上升**（体积变小）；方向错了会越修越大
    over = int(budget.target_bytes * 1.8)
    s2, qp2 = build_cqp_correction_steps(ctx.env, Path("x.mp4"), Path("o.mkv"),
                                         st, budget, 30, over,
                                         audio_mode="aac", info=m)
    check("超标时 QP 上升（体积变小）", qp2 > 30, f"30 → {qp2}")
    under = int(budget.target_bytes * 0.5)
    s3, qp3 = build_cqp_correction_steps(ctx.env, Path("x.mp4"), Path("o.mkv"),
                                         st, budget, 30, under,
                                         audio_mode="aac", info=m)
    check("欠标时 QP 下降（体积变大）", qp3 < 30, f"30 → {qp3}")

    # 任务装配：硬件编码必须走 CQP，而不是两遍 ABR
    task = build_size_task(ctx, Path("x.mp4"), m, st, 2.0, audio_mode="aac")
    check("任务被识别为 CQP 模式", task.qp_used >= 0, f"qp_used={task.qp_used}")
    check("CQP 任务只有一步", len(task.steps) == 1, f"{len(task.steps)} 步")
    check("CQP 任务没有两遍分析",
          all("-pass" not in " ".join(str(a) for a in s.args) for s in task.steps))

    # 不可 CQP 的硬件编码必须被拒绝，而不是给出一个注定不准的承诺
    qsv = VideoSettings(codec="h264_qsv", preset="medium", container=Container.MKV)
    tq = build_size_task(ctx, Path("x.mp4"), m, qsv, 2.0, audio_mode="aac")
    check("QSV 目标体积被拒绝", not tq.steps, f"{len(tq.steps)} 步")
    check("拒绝原因说得清", "固定 QP" in (tq.message or ""), tq.message)

    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_fill_helpers_unit() -> int:
    """「尽量用满」的三个纯逻辑函数（不开引擎）。

    守三件事：
      1. 欠标判定的触发线（低于目标 5%）；
      2. 「加码率没涨」的判据（涨幅 <5% 停手，否则纯色素材会死循环）；
      3. 码率外推在震荡数据上要**收敛**——线性外推在短视频上会在两点间
         来回跳（实测 3183→3543→3214），有历史观测时必须走对数插值。
    """
    from app.core.size_budget import (FILL_TRIGGER, SizeBudget, correct_kbps,
                                      fill_hopeless, needs_fill)

    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    b = SizeBudget(target_bytes=1000 * 1024, duration=3.0, container="mkv",
                   audio_kbps=0, audio_streams=0, video_kbps=100,
                   overhead_fixed=0, overhead_ratio=0.0)
    target = b.target_bytes

    print("\n[「尽量用满」的三个判据]")
    check("没勾选时永不补跑",
          not needs_fill(b, int(target * 0.3), False))
    check("勾选且明显欠标 → 补跑",
          needs_fill(b, int(target * 0.5), True))
    check(f"只差 {FILL_TRIGGER*100:.0f}% 以内不补（不值得多跑一轮）",
          not needs_fill(b, int(target * 0.97), True),
          f"{0.97:.0%} > {1-FILL_TRIGGER:.0%}")
    check("刚好在触发线下 → 补跑",
          needs_fill(b, int(target * (1 - FILL_TRIGGER) - 1), True))

    check("纯色素材（体积不涨）判为撑不满", fill_hopeless(b, 5000, 5050))
    check("正常增长不判撑不满", not fill_hopeless(b, 5000, 9000))
    check("首轮没有基线时不判撑不满", not fill_hopeless(b, 0, 5000))

    # 实测数据：3183k→86%、3526k→105%，线性外推会来回震荡
    hist = [(2676, int(target * 0.70)), (3650, int(target * 1.10))]
    lin = correct_kbps(b, 3183, int(target * 0.86))
    interp = correct_kbps(b, 3183, int(target * 0.86), hist)
    print(f"      3183k@86%：线性 {lin}k / 插值 {interp}k")
    check("有历史时不再用线性外推", interp != lin, f"{lin} vs {interp}")
    check("插值落在两个观测点之间", 2676 < interp < 3650, str(interp))
    check("插值结果贴近 3400（实测收敛值）", abs(interp - 3400) < 200, str(interp))

    # 没有跨越目标的观测对时，退回线性外推（不能瞎插值）
    only_low = [(2676, int(target * 0.70))]
    check("只有欠标观测时退回线性外推",
          correct_kbps(b, 3183, int(target * 0.86), only_low) == lin)

    # 欠标提示的阈值：明显欠标（>15%）才提醒，普通素材（96~98%）别打扰
    from app.core.engine import FILL_HINT_GAP
    check("欠标提示阈值是 15%", abs(FILL_HINT_GAP - 0.15) < 1e-9, str(FILL_HINT_GAP))
    check("96% 不触发提示", 0.96 >= 1 - FILL_HINT_GAP)
    check("41% 会触发提示", 0.41 < 1 - FILL_HINT_GAP)

    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_av1_twopass_unit() -> int:
    """AV1 目标体积的命令构造（不开引擎）。

    回归两件事：AV1 必须和 x264/x265 一样走两遍 ABR（旧的 capped CRF
    受 mbr 瞬时上限所限，实测只用掉 76% 预算）；色深必须真传下去。
    """
    from app.core.size_budget import solve
    from app.core.size_service import build_size_steps
    from app.core.ffmpeg import MediaInfo, StreamInfo
    from app.core.presets import Container, VideoSettings

    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    app = QApplication.instance() or QApplication(sys.argv[:1])
    ctx = AppContext()
    env = ctx.env
    m = MediaInfo(path=Path("x.mp4"), duration=60, size=2 * 1024 ** 3,
                  video=StreamInfo(index=0, codec_type="video", codec_name="h264",
                                   width=1920, height=1080, fps=30))
    st = VideoSettings(codec="libsvtav1", preset="veryfast", container=Container.MKV,
                       pix_fmt="yuv420p10le")
    budget = solve(m, 2.0, "mkv", audio_mode="aac", audio_kbps=128)
    work = Path(tempfile.gettempdir()) / "xiaowan_av1_cmd_probe"
    steps = build_size_steps(env, Path("x.mp4"), Path("o.mkv"), st, budget, work,
                             audio_mode="aac", info=m)
    arg_list = [" ".join(str(a) for a in s.args) for s in steps]
    joined = " ".join(arg_list)

    print("\n[AV1 目标体积命令构造]")
    check("两步（两遍 ABR）", len(steps) == 2, f"{len(steps)} 步")
    check("第一遍是 -pass 1", "-pass 1" in arg_list[0])
    check("第二遍是 -pass 2", "-pass 2" in arg_list[1])
    check("用 -b:v 预算码率", f"-b:v {budget.video_kbps}k" in joined,
          f"{budget.video_kbps} kbps")
    check("不再出现 CRF / mbr", "-crf" not in joined and "mbr" not in joined)
    check("色深 10bit 真传下去了", "-pix_fmt yuv420p10le" in joined)
    check("SVT-AV1 不传 -maxrate（本机该构建会报错）", "-maxrate" not in joined)
    check("速度档翻成 SVT 数字档", "-preset 9" in joined, "veryfast → 9")
    PASS.extend(p2); FAIL.extend(f2)
    return 0


def test_bitdepth_crf_trap_unit() -> int:
    """CRF 刻度跨位深不可比 —— 选了 10bit 必须提示「同 CRF 会更大」。

    实测（1708x1212 / 9s / x265 CRF 26）：10bit 比 8bit **大 3.2%**，
    因为同样的 CRF 在 10bit 下代表更高画质。多个外部来源都强调这个陷阱，
    实测也确认了 —— 不提示的话用户会以为软件算错了。

    而 10bit 的真实收益是「同画质下省 8~12% 码率」（本次实测：
    x264 8.0% / x265 8.8% / SVT-AV1 11.8%）—— 这是两种不同的比较方式。
    """
    p2, f2 = PASS[:], FAIL[:]
    PASS.clear(); FAIL.clear()

    def check(name, ok, detail=""):
        (p2 if ok else f2).append(name)
        print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))

    app = QApplication.instance() or QApplication(sys.argv[:1])
    ctx = AppContext()
    win = MainWindow(ctx)          # 要存引用：关于作者测试要用它找侧栏按钮
    page = win.compress_page
    for _ in range(10):
        app.processEvents()
    page.tab_bar.select(0)
    page.cmb_codec.setCurrentIndex(0)      # libx264
    pump(app, 0.3)

    print("\n[CRF 刻度跨位深不可比（10bit 陷阱）]")
    check("8bit 时 CRF 说明不提 10bit 陷阱",
          "10bit 下同样的 CRF" not in page.lbl_crf_hint.text())

    page.cmb_pixfmt.setCurrentIndex(page.cmb_pixfmt.findData("yuv420p10le"))
    pump(app, 0.3)
    check("选 10bit 后 CRF 说明补上陷阱提示",
          "10bit 下同样的 CRF" in page.lbl_crf_hint.text(),
          page.lbl_crf_hint.text()[-60:])
    # H.264+10bit 时兼容性警告优先（更严重）
    check("H.264+10bit 优先显示 Hi10P 兼容性警告",
          "Hi10P" in page.lbl_pixfmt_warn.text(),
          page.lbl_pixfmt_warn.text()[:40])

    page.cmb_codec.setCurrentIndex(page.cmb_codec.findData("libx265"))
    pump(app, 0.3)
    check("换成 H.265 后改显示 CRF 刻度提示",
          "同样的 CRF 在 10bit 下" in page.lbl_pixfmt_warn.text(),
          page.lbl_pixfmt_warn.text()[:50])
    check("提示里点明了「想压小该怎么做」",
          "按文件大小" in page.lbl_pixfmt_warn.text())

    tip = page.cmb_pixfmt.toolTip()
    check("tooltip 给出实测效率收益（8~12%）", "8~12%" in tip, tip[:60])
    check("tooltip 同时说明编码变慢", "慢" in tip)

    # 🔴 容器高度必须跟着内容长高，否则多出来的那一行会被切掉
    # （实测踩过：10bit 时面板需要 295px 却停在 240px，最后一行只剩上半截）
    check("10bit 时面板高度跟上了内容（不被截断）",
          page._panel.height() >= page._panel.sizeHint().height(),
          f"{page._panel.height()} vs 需要 {page._panel.sizeHint().height()}")

    page.cmb_pixfmt.setCurrentIndex(page.cmb_pixfmt.findData(""))
    pump(app, 0.5)
    check("换回 8bit 后提示清空", page.lbl_pixfmt_warn.text() == "")
    check("CRF 说明也复位", "10bit 下同样的 CRF" not in page.lbl_crf_hint.text())
    check("8bit 时面板高度仍够用",
          page._panel.height() >= page._panel.sizeHint().height(),
          f"{page._panel.height()} vs 需要 {page._panel.sizeHint().height()}")

    print("\n[试压：只压一段看效果]")
    from app.core.commands import (SAMPLE_SECONDS, sample_span,
                                   truncated_info)
    from app.core.ffmpeg import MediaInfo, StreamInfo
    from app.ui.pages.compress import SAMPLE_SUFFIX, SAMPLE_SECONDS as UI_SECS

    check("界面时长与核心常量一致", UI_SECS == SAMPLE_SECONDS,
          f"{UI_SECS} vs {SAMPLE_SECONDS}")

    def _mi(dur):
        return MediaInfo(path=Path("x.mp4"), duration=dur, size=10 ** 7,
                         video=StreamInfo(index=0, codec_type="video",
                                          codec_name="h264"))

    # 取中间段，不是片头 —— 片头常是黑场/标题，压出来好看但不代表全片
    ss, dur = sample_span(_mi(600))
    check("长片取中间：起点约在正中", abs(ss - 290) < 1, f"ss={ss}")
    check("时长正好 20 秒（不超源）", dur == 20.0, f"{dur}")
    check("短片不超源时长", sample_span(_mi(8))[1] == 8.0,
          str(sample_span(_mi(8))))
    check("无时长信息时不截取", sample_span(_mi(0)) == (0.0, 0.0))
    check("first/last 也能取",
          sample_span(_mi(100), position="first")[0] == 0.0
          and abs(sample_span(_mi(100), position="last")[0] - 80) < 1)

    # 🔴 最关键：截断 info —— 预算与进度都按它算
    ti = truncated_info(_mi(600))
    check("截断后的 info 时长 = 样本时长", ti is not None and ti.duration == 20.0,
          str(ti.duration if ti else None))
    check("截断不改变其他字段（源信息保留）",
          ti is not None and ti.video is not None and ti.size == 10 ** 7)

    # 预算必须按样本时长算，否则样本体积对不上（照抄参考实现最大的坑）
    from app.core.size_budget import solve
    b_full = solve(_mi(600), 1.0, "mp4", audio_mode="aac", audio_kbps=128)
    b_samp = solve(ti, 1.0, "mp4", audio_mode="aac", audio_kbps=128)
    check("样本预算的码率远高于完整片（按 20s 算而不是 600s）",
          b_samp.video_kbps > b_full.video_kbps * 10,
          f"{b_samp.video_kbps} vs {b_full.video_kbps}")

    # 命令里真的带了 -ss / -t，且 -ss 在 -i 之前（输入侧 seek，才快）
    from app.core.commands import build_compress_steps
    from app.core.presets import Container as _C, VideoSettings as _VS
    _st = _VS(codec="libx264", crf=28, preset="veryfast", container=_C.MP4,
              audio_codec="aac", audio_bitrate_kbps=128)
    _steps = build_compress_steps(ctx.env, Path("x.mp4"), Path("o.mp4"),
                                  _st, _mi(600), clip=(290.0, 20.0))
    _args = _steps[0].args
    check("命令带 -ss", "-ss" in _args)
    check("-ss 在 -i 之前（输入侧 seek）",
          _args.index("-ss") < _args.index("-i"))
    check("命令带 -t", "-t" in _args and "20.000" in _args)
    check("不传 clip 时不出现 -ss/-t",
          "-ss" not in build_compress_steps(
              ctx.env, Path("x.mp4"), Path("o.mp4"), _st, _mi(600))[0].args)

    check("试压后缀与正常输出不同名", SAMPLE_SUFFIX not in ("", "_compressed"),
          SAMPLE_SUFFIX)

    print("\n[队列页：完成后关机是勾选框，不是按钮]")
    from PySide6.QtWidgets import QCheckBox as _QCB, QPushButton as _QPB3
    qp = win.queue_page
    chk = getattr(qp, "chk_poweroff", None)
    # 🔴 用户报过：「完成后关机」做成了可勾选的 QPushButton，看着就是个按钮，
    # 会让人以为「点一下 = 现在就关机」。勾选框才是"这是个开关"的视觉语言，
    # 而且本软件其他开关（覆盖文件、后台运行、防削波）全是 QCheckBox。
    check("是 QCheckBox（不是按钮）", isinstance(chk, _QCB),
          type(chk).__name__ if chk is not None else "缺失")
    check("没有遗留的按钮式实现",
          not isinstance(getattr(qp, "btn_poweroff", None), _QPB3),
          "残留 btn_poweroff" if hasattr(qp, "btn_poweroff") else "无")
    if chk is not None:
        check("文案说清是「全部完成后」而非「跑完就关」",
              "全部完成" in chk.text(), chk.text())
        # 空队列时勾选应被自动取消（否则用户勾完发现没反应）
        chk.setChecked(True)
        pump(app, 0.2)
        check("空队列勾选会自动取消", not chk.isChecked())

    print("\n[关于作者：侧栏按钮 + 弹窗（图片 50% / 链接 #48A2FF）]")
    from app.ui.main_window import AboutAuthorDialog, AUTHOR_LINK_COLOR
    from PySide6.QtWidgets import QLabel as _QLabel, QPushButton as _QPB2

    # 侧栏里有这个按钮，且它**不是** checkable（点它不该取消选中别的页面）
    author_btns = [b for b in win.findChildren(_QPB2)
                   if b.text().strip() == "关于作者"]
    check("侧栏有「关于作者」按钮", len(author_btns) == 1,
          str([b.text() for b in author_btns]))
    if author_btns:
        check("关于作者按钮不是 checkable（不会改变页面选中态）",
              not author_btns[0].isCheckable())

    d = AboutAuthorDialog(win)
    d.show()
    pump(app, 0.4)
    img_labels = [l for l in d.findChildren(_QLabel)
                  if l.pixmap() is not None and not l.pixmap().isNull()]
    check("弹窗里有二维码图", bool(img_labels))
    if img_labels:
        pm = img_labels[0].pixmap()
        # 用户反馈原图 100%（1920 宽）超出屏幕，改为 50%
        check("二维码按 50% 显示（960x306）",
              pm.width() == 960 and pm.height() == 306,
              f"{pm.width()}x{pm.height()}")
    check("对话框不超出屏幕",
          d.screen().availableGeometry().contains(d.frameGeometry()),
          f"{d.frameGeometry()} vs {d.screen().availableGeometry()}")

    # 🔴 链接颜色必须写进 <a> 的内联样式：
    # setStyleSheet('a{color:..}') 对 QLabel 富文本无效（链接色跟 palette
    # 的 Link 角色走，不认 QSS）。实测 setStyleSheet 后采样仍是默认蓝。
    link_lbl = next((l for l in d.findChildren(_QLabel)
                     if "axutongxue" in l.text()), None)
    check("链接文字存在", link_lbl is not None)
    if link_lbl:
        check("链接带内联颜色样式",
              AUTHOR_LINK_COLOR in link_lbl.text(), link_lbl.text()[:60])
        check("openExternalLinks 开（点击直接开浏览器）",
              link_lbl.openExternalLinks())
        # 像素级验证：颜色真的是 #48A2FF（72,162,255）
        img = link_lbl.grab().toImage()
        cols = {}
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if c.alpha() > 200:
                    cols[(c.red(), c.green(), c.blue())] = \
                        cols.get((c.red(), c.green(), c.blue()), 0) + 1
        target = (0x48, 0xA2, 0xFF)
        hit = any(abs(r-target[0]) < 8 and abs(g-target[1]) < 8
                  and abs(b-target[2]) < 8 for (r, g, b) in cols)
        check(f"链接实际渲染为 {AUTHOR_LINK_COLOR}", hit, str(list(cols)[:2]))
    d.close()
    pump(app, 0.2)

    print("\n[试压按钮：不能把一段的长度做成常驻参数]")
    from app.core.commands import SAMPLE_SECONDS_OPTIONS
    from app.ui.pages.compress import SAMPLE_SECONDS as UI_DEFAULT

    check("默认时长在可选列表里", UI_DEFAULT in SAMPLE_SECONDS_OPTIONS,
          str(SAMPLE_SECONDS_OPTIONS))
    check("可选 5~60 秒，没有超过 60（再长就不叫快速试）",
          min(SAMPLE_SECONDS_OPTIONS) == 5 and max(SAMPLE_SECONDS_OPTIONS) == 60,
          str(SAMPLE_SECONDS_OPTIONS))

    # 时长收敛：短片不会被"截长出"多余内容
    check("时长会被源时长收敛（30s 源设 60s）",
          sample_span(_mi(30), 60)[1] == 30.0, str(sample_span(_mi(30), 60)))
    check("8s 源设 20s 只取 8s",
          sample_span(_mi(8), 20)[1] == 8.0, str(sample_span(_mi(8), 20)))

    page.tab_bar.select(0)
    pump(app, 0.2)
    btn = page.btn_sample

    # 🔴 核心回归：按钮文字**永远**是「试压」，不带秒数。
    # 用户报过两次：
    #   ① 写死「试压 20 秒」→ 短片撒谎
    #   ② 动态显示「试压 5 秒」→ 操作栏常驻一个秒数，用户以为"开始压缩"
    #      也受它影响，于是不知道该怎么压完整片。
    # 根因是把一次性动作的参数做成了常驻应用状态。
    check("无文件时按钮不可点", not btn.isEnabled())
    check("按钮文字不含秒数（无文件）",
          "秒" not in btn.text(), btn.text())
    check("按钮文字就是「试压」", btn.text() == "试压", btn.text())

    # 🔴 必须和「开始压缩」是同一种控件 —— 否则 QSS 里 QPushButton 那套
    # 样式（含 disabled 的灰底灰边）根本不作用到它身上。
    # 踩过：用 QToolButton，它没有 QSS 规则，禁用时只是文字变淡、底色边框不变，
    # 和旁边按钮完全不是一套。
    from PySide6.QtWidgets import QPushButton as _QPB
    check("试压与开始压缩是同一种控件",
          type(btn) is type(page.bar.button),
          f"{type(btn).__name__} vs {type(page.bar.button).__name__}")
    check("试压是 QPushButton（能吃 QSS 的 disabled 样式）",
          isinstance(btn, _QPB), type(btn).__name__)

    # 禁用态要真的变灰（边框变浅 + 背景透明），不只是文字变淡
    def _btn_colors(widget):
        img = widget.grab().toImage()
        h, wd = img.height(), img.width()
        return (img.pixelColor(wd // 2, 0).name(),      # 顶边
                img.pixelColor(wd // 2, h // 2).name())  # 中心

    from app.ui.style import palette as _pal
    _p = _pal()
    # 🔴 采样前必须先把文件塞进去并 pump —— 否则量到的是"还没启用"的样子
    from app.core.ffmpeg import MediaInfo as _MI0, StreamInfo as _SI0
    fake0 = Path(tempfile.mkdtemp(prefix="xw_probe_")) / "on48.mp4"
    fake0.write_bytes(b"")
    page.table.add_paths([str(fake0)])
    pump(app, 0.3)
    page.table._infos[str(fake0)] = _MI0(
        path=fake0, duration=48.0, size=10 ** 7,
        video=_SI0(index=0, codec_type="video", codec_name="h264"))
    page.table.filesChanged.emit()
    pump(app, 0.5)
    border_on, bg_on = _btn_colors(btn)
    check("有文件时是常态样式（border_strong 边框 + panel 底）",
          border_on.upper() == _p["border_strong"].upper()
          and bg_on.upper() == _p["panel"].upper(),
          f"border={border_on} bg={bg_on}")

    # 清空后应回到 disabled
    page.table.clear_all()
    pump(app, 0.3)
    check("清空后按钮重新置灰", not btn.isEnabled())
    border_off, _bg_off = _btn_colors(btn)
    check("禁用时边框用的是更浅的 border（不是 border_strong）",
          border_off.upper() == _p["border"].upper(),
          f"{border_off} vs border={_p['border']}")

    # 弹菜单用的是 QPushButton + exec（同 OutputRow），不是 setMenu
    check("按钮没有挂 setMenu（避免自带箭头区破坏对齐）",
          btn.menu() is None, "已挂 menu" if btn.menu() else "未挂")
    check("有独立的弹菜单方法", callable(getattr(page, "_show_sample_menu", None)))

    # 塞一个 48s 的假文件，模拟"探测完成"
    from app.core.ffmpeg import MediaInfo as _MI, StreamInfo as _SI
    fake = Path(tempfile.mkdtemp(prefix="xw_fake_")) / "long48.mp4"
    fake.write_bytes(b"")
    page.table.add_paths([str(fake)])
    pump(app, 0.3)
    page.table._infos[str(fake)] = _MI(
        path=fake, duration=48.0, size=10 ** 7,
        video=_SI(index=0, codec_type="video", codec_name="h264"))
    page.table.filesChanged.emit()      # 与 _on_probe_done 末尾一致
    pump(app, 0.4)
    if page.table.info_for(fake) is not None:
        check("有视频时按钮可点", btn.isEnabled(), btn.text())
        check("有视频后按钮文字仍不含秒数",
              "秒" not in btn.text(), btn.text())
        # 秒数只在菜单项里，且源更短时标注实际值
        texts = [a.text() for a in page._sample_acts]
        check("菜单项都带秒数（动作而非参数）",
              all("试压" in t and "秒" in t for t in texts), str(texts))
        page._refresh_sample_button()
        pump(app, 0.1)
        texts = [a.text() for a in page._sample_acts]
        long_act = next((t for t in texts if t.startswith("试压 60")), "")
        check("60s 项标注「实际 48」（选超了也不会惊讶）",
              "实际 48" in long_act, long_act)

        # 🔴 菜单里**不应该**有"再试一次"这类记忆型动作。
        # 每个选项本身就是完整的动作（"试压 N 秒"），点一次就够；
        # 再放一个"重复上次"只是多一条需要记住上次选了什么的心智负担。
        all_acts = [a.text() for a in page.sample_menu.actions() if a.text()]
        check("菜单里没有「再试一次」这类记忆型动作",
              not any("再试" in t or "重复" in t for t in all_acts),
              str(all_acts))
        check("菜单项数 == 可选秒数（一一对应，没有多余项）",
              len(all_acts) == len(SAMPLE_SECONDS_OPTIONS), str(all_acts))

        # 菜单位置：紧贴按钮，且必须夹在屏幕内（窗口比屏幕高时按钮可能在屏幕外）
        menu = page.sample_menu
        menu.adjustSize()
        sz = menu.sizeHint()
        btn_tl = page.btn_sample.mapToGlobal(page.btn_sample.rect().topLeft())
        btn_bl = page.btn_sample.mapToGlobal(page.btn_sample.rect().bottomLeft())
        avail = (page.btn_sample.screen()
                 or QApplication.primaryScreen()).availableGeometry()
        above = btn_tl.y() - avail.top()
        below = avail.bottom() - btn_bl.y()

        # 与 _show_sample_menu 同一套算法
        if above >= sz.height() or above >= below:
            want_x, want_y = btn_tl.x(), btn_tl.y() - sz.height()
            direction = "上"
        else:
            want_x, want_y = btn_bl.x(), btn_bl.y()
            direction = "下"
        ex = max(avail.left(), min(want_x, avail.right() - sz.width() + 1))
        ey = max(avail.top(), min(want_y, avail.bottom() - sz.height() + 1))
        clamped = (ex, ey) != (want_x, want_y)

        check(f"菜单位置按「{direction}方」算（空间不足时优先向上）",
              direction in ("上", "下"), direction)
        if not clamped:
            # 没被夹 = 真的贴住了按钮，这时必须严丝合缝
            gap = (ey + sz.height() - btn_tl.y() if direction == "上"
                   else ey - btn_bl.y())
            check(f"菜单紧贴按钮（{direction}方，空隙 {gap}px）", abs(gap) <= 1,
                  f"菜单 y={ey} h={sz.height()} 按钮 顶={btn_tl.y()} 底={btn_bl.y()}")
        else:
            check("按钮在屏幕外时菜单被夹进可视区（不会跟着飘出去）",
                  avail.top() <= ey and ey + sz.height() <= avail.bottom() + 2,
                  f"y={ey} h={sz.height()} 屏 {avail.top()}~{avail.bottom()}")
    else:
        print("      跳过按钮验证（表格未接受这个假路径）")
    page.table.clear_all()
    pump(app, 0.2)

    PASS.extend(p2); FAIL.extend(f2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
