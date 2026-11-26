#!/usr/bin/env python3
"""生成应用图标（app/resources/icon.png）。

源图是 packaging/icon_source.png（设计稿，256×256，四角透明）。
这里只做等比放大到 1024 并写出，保证打包脚本（macOS iconset / Windows .ico）
拿到足够大的图。

    python packaging/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SIZE = 1024
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "packaging" / "icon_source.png"
DST = ROOT / "app" / "resources" / "icon.png"


def main() -> int:
    if not SRC.is_file():
        print(f"找不到源图：{SRC}")
        return 1

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QImage

    app = QGuiApplication(sys.argv[:1])  # noqa: F841  (Qt 需要先初始化)

    img = QImage(str(SRC))
    if img.isNull():
        print(f"源图读不出来：{SRC}")
        return 1

    # 非正方形先补成正方形，避免后续被拉伸
    if img.width() != img.height():
        side = max(img.width(), img.height())
        canvas = QImage(side, side, QImage.Format.Format_ARGB32)
        canvas.fill(Qt.GlobalColor.transparent)
        from PySide6.QtGui import QPainter

        p = QPainter(canvas)
        p.drawImage((side - img.width()) // 2, (side - img.height()) // 2, img)
        p.end()
        img = canvas

    # 放大到 1024：用平滑插值，边缘不会出现锯齿
    out = img.scaled(SIZE, SIZE, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)

    DST.parent.mkdir(parents=True, exist_ok=True)
    if not out.save(str(DST), "PNG"):
        print(f"写不进去：{DST}")
        return 1
    print(f"图标已生成：{DST}（{out.width()}×{out.height()}，源图 {img.width()}×{img.height()}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
