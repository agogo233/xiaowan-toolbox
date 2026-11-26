"""封装混流页的「封面」子卡。

设计稿见 `_design/remux-cover.html`。四个要点：

  1. **一个主开关**「添加封面」—— 之前只有子选项，用户看不出这一页到底会不会
     加封面。主开关关掉时整块收起，状态一眼可见。
  2. **两级来源**：同名图片（每文件各用各的）→ 全局封面（兜底）→ 不加
  3. **按容器给能力，并且能一键纠正**：MKV 只写附件、资源管理器不认 ——
     这是用户实测踩到的坑（选了 MKV、旁边有同名图、封装完缩略图没变）。
     光写一行灰字没用，要橙色告警 + 「改用 MP4」按钮。
  4. **把体积代价说清**：不加「压缩封面」按钮（那会破坏本页"不重新编码"的承诺）
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from ...core.ffmpeg import find_cover
from ...core.presets import Container, cover_support
from ...core.utils import COVER_EXTS, human_size
from ..widgets import Card, label

#: 封面缩略图尺寸（16:9，和视频观感一致）
THUMB_W, THUMB_H = 128, 72

#: 封面字节数超过输出体积的这个比例时才提示 —— 低于此不吵。
COVER_SIZE_WARN_RATIO = 0.05

#: 全局封面超过这个大小就建议换小图（缩略图最多显示 256px）
BIG_COVER_BYTES = 2 * 1024 * 1024

_IMAGE_FILTER = "图片 (" + " ".join(f"*{e}" for e in COVER_EXTS) + ");;所有文件 (*)"


class CoverThumb(QLabel):
    """封面预览：有图就显示图，没有就显示虚线占位。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(THUMB_W, THUMB_H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("CoverThumb")
        self._clear()

    def _clear(self) -> None:
        self.setPixmap(QPixmap())
        self.setText("未选择\n封面")

    def set_image(self, path: Path | None) -> None:
        if path is None:
            self._clear()
            return
        pix = QPixmap(str(path))
        if pix.isNull():
            self._clear()
            self.setText("无法预览")
            return
        scaled = pix.scaled(THUMB_W, THUMB_H, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(scaled)


class CoverSection(Card):
    """封面子卡。"""

    changed = Signal()
    #: 用户在告警里点了「改用 MP4」，请上层切容器
    requestContainer = Signal(object)

    def __init__(self, ctx, parent=None):
        # 🔴 边距压到 10/9：这一页已经有文件区 + 参数卡 + 操作栏，
        # 封面卡按默认 14/12 会到 247px 高，整页直接超出窗口高度、
        # 「开始封装」按钮掉到可视区外（实测 927 > 900）。
        super().__init__(parent, margins=(14, 10, 14, 9), spacing=6)
        self.ctx = ctx
        self._global_cover: Path | None = None
        self._container = Container.MKV

        # ---------- 主开关 ----------
        # 🔴 默认**不勾**（用户 2026-11-24 明确要求）。
        # 理由：加封面会改变输出文件（体积变大、资源管理器缩略图变化），
        # 属于"用户没要求就不该发生"的副作用。默认开会让"只是换个容器"
        # 的人莫名其妙拿到一个变大的文件。
        # 这也和本页其它选项一致：容器/音轨那些是"总要选一个"，
        # 而封面是"不加也能正常完成"的可选项。
        self.chk_enable = QCheckBox("添加封面")
        self.chk_enable.setChecked(False)
        self.chk_enable.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_enable.toggled.connect(self._on_enable_toggled)
        self.add(self.chk_enable)

        # 以下全部内容受主开关控制，收进一个容器里好整体隐藏
        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)

        # ---------- ① 同名图片自动匹配 ----------
        self.chk_auto = QCheckBox("自动用同名的图片文件")
        self.chk_auto.setChecked(True)
        self.chk_auto.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_auto.toggled.connect(lambda _: self.changed.emit())
        body.addWidget(self.chk_auto)
        body.addWidget(label(
            "和字幕一样：课时2.mp4 旁边放课时2.jpg 就会自动配上，"
            "每个文件各用各的。.jpg / .jpeg / .png / .webp", "Hint", wrap=True))

        # ---------- ② 全局封面兜底 ----------
        body.addWidget(label("没匹配到同名图片时，用这一张（可选）", "HintStrong"))

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(14)

        self.thumb = CoverThumb()
        rl.addWidget(self.thumb)

        right = QVBoxLayout()
        right.setSpacing(5)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_pick = QPushButton("选择图片…")
        self.btn_pick.clicked.connect(self._pick)
        self.btn_clear = QPushButton("移除")
        self.btn_clear.clicked.connect(self._clear_cover)
        btns.addWidget(self.btn_pick)
        btns.addWidget(self.btn_clear)
        btns.addStretch(1)
        right.addLayout(btns)

        self.lbl_meta = label("", "Hint", wrap=True)
        right.addWidget(self.lbl_meta)
        self.lbl_delta = label("", "HintStrong", wrap=True)
        right.addWidget(self.lbl_delta)
        self.lbl_advice = label("", "Warn", wrap=True)
        right.addWidget(self.lbl_advice)
        right.addStretch(1)
        rl.addLayout(right, 1)
        body.addWidget(row)

        # ---------- ③ 容器能力告警（可一键纠正）----------
        warn_row = QWidget()
        wl = QHBoxLayout(warn_row)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(8)
        self.lbl_container = label("", "Hint", wrap=True)
        wl.addWidget(self.lbl_container, 1)
        self.btn_switch_mp4 = QPushButton("改用 MP4")
        self.btn_switch_mp4.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_switch_mp4.clicked.connect(
            lambda: self.requestContainer.emit(Container.MP4))
        wl.addWidget(self.btn_switch_mp4)
        body.addWidget(warn_row)

        self.add(self._body)
        # 🔴 必须显式同步一次初始可见性：`setChecked(False)` 在值本来就是
        # False 时**不发 toggled**，于是 `_on_enable_toggled` 永远不执行，
        # 子区块就会带着默认的"可见"状态显示出来（实测就是这样：开关没勾、
        # 下面却摆着完整的封面设置）。
        self._body.setVisible(self.chk_enable.isChecked())
        self._sync()
        self.set_container(Container.MKV)

    # ------------------------------------------------------------------ #
    def set_container(self, container: Container) -> None:
        """容器变了就更新可用性与说明。"""
        self._container = container
        support = cover_support(container)
        name = container.value.upper()

        if support == "full":
            # MP4：一切正常，说清"两个地方都会变"就够了，不喧宾夺主
            self.lbl_container.setText(
                "MP4 会把封面同时写进播放器海报和 Windows 资源管理器缩略图。")
            self.lbl_container.setObjectName("Hint")
            self.btn_switch_mp4.setVisible(False)
        elif support == "player":
            # 🔴 MKV 是实测踩过的坑：封面只进附件，缩略图不变。
            # 这里必须是橙色告警 + 可点的纠正动作，不能只是一行灰字 ——
            # 用户选容器时根本不会去读那片灰字（他实测就是这么漏掉的）。
            # 但**没选封面时不该报红**：用户还没做任何错事，先挨一个警告
            # 会让正常路径看起来像出错。只在"确实会有封面"时告警，
            # 由 refresh_state() 在选图/检测到同名图后再点亮。
            self.lbl_container.setText(
                f"⚠️ {name} 的封面只在播放器里可见，"
                "Windows 资源管理器缩略图不会变。")
            self.lbl_container.setObjectName("Hint")
            self.btn_switch_mp4.setVisible(False)
        else:
            # 🔴 MOV / WebM 是**完全装不下**封面，和 MKV 的"能加但缩略图不变"
            # 是两回事，文案必须区分开，否则用户会以为都是"部分支持"。
            # 实测：MOV 里 ffmpeg 日志显示写入了，但 muxer 不落盘
            # （换 tag 也没用）；WebM 容器本身没有封面槽位。
            reason = ("MOV 容器里存不住封面（ffmpeg 写入后不会落盘）"
                      if container == Container.MOV
                      else "WebM 容器没有封面槽位")
            self.lbl_container.setText(
                f"⛔ {name} 完全不支持封面：{reason}。"
                "选了也不会加，请改用 MP4。")
            self.lbl_container.setObjectName("Warn")
            self.btn_switch_mp4.setVisible(True)
        self.lbl_container.style().unpolish(self.lbl_container)
        self.lbl_container.style().polish(self.lbl_container)
        self._sync()

    # ------------------------------------------------------------------ #
    def refresh_state(self, paths: list) -> None:
        """按"这批文件到底会不会加封面"来点亮/熄灭容器告警。

        容器本身不是问题，**有封面 + 容器不合适**才是问题。用户刚进这一页、
        还没拖文件时先挂一个橙色警告，会让默认路径看起来像出错。

        MKV（player）：能加，但缩略图不变 → 橙色警告 + 「改用 MP4」
        MOV/WebM（none）：完全加不上 → 也是警告（此时"加不上"是真会发生的事，
                          用户旁边有同名图却拿不到封面，比 MKV 更该说）
        """
        support = cover_support(self._container)
        if support == "full":
            # MP4 一切都好，不显示切换按钮
            self.lbl_container.setObjectName("Hint")
            self.btn_switch_mp4.setVisible(False)
            self.lbl_container.style().unpolish(self.lbl_container)
            self.lbl_container.style().polish(self.lbl_container)
            return

        will_add = self.enabled() and (
            self._global_cover is not None
            or (self.chk_auto.isChecked() and self.has_same_name_cover(paths)))
        if will_add:
            self.lbl_container.setObjectName("Warn")
            self.btn_switch_mp4.setVisible(True)
        else:
            self.lbl_container.setObjectName("Hint")
            # 🔴 MOV/WebM 的切换按钮即使在没检测到封面时也留着：
            # 它没有副作用（就是换个容器），而且用户可能下一步才选图。
            self.btn_switch_mp4.setVisible(support == "none" and self.enabled())
        self.lbl_container.style().unpolish(self.lbl_container)
        self.lbl_container.style().polish(self.lbl_container)

    # ------------------------------------------------------------------ #
    def enabled(self) -> bool:
        """主开关是否打开。"""
        return self.chk_enable.isChecked()

    def auto_cover(self) -> bool:
        return self.enabled() and self.chk_auto.isChecked() and \
            cover_support(self._container) != "none"

    def global_cover(self) -> Path | None:
        return self._global_cover if self.enabled() else None

    def cover_for(self, src: Path) -> Path | None:
        """这个文件最终会用哪张封面（UI 汇总用，与 pipeline 同一套规则）。"""
        if cover_support(self._container) == "none" or not self.enabled():
            return None
        if self.auto_cover():
            found = find_cover(src)
            if found is not None:
                return found
        return self._global_cover

    def has_same_name_cover(self, paths: list) -> bool:
        """这批文件里有没有任何一个是能找到同名图的（用于告警文案）。"""
        return any(find_cover(p) is not None for p in paths)

    # ------------------------------------------------------------------ #
    def _on_enable_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self._sync()
        self.changed.emit()

    def _pick(self) -> None:
        start = str(self._global_cover.parent) if self._global_cover else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", start, _IMAGE_FILTER)
        if not path:
            return
        self._global_cover = Path(path)
        self._sync()
        self.changed.emit()

    def _clear_cover(self) -> None:
        self._global_cover = None
        self._sync()
        self.changed.emit()

    # ------------------------------------------------------------------ #
    def _sync(self) -> None:
        support = cover_support(self._container)
        unavailable = support == "none"
        disabled = unavailable or not self.enabled()

        for w in (self.chk_auto, self.btn_pick):
            w.setEnabled(not disabled)

        self.thumb.set_image(self._global_cover if self.enabled() else None)

        if self._global_cover is None:
            self.lbl_meta.setText(
                "未选择 —— 没匹配到同名图片的文件就不加封面")
            self.lbl_delta.setText("")
            self.lbl_advice.setText("")
            self.btn_clear.setEnabled(False)
            return

        # 有全局封面但主开关关着 / 容器不支持时，只是"存着但不用"
        self.btn_clear.setEnabled(True)
        try:
            size = self._global_cover.stat().st_size
        except OSError:
            size = 0
        self.lbl_meta.setText(
            f"当前：{self._global_cover.name} · {human_size(size)}")

        if disabled:
            self.lbl_delta.setText("")
            self.lbl_advice.setText("")
            return

        self.lbl_delta.setText(
            f"封面会原样写入，输出增加约 {human_size(size)}" if size else "")
        self.lbl_advice.setText(
            "缩略图最多显示 256px，换一张 1280×720 左右的图效果一样、文件小得多。"
            if size > BIG_COVER_BYTES else "")

    def size_warning(self, cover_bytes: int, output_bytes: int) -> str:
        """超过阈值才返回警告文本（队列汇总用）。"""
        if not cover_bytes or not output_bytes:
            return ""
        if cover_bytes <= output_bytes * COVER_SIZE_WARN_RATIO:
            return ""
        return (f"封面 {human_size(cover_bytes)}，占输出的 "
                f"{cover_bytes / output_bytes:.0%}")