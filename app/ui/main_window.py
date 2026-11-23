"""主窗口：左侧导航 + 右侧页面栈。"""

from __future__ import annotations

import base64
import re

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFrame,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..context import AppContext
from ..core.task import TaskStatus
from ..core.utils import (ffmpeg_install_hint, platform_label,
                          resource_path)
from .pages.audio import AudioPage
from .pages.compress import CompressPage
from .pages.downmix import DownmixPage
from .pages.media_info import MediaInfoPage
from .pages.queue import QueuePage
from .pages.remux import RemuxPage
from .pages.settings import SettingsPage
from .style import palette
from .widgets import FileTable, label

NAV_ITEMS = [
    ("视频压缩", "把大视频压小，日常主力功能"),
    ("音频提取", "从视频里取音频，或转换音频格式"),
    ("声道下混", "多声道转立体声；多个单声道文件合成一条"),
    ("封装混流", "换容器 / 挑音轨 / 嵌字幕，不重编码"),
    ("媒体信息", "查看视频或音频的全部参数，只读不改"),
    ("任务队列", "查看进度、速度与日志"),
    ("功能设置", "ffmpeg 路径、输出偏好、外观"),
]

# 任务队列页在导航里的位置（按名字查，避免以后插页面时索引错位）
QUEUE_INDEX = next(i for i, (name, _) in enumerate(NAV_ITEMS) if name == "任务队列")
# 「媒体信息 → 拿去压缩」要跳回压缩页（压缩页永远在第 0 位，仍按名字查防错位）
COMPRESS_INDEX = next(i for i, (name, _) in enumerate(NAV_ITEMS) if name == "视频压缩")

AUTHOR_SITE = "https://axutongxue.com/"
AUTHOR_SITE_TEXT = "axutongxue.com"
#: 链接颜色用软件主色（用户指定 #48A2FF，与 accent 保持一致的观感）
AUTHOR_LINK_COLOR = "#48A2FF"


