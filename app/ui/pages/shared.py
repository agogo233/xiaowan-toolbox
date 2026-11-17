"""页面公用构件：页头、文件工具栏、预设卡片、输出目录行。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QMenu,
                               QPushButton, QVBoxLayout, QWidget)

from ...core.presets import Preset
from ...core.utils import VIDEO_EXTS, AUDIO_EXTS, open_in_file_manager
from ..style import palette
from ..widgets import FileTable, label


# --------------------------------------------------------------------------- #
# 页头
# --------------------------------------------------------------------------- #

class Page(QWidget):
    """所有页面的基类：统一标题 + 边距。"""

    def __init__(self, ctx, title: str, desc: str = "", parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(22, 18, 22, 16)
        self.root.setSpacing(12)

        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(label(title, "PageTitle"))
        if desc:
            head.addWidget(label(desc, "PageDesc"))
        self.root.addLayout(head)

    def add(self, widget_or_layout) -> None:
        if isinstance(widget_or_layout, QWidget):
            self.root.addWidget(widget_or_layout)
        else:
            self.root.addLayout(widget_or_layout)


# --------------------------------------------------------------------------- #
# 文件工具栏
# --------------------------------------------------------------------------- #

def _file_filter(exts: set[str], all_label: str = "媒体文件") -> str:
    patterns = " ".join(f"*{e}" for e in sorted(exts))
    return f"{all_label} ({patterns});;所有文件 (*)"


def make_file_toolbar(table: FileTable, exts: set[str] | None = None,
                      hint: str = "") -> QWidget:
    exts = exts or (VIDEO_EXTS | AUDIO_EXTS)
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)

    btn_files = QPushButton("添加文件")
    btn_dir = QPushButton("添加文件夹")
    btn_remove = QPushButton("移除所选")
    btn_clear = QPushButton("清空")

    lay.addWidget(btn_files)
    lay.addWidget(btn_dir)
    lay.addWidget(btn_remove)
    lay.addWidget(btn_clear)
    lay.addStretch(1)

    if hint:
        hint_label = label(hint, "Hint")
        hint_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(hint_label)

    count = label("", "Hint")
    lay.addWidget(count)

    def refresh_count() -> None:
        n = table.rowCount()
        count.setText(f"共 {n} 个文件" if n else "")

    def on_files() -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            table, "选择文件", "", _file_filter(exts))
        if paths:
            table.add_paths(paths)

    def on_dir() -> None:
        path = QFileDialog.getExistingDirectory(table, "选择文件夹")
        if path:
            n = table.add_paths([path])
            if not n:
                count.setText("该文件夹里没有可处理的媒体文件")

    btn_files.clicked.connect(on_files)
    btn_dir.clicked.connect(on_dir)
    btn_remove.clicked.connect(table.remove_selected)
    btn_clear.clicked.connect(table.clear_all)
    table.filesChanged.connect(refresh_count)

    refresh_count()
    return row


# --------------------------------------------------------------------------- #
# 预设卡片
# --------------------------------------------------------------------------- #

def _wrap(text: str, fm, width: int, max_lines: int) -> list[str]:
    """按像素宽度折行：英文单词保持完整，中文逐字断行。"""
    import re

    # 词尾带空格一起当成一个 token：否则拼回去时 "tune animation" 会变成
    # "tuneanimation"、分隔符 " · " 也会被吞掉
    tokens = re.findall(r"[A-Za-z0-9._\-/%]+\s*|\S\s*", text)
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        candidate = cur + tok
        if fm.horizontalAdvance(candidate) > width and cur:
            lines.append(cur)
            cur = tok.lstrip()
            if len(lines) >= max_lines:
                break
        else:
            cur = candidate
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines and "".join(lines).replace(" ", "") != text.replace(" ", ""):
        # 比较时忽略空格：重新拼行时词间空格会被吃掉，直接比字符串会永远不等，
        # 导致文字明明放得下也硬加一个「…」（实测每张预设卡都带省略号）
        last = lines[-1]
        while last and fm.horizontalAdvance(last + "…") > width:
            last = last[:-1]
        lines[-1] = last + "…"
    elif len(lines) > 1 and fm.horizontalAdvance(lines[-1]) < width * 0.35:
        # 末行只剩一两个字（实测「动漫番剧」的「块」）时，从上一行末尾匀几个字过来
        while len(lines[-2]) > 1:
            moved = lines[-2][-1] + lines[-1]
            rest = lines[-2][:-1]
            if fm.horizontalAdvance(moved) > width or fm.horizontalAdvance(moved) > fm.horizontalAdvance(rest):
                break
            lines[-2], lines[-1] = rest, moved
    return lines


# 预设卡片的文字尺寸与内边距。名称和标签抢的是同一行的宽度：
#   CHIP_W - CHIP_PAD*2 - 标签宽 - TAG_GAP  ≥  名称宽
# 不满足时名称被画成省略号（标题就看不见了），所以这几个数别随手改，
# 改完跑 tests/preset_chip_test.py。
CHIP_PAD = 11      # 卡片左右内边距
TAG_PAD_X = 12     # 标签胶囊的左右内边距
TAG_GAP = 8        # 名称与标签之间的最小间隙


def tag_width(fm: QFontMetrics, tagline: str) -> int:
    """标签胶囊占的宽度（含内边距）；没有标签就是 0。"""
    return fm.horizontalAdvance(tagline) + TAG_PAD_X if tagline else 0


def name_room(chip_w: int, fm_tag: QFontMetrics, tagline: str) -> int:
    """名称能用多少像素 = 卡片内宽扣掉标签和间隙。"""
    return chip_w - CHIP_PAD * 2 - tag_width(fm_tag, tagline) - TAG_GAP


class PresetFlowRow(QWidget):
    """预设卡片流式排布：按可用宽度自动决定每行张数，窗口窄时自动换行。"""

    heightChanged = Signal(int)

    CHIP_W, CHIP_H, GAP = 168, 78, 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips: list[QPushButton] = []
        self._cols = 0

    def addChip(self, chip: QPushButton) -> None:
        chip.setParent(self)
        chip.setFixedSize(QSize(self.CHIP_W, self.CHIP_H))
        chip.show()
        self._chips.append(chip)
        self._relayout(self.width())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout(event.size().width())

    def _relayout(self, w: int) -> None:
        if not self._chips or w <= 0:
            return
        cols = max(1, (w + self.GAP) // (self.CHIP_W + self.GAP))
        if cols == self._cols:
            return
        self._cols = cols
        for i, chip in enumerate(self._chips):
            r, c = divmod(i, cols)
            chip.move(c * (self.CHIP_W + self.GAP),
                      r * (self.CHIP_H + self.GAP))
        rows = (len(self._chips) + cols - 1) // cols
        h = rows * self.CHIP_H + (rows - 1) * self.GAP
        self.setMinimumHeight(h)
        self.heightChanged.emit(h)


class PresetChip(QPushButton):
    """一键预设卡片：名称 + 标签 + 说明。"""

    def __init__(self, preset: Preset, parent=None):
        super().__init__(parent)
        self.preset = preset
        self.setObjectName("PresetChip")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(preset.detail)
        self.setFixedSize(QSize(PresetFlowRow.CHIP_W, PresetFlowRow.CHIP_H))

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        pal = palette()
        checked = self.isChecked()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        pad = CHIP_PAD
        w = self.width() - pad * 2

        # 右上角标签（先量出宽度，给名称让位）
        tag_w = 0
        if self.preset.tagline:
            f2 = QFont()
            f2.setPointSize(8)
            f2.setBold(True)
            p.setFont(f2)
            tag_w = tag_width(p.fontMetrics(), self.preset.tagline)

        # 名称：放不下就按剩余宽度省略。不给 0.55 的下限——之前用下限兜底，
        # 结果 4 字名称会画到标签底下去（「极限小体积」实测被压住）。
        # 省略号只是兜底，正常情况这里不该触发：名称宽度要留够，
        # 靠 tests/preset_chip_test.py 守住（「H.265 兼容压缩」就是这么被切没的）。
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        name = self.preset.name
        if tag_w:
            name = fm.elidedText(name, Qt.TextElideMode.ElideRight,
                                 max(w - tag_w - TAG_GAP, 0))
        p.setPen(QColor(pal["text"]))
        p.drawText(pad, 22, name)

        if self.preset.tagline:
            from PySide6.QtCore import QRectF

            p.setFont(f2)
            chip = QRectF(pad + w - tag_w, 10, tag_w, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(pal["accent_soft"]))
            p.drawRoundedRect(chip, 7, 7)
            p.setPen(QColor(pal["accent"]))
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, self.preset.tagline)

        # 说明
        f3 = QFont()
        f3.setPointSize(8)
        p.setFont(f3)
        p.setPen(QColor(pal["text_dim"]))
        fm3 = p.fontMetrics()
        y = 38
        for line in _wrap(self.preset.detail, fm3, w, 3):
            p.drawText(pad, y, line)
            y += 14
        p.end()


# --------------------------------------------------------------------------- #
# 输出目录行
# --------------------------------------------------------------------------- #

class OutputRow(QWidget):
    """输出位置：一个控件说清"文件放哪"。

    原来拆成「下拉选模式 + 选择…按钮 + 打开目录」三个控件，模式和值是两回事：
    在「与源文件相同目录」模式下点选择…，选完文件夹界面毫无变化（mode 没改）。
    现在合并成一个按钮，显示当前去向，点开就是三个动作的菜单。
    """

    changed = Signal()

    WIDTH = 240          # 定宽：路径过长时中间省略，菜单里能看到全路径

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        lay.addWidget(label("输出到"))
        self.btn_dest = QPushButton()
        self.btn_dest.setObjectName("OutputDest")
        self.btn_dest.setFixedWidth(self.WIDTH)
        self.btn_dest.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dest.clicked.connect(self._show_menu)
        lay.addWidget(self.btn_dest)

        self.menu = QMenu(self)
        self.act_pick = self.menu.addAction("选择文件夹…")
        self.act_reset = self.menu.addAction("恢复为源文件目录")
        self.act_open = self.menu.addAction("打开当前目录")
        self.act_pick.triggered.connect(self._pick)
        self.act_reset.triggered.connect(self._reset)
        self.act_open.triggered.connect(self._open)
        self._sync()

    # ------------------------------------------------------------------ #
    def _sync(self) -> None:
        cfg = self.ctx.config
        if cfg.output_mode == "source":
            self.btn_dest.setText("与源文件相同目录")
            self.btn_dest.setToolTip("输出到每个源文件所在的文件夹")
        else:
            path = cfg.output_dir or "（未设置）"
            fm = QFontMetrics(self.btn_dest.font())
            self.btn_dest.setText(fm.elidedText(path, Qt.TextElideMode.ElideMiddle,
                                               self.WIDTH - 34))
            self.btn_dest.setToolTip(path)
        self.act_reset.setEnabled(cfg.output_mode != "source")
        self.act_open.setEnabled(bool(cfg.output_dir) and Path(cfg.output_dir).exists())

    def _show_menu(self) -> None:
        self.menu.exec(self.btn_dest.mapToGlobal(self.btn_dest.rect().bottomLeft()))

    def _pick(self) -> None:
        start = self.ctx.config.output_dir or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if not path:
            return
        # 选完必须同时切到「指定文件夹」，否则模式还是 source，界面不会有任何变化
        self.ctx.config.output_dir = path
        self.ctx.config.output_mode = "custom"
        self.ctx.save_config()
        self._sync()
        self.changed.emit()

    def _reset(self) -> None:
        self.ctx.config.output_mode = "source"
        self.ctx.save_config()
        self._sync()
        self.changed.emit()

    def _open(self) -> None:
        target = self.ctx.config.output_dir
        if target and Path(target).exists():
            open_in_file_manager(Path(target))
        else:
            self._sync()


class ActionBar(QWidget):
    """页面底部：输出位置 + 说明文字 + 主操作按钮，同处一行。"""

    def __init__(self, button_text: str, output: OutputRow | None = None,
                 parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.output = output
        if output is not None:
            lay.addWidget(output)
        self.hint = label("", "Hint")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint, 1)
        self.button = QPushButton(button_text)
        self.button.setObjectName("Primary")
        self.button.setMinimumWidth(140)
        lay.addWidget(self.button)

    def set_hint(self, text: str, kind: str = "Hint") -> None:
        self.hint.setText(text)
        self.hint.setObjectName(kind)
        self.hint.style().unpolish(self.hint)
        self.hint.style().polish(self.hint)
