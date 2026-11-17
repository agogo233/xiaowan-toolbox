#!/usr/bin/env python3
"""禁用态（disabled）的可辨识度验证。

起因：用户反馈「复选框不可选时和可选状态区别不明显」。

根因不是配色不够浅，而是 **QSS 一旦给控件设了 color，Qt 就不再套用
palette 的 disabled 组** —— 于是禁用和启用的文字颜色完全一样。
实测 QCheckBox / QRadioButton / QComboBox / QSpinBox 四个控件
禁用前后最深像素都是 #1D1D1F，等于没有任何禁用反馈。
（QLineEdit / QPushButton 之所以正常，是因为它们的 QSS 规则里没设 color。）

这条测试用像素量化的方式守住：
  1. 这些控件的禁用态文字必须比启用态明显更浅；
  2. 指示器（方框/圆点）禁用时不能还是白底；
  3. 复选框和单选都要覆盖（两种主题）。

用法：
    python tests/disabled_look_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# 🔴 必须在 import app.context 之前：构造 AppContext 会写配置
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="xiaowan_disabled_cfg_")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLineEdit,  # noqa: E402
                               QPushButton, QRadioButton, QSpinBox)

from app.ui.style import apply_theme, palette  # noqa: E402

PASS, FAIL = [], []

# 文字"墨迹"与背景的对比：浅色主题里文字比背景暗，深色主题里比背景亮，
# 所以不能用"最暗像素"这种单向指标（深色主题会量到边框）。
MIN_TEXT_GAP = 40      # 禁用态离背景更近，至少差这么多才算"看得出"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def ink_level(img, theme: str, from_x: int = 8) -> int:
    """文字的"墨迹"亮度：浅色主题取暗端，深色主题取亮端。

    用直方图并要求至少 MIN_PIXELS 个同值像素，过滤掉光标那种 1px 竖线干扰。
    """
    counts: dict[int, int] = {}
    for y in range(img.height()):
        for x in range(from_x, img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 150:
                counts[c.lightness()] = counts.get(c.lightness(), 0) + 1
    solid = [lv for lv, n in counts.items() if n >= 6] or list(counts)
    return min(solid) if theme == "light" else max(solid)


def indicator_fill(img) -> int:
    """指示器中心区域的平均亮度（判断方框是不是还白着）。"""
    w, h = img.width(), img.height()
    xs = range(3, min(13, w))
    ys = range(4, min(14, h))
    tot = n = 0
    for y in ys:
        for x in xs:
            c = img.pixelColor(x, y)
            if c.alpha() > 200:
                tot += c.lightness()
                n += 1
    return int(tot / n) if n else -1


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    widgets = [
        ("QCheckBox", lambda: QCheckBox("选项文字")),
        ("QRadioButton", lambda: QRadioButton("选项文字")),
        ("QComboBox", lambda: (lambda c: (c.addItems(["选项文字"]), c)[1])(QComboBox())),
        ("QSpinBox", lambda: (lambda s: (s.setValue(23), s)[1])(QSpinBox())),
        ("QLineEdit", lambda: (lambda e: (e.setText("选项文字"), e)[1])(QLineEdit())),
        ("QPushButton", lambda: QPushButton("按钮文字")),
    ]

    for theme in ("light", "dark"):
        print(f"\n[{theme}] 文字：禁用必须比启用更接近背景")
        apply_theme(app, theme)
        pal = palette(theme)
        print(f"      调色板 text={pal['text']}  text_faint={pal['text_faint']}")
        for name, make in widgets:
            levels = {}
            for enabled in (True, False):
                w = make()
                w.setEnabled(enabled)
                w.resize(200, 26)
                w.show()
                pump(app, 0.06)
                levels[enabled] = ink_level(w.grab().toImage(), theme)
                w.close()
                pump(app, 0.03)
            # 浅色主题：禁用更亮（更接近白底）；深色主题：禁用更暗（更接近黑底）
            gap = (levels[False] - levels[True]) if theme == "light" \
                else (levels[True] - levels[False])
            check(f"{theme} {name}: 禁用更淡", gap >= MIN_TEXT_GAP,
                  f"启用 {levels[True]} / 禁用 {levels[False]} (差 {gap})")

        print(f"\n[{theme}] 指示器：禁用不能还是白底")
        for cls, label in ((QCheckBox, "复选框"), (QRadioButton, "单选")):
            fills = {}
            for enabled in (True, False):
                w = cls("x")
                w.setEnabled(enabled)
                w.setChecked(False)
                w.resize(120, 20)
                w.show()
                pump(app, 0.06)
                fills[enabled] = indicator_fill(w.grab().toImage())
                w.close()
                pump(app, 0.03)
            # 浅色主题下用"变暗"，深色主题下用"变亮"
            if theme == "light":
                ok = fills[False] < fills[True] - 4
            else:
                ok = fills[False] > fills[True] + 4
            check(f"{theme} {label}: 禁用框内底色有变化", ok,
                  f"启用 {fills[True]} / 禁用 {fills[False]}")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())