class AboutAuthorDialog(QDialog):
    """「关于作者」弹窗：官网链接（可点，#48A2FF）+ 二维码图（50% 尺寸，居中）。

    🔴 图片尺寸的演变（都是实际踩出来的）：
    最初按原图 100%（1920×612）显示 —— 对话框要 1920+ 宽，超出多数屏幕，
    用户反馈「图片太大」；改成 50%（960×306），信息完整可扫，窗口也轻了。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于作者")

        qr = resource_path("qrcode.png")
        pix = QPixmap(str(qr)) if qr.exists() else QPixmap()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)

        # 官网链接：富文本 + openExternalLinks，点击直接开浏览器。
        # 🔴 两个坑（都实测过）：
        # 1. 链接颜色必须写进 <a> 的内联样式 —— setStyleSheet('a{color:..}')
        #    对 QLabel 富文本无效（链接色跟 palette 的 Link 角色走，不认 QSS）。
        # 2. 字号也必须写进 HTML —— QLabel 继承的是界面默认字号（9pt≈12px），
        #    放大图旁边显得太小；HTML 里的 font-size 才对富文本生效。
        #    20px + 加粗：这行是弹窗的标题，得压得住 960 宽的二维码图。
        link = QLabel(
            f'<span style="font-size: 20px; font-weight: 600;">作者官网：</span>'
            f'<a href="{AUTHOR_SITE}" '
            f'style="color: {AUTHOR_LINK_COLOR}; font-size: 20px;'
            f' font-weight: 600;">{AUTHOR_SITE_TEXT}</a>')
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        link.setOpenExternalLinks(True)
        link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(link)

        if pix.isNull():
            lay.addWidget(label("（二维码图片缺失：app/resources/qrcode.png）",
                                "Warn", wrap=True))
        else:
            # 50% 尺寸显示：原图 1920×612 对话框要 1920+ 宽，超出多数屏幕。
            # KeepAspectRatio 保证不变形；SmoothTransformation 避免锯齿。
            shown = pix.scaled(pix.width() // 2, pix.height() // 2,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            img = QLabel()
            img.setPixmap(shown)
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(img)
            self.adjustSize()

        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("关闭")
        btn.setObjectName("Primary")
        btn.clicked.connect(self.accept)
        row.addWidget(btn)
        lay.addLayout(row)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("小丸工具箱")
        self.setMinimumSize(980, 660)
        self.resize(1120, 820)
        self._fitted_once = False

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = self._build_sidebar()
        layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.compress_page = CompressPage(ctx)
        self.audio_page = AudioPage(ctx)
        self.downmix_page = DownmixPage(ctx)
        self.remux_page = RemuxPage(ctx)
        self.media_info_page = MediaInfoPage(ctx)
        self.queue_page = QueuePage(ctx)
        self.settings_page = SettingsPage(ctx)
        for page in (self.compress_page, self.audio_page, self.downmix_page,
                     self.remux_page, self.media_info_page,
                     self.queue_page, self.settings_page):
            self.stack.addWidget(self._wrap_scroll(page))
        layout.addWidget(self.stack, 1)

        for page in (self.compress_page, self.audio_page, self.downmix_page,
                     self.remux_page):
            page.requestQueue.connect(lambda: self.go_to(QUEUE_INDEX))

        # 媒体信息页：探测结果直接喂给信息卡；「拿去压缩」带文件跳到压缩页
        self.media_info_page.table.infoReady.connect(
            self.media_info_page.on_probe_done)
        self.media_info_page.requestCompress.connect(
            self._send_to_compress)

        self.settings_page.themeChanged.connect(self._apply_theme)
        self.ctx.envChanged.connect(self._refresh_env_badge)

        self._restore_geometry()
        self._refresh_env_badge()
        self.go_to(0)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _wrap_scroll(page: QWidget) -> QScrollArea:
        """页面套滚动容器：窗口不够高时页面可竖向滚动，内容不再被压扁。"""
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setViewportMargins(0, 0, 0, 0)
        sc.viewport().setAutoFillBackground(False)
        sc.setWidget(page)
        return sc

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(216)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 18, 14, 14)
        lay.setSpacing(4)

        brand = QWidget()
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(2, 0, 0, 0)
        bl.setSpacing(10)
        badge = QLabel()
        badge.setObjectName("BrandBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(34, 34)
        # 用应用图标的缩略版，保持和标题栏 / 任务栏 / 安装包同一个形象
        icon_file = resource_path("icon.png")
        if icon_file.exists():
            pix = QPixmap(str(icon_file))
            if not pix.isNull():
                ratio = badge.devicePixelRatioF() or 1.0
                # ⚠️ 别用 side 做变量名：上面 side 是侧栏 QWidget，
                # 覆盖掉它会让布局的父控件被回收（实测报 "already deleted"）
                px = int(round(34 * ratio))
                badge.setPixmap(pix.scaled(
                    px, px, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                badge.setScaledContents(False)
                # 高 DPI 下让像素图按逻辑尺寸绘制，否则会被放大成虚的
                pm = badge.pixmap()
                if pm is not None:
                    pm.setDevicePixelRatio(ratio)
        else:
            badge.setText("丸")     # 图标缺失时退回文字徽标
        bl.addWidget(badge)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(label("小丸工具箱", "BrandTitle"))
        text.addWidget(label(f"{platform_label()} 版 · v{__version__}", "BrandSub"))
        bl.addLayout(text)
        bl.addStretch(1)
        lay.addWidget(brand)
        lay.addSpacing(16)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []
        for i, (name, tip) in enumerate(NAV_ITEMS):
            btn = QPushButton(f"  {name}")
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, idx=i: self.go_to(idx))
            self.nav_group.addButton(btn, i)
            self._nav_buttons.append(btn)
            lay.addWidget(btn)

        # 「关于作者」不是页面，是弹窗 —— 所以不用 NavButton 的 checkable
        # 语义（点它不该把别的页面取消选中），放在导航组下面单独一个按钮。
        btn_author = QPushButton("  关于作者")
        btn_author.setObjectName("NavButton")
        btn_author.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_author.clicked.connect(self._show_about_author)
        lay.addWidget(btn_author)

        lay.addStretch(1)

        env_box = QWidget()
        el = QVBoxLayout(env_box)
        el.setContentsMargins(4, 0, 4, 0)
        el.setSpacing(2)
        self.env_dot = label("● 检测中", "EnvDot")
        self.env_text = label("", "EnvText", wrap=True)
        el.addWidget(self.env_dot)
        el.addWidget(self.env_text)
        lay.addWidget(env_box)
        return side

    # ------------------------------------------------------------------ #
    def go_to(self, index: int) -> None:
        index = max(0, min(index, self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        if 0 <= index < len(self._nav_buttons):
            self._nav_buttons[index].setChecked(True)

    def _show_about_author(self) -> None:
        AboutAuthorDialog(self).exec()

    def _send_to_compress(self, paths: list) -> None:
        """「拿去压缩」：把文件加进压缩页并切过去。

        只加不覆盖 —— 用户可能已经在压缩页排了别的文件，替他清空
        是破坏性操作。压缩页自己会去重（同路径不会重复加）。
        """
        self.go_to(COMPRESS_INDEX)
        if paths:
            self.compress_page.table.add_paths([str(p) for p in paths])

    def _apply_theme(self, theme: str) -> None:
        from .style import apply_theme

        apply_theme(QApplication.instance(), theme)
        self.ctx.config.theme = theme
        # 让已存在的表格按新主题重新着色
        for table in self.findChildren(FileTable):
            table.refresh_colors()
        self._refresh_env_badge()

    def _refresh_env_badge(self) -> None:
        env = self.ctx.env
        pal = palette(self.ctx.config.theme)
        if env.ready:
            self.env_dot.setText("● ffmpeg 就绪")
            self.env_dot.setStyleSheet(f"color: {pal['success']};")
            detail = "ffprobe 已启用" if env.has_probe else "ffprobe 缺失（功能不受影响）"
            m = re.search(r"ffmpeg version (\S+)", env.version or "")
            ver = (m.group(1) if m else "").split("-")[0][:24]
            self.env_text.setText(f"{detail}\nffmpeg {ver}" if ver else detail)
        else:
            self.env_dot.setText("● 未找到 ffmpeg")
            self.env_dot.setStyleSheet(f"color: {pal['danger']};")
            self.env_text.setText(f"请到「功能设置」指定路径\n或：{ffmpeg_install_hint()}")

    # ------------------------------------------------------------------ #
    def _restore_geometry(self) -> None:
        data = self.ctx.config.window_geometry
        if data:
            try:
                self.restoreGeometry(QByteArray(base64.b64decode(data)))
                return
            except Exception:
                pass
        self._center()

    def showEvent(self, event) -> None:      # noqa: N802
        super().showEvent(event)
        # 首次显示后再拟合高度：构造期布局还没算完，page.sizeHint() 不是最终值
        if not self._fitted_once:
            self._fitted_once = True
            QTimer.singleShot(0, self._fit_to_content)

    def _content_min_width(self) -> int:
        """各页内容需要的最小宽度（取最宽的一页）。"""
        need = 0
        for i in range(self.stack.count()):
            area = self.stack.widget(i)
            inner = area.widget() if isinstance(area, QScrollArea) else None
            if inner is not None:
                need = max(need, inner.minimumSizeHint().width())
        return need

    def _fit_to_content(self) -> None:
        """按内容需求设定窗口最小宽度，并把高度补足到不出现滚动条。

        宽度：滚动区关掉了横向滚动条，窗口一旦窄于内容，右侧会被直接裁掉
        （长说明文字、单行参数行的固有最小宽度都不小）。所以把最小宽度顶到
        内容需求，让窗口根本缩不到裁切的程度。
        """
        need_w = self._content_min_width() + self.sidebar.width() + 8
        if need_w > self.minimumWidth():
            self.setMinimumWidth(need_w)
        if self.width() < need_w:
            self.resize(need_w, self.height())
        self._fit_height_to_content()

    def _fit_height_to_content(self) -> None:
        """窗口高度不足以放下当前页内容时撑高，避免一启动就出现滚动条。

        只在「差得不多」时介入（≤120px）：差太多说明用户是有意把窗口缩小的，
        那时滚动是预期行为，不该擅自改尺寸。
        """
        page = self.compress_page
        page.adjustSize()
        need = page.sizeHint().height() + 6
        gap = need - self.height()
        if gap <= 0 or gap > 120:
            return
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry().height() - 40
            if need > avail:
                return
        self.resize(self.width(), need)

    def _center(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2,
                      geo.center().y() - self.height() // 2)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        running = [t for t in self.ctx.queue.tasks
                   if t.status == TaskStatus.RUNNING]
        if running:
            box = QMessageBox(self)
            box.setWindowTitle("还有任务在跑")
            box.setText(f"还有 {len(running)} 个任务正在处理，确定要退出吗？")
            box.setInformativeText("退出会中断这些任务，已生成的不完整文件会被删除。")
            box.setStandardButtons(QMessageBox.StandardButton.Cancel
                                   | QMessageBox.StandardButton.Yes)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            box.button(QMessageBox.StandardButton.Yes).setText("仍然退出")
            box.button(QMessageBox.StandardButton.Cancel).setText("继续等待")
            if box.exec() != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.ctx.queue.clear_all()

        try:
            geo = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.ctx.config.window_geometry = geo
            self.ctx.save_config()
        except Exception:
            pass
        event.accept()
