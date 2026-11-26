"""媒体信息页：拖入文件，显示它的全部参数（不改变任何东西）。

对应老版的 MediaInfo 功能。设计与字段来源见 `_design/media-info.html`。

🔴 这一页只读：不发任务、不写文件、不进队列。唯一的"动作"是
   「拿去压缩」跳转（把当前文件带到压缩页）和「播放」（系统默认播放器）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from ...core.ffmpeg import MediaInfo, StreamInfo, probe
from ...core.utils import AUDIO_EXTS, VIDEO_EXTS
from ..widgets import Card, FileArea, FileTable, label
from .shared import Page, make_file_toolbar

#: 这一页接受的文件比压缩页更宽：纯音频也要能读（老版能读 FLAC/MP3）
INFO_EXTS = VIDEO_EXTS | AUDIO_EXTS


#: ffprobe 的 codec_name → 用户认得的显示名。
#: 只列容易看懵的几个：h264 显示成 AVC（老版就是这个写法），
#: hevc 显示成 HEVC，其余直接用大写原名（aac → AAC 已经对了）。
CODEC_DISPLAY = {
    "h264": "AVC",
    "hevc": "HEVC",
    "av1": "AV1",
    "vp9": "VP9",
    "subrip": "SRT",
    "ass": "ASS",
    "mov_text": "MP4 字幕",
}


def _codec_display(codec_name: str) -> str:
    name = (codec_name or "").lower()
    return CODEC_DISPLAY.get(name, (codec_name or "").upper())


def _fmt_bitrate(bps: int) -> str:
    """1790000 → '1.79 Mbps'；48000 → '48 kb/s'（老版用的就是 kb/s 写法）。"""
    if not bps:
        return ""
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    return f"{bps / 1000:.0f} kb/s"


def _fmt_size(num: int) -> str:
    """字节数 → 人话（老版用的是 KiB/MiB，这里沿用同一套单位）。

    ⚠️ 不直接用 `utils.human_size`：那个返回 "KB"（1000 进制习惯）且保留
    两位小数（"179.69 KB"），而这一页**流大小**要和老版对得上
    （老版写 185 KiB / 6.46 MiB），所以自己按 1024 进制算。
    """
    if not num or num <= 0:
        return ""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    val = float(num)
    idx = 0
    while val >= 1024 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(val)} B"
    if val >= 100:
        return f"{val:.0f} {units[idx]}"
    if val >= 10:
        return f"{val:.1f} {units[idx]}"
    return f"{val:.2f} {units[idx]}"


def _fmt_duration(seconds: float) -> str:
    """31.208 → '31 s 208 ms'；4620 → '3 min 31 s 160 ms'（老版写法）。"""
    if not seconds or seconds <= 0:
        return ""
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    parts: list[str] = []
    if h:
        parts.append(f"{h} h")
    if m:
        parts.append(f"{m} min")
    if s or (not h and not m):
        parts.append(f"{s} s")
    if ms:
        parts.append(f"{ms:03d} ms")
    return " ".join(parts)


class InfoRow(QWidget):
    """一行「标签 + 值」。标签定宽，保证跨卡片对齐。"""

    def __init__(self, caption: str, value: str = "", mono: bool = False,
                 parent=None):
        super().__init__(parent)
        # 高度必须稳定：关掉 WordWrap 并固定行高。
        # 🔴 踩过的坑：开 WordWrap(True) 后 QLabel 首次布局按较窄宽度计算，
        # 判定"要换行"，行高在 19/30/36/53 之间乱跳，
        # 结果每个卡片高度都对不上、内容被裁。这些值都很短，不需要换行。
        self.setFixedHeight(20)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.cap = label(caption, "InfoCaption")
        self.cap.setFixedWidth(76)
        lay.addWidget(self.cap)
        self.val = QLabel(value)
        self.val.setObjectName("InfoValueMono" if mono else "InfoValue")
        self.val.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.val.setWordWrap(False)
        self.val.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.val, 1)

    def set_value(self, text: str) -> None:
        self.val.setText(text or "—")

    def hide_if_empty(self, text: str) -> None:
        self.setVisible(bool(text))
        if text:
            self.val.setText(text)


class InfoCard(Card):
    """带标题与徽标的卡片。

    行按 (行号, 列) 排：`column=0` 左栏、`column=1` 右栏，行号在各列内递增。
    这样一个卡片能摆成「左 4 行 / 右 3 行」的紧凑两列，而不是一长条。
    """

    def __init__(self, title: str, badge: str = "", parent=None):
        super().__init__(parent)
        self._rows: list[InfoRow] = []
        self._extras: list[QWidget] = []
        self._badge: QLabel | None = None

        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        if badge:
            self._badge = label(badge, "InfoBadge")
            hl.addWidget(self._badge)
        self._title = label(title, "SectionTitle")
        hl.addWidget(self._title)
        hl.addStretch(1)
        self.add(head)

        self._body = QWidget()
        self._grid = QGridLayout(self._body)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(26)
        self._grid.setVerticalSpacing(0)
        self.add(self._body)

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def add_row(self, caption: str, value: str = "", mono: bool = False,
                column: int = 0) -> InfoRow:
        row = InfoRow(caption, value, mono)
        self._rows.append(row)
        self._grid.addWidget(row, self._next_row(column), column)
        return row

    def add_extra(self, widget: QWidget) -> None:
        """整行宽的小部件（音轨分隔标题等），插在网格最后一行。"""
        self._extras.append(widget)
        r = max((self._grid.getItemPosition(i)[0] + 1
                 for i in range(self._grid.count())), default=0)
        self._grid.addWidget(widget, r, 0, 1, 2)

    def _next_row(self, column: int) -> int:
        n = 0
        for i in range(self._grid.count()):
            _r, c, _rs, _cs = self._grid.getItemPosition(i)
            if c == column:
                n = max(n, _r + 1)
        return n

    def rows(self) -> list[InfoRow]:
        return list(self._rows)

    def clear(self) -> None:
        """清空所有内容行，供重复渲染复用。"""
        for w in self._rows + self._extras:
            w.setParent(None)
            w.deleteLater()
        self._rows.clear()
        self._extras.clear()
        while self._grid.count():
            self._grid.takeAt(0)


class MediaInfoPage(Page):
    """媒体信息页（对应老版 MediaInfo 功能）。"""

    #: 「拿去压缩」携带文件路径，由主窗口接到压缩页
    requestCompress = Signal(list)

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, "媒体信息",
                         "拖入视频或音频，立刻看到它的全部参数。只读，不会改动原文件。")

        # ---------------- 文件区（拖放入口） ---------------- #
        self.table = FileTable(ctx.env_provider, INFO_EXTS)
        # 🔴 不要 setMaximumHeight 压表格：
        # 实测把上限设到 140 会把 FileTable 自己画的空态提示
        # （「这里可以放文件」那两行）裁掉，表格看起来像一条空白带。
        # 高度交给 FileArea 的 stretch=0 去自然决定（它按 sizeHint 走）。
        self.area = FileArea(
            self.table, make_file_toolbar(self.table, INFO_EXTS))
        # 这一页只看第一个文件；多选时也只显示最后一个拖入的
        self.table.filesChanged.connect(self._on_files_changed)
        # 🔴 stretch=0：文件区只是"入口"，不跟信息区抢高度。
        # 给它 stretch 会让它在有文件时撑满整页，把下面的信息卡挤没。
        self.root.addWidget(self.area)

        # ---------------- 信息区 ---------------- #
        self._scroll_holder = QWidget()
        self._holder_lay = QVBoxLayout(self._scroll_holder)
        self._holder_lay.setContentsMargins(0, 0, 0, 0)
        self._holder_lay.setSpacing(10)
        # 信息区拿全部剩余空间（这一页的主角）
        self.root.addWidget(self._scroll_holder, 1)

        # 文件条
        self.bar_file = QFrame()
        self.bar_file.setObjectName("FileBar")
        fb = QHBoxLayout(self.bar_file)
        fb.setContentsMargins(14, 11, 14, 11)
        fb.setSpacing(10)
        self.lbl_name = label("", "FileBarName")
        self.lbl_path = label("", "FileBarPath")
        name_box = QVBoxLayout()
        name_box.setSpacing(1)
        name_box.addWidget(self.lbl_name)
        name_box.addWidget(self.lbl_path)
        fb.addLayout(name_box, 1)
        self.lbl_quick = label("", "FileBarMeta")
        fb.addWidget(self.lbl_quick)
        self._holder_lay.addWidget(self.bar_file)

        # 卡片网格
        self.grid_holder = QWidget()
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self._holder_lay.addWidget(self.grid_holder)

        self.card_video = InfoCard("视频", "视频")
        self.card_container = InfoCard("容器", "文件")
        self.card_audio = InfoCard("音频", "音频")
        self.card_sub = InfoCard("字幕", "字幕")
        self.card_color = InfoCard("色彩元数据", "高级")
        self._cards = [self.card_video, self.card_container,
                       self.card_audio, self.card_sub, self.card_color]
        for c in self._cards:
            c.setVisible(False)

        # 错误卡
        self.card_error = Card()
        self.lbl_error = label("", "Danger", wrap=True)
        self.lbl_error_hint = label("", "Hint", wrap=True)
        self.card_error.add(self.lbl_error)
        self.card_error.add(self.lbl_error_hint)
        self.card_error.setVisible(False)
        self._holder_lay.addWidget(self.card_error)

        self._holder_lay.addStretch(1)

        # ---------------- 底部操作栏 ---------------- #
        self.bar = QWidget()
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(12)
        self.hint = label("只读取信息，不会改动原文件。", "Hint")
        self.hint.setWordWrap(True)
        bl.addWidget(self.hint, 1)

        # 按钮从次要到主要：复制 / 播放 / 拿去压缩。
        # 「拿去压缩」是这一页最有价值的出路（否则用户看完还得手动
        # 去压缩页再拖一遍），所以给它主色 + 最右位置，和别的页
        # 「开始压缩」一样是视觉终点。
        self.btn_copy = QPushButton("复制信息")
        self.btn_copy.clicked.connect(self._on_copy)
        bl.addWidget(self.btn_copy)

        self.btn_play = QPushButton("播放")
        self.btn_play.clicked.connect(self._on_play)
        bl.addWidget(self.btn_play)

        self.btn_compress = QPushButton("拿去压缩")
        self.btn_compress.setObjectName("Primary")
        self.btn_compress.setMinimumWidth(140)
        self.btn_compress.clicked.connect(self._on_compress)
        bl.addWidget(self.btn_compress)

        self.root.addWidget(self.bar)

        self._info: MediaInfo | None = None
        self._sync_buttons()
        self._show_empty()
        self.bar_file.setVisible(False)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _show_empty(self) -> None:
        self.grid_holder.setVisible(False)
        for c in self._cards:
            c.setVisible(False)
        self.card_error.setVisible(False)
        self.hint.setText("把文件拖到上面的区域，或点「添加文件」。"
                          "只读取信息，不会改动原文件。")
        # 🔴 空态时把信息区整个收起来，把高度让给文件区。
        # 不这么做的话，下面会留 400+ px 纯白 —— 看着像界面没加载完。
        self._scroll_holder.setVisible(False)

    def _sync_buttons(self) -> None:
        has = self._info is not None and self._info.ok
        has_path = self._current_path() is not None
        self.btn_compress.setEnabled(has_path)
        self.btn_play.setEnabled(has_path)
        self.btn_copy.setEnabled(has)

    def _current_path(self) -> Path | None:
        paths = self.table.paths
        return paths[-1] if paths else None

    def _on_files_changed(self) -> None:
        """表格刚加了文件。等它的异步探测回调 `_on_info` 来填内容。

        这里只处理"文件被清空"的情形；判断依据不能是 `_info`，
        因为清空时旧 info 还在。
        """
        if not self.table.paths:
            self._info = None
            self.bar_file.setVisible(False)
            self._show_empty()
            self._sync_buttons()
            return
        self._sync_buttons()

    def on_probe_done(self, path_str: str, info: MediaInfo) -> None:
        """FileArea/FileTable 探测完成后调用（由 main_window 接线）。"""
        cur = self._current_path()
        if cur is None or str(cur) != path_str:
            return
        self._info = info
        self._render(info)
        self._sync_buttons()

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _render(self, info: MediaInfo) -> None:
        p = info.path
        self._scroll_holder.setVisible(True)
        self.bar_file.setVisible(True)
        self.lbl_name.setText(p.name)
        self.lbl_path.setText(str(p))
        self.lbl_path.setToolTip(str(p))
        quick = [_fmt_duration(info.duration), _fmt_size(info.size),
                 _fmt_bitrate(info.bit_rate)]
        self.lbl_quick.setText("   ·   ".join(x for x in quick if x))

        if not info.ok:
            self.grid_holder.setVisible(False)
            for c in self._cards:
                c.setVisible(False)
            self.card_error.setVisible(True)
            self.lbl_error.setText(
                info.error or "读不出这个文件的信息 —— 可能已损坏，"
                              "或不是 ffmpeg 支持的格式。")
            self.lbl_error_hint.setText(
                "换一个文件试试。如果确定文件没问题，可能是内置 ffmpeg 出了状况："
                "点「功能设置 → 重新检测」恢复。")
            return

        self.card_error.setVisible(False)
        self.grid_holder.setVisible(True)
        self._fill_container(info)
        self._fill_video(info)
        self._fill_audio(info)
        self._fill_subtitles(info)
        self._fill_color(info)
        self._relayout()

    def _fill_container(self, info: MediaInfo) -> None:
        c = self.card_container
        self._reset(c)
        long_name = info.format_long_name or ""
        c.set_title(f"{info.format_name.upper() or '容器'}"
                         + (f" · {long_name}" if long_name else ""))
        c.add_row("容器格式", f"{info.format_name.upper()} ({long_name})"
                   if long_name else info.format_name.upper())
        c.add_row("总码率", _fmt_bitrate(info.bit_rate))
        c.add_row("总时长", _fmt_duration(info.duration))
        c.add_row("文件大小", _fmt_size(info.size))
        n_v = 1 if info.video else 0
        c.add_row("流数量", f"{n_v} 视频 · {len(info.audios)} 音频 · "
                             f"{len(info.subtitles)} 字幕")
        c.add_row("写入程序", info.encoder, mono=True, column=1)
        c.setVisible(True)

    def _fill_video(self, info: MediaInfo) -> None:
        v = info.video
        if not v:
            self.card_video.setVisible(False)
            return
        c = self.card_video
        self._reset(c)
        prof = v.profile
        lvl = v.level_text
        suffix = ""
        if prof and lvl:
            suffix = f" {prof} @ {lvl}"
        elif prof:
            suffix = f" {prof}"
        c.set_title(f"{v.resolution} · {v.fps:g}fps" if v.fps
                         else v.resolution)

        c.add_row("编码", f"{_codec_display(v.codec_name)}{suffix}")
        c.add_row("分辨率", v.resolution)
        c.add_row("画面比例", v.aspect_ratio_text)
        c.add_row("像素宽高比", v.sample_aspect_ratio.replace(":", ":")
                   if v.sample_aspect_ratio else "")
        fps_txt = f"{v.fps:.3f} FPS".replace(".000", "") if v.fps else ""
        if fps_txt and v.fps_mode_text:
            fps_txt = f"{v.fps:g} FPS（{v.fps_mode_text}）"
        c.add_row("帧率", fps_txt)
        c.add_row("色彩空间", " · ".join(x for x in
                   (v.color_space_text, v.chroma_text) if x))
        c.add_row("位深度", v.bit_depth_text)
        c.add_row("扫描方式", v.field_order_text)
        c.add_row("视频码率", _fmt_bitrate(v.bit_rate), column=1)
        share = info.stream_share(v)
        size_b = info.stream_bytes(v)
        c.add_row("流大小",
                  f"{_fmt_size(size_b)}（占 {share:.0%}）" if size_b else "",
                  column=1)
        c.add_row("总帧数", f"{v.nb_frames:,}" if v.nb_frames else "",
                  column=1)
        c.add_row("编码时间", v.creation_text, mono=True, column=1)
        c.setVisible(True)

    def _fill_audio(self, info: MediaInfo) -> None:
        c = self.card_audio
        self._reset(c)
        if not info.audios:
            c.setVisible(False)
            return
        a = info.audios[0]
        title = _codec_display(a.codec_name)
        if a.profile:
            title += f" · {a.profile}"
        if a.channel_text:
            title += f" · {a.channel_text}"
        if a.sample_rate:
            title += f" · {a.sample_rate / 1000:g} kHz"
        c.set_title(title)

        for i, a in enumerate(info.audios):
            if i:
                sep = label(f"音轨 #{a.index}", "InfoSubHead")
                c.add_extra(sep)
            prefix = f"#{a.index} " if len(info.audios) > 1 else ""
            c.add_row("编码", f"{prefix}{_codec_display(a.codec_name)}"
                       + (f" · {a.profile}" if a.profile else ""))
            c.add_row("声道", f"{a.channel_text}（{a.channels}ch）"
                       if a.channels else a.channel_text)
            c.add_row("采样率", f"{a.sample_rate / 1000:g} kHz"
                       if a.sample_rate else "")
            c.add_row("位深度", a.bit_depth_text, column=1)
            c.add_row("音频码率", _fmt_bitrate(a.bit_rate), column=1)
            share = info.stream_share(a)
            size_b = info.stream_bytes(a)
            c.add_row("流大小",
                      f"{_fmt_size(size_b)}（占 {share:.0%}）" if size_b else "",
                      column=1)
            if a.language:
                c.add_row("语言", a.language, column=1)
        c.setVisible(True)

    def _fill_subtitles(self, info: MediaInfo) -> None:
        c = self.card_sub
        self._reset(c)
        if not info.subtitles:
            c.setVisible(False)
            return
        c.set_title(f"{len(info.subtitles)} 条字幕轨")
        for i, s in enumerate(info.subtitles):
            if i:
                c.add_extra(label(f"字幕 #{s.index}", "InfoSubHead"))
            c.add_row("编码", _codec_display(s.codec_name))
            c.add_row("语言", s.language or "未标注")
            c.add_row("轨道号", f"#{s.index}", column=1)
            c.add_row("默认", "是" if s.is_default else "否", column=1)
        c.setVisible(True)

    def _fill_color(self, info: MediaInfo) -> None:
        v = info.video
        if not v:
            self.card_color.setVisible(False)
            return
        c = self.card_color
        self._reset(c)
        rows = [
            ("色彩范围", v.color_range_text),
            ("色彩原色", v.color_primaries),
            ("传输特性", v.color_transfer),
            ("矩阵系数", v.color_space),
        ]
        shown = [r for r in rows if r[1]]
        if not shown:
            c.setVisible(False)
            return
        for i, (cap, val) in enumerate(shown):
            c.add_row(cap, val, column=i % 2)
        c.setVisible(True)

    def _reset(self, card: InfoCard) -> None:
        """清空卡片内容，供重新渲染复用。"""
        card.clear()

    def _relayout(self) -> None:
        """按实际存在的内容重新排卡片：有视频 → 两列，纯音频 → 单列。"""
        for i in reversed(range(self.grid.count())):
            self.grid.takeAt(i)
        col_count = 2 if self._info and self._info.video else 1
        visible = [c for c in (self.card_video, self.card_container)
                   if c.isVisible()]
        r = 0
        for i in range(0, len(visible), col_count):
            for j, c in enumerate(visible[i:i + col_count]):
                self.grid.addWidget(c, r, j)
            r += 1
        for c in (self.card_audio, self.card_sub, self.card_color):
            if c.isVisible():
                self.grid.addWidget(c, r, 0, 1, col_count)
                r += 1

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def _on_copy(self) -> None:
        if not self._info:
            return
        text = build_info_text(self._info)
        QGuiApplication.clipboard().setText(text)
        self.hint.setText("✓ 已复制到剪贴板。")

    def _on_play(self) -> None:
        p = self._current_path()
        if p is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _on_compress(self) -> None:
        p = self._current_path()
        if p is None:
            return
        self.requestCompress.emit([str(p)])


def display_width(text: str) -> int:
    """等宽字体下的显示宽度：中文/全角算 2 格，其余 1 格。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def pad_label(text: str, width: int = 10) -> str:
    """把标签按**显示宽度**补到 width 格。

    🔴 不能用 str.ljust(width)：「音频(1)」是 6 个字符但显示占 10 格，
    ljust 会再补 4 个空格 → 整块右移，下面各条音轨全歪。
    """
    return text + " " * max(0, width - display_width(text))


