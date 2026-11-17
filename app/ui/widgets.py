"""可复用界面组件：卡片、文件表（拖拽 + 异步探测）、进度绘制、状态徽标。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (QEvent, QEasingCurve, QObject, QPoint,
                            QPropertyAnimation, QRect, QRectF, QRunnable, QSize,
                            Qt, QThreadPool, Signal)
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                               QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QHeaderView, QLabel, QListView, QSizePolicy,
                               QStyledItemDelegate, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.ffmpeg import MediaInfo, probe
from ..core.utils import (VIDEO_EXTS, AUDIO_EXTS, file_manager_label,
                          human_duration, human_size, open_in_file_manager)
from .style import palette


# --------------------------------------------------------------------------- #
# 基础小组件
# --------------------------------------------------------------------------- #

def label(text: str = "", role: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if role:
        lb.setObjectName(role)
    lb.setWordWrap(wrap)
    return lb


class Combo(QComboBox):
    """下拉框：选项面板画在**主窗口里**，不再开独立弹窗。

    Qt 的 QComboBox 下拉，本质是一个独立的 Qt::Popup 窗口
    （QComboBoxPrivateContainer）。那个窗口是所有别扭感的来源：
    圆角要隔着窗口描、动画只能动窗口几何（看着像在跳）、
    字体渲染和重绘上下文都跟主界面不是一套；贴在屏幕边缘时还会翻向另一侧。

    这里只重写 showPopup / hidePopup —— 模型、currentData / findData / addItem
    和全部信号都沿用 QComboBox，**调用点一行都不用改**。
    选项列表是主窗口的子控件，于是：同一套主题、同一套圆角、窗口内重绘的展开动画。

    键盘（主窗口内面板自己能收到按键，不用依赖那个弹窗窗口）：
    上下键移动、回车确认、Esc 关闭。
    """

    ROW_H = 30          # 每项行高
    PAD = 6             # 面板内边距
    MAX_H = 340         # 面板最高，超出就滚动
    DURATION = 130      # 展开时长

    _open: "Combo | None" = None    # 同一时刻只允许一个面板打开

    def __init__(self, parent=None):
        super().__init__(parent)
        self._panel: QFrame | None = None
        self._view: QListView | None = None
        self._anim: QPropertyAnimation | None = None
        self.currentIndexChanged.connect(self._sync_highlight)

    # ------------------------------------------------------------------ #
    # 面板
    # ------------------------------------------------------------------ #
    def _build_panel(self) -> None:
        panel = QFrame(self.window())
        panel.setObjectName("ComboPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 60))
        panel.setGraphicsEffect(shadow)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(self.PAD, self.PAD, self.PAD, self.PAD)
        lay.setSpacing(0)
        view = QListView(panel)
        view.setObjectName("ComboPanelList")
        view.setModel(self.model())
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setUniformItemSizes(True)
        view.setMouseTracking(True)
        view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.clicked.connect(self._on_item_clicked)
        lay.addWidget(view)
        panel.hide()
        self._panel, self._view = panel, view

    def _panel_rect(self) -> QRect:
        """面板放在控件下方；下方放不下就翻到上方，再夹在窗口内。"""
        top = self.window()
        fm = self.fontMetrics()
        text_w = max([fm.horizontalAdvance(self.itemText(i))
                      for i in range(self.count())] + [0])
        w = min(max(self.width(), text_w + self.PAD * 2 + 26), top.width() - 8)

        # 高度按真实行高算，别用固定值：差几 px 就会平白冒出一条滚动条
        row_h = self.ROW_H
        if self._view is not None and self.count():
            hint = self._view.sizeHintForRow(0)
            if hint > 0:
                row_h = hint
        # +2 是 Qt 的取整余量：内容只要比视口高 1px，视图就会多出一格可滚动范围，
        # 于是展开动画结束时"面板自己往下滚了一下"（用户报的 bug 就是这个）
        full = max(self.count(), 1) * row_h + self.PAD * 2 + 2
        h = min(full, self.MAX_H)
        if self._view is not None:
            self._view.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff if full <= self.MAX_H
                else Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        origin = top.mapFromGlobal(self.mapToGlobal(QPoint(0, 0)))
        x = max(4, min(origin.x(), top.width() - w - 4))
        below = origin.y() + self.height()
        y = below if below + h <= top.height() - 4 else origin.y() - h
        y = max(4, min(y, top.height() - h - 4))
        return QRect(x, y, w, h)

    # ------------------------------------------------------------------ #
    # 开关
    # ------------------------------------------------------------------ #
    def showPopup(self) -> None:      # noqa: N802
        if self.count() == 0:
            return
        if self._panel is not None and self._panel.isVisible():
            self.hidePopup()          # 再点一次收起
            return
        if Combo._open is not None and Combo._open is not self:
            Combo._open.hidePopup()
        if self._panel is None:
            self._build_panel()

        end = self._panel_rect()
        below = self.window().mapFromGlobal(
            self.mapToGlobal(QPoint(0, self.height()))).y()
        start = (QRect(end.x(), end.y(), end.width(), 0) if end.top() >= below - 2
                 else QRect(end.x(), end.bottom() + 1, end.width(), 0))

        self._view.setCurrentIndex(self.model().index(self.currentIndex(), 0))
        self._panel.setGeometry(start)
        self._panel.show()
        self._panel.raise_()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        Combo._open = self

        anim = QPropertyAnimation(self._panel, b"geometry", self._panel)
        anim.setDuration(self.DURATION)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.finished.connect(self._on_grown)
        self._anim = anim
        anim.start()

    def hidePopup(self) -> None:      # noqa: N802
        if self._anim is not None:
            self._anim.stop()
        if self._panel is not None and self._panel.isVisible():
            self._panel.hide()
        if Combo._open is self:
            Combo._open = None
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_grown(self) -> None:
        if self._panel is None:
            return
        self._panel.setGeometry(self._panel_rect())
        # 只保证当前项可见；不要 PositionAtCenter —— 只要视图有一格可滚动范围，
        # 它就会真的滚一下，看起来像"面板自己往下跳"
        self._view.scrollTo(self._view.currentIndex(),
                            QAbstractItemView.ScrollHint.EnsureVisible)

    def _on_item_clicked(self, index) -> None:
        self.setCurrentIndex(index.row())
        # 🔴 必须自己发 activated：重写 popup 之后 Qt 不再知道"这是用户选的"。
        # 而"用户选的"和"代码设的"要区分开 —— 例如目标体积页的色深会跟随编码器
        # 自动切换，一旦用户亲手选过就该停止跟随（用 activated 判定）。
        self.activated.emit(index.row())
        self.hidePopup()

    def _sync_highlight(self, row: int) -> None:
        if self._view is not None and self._panel is not None \
                and self._panel.isVisible():
            self._view.setCurrentIndex(self.model().index(row, 0))

    def eventFilter(self, obj, event) -> bool:      # noqa: N802
        from PySide6.QtWidgets import QComboBox as _QC

        if self._panel is None or not self._panel.isVisible():
            return False
        et = event.type()
        # 面板打开期间：点到别处、滚页面、窗口被拖动或缩放，都关掉
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
            pos = event.globalPosition().toPoint()
            inside = (self._panel.rect().contains(self._panel.mapFromGlobal(pos))
                      or self.rect().contains(self.mapFromGlobal(pos)))
            if not inside:
                self.hidePopup()
        elif et in (QEvent.Type.Move, QEvent.Type.Resize) and obj is self.window():
            self.hidePopup()
        return False

    def keyPressEvent(self, event) -> None:         # noqa: N802
        if self._panel is not None and self._panel.isVisible():
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.hidePopup()
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # 回车确认也是"用户选的"，与鼠标点击同等对待
                self.activated.emit(self.currentIndex())
                self.hidePopup()
                return
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.currentIndex() + (1 if key == Qt.Key.Key_Down else -1)
                row = max(0, min(row, self.count() - 1))
                self.setCurrentIndex(row)
                self._view.scrollTo(self._view.currentIndex(),
                                    QAbstractItemView.ScrollHint.EnsureVisible)
                return
            if key == Qt.Key.Key_F4:          # 再按一次收起
                self.hidePopup()
                return
        super().keyPressEvent(event)


class HLine(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HLine")
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFixedHeight(1)


class Card(QFrame):
    """圆角面板容器。"""

    def __init__(self, parent=None, margins=(14, 12, 14, 12), spacing: int = 10):
        super().__init__(parent)
        self.setObjectName("Card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(*margins)
        self._lay.setSpacing(spacing)

    def layout(self) -> QVBoxLayout:  # type: ignore[override]
        return self._lay

    def add(self, w) -> None:
        if isinstance(w, QWidget):
            self._lay.addWidget(w)
        else:
            self._lay.addLayout(w)


class StatCard(Card):
    """数值 + 说明。"""

    def __init__(self, caption: str, value: str = "—", parent=None):
        super().__init__(parent, margins=(12, 9, 12, 9), spacing=2)
        self.value_label = label(value, "StatValue")
        self.caption_label = label(caption, "StatLabel")
        self.add(self.value_label)
        self.add(self.caption_label)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)


class Pill(QLabel):
    def __init__(self, text: str, kind: str = "ok", parent=None):
        super().__init__(text, parent)
        self.setObjectName(f"Pill_{kind}")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_kind(self, kind: str) -> None:
        self.setObjectName(f"Pill_{kind}")
        self.style().unpolish(self)
        self.style().polish(self)


# --------------------------------------------------------------------------- #
# 异步探测
# --------------------------------------------------------------------------- #

class _ProbeSignals(QObject):
    done = Signal(str, object)


class _ProbeJob(QRunnable):
    def __init__(self, env_provider, path: Path, signals: _ProbeSignals):
        super().__init__()
        self._env_provider = env_provider
        self._path = path
        self._signals = signals

    def run(self) -> None:  # noqa: D102
        try:
            info = probe(self._env_provider(), self._path)
        except Exception as exc:  # noqa: BLE001
            info = MediaInfo(path=self._path, error=str(exc))
        self._signals.done.emit(str(self._path), info)


# --------------------------------------------------------------------------- #
# 文件列表
# --------------------------------------------------------------------------- #

class FileArea(Card):
    """文件区：工具栏 + 文件表，共用**一张**面板。

    原来这里是三层框叠在一起：卡片一层、表格自己一层边框、
    列表为空时表格里再画一个虚线框 —— 看着像三个盒子摞起来。

    现在只留一层：面板本身就是投放区。
    有文件时是普通卡片；列表为空时卡片边框变成虚线，
    「这里可以放文件」由面板直接表达，表格不再画自己的边框和虚线框。
    """

    def __init__(self, table: "FileTable", toolbar: QWidget,
                 extra: QWidget | list | None = None, parent=None):
        super().__init__(parent)
        self.table = table
        self.add(toolbar)
        if extra is not None:
            for w in (extra if isinstance(extra, (list, tuple)) else [extra]):
                self.add(w)
        self.layout().addWidget(table, 1)
        table.filesChanged.connect(self._sync_dropzone)
        self._sync_dropzone()

    def _sync_dropzone(self) -> None:
        empty = self.table.rowCount() == 0
        if self.property("dropzone") == empty:
            return
        # 动态属性 + 重新 polish，QSS 的 [dropzone="true"] 才会生效
        self.setProperty("dropzone", empty)
        self.style().unpolish(self)
        self.style().polish(self)


class FileTable(QTableWidget):
    """支持拖拽导入、异步读取媒体信息的文件表格。"""

    filesChanged = Signal()

    HEADERS = ["文件名", "规格", "时长", "大小", "状态"]

    def __init__(self, env_provider, extensions: set[str] | None = None,
                 accept_dirs: bool = True, parent=None):
        super().__init__(0, len(self.HEADERS), parent)
        self._env_provider = env_provider
        self._exts = extensions or (VIDEO_EXTS | AUDIO_EXTS)
        self._accept_dirs = accept_dirs
        self._paths: list[Path] = []
        self._infos: dict[str, MediaInfo] = {}
        self._row_of: dict[str, int] = {}
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(3)
        self._probe_signals = _ProbeSignals()
        self._probe_signals.done.connect(self._on_probe_done)
        # 可选的「文件名前缀」钩子：下混页用它显示每个文件是哪条声道
        self._name_prefix = None

        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(30)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        # 列表区最小高度：它直接决定窗口最矮能有多矮。140 = 表头 + 3 行多一点，
        # 再压会让 3 个文件就出现滚动条，列表反而不好用
        self.setMinimumHeight(140)

        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setHighlightSections(False)
        hh.setSectionsMovable(False)

    def sizeHint(self) -> QSize:      # noqa: N802
        """列表区是拉伸项：高度由布局给，别拿默认的"理想高度"去顶高窗口。

        QTableView 默认 sizeHint 要 190+ px，而它只是页面里的一块，
        结果整个窗口的最小高度被它白撑高几十像素。
        """
        hint = super().sizeHint()
        return QSize(hint.width(), self.minimumHeight())

    # ---------------- 数据 ---------------- #
    @property
    def paths(self) -> list[Path]:
        return list(self._paths)

    def infos(self) -> dict[str, MediaInfo]:
        return dict(self._infos)

    def info_for(self, path: Path) -> MediaInfo | None:
        return self._infos.get(str(path))

    def add_paths(self, raw_paths: list[str], recursive: bool = False) -> int:
        added = 0
        for raw in raw_paths:
            p = Path(raw)
            candidates: list[Path] = []
            if p.is_dir() and self._accept_dirs:
                it = p.rglob("*") if recursive else p.glob("*")
                candidates = sorted(c for c in it if c.is_file())
            elif p.is_file():
                candidates = [p]
            for cand in candidates:
                if cand.suffix.lower() not in self._exts:
                    continue
                if str(cand) in self._row_of:
                    continue
                self._append_row(cand)
                added += 1
        if added:
            self.filesChanged.emit()
        return added

    def _append_row(self, path: Path) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self._paths.append(path)
        self._row_of[str(path)] = row

        self.setItem(row, 0, self._make_name_item(path, row))

        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        self.setItem(row, 1, self._dim("读取中…"))
        self.setItem(row, 2, self._dim("—"))
        self.setItem(row, 3, self._dim(human_size(size)))
        self.setItem(row, 4, self._dim("待处理"))

        self._pool.start(_ProbeJob(self._env_provider, path, self._probe_signals))

    @staticmethod
    def _dim(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setForeground(QColor(palette()["text_dim"]))
        return item

    # ---------------- 文件名前缀（页面自定义） ---------------- #
    def set_name_prefix(self, fn) -> None:
        """给「文件名」列加前缀。

        fn(path_str, row) -> str | tuple[str, str]
        返回 tuple 时第二项是配色键（如 "warn" / "danger"），用来标出可疑行。
        """
        self._name_prefix = fn
        self.refresh_names()

    def _prefix_and_kind(self, path: Path, row: int) -> tuple[str, str]:
        if not self._name_prefix:
            return "", ""
        try:
            raw = self._name_prefix(str(path), row)
        except Exception:  # noqa: BLE001
            return "", ""
        if isinstance(raw, tuple):
            prefix = raw[0] if len(raw) > 0 else ""
            kind = raw[1] if len(raw) > 1 else ""
            return prefix or "", kind or ""
        return (raw or ""), ""

    @staticmethod
    def _apply_name_color(item: QTableWidgetItem, kind: str) -> None:
        pal = palette()
        color = pal.get(kind) if kind else None
        item.setForeground(QColor(color or pal["text"]))

    def _make_name_item(self, path: Path, row: int) -> QTableWidgetItem:
        prefix, kind = self._prefix_and_kind(path, row)
        item = QTableWidgetItem(f"{prefix}{path.name}")
        item.setToolTip(str(path))
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        self._apply_name_color(item, kind)
        return item

    def refresh_names(self) -> None:
        """重排或切换模式后，刷新文件名列的前缀与配色。"""
        for row, path in enumerate(self._paths):
            item = self.item(row, 0)
            if item is None:
                continue
            prefix, kind = self._prefix_and_kind(path, row)
            item.setText(f"{prefix}{path.name}")
            self._apply_name_color(item, kind)

    # ---------------- 排序 ---------------- #
    def move_selected(self, delta: int) -> bool:
        """把选中的行上移 / 下移（delta = -1 / +1）。"""
        rows = sorted({i.row() for i in self.selectedIndexes()})
        if not rows or delta == 0:
            return False
        order = rows if delta < 0 else list(reversed(rows))
        moved_to: list[int] = []
        for r in order:
            t = r + delta
            if t < 0 or t >= len(self._paths):
                continue
            self._swap_rows(r, t)
            moved_to.append(t)
        if not moved_to:
            return False
        self._row_of = {str(p): i for i, p in enumerate(self._paths)}
        self.refresh_names()
        self.clearSelection()
        for r in moved_to:
            self.selectRow(r)
        self.filesChanged.emit()
        return True

    def reorder(self, order: list[int]) -> bool:
        """按 order（旧行号的新排列）重排整个列表。"""
        if sorted(order) != list(range(len(self._paths))):
            return False
        snapshot = [[self.takeItem(r, c) for c in range(self.columnCount())]
                    for r in range(self.rowCount())]
        self._paths = [self._paths[i] for i in order]
        for new_row, old_index in enumerate(order):
            for col, item in enumerate(snapshot[old_index]):
                if item is not None:
                    self.setItem(new_row, col, item)
        self._row_of = {str(p): i for i, p in enumerate(self._paths)}
        self.refresh_names()
        self.filesChanged.emit()
        return True

    def _swap_rows(self, a: int, b: int) -> None:
        for col in range(self.columnCount()):
            item_a = self.takeItem(a, col)
            item_b = self.takeItem(b, col)
            if item_b is not None:
                self.setItem(a, col, item_b)
            if item_a is not None:
                self.setItem(b, col, item_a)
        self._paths[a], self._paths[b] = self._paths[b], self._paths[a]

    def _on_probe_done(self, path_str: str, info: MediaInfo) -> None:
        row = self._row_of.get(path_str)
        if row is None or row >= self.rowCount():
            return
        self._infos[path_str] = info

        spec = self.item(row, 1)
        dur = self.item(row, 2)
        if spec:
            spec.setText(info.summary)
            spec.setToolTip(info.error or info.summary)
        if dur:
            dur.setText(human_duration(info.duration))
        self._apply_row_colors(row, info)
        # 通知页面刷新体积估算等提示
        self.filesChanged.emit()

    def _apply_row_colors(self, row: int, info: MediaInfo | None) -> None:
        """按当前主题给「规格 / 状态」两列上色。"""
        pal = palette()
        spec = self.item(row, 1)
        st = self.item(row, 4)
        if info is None:
            if spec:
                spec.setForeground(QColor(pal["text_dim"]))
            if st:
                st.setText("读取中…")
                st.setForeground(QColor(pal["text_dim"]))
            return
        if spec:
            spec.setForeground(QColor(pal["text"] if info.ok else pal["danger"]))
        if st:
            if info.ok:
                st.setText("就绪")
                st.setForeground(QColor(pal["success"]))
            else:
                st.setText("无法读取")
                st.setForeground(QColor(pal["danger"]))
                st.setToolTip(info.error)

    def refresh_colors(self) -> None:
        """主题切换后由外部调用，让已有行按新主题重新着色。"""
        for row, path in enumerate(self._paths):
            self._apply_row_colors(row, self._infos.get(str(path)))
        self.refresh_names()

    def remove_selected(self) -> int:
        rows = sorted({i.row() for i in self.selectedIndexes()}, reverse=True)
        if not rows:
            return 0
        for r in rows:
            path = str(self._paths[r]) if r < len(self._paths) else None
            if path:
                self._row_of.pop(path, None)
                self._infos.pop(path, None)
            self._paths.pop(r)
            self.removeRow(r)
        self._row_of = {str(p): i for i, p in enumerate(self._paths)}
        self.refresh_names()
        self.filesChanged.emit()
        return len(rows)

    def clear_all(self) -> None:
        self.setRowCount(0)
        self._paths.clear()
        self._row_of.clear()
        self._infos.clear()
        self.filesChanged.emit()

    # ---------------- 拖拽 ---------------- #
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        files = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if files:
            self.add_paths(files)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    # ---------------- 空状态 ---------------- #
    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.rowCount() == 0:
            # 虚线框不在这里画：空列表时整张面板（FileArea）的边框就是虚线的，
            # 这里只补两行提示文字，避免再套一层框
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pal = palette()
            rect = self.viewport().rect()

            p.setPen(QColor(pal["text_dim"]))
            f = QFont()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(rect.adjusted(0, -14, 0, 0),
                       Qt.AlignmentFlag.AlignCenter, "把视频 / 音频文件拖到这里")

            f.setPointSize(10)
            p.setFont(f)
            p.setPen(QColor(pal["text_faint"]))
            p.drawText(rect.adjusted(0, 20, 0, 0),
                       Qt.AlignmentFlag.AlignCenter,
                       "也可以点上面的「添加文件」，支持批量与整个文件夹")
            p.end()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        row = self.rowAt(event.pos().y())
        if row < 0:
            return
        path = self._paths[row]
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        act_open = menu.addAction(f"在{file_manager_label()}中显示")
        act_remove = menu.addAction("从列表移除")
        chosen = menu.exec(event.globalPos())
        if chosen is act_open:
            open_in_file_manager(path)
        elif chosen is act_remove:
            self.remove_selected()


# --------------------------------------------------------------------------- #
# 进度绘制
# --------------------------------------------------------------------------- #

class ProgressDelegate(QStyledItemDelegate):
    """把单元格里的 0~1 浮点数画成细进度条 + 百分比。"""

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: D102
        value = index.data(Qt.ItemDataRole.UserRole)
        if value is None:
            super().paint(painter, option, index)
            return
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            super().paint(painter, option, index)
            return

        pal = palette()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        r = option.rect.adjusted(8, 7, -8, -7)
        if r.height() < 6:
            r.setHeight(6)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal["track"]))
        painter.drawRoundedRect(QRectF(r), 3, 3)

        if ratio > 0:
            fill = QRectF(r)
            fill.setWidth(max(3.0, r.width() * min(1.0, ratio)))
            color = QColor(pal["accent"])
            if ratio >= 1.0:
                color = QColor(pal["success"])
            painter.setBrush(color)
            painter.drawRoundedRect(fill, 3, 3)

        painter.setPen(QColor(pal["text_dim"]))
        f = QFont()
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(option.rect, Qt.AlignmentFlag.AlignCenter, str(text))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802, D102
        return QSize(120, 28)


# --------------------------------------------------------------------------- #
# 输入行辅助
# --------------------------------------------------------------------------- #

def field_row(text: str, widget: QWidget, hint: str = "") -> QWidget:
    """左侧标签 + 右侧控件的横向行。"""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    lb = label(text)
    lb.setMinimumWidth(72)
    lb.setMaximumWidth(72)
    lay.addWidget(lb)
    lay.addWidget(widget, 1)
    if hint:
        h = label(hint, "Hint")
        h.setMinimumWidth(74)
        lay.addWidget(h)
    return row


def grid_row(pairs: list[tuple[str, QWidget]], per_row: int = 2) -> QWidget:
    """把若干 (标签, 控件) 排成网格。"""
    from PySide6.QtWidgets import QGridLayout

    holder = QWidget()
    g = QGridLayout(holder)
    g.setContentsMargins(0, 0, 0, 0)
    g.setHorizontalSpacing(14)
    g.setVerticalSpacing(8)
    for i, (text, widget) in enumerate(pairs):
        r, c = divmod(i, per_row)
        g.addWidget(label(text), r, c * 2)
        g.addWidget(widget, r, c * 2 + 1)
    for c in range(per_row):
        g.setColumnStretch(c * 2 + 1, 1)
    return holder
