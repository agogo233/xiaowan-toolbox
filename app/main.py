"""程序入口。"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QIcon, QWheelEvent
from PySide6.QtWidgets import (QAbstractSlider, QAbstractSpinBox, QApplication,
                               QComboBox)

from . import __version__
from .context import AppContext
from .core.utils import resource_path
from .ui.main_window import MainWindow
from .ui.style import apply_theme


class SpinPadFilter(QObject):
    """给数值框内部文本框补内边距。

    QSS 的 padding 不会作用到 QSpinBox / QDoubleSpinBox 内部的 QLineEdit，
    文字会紧贴边框。实测：文字起点只有 2px；给 QSS 加 padding-left: 10px
    也只到 3px（几乎无效）；改设内部 lineEdit 的 textMargins 才真的有效（→13px）。

    在控件第一次 Show 时补，所以不受「主题先于窗口应用」的顺序影响。
    """

    LEFT, RIGHT = 8, 4

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.Show and isinstance(obj, QAbstractSpinBox):
            line_edit = obj.lineEdit()
            if line_edit is not None and line_edit.textMargins().left() != self.LEFT:
                line_edit.setTextMargins(self.LEFT, 0, self.RIGHT, 0)
        return False


class WheelGuard(QObject):
    """禁止鼠标滚轮修改参数控件。

    滚轮落在下拉框 / 数值框 / 滑杆上时不再改变数值，
    而是把滚动转发给所属的 QScrollArea 视口（页面照常滚动）。
    注意：不能只转发给直接父级——普通 QWidget 默认忽略滚轮且
    手动 sendEvent 不会触发 Qt 的向上传播，页面会滚不动。
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QScrollBar, QScrollArea

        if event.type() == QEvent.Type.Wheel and isinstance(
                obj, (QComboBox, QAbstractSpinBox, QAbstractSlider)):
            # 滚动条一律放行：滚轮悬在滚动条上就该滚它；
            # 且拦截它会形成 视口 -> 滚动条 -> 视口 的死循环
            # （QScrollArea 处理视口滚轮时会再次派生给内部滚动条，
            #   而滚动条的 parentWidget 是容器控件，不是滚动区）
            if isinstance(obj, QScrollBar):
                return False
            w = obj.parentWidget()
            while w is not None and not isinstance(w, QScrollArea):
                w = w.parentWidget()
            if w is not None:
                QApplication.sendEvent(w.viewport(), QWheelEvent(event))
            return True
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    app = QApplication(argv)
    app.setApplicationName("小丸工具箱")
    app.setApplicationDisplayName("小丸工具箱")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("XiaoWanToolbox")
    # Fusion 让 QSS 在各平台表现一致，避免 macOS 原生样式吞掉自定义样式
    app.setStyle("Fusion")

    guard = WheelGuard()
    app.installEventFilter(guard)
    app._wheel_guard = guard  # 持有引用，防止被垃圾回收

    spin_pad = SpinPadFilter()
    app.installEventFilter(spin_pad)
    app._spin_pad = spin_pad

    icon_path = resource_path("icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    ctx = AppContext()
    apply_theme(app, ctx.config.theme)

    window = MainWindow(ctx)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