def build_info_text(info: MediaInfo) -> str:
    """把信息拼成可粘贴的纯文本（设计稿里的格式）。

    🔴 对齐用**固定宽度的标签列**而不是中文标签 + 空格：
    「文件名」「容器」「总码率」字数不同，中文字符在等宽字体里占两格，
    用空格硬凑的对齐一换字体就错位。这里统一按显示宽度补到 10 格。
    """
    def row(caption: str, value: str) -> str:
        return (pad_label(caption, 10) + value).rstrip()

    lines: list[str] = []
    lines.append(row("文件名", info.path.name))
    fmt = info.format_name.upper()
    if info.format_long_name:
        fmt += f" ({info.format_long_name})"
    lines.append(row("容器", fmt))
    if info.bit_rate:
        lines.append(row("总码率", _fmt_bitrate(info.bit_rate)))
    if info.size:
        lines.append(row("大小", _fmt_size(info.size)))
    if info.duration:
        lines.append(row("时长", _fmt_duration(info.duration)))
    lines.append("")

    v = info.video
    if v:
        prof = " ".join(x for x in (v.profile, v.level_text) if x)
        head = f"视频(1)   {_codec_display(v.codec_name)}"
        if prof:
            head += f" · {prof}"
        lines.append(head)
        detail = [v.resolution]
        if v.aspect_ratio_text:
            detail.append(f"画面比例 {v.aspect_ratio_text}")
        if v.sample_aspect_ratio:
            detail.append(f"像素宽高比 {v.sample_aspect_ratio}")
        lines.append("          " + " · ".join(detail))
        if v.fps:
            rate = f"{v.fps:g} FPS"
            if v.fps_mode_text:
                rate += f"（{v.fps_mode_text}）"
            if v.field_order_text:
                rate += f" · {v.field_order_text}"
            lines.append(f"          {rate}")
        color = [x for x in (v.color_space_text, v.chroma_text,
                             v.bit_depth_text, v.color_range_text) if x]
        if color:
            lines.append("          " + " · ".join(color))
        bits = []
        if v.bit_rate:
            bits.append(_fmt_bitrate(v.bit_rate))
        size_b = info.stream_bytes(v)
        if size_b:
            bits.append(f"{_fmt_size(size_b)}（占 {info.stream_share(v):.0%}）")
        if v.nb_frames:
            bits.append(f"{v.nb_frames:,} 帧")
        if bits:
            lines.append("          " + " · ".join(bits))
        if v.creation_text:
            lines.append(f"          编码时间 {v.creation_text}")
        lines.append("")

    INDENT = " " * 10

    for i, a in enumerate(info.audios):
        # 第一行带「音频(n)」标签，续行补等宽空白，让各条轨道的值都对齐
        head = f"音频({len(info.audios)})" if i == 0 else ""
        first = f"#{a.index}  {_codec_display(a.codec_name)}"
        if a.profile:
            first += f" · {a.profile}"
        if a.channel_text:
            first += f" · {a.channel_text}（{a.channels}ch）"
        if a.sample_rate:
            first += f" · {a.sample_rate / 1000:g} kHz"
        if a.bit_rate:
            first += f" · {_fmt_bitrate(a.bit_rate)}"
        lines.append(pad_label(head) + first)
        extra = []
        if a.bit_depth_text:
            extra.append(a.bit_depth_text)
        size_b = info.stream_bytes(a)
        if size_b:
            extra.append(f"{_fmt_size(size_b)}（占 {info.stream_share(a):.0%}）")
        if a.language:
            extra.append(f"[{a.language}]")
        if extra:
            lines.append(INDENT + " · ".join(extra))
    if info.audios:
        lines.append("")

    for i, s in enumerate(info.subtitles):
        head = f"字幕({len(info.subtitles)})" if i == 0 else ""
        row = f"#{s.index}  {_codec_display(s.codec_name)}"
        if s.language:
            row += f"  [{s.language}]"
        if s.is_default:
            row += "  默认"
        lines.append(pad_label(head) + row)

    return "\n".join(lines).rstrip() + "\n"