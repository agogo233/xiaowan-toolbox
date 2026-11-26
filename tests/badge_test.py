#!/usr/bin/env python3
"""侧栏品牌徽标的渲染测试。

钉的是一个**mac 上直接肉眼可见**的 bug：高 DPI 屏上徽标只显示左上角 1/4。

根因在 Qt 的语义细节：
    QLabel::pixmap() 是**按值返回副本**。
    原代码是 `setPixmap(scaled)` 之后 `pm = badge.pixmap(); pm.setDevicePixelRatio(r)`
    —— 改的是副本，label 里那份仍是 dpr=1.0。
    于是 label 存 68×68 但 dpr=1.0 ⇒ 逻辑尺寸 68px，
    被 setFixedSize(34,34) 裁成左上角 —— 图标只露出一角。

正确写法：**先 setDevicePixelRatio，再 setPixmap**。

判据是「逻辑尺寸 == 徽标框尺寸」：
    pixmap.width() / pixmap.devicePixelRatio() ≈ 34
这个判据和 DPR 无关，1x / 2x 都成立 —— 所以当时在 Windows（1x）上
测不出来，只有高 DPI 的 mac 才暴露。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from app.context import AppContext  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.style import apply_theme  # noqa: E402

PASS, FAIL = [], []

#: 徽标固定尺寸，和 main_window 里的 setFixedSize 一致
BADGE_SIZE = 34


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app, "light")
    win = MainWindow(AppContext())
    win.show()
    for _ in range(10):
        app.processEvents()

    badge = win.findChild(QLabel, "BrandBadge")
    check("找到侧栏徽标控件", badge is not None)
    if badge is None:
        return 1

    check(f"徽标框是 {BADGE_SIZE}×{BADGE_SIZE}",
          badge.width() == BADGE_SIZE and badge.height() == BADGE_SIZE,
          f"{badge.width()}×{badge.height()}")

    pm = badge.pixmap()
    check("徽标里有图（不是空 QPixmap、也不是退回文字）",
          pm is not None and not pm.isNull(),
          f"{pm.width()}×{pm.height()}" if pm is not None else "None")

    if pm is not None and not pm.isNull():
        dpr = pm.devicePixelRatio()
        # 🔴 核心断言：物理像素 ÷ dpr = 逻辑尺寸，必须等于框尺寸。
        # 旧写法在 2x 下 pixmap 是 68×68、dpr=1.0 → 逻辑 68px → 被裁。
        logical_w = pm.width() / dpr
        logical_h = pm.height() / dpr
        check("逻辑宽度 == 徽标框宽度（否则会被裁切）",
              abs(logical_w - BADGE_SIZE) <= 1,
              f"{pm.width()}px / dpr {dpr} = {logical_w:.0f} (期望 {BADGE_SIZE})")
        check("逻辑高度 == 徽标框高度",
              abs(logical_h - BADGE_SIZE) <= 1,
              f"{pm.height()}px / dpr {dpr} = {logical_h:.0f}")
        # 高清屏上物理像素应该是 2 倍（图清楚，不被拉虚）
        check("物理像素 ≥ 逻辑尺寸（高清屏不糊）",
              pm.width() >= BADGE_SIZE, f"{pm.width()}px")
        # dpr 必须实际生效（不是 1.0 蒙对）
        if badge.devicePixelRatioF() > 1.5:
            check("2x 屏上 dpr 被正确写进 pixmap",
                  dpr > 1.5, f"dpr={dpr}")

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print(f"  失败：{n}")
    win.close()
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())