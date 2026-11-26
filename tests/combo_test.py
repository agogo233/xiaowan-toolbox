#!/usr/bin/env python3
"""下拉面板（app/ui/widgets.py:Combo）的界面层验证。

用法：
    python tests/combo_test.py

要点：面板必须是**主窗口里的子控件**，不是独立的 Qt::Popup 窗口——
那是这套下拉"看起来像弹窗"的根源。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

# 🔴 必须在 import app.context 之前：构造 AppContext 会写配置
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="xiaowan_combo_cfg_")

from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from app.context import AppContext  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
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


def sample_height(app, widget, seconds: float) -> list[int]:
    """动画期间采样高度，用来证明"是长出来的"而不是瞬间出现。"""
    seen = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        seen.append(widget.height())
        time.sleep(0.012)
    return seen


def key(combo, code) -> None:
    for kind in (QKeyEvent.Type.KeyPress, QKeyEvent.Type.KeyRelease):
        QApplication.sendEvent(combo, QKeyEvent(kind, code,
                                                Qt.KeyboardModifier.NoModifier))


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    ctx = AppContext()
    apply_theme(app, ctx.config.theme)
    window = MainWindow(ctx)
    window.resize(1120, 900)
    window.show()
    pump(app, 0.4)

    page = window.compress_page
    combo = page.cmb_speed

    print("\n[1] 类型与旧接口")
    check("是 Combo", isinstance(combo, Combo))
    check("仍是 QComboBox（页面/测试调用点不用改）", isinstance(combo, QComboBox))
    check("currentData 可用", combo.currentData() is not None, str(combo.currentData()))
    check("findData 可用", combo.findData("medium") >= 0)

    print("\n[2] 面板归属：窗口内子控件，不是独立窗口")
    combo.showPopup()
    pump(app, 0.3)
    panel = combo._panel
    check("面板已显示", panel is not None and panel.isVisible())
    check("父控件是主窗口", panel.parent() is window,
          type(panel.parent()).__name__)
    check("不是独立窗口", not panel.isWindow())
    check("面板在组合框下方水平对齐",
          abs(panel.x() - combo.mapTo(window, QPoint(0, 0)).x()) <= 6,
          f"panel.x={panel.x()} combo.x={combo.mapTo(window, QPoint(0, 0)).x()}")
    # 组合框在页面靠下位置时面板会翻到上方（正确行为）；
    # 断言改为「面板紧贴控件（下沿或上沿），不悬空、不遮挡别的区域」
    combo_top = combo.mapTo(window, QPoint(0, 0)).y()
    combo_bottom = combo.mapTo(window, QPoint(0, combo.height())).y()
    attached_below = abs(panel.y() - combo_bottom) <= 4
    attached_above = abs(panel.y() + panel.height() - combo_top) <= 4
    check("面板紧贴组合框（下沿或上沿）", attached_below or attached_above,
          f"panel.y={panel.y()} 控件={combo_top}..{combo_bottom}")
    check("面板不比组合框窄", panel.width() >= combo.width(),
          f"{panel.width()} vs {combo.width()}")

    print("\n[3] 内容放得下就不该有滚动条")
    rows = combo.count()
    full = rows * combo._view.sizeHintForRow(0) + combo.PAD * 2
    check("面板高度容得下全部内容", panel.height() >= full,
          f"{panel.height()} vs {full}")
    check("内容放得下时没有多余滚动条",
          not combo._view.verticalScrollBar().isVisible() or full > combo.MAX_H)
    check("内容放得下时不产生可滚动范围（差 1px 就会自己滚一下）",
          combo._view.verticalScrollBar().maximum() == 0,
          f"最大滚动值 {combo._view.verticalScrollBar().maximum()}")

    print("\n[4] 展开是动画（有中间态），且过程中不会自己滚动")
    combo.hidePopup()
    pump(app, 0.2)
    combo.showPopup()
    heights = sample_height(app, panel, 0.25)
    check("高度从 0 开始", heights[0] == 0, str(heights[:4]))
    check("存在中间态（不是瞬开）", len({h for h in heights}) > 2,
          f"采样 {heights[:8]}")
    check("最终回到完整高度", heights[-1] == panel.height() >= full,
          f"{heights[-1]}")
    check("高度单调不减", all(b >= a for a, b in zip(heights, heights[1:])))

    # 单独再开一次，全程盯滚动位置（展开结束时会 scrollTo，曾经因此自己滚一下）
    combo.hidePopup()
    pump(app, 0.2)
    combo.showPopup()
    bar = combo._view.verticalScrollBar()
    values = []
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        app.processEvents()
        values.append(bar.value())
        time.sleep(0.012)
    check("滚动位置始终为 0", set(values) == {0}, f"采样 {sorted(set(values))}")

    print("\n[5] 点击选项")
    before = combo.currentIndex()
    target = 1 if before != 1 else 2
    combo._on_item_clicked(combo.model().index(target, 0))
    pump(app, 0.15)
    check("写回当前值", combo.currentIndex() == target,
          f"{before} → {combo.currentIndex()}")
    check("选完自动关闭", not panel.isVisible())

    print("\n[6] 键盘")
    combo.setCurrentIndex(0)
    combo.showPopup()
    pump(app, 0.2)
    key(combo, Qt.Key.Key_Down)
    key(combo, Qt.Key.Key_Down)
    check("↓ 能移动选中", combo.currentIndex() == 2, str(combo.currentIndex()))
    key(combo, Qt.Key.Key_Up)
    check("↑ 能回退", combo.currentIndex() == 1, str(combo.currentIndex()))
    key(combo, Qt.Key.Key_Escape)
    pump(app, 0.1)
    check("Esc 关闭面板", not panel.isVisible())

    print("\n[7] 点外面 / 再点控件都能关")
    combo.showPopup()
    pump(app, 0.2)
    check("重新打开", panel.isVisible())
    combo.showPopup()          # 同一个控件再点一次 = 收起
    pump(app, 0.1)
    check("再点控件收起", not panel.isVisible())

    print("\n[8] 窗口底部时翻到上方")
    window.move(40, app.primaryScreen().availableGeometry().height() - 420)
    pump(app, 0.3)
    # 页面加过行（色深等），靠下的下拉要动态找：取最靠下的那个
    from app.ui.widgets import Combo as _Combo
    combos = [c for c in window.compress_page.findChildren(_Combo)
              if c.isVisible() and c.count() >= 2]
    low = max(combos, key=lambda c: c.mapTo(window, QPoint(0, 0)).y())
    low.showPopup()
    pump(app, 0.3)
    p2 = low._panel
    combo_top = low.mapTo(window, QPoint(0, 0)).y()
    combo_bottom = low.mapTo(window, QPoint(0, low.height())).y()
    # 要么贴着控件下方，要么在控件上方（且不遮挡控件本体）
    below = p2.y() >= combo_bottom - 2
    above = p2.y() + p2.height() <= combo_top + 2
    check("面板贴控件下沿或翻到上方", below or above,
          f"panel={p2.y()}..{p2.y() + p2.height()} 控件={combo_top}..{combo_bottom}")
    check("面板仍完全落在窗口里",
          p2.y() >= 0 and p2.y() + p2.height() <= window.height(),
          f"{p2.y()}..{p2.y() + p2.height()} / 窗口 {window.height()}")
    low.hidePopup()
    pump(app, 0.2)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
