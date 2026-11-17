#!/usr/bin/env python3
"""编码器与「速度档 / 场景优化」的可用性验证。

起因：选了「H.264 硬件加速(NVIDIA NVENC)」后，编码速度下拉点不动。
原因不是控件坏了，而是界面把它 setEnabled(False) 了 —— 可那个档位对 NVENC
是真的生效的（medium → -preset p4）。这条测试守两件事：

  1. 该可用的编码器不能被禁用，只有真正不生效的 QSV 才禁；
  2. 选中的档位确实会被翻译进 ffmpeg 命令。

用法：
    python tests/encoder_speed_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# 🔴 必须在 import app.context 之前：构造 AppContext 会写配置
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="xiaowan_enc_cfg_")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.context import AppContext  # noqa: E402
from app.core.commands import _speed_args, build_compress_steps  # noqa: E402
from app.core.ffmpeg import MediaInfo, StreamInfo  # noqa: E402
from app.core.presets import is_hw_encoder, resolve_codec  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.pages.compress import _speed_available  # noqa: E402
from app.ui.style import apply_theme  # noqa: E402
from app.ui.widgets import Combo  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


class _FakeEnv:
    """假环境，只为验证编码器门控：不必真换显卡就能造出「用不了」的机器。

    compiled = ffmpeg 里编进去了哪些编码器（`-encoders` 能看到）；
    usable   = 这台机器上真能跑通的（试编结果）。两者是两回事 ——
    h264_qsv 常年出现在列表里，没 Intel 显卡时一跑就报错。
    """

    ready = True
    # 🔴 必须有：resolve_hw_encoder 开头就查 env.ffmpeg，缺了它一律返回 None，
    # 于是"全都不可用"—— 第一版假 env 就是这样，看着通过其实是碰巧。
    ffmpeg = "ffmpeg"

    def __init__(self, compiled, usable,
                 reason: str = "驱动过旧（需要 610 以上）"):
        self._compiled = set(compiled)
        self._usable = set(usable)
        self._reason = reason

    def encoder_available(self, name: str) -> bool:
        return name in self._compiled

    def encoder_usable(self, name: str, extra=None) -> bool:
        return name in self._usable

    def unusable_reason(self, name: str) -> str:
        return self._reason if name in self._compiled else ""

    def ten_bit_reason(self, name: str) -> str:
        return ""

    def encoder_usable_10bit(self, name: str) -> bool:
        return True


def main() -> int:
    print("\n[1] 速度档该不该可用（纯逻辑，不依赖本机硬件）")
    check("libx264 可用", _speed_available("libx264"))
    check("libx265 可用", _speed_available("libx265"))
    check("NVENC 可用（这正是那个 bug）", _speed_available("h264_nvenc"))
    check("HEVC NVENC 可用", _speed_available("hevc_nvenc"))
    check("AMD AMF 可用", _speed_available("h264_amf"))
    check("VideoToolbox 可用", _speed_available("h264_videotoolbox"))
    check("QSV 不可用（ffmpeg 忽略 -preset）", not _speed_available("h264_qsv"))

    print("\n[2] 档位真的会进 ffmpeg 命令")
    check("NVENC medium → p4", _speed_args("h264_nvenc", "medium") == ["-preset", "p4"],
          str(_speed_args("h264_nvenc", "medium")))
    check("NVENC veryslow → p7",
          _speed_args("hevc_nvenc", "veryslow") == ["-preset", "p7"],
          str(_speed_args("hevc_nvenc", "veryslow")))
    check("QSV 不产出 -preset", _speed_args("h264_qsv", "medium") == [],
          str(_speed_args("h264_qsv", "medium")))
    check("AMF 产出 -quality",
          _speed_args("h264_amf", "slow") == ["-quality", "quality"],
          str(_speed_args("h264_amf", "slow")))

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    ctx = AppContext()
    apply_theme(app, ctx.config.theme)
    window = MainWindow(ctx)
    window.show()
    pump(app, 0.5)
    page = window.compress_page

    print("\n[3] 界面：每个编码器下两个下拉的可用状态")
    for i in range(page.cmb_codec.count()):
        page.cmb_codec.setCurrentIndex(i)
        pump(app, 0.15)
        data = page.cmb_codec.itemData(i)
        real = resolve_codec(data, ctx.env)
        soft = data in ("libx264", "libx265")
        if data == "copy":
            continue
        check(f"{real} 速度档可用={_speed_available(real)}",
              page.cmb_speed.isEnabled() == _speed_available(real),
              f"enabled={page.cmb_speed.isEnabled()}")
        check(f"{real} 场景优化={'可用' if soft else '禁用'}（-tune 只认 x26x）",
              page.cmb_tune.isEnabled() == soft,
              f"enabled={page.cmb_tune.isEnabled()}")
        if is_hw_encoder(real):
            check(f"{real} 禁用时给出了原因", bool(page.cmb_speed.toolTip()),
                  page.cmb_speed.toolTip()[:40])

    hw = resolve_codec("auto_hw", ctx.env)
    print(f"\n[4] 端到端：{hw} 下换档位，命令里的 -preset 要跟着变")
    idx = page.cmb_codec.findData("auto_hw")
    if idx >= 0 and hw.endswith("_nvenc"):
        page.cmb_codec.setCurrentIndex(idx)
        pump(app, 0.2)
        info = MediaInfo(path=Path("D:/x.mp4"), duration=60, size=10 ** 8,
                         video=StreamInfo(index=0, codec_type="video",
                                          codec_name="h264", width=1920,
                                          height=1080, fps=30))
        got = {}
        for name in ("veryfast", "medium", "veryslow"):
            page.cmb_speed.setCurrentIndex(page.cmb_speed.findData(name))
            pump(app, 0.1)
            steps = build_compress_steps(ctx.env, Path("D:/x.mp4"),
                                         Path("D:/out.mp4"),
                                         page.current_settings(), info)
            text = " ".join(" ".join(s.args) for s in steps)
            got[name] = next((tok for tok in text.split() if tok.startswith("p")
                              and len(tok) == 2 and tok[1].isdigit()), None)
        check("三个档位映射到不同 p 值", len({v for v in got.values() if v}) == 3,
              str(got))
        check("medium → p4", got.get("medium") == "p4", str(got))
    else:
        print(f"      本机没有 NVENC（{hw}），跳过命令级验证")

    print("\n[5] 「按文件大小」页签的档位必须与预设页一致")
    from app.core.presets import X264_PRESETS

    page.tab_bar.select(1)
    pump(app, 0.3)
    size_items = [page.size_speed.itemData(i)
                  for i in range(page.size_speed.count())]
    page.tab_bar.select(0)
    pump(app, 0.3)
    preset_items = [page.cmb_speed.itemData(i)
                    for i in range(page.cmb_speed.count())]
    print(f"      预设页 {len(preset_items)} 档 / 目标体积页 {len(size_items)} 档")
    check("两页档位完全一致", size_items == preset_items,
          f"{size_items} vs {preset_items}")
    check("两页都是 x264 的完整 9 档", size_items == X264_PRESETS,
          str(size_items))
    check("都在其中", "veryslow" in size_items and "slower" in size_items)

    page.tab_bar.select(1)
    pump(app, 0.3)
    check("目标体积页默认仍是 medium",
          page.size_speed.currentData() == "medium",
          str(page.size_speed.currentData()))
    check("目标体积页说明了「两遍编码耗时约两倍」",
          "两遍" in page.size_speed.toolTip(), page.size_speed.toolTip()[:34])

    print("\n[6] 慢档在目标体积模式下确实传到了命令里")
    from app.core.presets import Container, VideoSettings
    from app.core.size_service import _video_args
    settings = VideoSettings(codec="libx264", preset="veryslow",
                             container=Container.MP4)
    args = _video_args("libx264", settings, 500)
    check("libx264 带 -preset veryslow",
          "-preset" in args and "veryslow" in args, str(args[:6]))
    vp9 = _video_args("libvpx-vp9", settings, 500)
    check("VP9 把 veryslow 映射为 cpu-used 0",
          vp9[:4] == ["-c:v", "libvpx-vp9", "-cpu-used", "0"], str(vp9[:5]))
    check("NVENC 把 veryslow 映射为 p7",
          _speed_args("h264_nvenc", "veryslow") == ["-preset", "p7"],
          str(_speed_args("h264_nvenc", "veryslow")))

    print("\n[7] 色深（10bit）按编码器门控")
    from app.core.presets import ten_bit_note
    from app.ui.pages.compress import _pixfmt_state, _pixfmt_tip

    # 提示必须按编码器分开说：Hi10P 没有硬解，H.265/AV1 的 10bit 是主流格式，
    # 两者混成一句"兼容性稍差"会让用户以为风险相同（实测过：h264_amf 编 10bit
    # 直接失败，H.265 却毫无问题）
    check("H.264 的提示点明 Hi10P 无硬解",
          "Hi10P" in ten_bit_note("libx264")
          and "硬解" in ten_bit_note("h264_nvenc"), ten_bit_note("libx264")[:36])
    check("H.265/AV1 的提示说的是能硬解",
          "硬解" in ten_bit_note("libx265") and "HEVC 视频扩展" in ten_bit_note("libsvtav1"))
    check("VP9 单独一条（Profile 2）",
          "Profile 2" in ten_bit_note("libvpx-vp9"), ten_bit_note("libvpx-vp9")[:36])

    check("libx265 判定支持 10bit", _pixfmt_state(ctx.env, "libx265")[0])
    check("libsvtav1 判定支持 10bit", _pixfmt_state(ctx.env, "libsvtav1")[0])
    check("没有 ffmpeg 时不禁用（交给编码时报错）",
          _pixfmt_state(None, "libx264")[0])

    if ctx.env.encoder_usable("h264_amf"):
        ok, why = _pixfmt_state(ctx.env, "h264_amf")
        check("h264_amf 的 10bit 判为不支持（实测 Conversion failed）", not ok, why)
        # 起因必须来自编码器自己那句「10-bit ... is not supported」，
        # 不能是末尾的 "Nothing was written into output file" 这类善后噪音
        check("不支持时给的是人话（起因而非善后噪音）",
              "10-bit" in why and "not supported" in why, why)
        check("8bit 那次探测的原因没被 10bit 覆盖",
              ctx.env.unusable_reason("h264_amf") == ""
              and ctx.env.ten_bit_reason("h264_amf") != "",
              f"8bit='{ctx.env.unusable_reason('h264_amf')}' "
              f"10bit='{ctx.env.ten_bit_reason('h264_amf')}'")

        page.tab_bar.select(0)
        pump(app, 0.2)
        ten = page.cmb_pixfmt.findData("yuv420p10le")
        page.cmb_pixfmt.setCurrentIndex(ten)          # 先选 10bit
        pump(app, 0.1)
        page.cmb_codec.setCurrentIndex(page.cmb_codec.findData("auto_hw"))
        pump(app, 0.3)
        item = page.cmb_pixfmt.model().item(ten)
        # 🔴 auto_hw 解析成哪个硬件编码器是**动态**的：驱动升级前后答案不同
        # （驱动 581.29 时 NVENC 不可用 → h264_amf，不支持 10bit；
        #   升到 621.14 后 NVENC 可用 → h264_nvenc，支持 10bit）。
        # 所以断言不能写死"10bit 被禁用"，要按解析结果的条件断言：
        resolved = resolve_codec("auto_hw", ctx.env)
        supports_10bit = ctx.env.encoder_usable_10bit(resolved)
        if not supports_10bit:
            check(f"切到不支持的编码器后「10bit」被禁用（{resolved}）",
                  item is not None and not item.isEnabled())
            check("并且回退到 8bit", page.cmb_pixfmt.currentData() == "",
                  repr(page.cmb_pixfmt.currentData()))
            check("下拉的 tooltip 说明了原因",
                  "不支持" in page.cmb_pixfmt.toolTip(),
                  page.cmb_pixfmt.toolTip()[:40])
            check("禁用的那一项自己也有 tooltip",
                  bool(item.toolTip()) if item is not None else False)
        else:
            check(f"{resolved} 支持 10bit，保持可用",
                  item is not None and item.isEnabled())
            check("tooltip 没有误导性的禁用说明",
                  "不支持" not in page.cmb_pixfmt.toolTip(),
                  page.cmb_pixfmt.toolTip()[:40])
    else:
        print("      本机没有可用的 AMF，跳过 UI 门控验证")

    print("\n[8] 编码器下拉：本机用不了的项必须灰掉")
    from app.core.presets import codec_available, codec_labels
    from app.ui.pages.compress import _gate_codec_items

    # 真实一项：下拉里每个条目的灰/亮状态都要与 codec_available 一致。
    # 本机全都可用时这条只能证明"没乱灰"，所以下面再用假 env 造不可用场景。
    for combo_name, combo in (("预设页", page.cmb_codec), ("目标体积页", page.size_codec)):
        model = combo.model()
        mismatch = []
        hw_items = 0
        for i in range(combo.count()):
            value = combo.itemData(i)
            want = codec_available(value, ctx.env)[0]
            item = model.item(i)
            if item is None or item.isEnabled() != want:
                mismatch.append(f"{value}: 灰={item is not None and not item.isEnabled()} 应为可用={want}")
            if value.startswith("auto_hw"):
                hw_items += 1
        check(f"{combo_name}所有条目的灰显与实际可用性一致", not mismatch,
              "; ".join(mismatch))
        check(f"{combo_name}列出了三个硬件家族", hw_items == 3, f"{hw_items} 项")
        check(f"{combo_name}没有默认停在灰掉的项上",
              model.item(combo.currentIndex()) is None
              or model.item(combo.currentIndex()).isEnabled(),
              f"当前 = {combo.currentData()}")

        # 不可用的硬件项不能是"选得动但静默回退软编"
        for i in range(combo.count()):
            value = combo.itemData(i)
            if value.startswith("auto_hw"):
                ok, why = codec_available(value, ctx.env)
                item = model.item(i)
                check(f"{combo_name}「{value}」灰掉时有原因",
                      item is not None and (item.isEnabled() or bool(item.toolTip())),
                      item.toolTip() if item is not None else "")
                if not ok:
                    check(f"{combo_name}「{value}」不可用时不是静默回退",
                          "用不了" in item.toolTip(), item.toolTip())

    # 假 env：不必真换硬件就能验证门控本身
    soft = {"libx264", "libx265", "libsvtav1"}
    hw = {"h264_nvenc", "hevc_nvenc", "av1_nvenc",
          "h264_qsv", "hevc_qsv", "h264_amf", "hevc_amf"}
    no_driver = _FakeEnv(soft | hw, soft)              # 编了但没设备/驱动过旧
    not_built = _FakeEnv(soft, soft)                   # 连编都没编
    healthy = _FakeEnv(soft | hw, soft | {"h264_nvenc", "hevc_nvenc", "av1_nvenc"})

    # 🔴 造出来的下拉必须留着引用：控件一被 GC，它 model 里的 item 就成了野指针，
    # 再去 item.toolTip() 直接**堆损坏**崩掉（0xC0000374，没有 traceback，
    # 连已经 print 出来的断言都会一起丢）。
    combos = []

    def gate_labels(env):
        c = Combo()
        combos.append(c)
        for value, text in codec_labels(env):
            c.addItem(text, value)
        _gate_codec_items(c, env)
        m = c.model()
        return {c.itemData(i): m.item(i) for i in range(c.count())}

    print("      —— 驱动过旧（编码器编进去了、真跑失败）")
    g = gate_labels(no_driver)
    for value in ("auto_hw", "auto_hw_hevc", "auto_hw_av1"):
        check(f"{value} 被灰掉", not g[value].isEnabled())
        check(f"{value} 的 tooltip 带上探测到的原因",
              "驱动过旧" in g[value].toolTip(), g[value].toolTip())
    for value in ("libx264", "libx265", "libsvtav1", "copy"):
        check(f"{value} 不受影响", g[value].isEnabled())

    print("      —— 该家族的硬件编码器没编进 ffmpeg")
    g2 = gate_labels(not_built)
    for value in ("auto_hw", "auto_hw_hevc", "auto_hw_av1"):
        check(f"{value} 也被灰掉", not g2[value].isEnabled())
        check(f"{value} 给的是「本机没有可用的硬件编码器」",
              "没有可用的硬件编码器" in g2[value].toolTip(),
              g2[value].toolTip())

    print("      —— 硬件正常时不许误灰")
    g3 = gate_labels(healthy)
    for value in ("auto_hw", "auto_hw_hevc", "auto_hw_av1"):
        check(f"{value} 保持可用", g3[value].isEnabled())
        check(f"{value} 没有残留说明", g3[value].toolTip() == "",
              g3[value].toolTip())

    print("      —— 标签文案")
    for env_name, env_obj in (("无硬件", no_driver), ("没编进来", not_built)):
        for value, text in codec_labels(env_obj):
            check(f"{env_name}：{value} 的文案没有「（，」这种拼接残渣",
                  "（，" not in text and "（）" not in text, text)
    check("原因拼在括号里，不是多余的标点",
          codec_labels(no_driver)[3][1].endswith("））"),
          codec_labels(no_driver)[3][1])
    check("没有原因时给的是「本机不可用，将回退软编」",
          "本机不可用" in codec_labels(not_built)[3][1],
          codec_labels(not_built)[3][1])

    # resolve_codec 对用不了的硬件项会返回 libx264 —— 判可用性绝不能拿它当依据
    check("resolve_codec 会静默回退（这正是要灰掉的理由）",
          resolve_codec("auto_hw", no_driver) == "libx264",
          resolve_codec("auto_hw", no_driver))

    # 被灰的项若被代码选中（预设卡片就是这条路），必须切回软编而不是装作在跑硬件
    probe = Combo()
    for value, text in codec_labels(no_driver):
        probe.addItem(text, value)
    _gate_codec_items(probe, no_driver)
    probe.setCurrentIndex(probe.findData("auto_hw"))
    page._codec_usable_or_base(probe)
    check("代码选中被灰的硬件项 → 切回 libx264",
          probe.currentData() == "libx264", str(probe.currentData()))
    check("并且给出了原因", "用不了" in probe.toolTip(), probe.toolTip())
    page._codec_usable_or_base(probe)
    check("切回之后不再重复提示（tooltip 清空）", probe.toolTip() == "",
          probe.toolTip())

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
