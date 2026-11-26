"""抽取拆分页：把画面 / 音轨 / 字幕分别拆成独立文件。

设计稿见 `_design/extract-split.html`。三条核心设计：

  1. **一律 copy 不重编码** —— 拆出来的画面和音轨与原文件完全一致，
     秒级完成。想转格式请用「封装混流」。
  2. **复选框 = 多条任务** —— 勾 3 条流产出 3 个文件，但仍然是
     3 个独立 Task，每个只产出一个文件。任务模型不用改。
  3. **输出名带类型和序号** —— 课时2_画面.mp4 / 课时2_音轨1.m4a /
     课时2_字幕1.srt。撞名时不会退化成 _1 / _2 这种分不清的名字。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QVBoxLayout,
                               QWidget)

from ...core.ffmpeg import MediaInfo
from ...core.pipeline import build_extract_tasks
from ...core.utils import AUDIO_EXTS, VIDEO_EXTS, human_size
from ..widgets import Card, Combo, FileArea, FileTable, label
from .shared import ActionBar, OutputRow, Page, make_file_toolbar

#: 流类型 → 界面上的标签。视频叫「画面」而不是「视频」：
#: 对用户来说"从视频里抽画面"比"抽视频流"好懂。
_KIND_LABEL = {"video": "画面", "audio": "音轨", "subtitle": "字幕"}
_KIND_ORDER = {"video": 0, "audio": 1, "subtitle": 2}

#: 同名文件已存在时的行为
COLLISION_MODES = [
    ("rename", "自动改名（加 _1）"),
    ("overwrite", "直接覆盖"),
]


class StreamRow(QWidget):
    """一条流的勾选行。"""

    toggled = Signal()

    def __init__(self, kind: str, index: int, title: str, detail: str,
                 out_name: str, size_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("StreamRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.kind = kind
        self.index = index

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        self.chk = QCheckBox()
        self.chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk.toggled.connect(lambda _: self._sync())
        self.chk.toggled.connect(lambda _: self.toggled.emit())
        lay.addWidget(self.chk)

        badge = QLabel(_KIND_LABEL.get(kind, kind))
        badge.setObjectName(f"StreamKind_{kind}")
        badge.setFixedHeight(18)
        lay.addWidget(badge)

        name = label(title, "StreamName")
        lay.addWidget(name)

        if detail:
            lay.addWidget(label(detail, "StreamDetail"))

        lay.addStretch(1)

        # 目标文件名（等宽字体，一眼能看出会不会撞名）
        out = label(out_name, "StreamOut")
        lay.addWidget(out)
        if size_text:
            lay.addWidget(label(size_text, "StreamSize"))

        self._sync()

    def is_checked(self) -> bool:
        return self.chk.isChecked()

    def _sync(self) -> None:
        on = self.chk.isChecked()
        if self.property("picked") == on:
            return
        self.setProperty("picked", on)
        self.style().unpolish(self)
        self.style().polish(self)


class ExtractPage(Page):
    """抽取拆分页。"""

    requestQueue = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, "抽取拆分",
                         "把源文件里的画面、音轨、字幕分别拆成独立文件。"
                         "不重新编码，秒级完成。")

        self.table = FileTable(ctx.env_provider, VIDEO_EXTS | AUDIO_EXTS)
        area_files = FileArea(
            self.table, make_file_toolbar(self.table, VIDEO_EXTS | AUDIO_EXTS))

        self.bar = ActionBar("开始拆分", OutputRow(ctx))
        self.bar.button.clicked.connect(self._on_start)

        self.rows: list[StreamRow] = []
        self._current_path: Path | None = None

        self.root.addWidget(area_files)
        self.root.addWidget(self._build_streams_card())
        self.root.addWidget(self._build_output_card())
        self.root.addWidget(self.bar)
        self.root.addStretch(1)

        self.table.filesChanged.connect(self._on_files_changed)
        self._update_hint()

    # ------------------------------------------------------------------ #
    def _build_streams_card(self) -> QWidget:
        self.streams_card = Card()
        self.streams_card.add(label("要拆出哪些", "SectionTitle"))
        self.lbl_streams_hint = label(
            "可多选：每个勾选项单独产出一个文件，原文件不动。", "Hint")
        self.streams_card.add(self.lbl_streams_hint)

        self.rows_holder = QWidget()
        self.rows_lay = QVBoxLayout(self.rows_holder)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(6)
        self.streams_card.add(self.rows_holder)

        self.lbl_empty = label("", "Hint", wrap=True)
        self.streams_card.add(self.lbl_empty)
        return self.streams_card

    def _build_output_card(self) -> QWidget:
        card = Card()
        card.add(label("输出", "SectionTitle"))
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        rl.addWidget(label("同名文件已存在时"))
        self.cmb_collision = Combo()
        for value, text in COLLISION_MODES:
            self.cmb_collision.addItem(text, value)
        self.cmb_collision.setFixedWidth(200)
        self.cmb_collision.currentIndexChanged.connect(lambda _: self._update_hint())
        rl.addWidget(self.cmb_collision)
        rl.addStretch(1)
        card.add(row)
        card.add(label("所有流都原样复制，不重新编码 —— 拆出来的内容与源文件完全一致。"
                       "想转格式请用「封装混流」。", "Hint", wrap=True))
        return card

    # ------------------------------------------------------------------ #
    # 流列表
    # ------------------------------------------------------------------ #
    def _clear_rows(self) -> None:
        for w in self.rows:
            w.setParent(None)
            w.deleteLater()
        self.rows.clear()

    def _on_files_changed(self) -> None:
        """文件列表变了：按第一个文件重建流列表。

        🔴 `filesChanged` 会被发**两次**：添加文件时一次（此时 info 还是
        None，探测在后台跑）、探测完成时一次。所以不能只在"路径变了"时
        重建 —— 那样第二次会被跳过，列表永远是空的（实测踩到）。
        这里按「路径 + 有没有 info」共同判断。
        """
        paths = self.table.paths
        if not paths:
            self._current_path = None
            self._clear_rows()
            self.lbl_empty.setText("")
            self._update_hint()
            return
        path = paths[0]
        info = self.table.info_for(path)
        key = (path, info is not None)
        if key != getattr(self, "_last_key", None):
            self._last_key = key
            self._current_path = path
            self._rebuild_rows(path)
        self._update_hint()

    def _rebuild_rows(self, path: Path) -> None:
        self._clear_rows()
        info = self.table.info_for(path)
        if info is None:
            self.lbl_empty.setText("正在读取文件信息…")
            return
        if not info.ok:
            self.lbl_empty.setText(
                info.error or "读不出这个文件的信息 —— 可能已损坏，"
                              "或不是 ffmpeg 支持的格式。")
            return

        entries = self._stream_entries(path, info)
        if not entries:
            self.lbl_empty.setText("这个文件里没有可拆出的流。")
            return

        self.lbl_empty.setText("")
        for kind, index, title, detail, out_name, size_text in entries:
            row = StreamRow(kind, index, title, detail, out_name, size_text)
            row.toggled.connect(self._update_hint)
            self.rows_lay.addWidget(row)
            self.rows.append(row)
        # 默认全选：用户来这一页就是要拆，默认不勾会让他多点一次
        for row in self.rows:
            row.chk.setChecked(True)

    def _stream_entries(self, path: Path, info: MediaInfo) -> list[tuple]:
        """把 MediaInfo 转成界面行。序号按源的绝对 index 排序，稳定。"""
        # 延迟导入避免顶层循环依赖
        from ...core.commands import audio_ext_for, subtitle_ext_for

        out: list[tuple] = []
        counters = {"video": 0, "audio": 0, "subtitle": 0}

        videos = [info.video] if info.video else []
        groups = (("video", videos), ("audio", info.audios),
                  ("subtitle", info.subtitles))
        # 🔴 按源的绝对 index 统一排序，这样「音轨1/音轨2」的编号
        # 和源文件里流的顺序一致，不受 MediaInfo 内部分组影响。
        flat = [(kind, s) for kind, pool in groups for s in pool if s]
        flat.sort(key=lambda kv: kv[1].index)

        for kind, s in flat:
            counters[kind] += 1
            n = counters[kind]
            if kind == "video":
                title = f"#{s.index} · {s.codec_name.upper()} {s.resolution}"
                detail = f"{s.fps:g} fps" if s.fps else ""
                ext = path.suffix.lstrip(".").lower() or "mp4"
                tag = "画面"
            elif kind == "audio":
                title = f"#{s.index} · {s.codec_name.upper()} {s.channel_text}"
                if s.language:
                    title += f" · {s.language}"
                detail = " · ".join(x for x in (
                    f"{s.sample_rate / 1000:g} kHz" if s.sample_rate else "",
                    f"{int(s.bit_rate / 1000)} kbps" if s.bit_rate else "",
                ) if x)
                ext = audio_ext_for(s.codec_name).lstrip(".")
                tag = f"音轨{n}"
            else:
                title = f"#{s.index} · {s.codec_name.upper()}"
                if s.language:
                    title += f" · {s.language}"
                # 图形字幕明确标注，否则用户会奇怪为什么抽出来的不是文本
                if not _is_text(s.codec_name):
                    title += "（图形字幕，原样抽取）"
                detail = ""
                ext = subtitle_ext_for(s.codec_name).lstrip(".")
                tag = f"字幕{n}"

            out_name = f"{path.stem}_{tag}.{ext}"
            size_text = self._estimate_size(info, s, kind)
            out.append((kind, s.index, title, detail, out_name, size_text))
        return out

    @staticmethod
    def _estimate_size(info: MediaInfo, stream, kind: str) -> str:
        """按「流码率 × 时长」估算。拿不到码率就不显示 —— 不硬凑。

        🔴 不能用"占源文件的百分比"：凑出来的数字会让用户以为
        "拆出来能省一半"，实际不是（各流大小之和 ≈ 源文件）。
        """
        if kind == "subtitle":
            return ""
        br = getattr(stream, "bit_rate", 0) or 0
        dur = info.duration or 0
        if not (br and dur):
            return ""
        return human_size(int(br * dur / 8))

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _selected(self) -> list[tuple[str, int]]:
        return [(r.kind, r.index) for r in self.rows if r.is_checked()]

    def _update_hint(self) -> None:
        paths = self.table.paths
        if not paths:
            self.bar.set_hint("还没有添加文件 —— 拖入视频或音频即可")
            self.bar.button.setEnabled(False)
            return
        if not self.ctx.env.ready:
            self.bar.set_hint("未找到 ffmpeg，请到「功能设置」里指定路径", "Danger")
            self.bar.button.setEnabled(False)
            return

        sel = self._selected()
        if not sel:
            self.bar.set_hint("先勾选要拆出的内容")
            self.bar.button.setEnabled(False)
            return

        self.bar.button.setEnabled(True)
        counts = {"video": 0, "audio": 0, "subtitle": 0}
        for kind, _ in sel:
            counts[kind] += 1
        bits = []
        if counts["video"]:
            bits.append("画面")
        if counts["audio"]:
            bits.append(f"音轨 {counts['audio']} 条")
        if counts["subtitle"]:
            bits.append(f"字幕 {counts['subtitle']} 条")
        per = len(paths)
        total = len(paths) * len(sel)
        prefix = (f"共 {per} 个文件 · " if per > 1 else "")
        self.bar.set_hint(
            f"{prefix}每个文件拆出「{' + '.join(bits)}」"
            f" → 共 {total} 个文件")

    # ------------------------------------------------------------------ #
    def _on_start(self) -> None:
        paths = self.table.paths
        sel = self._selected()
        if not paths or not sel or not self.ctx.env.ready:
            return

        suffix = ""        # 抽取不加「_封装」这类后缀，靠 _画面/_音轨1 区分
        overwrite = self.cmb_collision.currentData() == "overwrite"

        tasks = []
        for path in paths:
            info = self.table.info_for(path)
            tasks += build_extract_tasks(self.ctx, path, info, sel,
                                         suffix=suffix)

        self.ctx.queue.add_many(tasks)
        self.table.clear_all()
        self.requestQueue.emit()


def _is_text(codec: str) -> bool:
    from ...core.commands import is_text_subtitle
    return is_text_subtitle(codec)