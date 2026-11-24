"""封装 / 混流页（换容器、挑音轨、嵌字幕、加封面、加音频）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout,
                               QSpinBox, QWidget)

from ...core.pipeline import build_remux_task
from ...core.presets import REMUX_CONTAINERS, Container, cover_support
from ...core.utils import VIDEO_EXTS, human_size
from ..widgets import Card, Combo, FileArea, FileTable, label
from .remux_audio import ExtraAudioSection
from .remux_cover import CoverSection
from .shared import ActionBar, OutputRow, Page, make_file_toolbar

AUDIO_MODES = [
    ("all", "保留全部音轨"),
    ("first", "只保留第一条"),
    ("none", "不要音轨"),
]

AUDIO_CODECS = [
    ("copy", "原样复制（不重编码）"),
    ("aac", "转成 AAC"),
    ("libmp3lame", "转成 MP3"),
    ("libopus", "转成 Opus"),
    ("flac", "转成 FLAC（无损）"),
    # 音频转格式从「音频提取」页搬到这里（那一页改成只做抽取拆分、不转码）。
    # 补全是为了不让老用户发现"以前能转 FLAC，现在不行了"。
    # ⚠️ 容器限制由 _CONTAINER_AUDIO 过滤：WebM 只认 Opus/Vorbis，
    #    MP4/MOV 不认 FLAC —— 选到装不下的组合时界面会退回 copy 并说明。
]

#: 需要"采样率"下拉的编码（copy 时保持原样，不给这个选项）
_AUDIO_SAMPLE_RATES = [
    (0, "保持原始采样率"),
    (48000, "48 kHz"),
    (44100, "44.1 kHz"),
    (32000, "32 kHz"),
    (22050, "22.05 kHz"),
]

_AUDIO_CHANNELS = [
    (0, "保持原始声道"),
    (2, "立体声（2 声道）"),
    (1, "单声道"),
]


class RemuxPage(Page):
    requestQueue = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, "封装混流",
                         "换容器、挑音轨、挂字幕、加封面、加音频。默认不重新编码，速度极快、画质无损。")

        self.table = FileTable(ctx.env_provider, VIDEO_EXTS)
        area_files = FileArea(
            self.table, make_file_toolbar(self.table, VIDEO_EXTS))

        # 底部操作栏要先创建：参数区在构建过程中就会回调 _update_hint
        self.bar = ActionBar("开始封装", OutputRow(ctx))
        self.bar.button.clicked.connect(self._on_start)

        self.cover = CoverSection(ctx)
        # 告警里点「改用 MP4」直接切容器（用户不必自己去上面找那个下拉）
        self.cover.requestContainer.connect(self._switch_container)

        # 额外音频排在封面之后（顺序与 build_remux_steps 的输入序号一致：
        # 源 → 字幕 → 封面 → 额外音频）
        self.extra = ExtraAudioSection(ctx)
        self.extra.changed.connect(self._update_hint)

        # 🔴 文件区不给 stretch：加了封面卡之后整页高度会超出窗口
        # （实测 927 > 900，「开始封装」被挤出可视区）。文件表自己有
        # 140px 的下限够用了，多余空间留给参数卡和封面卡。
        # 页面外层本来就是 QScrollArea，窗口再矮也能滚动，不会切内容。
        self.root.addWidget(area_files)
        self.root.addWidget(self._build_params())
        self.root.addWidget(self.cover)
        self.root.addWidget(self.extra)
        self.root.addWidget(self.bar)
        self.root.addStretch(1)

        self.table.filesChanged.connect(self._update_hint)
        self.cover.changed.connect(self._update_hint)
        self._update_hint()

    # ------------------------------------------------------------------ #
    def _build_params(self) -> QWidget:
        card = Card()
        card.add(label("封装选项", "SectionTitle"))

        self.cmb_container = Combo()
        for container, text, desc in REMUX_CONTAINERS:
            self.cmb_container.addItem(f"{text} —— {desc}", container.value)
        self.cmb_container.setCurrentIndex(1)  # 默认 MKV，兼容性最稳
        self.cmb_container.currentIndexChanged.connect(self._on_container_changed)

        self.cmb_audio_mode = Combo()
        for value, text in AUDIO_MODES:
            self.cmb_audio_mode.addItem(text, value)
        self.cmb_audio_mode.currentIndexChanged.connect(self._on_audio_mode_changed)

        self.cmb_audio_codec = Combo()
        for value, text in AUDIO_CODECS:
            self.cmb_audio_codec.addItem(text, value)
        self.cmb_audio_codec.currentIndexChanged.connect(self._on_audio_mode_changed)

        self.spin_audio_br = QSpinBox()
        self.spin_audio_br.setRange(32, 512)
        self.spin_audio_br.setValue(192)
        self.spin_audio_br.setSuffix(" kbps")

        audio_row = QWidget()
        al = QHBoxLayout(audio_row)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        al.addWidget(self.cmb_audio_codec, 1)
        al.addWidget(self.spin_audio_br)

        # 采样率与声道：音频转格式从「音频提取」页搬过来时补的。
        # copy 模式下这两项没意义（保持原样），由 _on_audio_mode_changed 禁用。
        self.cmb_audio_rate = Combo()
        for value, text in _AUDIO_SAMPLE_RATES:
            self.cmb_audio_rate.addItem(text, value)
        self.cmb_audio_rate.currentIndexChanged.connect(
            lambda _: self._update_hint())

        self.cmb_audio_channels = Combo()
        for value, text in _AUDIO_CHANNELS:
            self.cmb_audio_channels.addItem(text, value)
        self.cmb_audio_channels.currentIndexChanged.connect(
            lambda _: self._update_hint())

        self.chk_subtitles = QCheckBox("嵌入同名字幕文件（.srt / .ass / .vtt）")
        self.chk_subtitles.setChecked(True)
        self.chk_faststart = QCheckBox("MP4 启用 faststart（网页秒开）")
        self.chk_faststart.setChecked(True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        grid.addWidget(label("目标容器"), 0, 0)
        grid.addWidget(self.cmb_container, 0, 1)
        grid.addWidget(label("音轨"), 0, 2)
        grid.addWidget(self.cmb_audio_mode, 0, 3)
        grid.addWidget(label("音轨编码"), 1, 0)
        grid.addWidget(audio_row, 1, 1)
        grid.addWidget(label("采样率"), 1, 2)
        grid.addWidget(self.cmb_audio_rate, 1, 3)
        grid.addWidget(label("声道"), 2, 0)
        grid.addWidget(self.cmb_audio_channels, 2, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        holder = QWidget()
        holder.setLayout(grid)
        card.add(holder)
        card.add(self.chk_subtitles)
        card.add(self.chk_faststart)
        self.note = label("", "Hint")
        card.add(self.note)
        self._on_container_changed()
        return card

    # ------------------------------------------------------------------ #
    def current_container(self) -> Container:
        return Container(self.cmb_container.currentData() or "mkv")

    def _switch_container(self, container: Container) -> None:
        """从封面告警里一键切容器（不改音轨/字幕等其它设置）。"""
        idx = self.cmb_container.findData(container.value)
        if idx >= 0:
            self.cmb_container.setCurrentIndex(idx)

    def _on_container_changed(self) -> None:
        container = self.current_container()
        self.chk_faststart.setEnabled(container == Container.MP4)
        # 封面能力随容器变（MP4 双认 / MKV 只播放器认 / MOV、WebM 不支持）
        self.cover.set_container(container)
        if container == Container.WEBM:
            self.note.setText("WebM 只支持 VP9 / Opus，无法直接复制 H.264 流，"
                              "遇到失败请改用 MKV。")
        elif container == Container.MP4:
            self.note.setText("MP4 对音轨格式挑剔，若源音轨是 AC3 / DTS / Opus，"
                              "建议把「音轨编码」改成转 AAC。")
        else:
            self.note.setText("MKV 什么都能装：多音轨、多字幕、章节都会保留。")
        self._update_hint()

    def _on_audio_mode_changed(self) -> None:
        mode = self.cmb_audio_mode.currentData()
        codec = self.cmb_audio_codec.currentData()
        self.cmb_audio_codec.setEnabled(mode != "none")
        # copy 时不重编码，码率/采样率/声道都没有意义 —— 显式禁用，
        # 免得用户以为改了会生效（那是最容易让人以为"设置没起作用"的坑）
        recoding = mode != "none" and codec != "copy"
        self.spin_audio_br.setEnabled(recoding)
        self.cmb_audio_rate.setEnabled(recoding)
        self.cmb_audio_channels.setEnabled(recoding)
        self._update_hint()

    def _update_hint(self) -> None:
        paths = self.table.paths
        # 先让封面卡按"这批文件到底会不会加封面"决定是否点亮 MKV 告警
        self.cover.refresh_state(paths)
        if not paths:
            self.bar.set_hint("还没有添加文件 —— 拖入视频即可")
            self.bar.button.setEnabled(False)
            return
        if not self.ctx.env.ready:
            self.bar.set_hint("未找到 ffmpeg，请到「功能设置」里指定路径", "Danger")
            self.bar.button.setEnabled(False)
            return
        self.bar.button.setEnabled(True)

        info = self.table.info_for(paths[0])
        prefix = f"共 {len(paths)} 个任务 · " if len(paths) > 1 else ""

        # 🔴 封面的结论不依赖探测结果（只看磁盘上有没有同名图），
        # 所以**不能**因为 info 还没回来就把它一起吞掉 ——
        # 文件刚拖进来的那几百毫秒里，恰恰是用户最需要看到
        # 「这个容器封面会失效」的时候。实测踩坑就发生在这个窗口。
        cover_hint = self._cover_summary(paths)
        cover_warn = ""
        support = cover_support(self.current_container())
        if cover_hint:
            if support == "player":
                cover_warn = " · ⚠️ 封面只在播放器可见，缩略图不变（换 MP4 才行）"
            elif support == "none":
                # MOV / WebM 是**完全不支持**，和 MKV 的程度不同，说法也要不同
                name = self.current_container().value.upper()
                cover_warn = f" · ⛔ {name} 装不下封面，选了也不会加"

        if not info:
            self.bar.set_hint(f"{prefix}读取文件信息中…{cover_warn}",
                              "Warn" if cover_warn else "Hint")
            return

        tracks = []
        if info.audios:
            tracks.append(f"{len(info.audios)} 条音轨")
        if info.subtitles:
            tracks.append(f"{len(info.subtitles)} 条内嵌字幕")
        # 让额外音频卡知道源时长（用于显示"音频短/长多少"的对比）
        self.extra.set_source(info)
        detail = "、".join(tracks) if tracks else "只有视频轨"
        action = "不重新编码，秒级完成" if self.cmb_audio_codec.currentData() == "copy" \
            else "音频需重新编码"
        hint = f"{prefix}{detail} · 原文件 {human_size(info.size)} · {action}"

        # 封面会失效时，警示顶到按钮旁边 —— 用户看的是「开始封装」，
        # 不是下面那张卡片。
        if cover_warn:
            self.bar.set_hint(hint + cover_warn, "Warn")
            return
        if cover_hint:
            hint += f" · {cover_hint}"
        # 额外音频也汇总进操作栏（它改变了输出，用户该在按钮旁看到）
        extra_hint = self.extra.summary()
        if extra_hint:
            hint += f" · {extra_hint}"
        self.bar.set_hint(hint)

    def _cover_summary(self, paths: list) -> str:
        """封面的汇总：几个同名、几个兜底、合计增加多少。

        🔴 这里不自己重新算"该用哪张"——直接问 `cover.cover_for()`，
        它和 pipeline 里的 `resolve_cover()` 是同一套规则。
        界面算一遍、后端算一遍，迟早出现"界面说加了、实际没加"。
        """
        # 🔴 注意：这里**不能**因为容器不支持就返回空 —— MOV/WebM 恰恰
        # 最需要警告（"你旁边有同名图，但这个容器加不上"）。容器能力
        # 由调用方判断该说"只在播放器可见"还是"完全加不上"。
        # 只有主开关关着时，才一个字都不提封面（用户主动选的，别啰嗦）。
        if not self.cover.enabled():
            return ""
        from ...core.ffmpeg import find_cover

        same, fallback, total_bytes = 0, 0, 0
        auto = self.cover.chk_auto.isChecked()
        for p in paths:
            # 🔴 按"来源"分桶，而不是按"路径是否等于全局封面" —— 用户完全
            # 可能把全局封面就设成与某个文件同名的图，那样两种判法会打架。
            if auto and find_cover(p) is not None:
                same += 1
                cover = find_cover(p)
            else:
                cover = self.cover.global_cover()
                if cover is not None:
                    fallback += 1
            if cover is None:
                continue
            try:
                total_bytes += cover.stat().st_size
            except OSError:
                pass
        if not same and not fallback:
            return "不加封面"
        bits = []
        if same:
            bits.append(f"{same} 个用同名图")
        if fallback:
            bits.append(f"{fallback} 个用全局封面")
        text = "封面： " + "、".join(bits)
        # 🔴 容器装不下时不能说"合计增加多少" —— 那是**不会发生**的事。
        # 说了反而让用户以为文件会变大，进而怀疑输出有问题。
        if total_bytes and cover_support(self.current_container()) != "none":
            text += f"，合计增加 {human_size(total_bytes)}"
        return text

    # ------------------------------------------------------------------ #
    def _on_start(self) -> None:
        paths = self.table.paths
        if not paths or not self.ctx.env.ready:
            return
        container = self.current_container()
        audio_mode = self.cmb_audio_mode.currentData() or "all"
        audio_codec = self.cmb_audio_codec.currentData() or "copy"
        bitrate = self.spin_audio_br.value()
        rate = int(self.cmb_audio_rate.currentData() or 0)
        channels = int(self.cmb_audio_channels.currentData() or 0)

        tasks = []
        for path in paths:
            info = self.table.info_for(path)
            tasks.append(build_remux_task(
                self.ctx, path, info, container,
                audio_mode=audio_mode, audio_codec=audio_codec,
                audio_bitrate_kbps=bitrate,
                audio_sample_rate=rate,
                audio_channels=channels,
                embed_subtitles=self.chk_subtitles.isChecked(),
                faststart=self.chk_faststart.isChecked(),
                auto_cover=self.cover.auto_cover(),
                fallback_cover=self.cover.global_cover(),
                extra_audio=self.extra.path(),
                extra_audio_replace=self.extra.replace_original(),
                extra_audio_fill=self.extra.fill_mode(),
                extra_audio_lang=self.extra.language_tag(),
            ))

        self.ctx.queue.add_many(tasks)
        self.table.clear_all()
        self.requestQueue.emit()
