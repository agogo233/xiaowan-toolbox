"""封装混流页的「额外音频」子卡。

设计稿见 `_design/remux-extra-audio.html`。要点：

  1. **独立子卡**，不塞进「封装选项」的网格 —— 现有的「音轨」那格是
     "从源视频里挑"，额外音频是"从外面引进来"，两者语义不同。
  2. **一条音频**，不做多路混音（那需要 amix，是另一个复杂度）。
  3. **时长以视频为准**：音频不够循环铺满 / 播放一次后静音，超出截断。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel,
                               QPushButton, QRadioButton, QVBoxLayout, QWidget)

from ...core.ffmpeg import probe
from ...core.utils import AUDIO_EXTS, VIDEO_EXTS, human_size
from ..widgets import Card, Combo, label

#: 能当额外音频用的文件：纯音频 + 视频（视频只取里面的音轨，实测可行）
_EXTRA_AUDIO_EXTS = sorted(AUDIO_EXTS | VIDEO_EXTS)
_AUDIO_FILTER = ("音频或视频 (" + " ".join(f"*{e}" for e in _EXTRA_AUDIO_EXTS)
                 + ");;所有文件 (*)")

#: 时长对齐的三种方式。loop 是默认（按用户拍板的规则）。
FILL_MODES = [
    ("loop", "音频不足时循环铺满", "短了就重复播放，铺满整段视频"),
    ("once", "播放一次后静音", "适合片头音乐、提示音这类一次性声音"),
    ("trim", "超出的部分截断", "音频比视频长时只保留前段"),
]

#: 音频短于视频这个比例时，提醒「可能长时间没声音」
SHORT_WARN_RATIO = 0.5


def _fmt_seconds(sec: float) -> str:
    """504.0 → '8 分 24 秒'；31.2 → '31 秒'"""
    if not sec or sec <= 0:
        return "—"
    total = int(round(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} 小时")
    if m:
        parts.append(f"{m} 分")
    parts.append(f"{s} 秒")
    return " ".join(parts)


class ExtraAudioSection(Card):
    """额外音频子卡。"""

    changed = Signal()

    def __init__(self, ctx, parent=None):
        # 边距与封面卡一致（14/10/14/9），这一页的垂直空间很紧
        super().__init__(parent, margins=(14, 10, 14, 9), spacing=6)
        self.ctx = ctx
        self._path: Path | None = None
        self._duration: float = 0.0
        self._has_audio: bool = False
        self._error: str = ""
        self._src_duration: float = 0.0
        self._src_is_video_file: bool = False

        # ---------- 主开关 ----------
        # 默认关闭：加音频会改变输出（多一条轨 / 换掉原声），
        # 属于"用户没要求就不该发生"的副作用。和封面那个开关同一原则。
        self.chk_enable = QCheckBox("加入额外音频")
        self.chk_enable.setChecked(False)
        self.chk_enable.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_enable.toggled.connect(self._on_enable_toggled)
        self.add(self.chk_enable)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)

        # ---------- 文件选择 ----------
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(12)

        self.thumb = QLabel()
        self.thumb.setFixedSize(44, 44)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setObjectName("AudioThumb")
        rl.addWidget(self.thumb)

        info = QVBoxLayout()
        info.setSpacing(3)
        self.lbl_name = label("", "SectionTitle")
        info.addWidget(self.lbl_name)
        self.lbl_meta = label("", "Hint", wrap=True)
        info.addWidget(self.lbl_meta)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_pick = QPushButton("选择音频…")
        self.btn_pick.clicked.connect(self._pick)
        self.btn_clear = QPushButton("移除")
        self.btn_clear.clicked.connect(self._clear)
        btns.addWidget(self.btn_pick)
        btns.addWidget(self.btn_clear)
        btns.addStretch(1)
        info.addLayout(btns)
        rl.addLayout(info, 1)
        body.addWidget(row)

        # ---------- 与原音轨的关系 ----------
        self.rel_box = QWidget()
        rb = QVBoxLayout(self.rel_box)
        rb.setContentsMargins(0, 8, 0, 0)
        rb.setSpacing(4)
        rb.addWidget(label("与原音轨的关系", "HintStrong"))
        # 并排而不是上下堆叠：两个选项是互斥的短句，竖排会白占一行高度，
        # 而这一页的高度本来就紧（文件区 + 参数 + 封面 + 音频 + 操作栏）。
        rel_row = QHBoxLayout()
        rel_row.setSpacing(18)
        self.rd_replace = QRadioButton("替换原声")
        self.rd_replace.setChecked(True)
        self.rd_replace.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rd_append = QRadioButton("保留原声，追加为第 2 条")
        self.rd_append.setCursor(Qt.CursorShape.PointingHandCursor)
        rel_row.addWidget(self.rd_replace)
        rel_row.addWidget(self.rd_append)
        rel_row.addStretch(1)
        rb.addLayout(rel_row)
        self.rd_replace.toggled.connect(lambda _: self.changed.emit())
        self.rd_append.toggled.connect(lambda _: self.changed.emit())
        body.addWidget(self.rel_box)

        # ---------- 时长对齐 ----------
        self.align_box = QWidget()
        ab = QVBoxLayout(self.align_box)
        ab.setContentsMargins(0, 8, 0, 0)
        ab.setSpacing(4)

        fill_row = QHBoxLayout()
        fill_row.setSpacing(8)
        fill_row.addWidget(label("时长不一致时"))
        self.cmb_fill = Combo()
        for value, text, _tip in FILL_MODES:
            self.cmb_fill.addItem(text, value)
        self.cmb_fill.setToolTip("\n".join(
            f"{text} —— {tip}" for _, text, tip in FILL_MODES))
        self.cmb_fill.currentIndexChanged.connect(lambda _: self._sync())
        # 定宽而不是 stretch=1：撑满整行会让它看起来像"最重要的那个控件"，
        # 而它只是个次要的兜底选项（设计稿里是 200px）。
        self.cmb_fill.setFixedWidth(200)
        fill_row.addWidget(self.cmb_fill)
        fill_row.addStretch(1)
        ab.addLayout(fill_row)

        self.lbl_dur = label("", "Hint", wrap=True)
        ab.addWidget(self.lbl_dur)
        self.lbl_warn = label("", "Warn", wrap=True)
        ab.addWidget(self.lbl_warn)
        body.addWidget(self.align_box)

        self.add(self._body)
        self._body.setVisible(False)
        self._sync()

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    def enabled(self) -> bool:
        return self.chk_enable.isChecked()

    def path(self) -> Path | None:
        return self._path if self.enabled() else None

    def replace_original(self) -> bool:
        return self.rd_replace.isChecked()

    def fill_mode(self) -> str:
        return self.cmb_fill.currentData() or "loop"

    def usable(self) -> bool:
        """选了文件、且它确实有音频流。"""
        return (self.enabled() and self._path is not None
                and self._has_audio and not self._error)

    def language_tag(self) -> str:
        """从文件名猜语言（复用字幕那套的简化版）。

        猜不出就返回空 —— 不要瞎填成"中文"。
        """
        if self._path is None:
            return ""
        stem = self._path.stem.lower().replace("_", ".").replace("-", ".")
        mapping = {"chs": "chi", "cht": "chi", "chi": "chi", "zh": "chi",
                   "eng": "eng", "en": "eng", "jpn": "jpn", "ja": "jpn",
                   "kor": "kor", "ko": "kor"}
        for token in reversed(stem.split(".")):
            if token in mapping:
                return mapping[token]
        return ""

    def set_source(self, info) -> None:
        """源视频换了就刷新时长对比。"""
        self._src_duration = info.duration if info else 0.0
        self._sync()

    def summary(self) -> str:
        """操作栏用的一句话（空串 = 不提）。"""
        if not self.usable():
            return ""
        how = "替换原声" if self.replace_original() else "追加为第 2 条"
        return f"额外音频 {self._path.name}（{how}，{self._fill_label()}）"

    def _fill_label(self) -> str:
        value = self.fill_mode()
        for v, text, _tip in FILL_MODES:
            if v == value:
                return text
        return ""

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_enable_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self._sync()
        self.changed.emit()

    def _pick(self) -> None:
        start = str(self._path.parent) if self._path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择音频文件", start, _AUDIO_FILTER)
        if not path:
            return
        self._load(Path(path))

    def _load(self, path: Path) -> None:
        self._path = path
        self._error = ""
        self._has_audio = False
        self._duration = 0.0
        # 视频文件只取音轨 —— 实测可行，但要在界面上说清
        self._src_is_video_file = path.suffix.lower() in VIDEO_EXTS

        info = probe(self.ctx.env, path)
        if not info.ok:
            self._error = info.error or "读不出音频信息"
        elif not info.audios:
            self._error = "这个文件里没有音频流"
        else:
            self._has_audio = True
            self._duration = info.duration
            a = info.audios[0]
            self._meta_text = " · ".join(x for x in (
                a.codec_name.upper() if a.codec_name else "",
                human_size(path.stat().st_size) if path.exists() else "",
                _fmt_seconds(info.duration),
                f"{int(a.bit_rate / 1000)} kbps" if a.bit_rate else "",
            ) if x)
        self._sync()
        self.changed.emit()

    def _clear(self) -> None:
        self._path = None
        self._has_audio = False
        self._duration = 0.0
        self._error = ""
        self._src_is_video_file = False
        self._sync()
        self.changed.emit()

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _sync(self) -> None:
        on = self.enabled()
        self.thumb.setText("🎬" if self._src_is_video_file else "🎵")
        if self._path is None:
            self.lbl_name.setText("未选择音频")
            self.lbl_name.setObjectName("Hint")
            self.lbl_meta.setText(
                "把一段音频（MP3 / AAC / WAV / FLAC / M4A）换成视频的音轨，"
                "或追加成第 2 条。")
            self.btn_clear.setEnabled(False)
            self.rel_box.setVisible(False)
            self.align_box.setVisible(False)
            self._repolish(self.lbl_name)
            return

        self.btn_clear.setEnabled(True)
        self.lbl_name.setObjectName("SectionTitle")
        self.lbl_name.setText(self._path.name)
        self._repolish(self.lbl_name)

        if self._error:
            self.lbl_meta.setText(f"⚠️ {self._error}")
            self.lbl_meta.setObjectName("Danger")
            self._repolish(self.lbl_meta)
            self.rel_box.setVisible(False)
            self.align_box.setVisible(False)
            return

        self.lbl_meta.setObjectName("Hint")
        self._repolish(self.lbl_meta)
        text = getattr(self, "_meta_text", "")
        if self._src_is_video_file:
            text += "　（视频文件，只取里面的音频）"
        self.lbl_meta.setText(text)

        self.rel_box.setVisible(on)
        self.align_box.setVisible(on)
        self._update_alignment()

    def _update_alignment(self) -> None:
        """刷新时长对比与警告。"""
        src, aud = self._src_duration, self._duration
        if not src or not aud:
            self.lbl_dur.setText("")
            self.lbl_warn.setText("")
            return

        fill = self.fill_mode()
        diff = aud - src
        if abs(diff) < 1:
            tail = "两者时长基本一致"
        elif diff < 0:
            tail = (f"音频短 {_fmt_seconds(-diff)}，会循环铺满" if fill == "loop"
                    else f"音频短 {_fmt_seconds(-diff)}")
        else:
            tail = f"音频长 {_fmt_seconds(diff)}，超出部分会被截断"

        self.lbl_dur.setText(
            f"视频 {_fmt_seconds(src)} · 音频 {_fmt_seconds(aud)}"
            f"　→ {tail}")

        # 警告：只在真有风险时出现
        warn = ""
        if fill == "once" and 0 < aud < src * SHORT_WARN_RATIO:
            pct = int(round((1 - aud / src) * 100))
            warn = (f"⚠️ 这样处理后，{_fmt_seconds(src - aud)}"
                    f"（{pct}% 的时长）会完全没有声音。"
                    "如果是片头音乐/提示音那正常，否则建议选「循环铺满」。")
        elif fill == "loop" and 0 < aud < src * SHORT_WARN_RATIO:
            times = int(round(src / aud))
            warn = (f"⚠️ 音频只有 {_fmt_seconds(aud)}，循环铺满会重复约 "
                    f"{times} 次。如果这是一次性声音（提示音之类），"
                    "选「播放一次后静音」更合适。")
        self.lbl_warn.setText(warn)
        self.lbl_warn.setVisible(bool(warn))

    @staticmethod
    def _repolish(widget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)