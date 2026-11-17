#!/usr/bin/env python3
"""预设卡片的文字排布验证：名称不能被右上角标签挤掉。

起因：用户反馈「按常用预设」页 H.265 那张卡的标题看不见。
根因不是绘制错误，是**宽度硬冲突**：卡片 168px、左右各 11px 内边距 →
内宽只有 146px，而「H.265 兼容压缩」名宽 107px + 「老设备友好」67px + 间隙 8px
= 182px，差 36px。名称于是走 elidedText 被切成「H.265 …」。

名称是主体、标签是修饰，所以规则是：**名称必须完整，标签一起挤的时候由
数据层（presets.py 的文案）让步**（已改名为「H.265 兼容」+「老设备」）。
名字仍留着省略号兜底，但正常情况不该触发 —— 这条测试就是守它别触发。

顺带守住另外三张余量很小的卡：社交平台 720p 只剩 3px、高质量收藏 / 极限小体积
各 7px，字体或 DPI 一变就会重演同样的症状。

用法：
    python tests/preset_chip_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# 🔴 必须在 import app.context 之前：构造 AppContext 会写配置
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="xiaowan_chip_cfg_")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.context import AppContext  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.pages.shared import (CHIP_PAD, TAG_GAP, _wrap,  # noqa: E402
                                 name_room, tag_width)
from app.ui.style import apply_theme  # noqa: E402

PASS, FAIL = [], []

# 与 PresetChip.paintEvent 里一致的字号
NAME_PT, TAG_PT, DETAIL_PT = 11, 8, 8


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _font(pt: int) -> QFont:
    """按绘制时的方式造字体：默认族 + 指定磅值 + 粗体。"""
    f = QFont()
    f.setPointSize(pt)
    f.setBold(True)
    return f


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    ctx = AppContext()
    apply_theme(app, ctx.config.theme)
    window = MainWindow(ctx)
    window.show()
    pump(app, 0.5)

    fm_name = QFontMetrics(_font(NAME_PT))
    fm_tag = QFontMetrics(_font(TAG_PT))

    # 两张页面都用同一套卡片构件，一起查
    chips = []
    for page_name, page in (("预设页", window.compress_page),
                            ("声道下混页", window.downmix_page)):
        for chip in getattr(page, "_chips", {}).values():
            chips.append((page_name, chip))

    print(f"\n[1] 卡片几何（内宽 = 卡片宽 - {CHIP_PAD * 2}）")
    widths = sorted({(n, c.width()) for n, c in chips})
    check("收集到了卡片", len(chips) >= 10, f"{len(chips)} 张 " + str(widths))
    # 算式里的宽度必须就是实际画上去的宽度，否则守的是个假数
    check("卡片实际宽度与算式一致",
          all(w == widths[0][1] for _, w in widths),
          " / ".join(f"{n}={w}px" for n, w in widths))
    chip_w = widths[0][1]

    print(f"\n[2] 名称必须完整（卡片 {chip_w}px，名称 11pt，标签 {TAG_PT}pt）")
    print(f"      {'卡片名称':<14}{'名宽':>5}{'标签':>10}{'签宽':>6}"
          f"{'名可用':>7}{'余量':>6}")
    for page_name, chip in chips:
        p = chip.preset
        name_w = fm_name.horizontalAdvance(p.name)
        room = name_room(chip.width(), fm_tag, p.tagline)
        slack = room - name_w
        elided = fm_name.elidedText(p.name, Qt.TextElideMode.ElideRight, max(room, 0))
        print(f"      {p.name:<14}{name_w:>5}{p.tagline:>10}"
              f"{tag_width(fm_tag, p.tagline):>6}{room:>7}{slack:>6}")
        check(f"{page_name}「{p.name}」名称完整", slack >= 0,
              f"名 {name_w}px / 可用 {room}px，余 {slack}px"
              + (f"，会被切成「{elided}」" if slack < 0 else ""))

    print("\n[3] 标签与名称不重叠")
    for page_name, chip in chips:
        p = chip.preset
        name_w = fm_name.horizontalAdvance(p.name)
        # 名称从左内边距画起，标签右对齐；两者间距必须 ≥ TAG_GAP
        gap = chip.width() - CHIP_PAD * 2 - name_w - tag_width(fm_tag, p.tagline)
        check(f"{page_name}「{p.name}」与标签留有 {TAG_GAP}px 间隙",
              gap >= TAG_GAP, f"实测 {gap}px")

    print("\n[4] 说明文字的行数（卡片只放 3 行摘要，全文在 tooltip）")
    fm_detail = QFontMetrics(_font(DETAIL_PT))
    inner = chip_w - CHIP_PAD * 2
    over = []
    for page_name, chip in chips:
        lines = _wrap(chip.preset.detail, fm_detail, inner, 99)
        mark = "" if len(lines) <= 3 else "  ← 卡片上只显示 3 行"
        print(f"      {chip.preset.name:<14} {len(lines)} 行{mark}")
        if len(lines) > 3:
            over.append(f"{page_name}/{chip.preset.name}")
    # 不算失败：卡片本来就是摘要，全文在 tooltip；列出来只为知情
    print(f"      超出 3 行的：{len(over)} 张" + (f"（{', '.join(over)}）" if over else ""))

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
