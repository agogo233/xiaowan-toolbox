"""设置页：ffmpeg 路径、输出偏好、并发与外观。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QGridLayout,
                               QHBoxLayout, QHeaderView, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QHBoxLayout, QLineEdit, QPushButton, QSpinBox,
                               QWidget)

from ...core.utils import (ffmpeg_install_hint, is_macos, is_windows,
                           open_in_file_manager)
from ..widgets import Card, Combo, Pill, label
from .shared import Page



# 硬件编码器的中文简称（弹窗表格用）
HW_LABEL = {
    "h264_nvenc": "H.264 · NVIDIA NVENC",
    "hevc_nvenc": "H.265 · NVIDIA NVENC",
    "h264_amf": "H.264 · AMD AMF",
    "hevc_amf": "H.265 · AMD AMF",
    "h264_videotoolbox": "H.264 · VideoToolbox",
    "hevc_videotoolbox": "H.265 · VideoToolbox",
}


class EncoderDialog(QDialog):
    """编码器支持清单弹窗：表格一目了然，硬件项带实测原因。"""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编码器支持")
        self.setMinimumSize(560, 420)

        lay = QVBoxLayout(self)
        note = label("软件编码只查 ffmpeg 是否编译；硬件编码在本机实测编码 3 帧，"
                     "不可用的会给出原因。", "Hint", wrap=True)
        lay.addWidget(note)

        table = QTableWidget(len(rows), 4, self)
        table.setHorizontalHeaderLabels(["类别", "编码器", "名称", "状态"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setWordWrap(False)
        table.setAlternatingRowColors(True)
        for r, (cat, family, name, state) in enumerate(rows):
            ok = state == "可用"
            for c, text in enumerate((cat, family, name, state)):
                item = QTableWidgetItem(text)
                if c == 3:
                    item.setForeground(QColor("#1FA34A" if ok else "#C77700"))
                table.setItem(r, c, item)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.setShowGrid(False)
        lay.addWidget(table, 1)

        tip = label("硬件加速没列出来或显示不可用？升级显卡驱动通常能解决"
                    "（NVIDIA 需要 610.00 或更新）。", "Hint", wrap=True)
        lay.addWidget(tip)

        btn = QPushButton("关闭")
        btn.setObjectName("Primary")
        btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn)
        lay.addLayout(row)


class SettingsPage(Page):
    themeChanged = Signal(str)

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, "功能设置", "一般保持默认就能用。找不到 ffmpeg 时再来这里指定。")
        self.root.addWidget(self._build_env())
        self.root.addWidget(self._build_output())
        self.root.addWidget(self._build_prefs())

        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addStretch(1)
        self.btn_reset = QPushButton("恢复默认")
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("Primary")
        bl.addWidget(self.btn_reset)
        bl.addWidget(self.btn_save)
        self.root.addWidget(bar)
        self.root.addStretch(1)

        self.btn_save.clicked.connect(self._save)
        self.btn_reset.clicked.connect(self._reset)
        self._load()

    # ------------------------------------------------------------------ #
    def _build_env(self) -> QWidget:
        card = Card()
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(label("运行环境", "SectionTitle"))
        hl.addStretch(1)
        self.env_pill = Pill("未检测", "err")
        hl.addWidget(self.env_pill)
        card.add(head)

        self.ed_ffmpeg = QLineEdit()
        self.ed_ffmpeg.setPlaceholderText("留空 = 自动检测（PATH / 应用内置 / 系统常见位置）")
        btn_ff = QPushButton("浏览…")
        btn_ff.clicked.connect(lambda: self._pick_file(self.ed_ffmpeg, "选择 ffmpeg"))
        row1 = QWidget()
        r1 = QHBoxLayout(row1)
        r1.setContentsMargins(0, 0, 0, 0)
        r1.setSpacing(8)
        r1.addWidget(label("ffmpeg 路径"))
        r1.addWidget(self.ed_ffmpeg, 1)
        r1.addWidget(btn_ff)
        card.add(row1)

        self.ed_ffprobe = QLineEdit()
        self.ed_ffprobe.setPlaceholderText("留空 = 自动检测（一般无需填写）")
        btn_fp = QPushButton("浏览…")
        btn_fp.clicked.connect(lambda: self._pick_file(self.ed_ffprobe, "选择 ffprobe"))
        row2 = QWidget()
        r2 = QHBoxLayout(row2)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.setSpacing(8)
        r2.addWidget(label("ffprobe 路径"))
        r2.addWidget(self.ed_ffprobe, 1)
        r2.addWidget(btn_fp)
        card.add(row2)

        act = QWidget()
        al = QHBoxLayout(act)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        self.btn_detect = QPushButton("重新检测")
        self.btn_ver = QPushButton("查看编码器支持")
        al.addWidget(self.btn_detect)
        al.addWidget(self.btn_ver)
        al.addStretch(1)
        card.add(act)

        self.btn_detect.clicked.connect(self._detect)
        self.btn_ver.clicked.connect(self._show_encoders)

        self.env_detail = label("", "Hint", wrap=True)
        card.add(self.env_detail)

        card.add(label(f"没装 ffmpeg？{ffmpeg_install_hint()}。", "Hint"))
        return card

    def _build_output(self) -> QWidget:
        card = Card()
        card.add(label("输出", "SectionTitle"))

        self.ed_outdir = QLineEdit()
        self.ed_outdir.setPlaceholderText("留空即「与源文件相同目录」")
        btn_dir = QPushButton("选择…")
        btn_dir.clicked.connect(self._pick_dir)
        btn_open = QPushButton("打开")
        btn_open.clicked.connect(
            lambda: open_in_file_manager(Path(self.ed_outdir.text()))
            if Path(self.ed_outdir.text()).exists() else None)
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        rl.addWidget(label("输出目录"))
        rl.addWidget(self.ed_outdir, 1)
        rl.addWidget(btn_dir)
        rl.addWidget(btn_open)
        card.add(row)
        card.add(label("默认输出到源文件所在目录，这里不用填。"
                       "只有把下面的「默认位置」改成「始终输出到指定目录」时，"
                       "才会用到上面这个目录。", "Hint", wrap=True))

        self.cmb_mode = Combo()
        self.cmb_mode.addItem("与源文件相同目录", "source")
        self.cmb_mode.addItem("始终输出到上面的目录", "custom")

        self.ed_suffix = QLineEdit()
        self.ed_suffix.setPlaceholderText("例如 _压缩（留空则仅在重名时自动加序号）")
        self.ed_suffix.setMaximumWidth(240)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        grid.addWidget(label("默认位置"), 0, 0)
        grid.addWidget(self.cmb_mode, 0, 1)
        grid.addWidget(label("文件名后缀"), 1, 0)
        grid.addWidget(self.ed_suffix, 1, 1)
        grid.setColumnStretch(1, 1)
        holder = QWidget()
        holder.setLayout(grid)
        card.add(holder)

        self.chk_overwrite = QCheckBox("直接覆盖同名文件（默认关闭，会自动加 _1 序号）")
        self.chk_after = QCheckBox("队列跑完后打开输出目录")
        card.add(self.chk_overwrite)
        card.add(self.chk_after)
        return card

    def _build_prefs(self) -> QWidget:
        card = Card()
        card.add(label("性能与外观", "SectionTitle"))

        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 8)
        self.spin_workers.setSuffix(" 个")

        self.cmb_theme = Combo()
        self.cmb_theme.addItem("浅色", "light")
        self.cmb_theme.addItem("深色", "dark")

        # 后台运行：降进程优先级，不是减线程 —— 两者目的相同（别卡我），
        # 但减线程直接牺牲吞吐，降优先级只在别人抢 CPU 时让路，没人抢时全速。
        self.chk_background = QCheckBox("后台运行（优先让出 CPU，压片不影响你用电脑）")
        self.chk_background.setToolTip(
            "把 ffmpeg 进程降到「低于正常」优先级。\n"
            "别人（你、别的程序）需要 CPU 时系统会先把它挪开，\n"
            "没人抢的时候它照样全速跑 —— 所以压缩速度基本不受影响。\n"
            "笔记本上还能顺带减少发热和风扇噪音。")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        grid.addWidget(label("同时处理"), 0, 0)
        grid.addWidget(self.spin_workers, 0, 1)
        grid.addWidget(label("主题"), 0, 2)
        grid.addWidget(self.cmb_theme, 0, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        holder = QWidget()
        holder.setLayout(grid)
        card.add(holder)
        card.add(label("x264 / x265 已自带多线程，一般设 1–2 就够。"
                       "设更高只多一点点速度（实测 2 个并行相对串行 1.15×），"
                       "但机器会更卡。", "Hint"))
        card.add(self.chk_background)
        return card

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        cfg = self.ctx.config
        self.ed_ffmpeg.setText(cfg.ffmpeg_path)
        self.ed_ffprobe.setText(cfg.ffprobe_path)
        self.ed_outdir.setText(cfg.output_dir)
        idx = self.cmb_mode.findData(cfg.output_mode)
        self.cmb_mode.setCurrentIndex(max(0, idx))
        self.ed_suffix.setText(cfg.filename_suffix)
        self.chk_overwrite.setChecked(cfg.overwrite)
        self.chk_after.setChecked(cfg.open_when_done)
        self.spin_workers.setValue(max(1, min(8, cfg.concurrency)))
        self.chk_background.setChecked(cfg.background_mode)
        tidx = self.cmb_theme.findData(cfg.theme)
        self.cmb_theme.setCurrentIndex(max(0, tidx))
        self._detect(apply=False)

    def _pick_file(self, line_edit: QLineEdit, title: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, title, str(Path.home()))
        if path:
            line_edit.setText(path)

    def _pick_dir(self) -> None:
        start = self.ed_outdir.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择默认输出目录", start)
        if path:
            self.ed_outdir.setText(path)

    def _detect(self, apply: bool = True) -> None:
        ffmpeg = self.ed_ffmpeg.text().strip()
        ffprobe = self.ed_ffprobe.text().strip()
        if apply:
            self.ctx.config.ffmpeg_path = ffmpeg
            self.ctx.config.ffprobe_path = ffprobe
        env = self.ctx.refresh_env()

        if env.ready:
            self.env_pill.setText("就绪")
            self.env_pill.set_kind("ok")
            probe_note = "ffprobe 已找到（读取信息更快）" if env.has_probe \
                else "未找到 ffprobe，将用 ffmpeg 兜底解析"
            self.env_detail.setText(
                f"{env.version or 'ffmpeg 已找到'}\n路径：{env.ffmpeg}\n{probe_note}")
        else:
            self.env_pill.setText("未找到")
            self.env_pill.set_kind("err")
            self.env_detail.setText(
                f"没有检测到 ffmpeg。{ffmpeg_install_hint()}，"
                "或手动指定可执行文件路径。")

    def _show_encoders(self) -> None:
        env = self.ctx.env
        if not env.ready:
            self.env_detail.setText("请先配置好 ffmpeg。")
            return

        rows: list[tuple[str, str, str, str]] = []
        # (类别, 显示名, 编码器名, 状态/原因)
        soft = [("H.264", "libx264"), ("H.265", "libx265"), ("AV1", "libsvtav1"),
                ("VP9", "libvpx-vp9"), ("MP3", "libmp3lame"), ("AAC", "aac"),
                ("Opus", "libopus"), ("FLAC", "flac"), ("AAC-FDK", "libfdk_aac")]
        for family, name in soft:
            ok = env.encoder_available(name)
            rows.append(("软件编码", family, name,
                         "可用" if ok else "当前 ffmpeg 未编译"))
        # 硬件编码器要实测（编译了≠能用），并显示不可用的人话原因
        for name in ("h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf"):
            if not env.encoder_available(name):
                rows.append(("硬件编码", name, name, "当前 ffmpeg 未编译"))
                continue
            ok = env.encoder_usable(name)
            reason = env.unusable_reason(name)
            state = "可用" if ok else (reason or "不可用")
            rows.append(("硬件编码", HW_LABEL.get(name, name), name, state))

        dlg = EncoderDialog(rows, self)
        dlg.exec()

    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        cfg = self.ctx.config
        cfg.ffmpeg_path = self.ed_ffmpeg.text().strip()
        cfg.ffprobe_path = self.ed_ffprobe.text().strip()
        cfg.output_dir = self.ed_outdir.text().strip()
        cfg.output_mode = self.cmb_mode.currentData() or "source"
        cfg.filename_suffix = self.ed_suffix.text().strip()
        cfg.overwrite = self.chk_overwrite.isChecked()
        cfg.open_when_done = self.chk_after.isChecked()
        cfg.concurrency = self.spin_workers.value()
        cfg.background_mode = self.chk_background.isChecked()
        cfg.theme = self.cmb_theme.currentData() or "light"

        self.ctx.save_config()
        self.ctx.refresh_env()
        self.themeChanged.emit(cfg.theme)
        self._detect(apply=False)
        self.env_detail.setText(self.env_detail.text() + "\n已保存。")

    def _reset(self) -> None:
        cfg = self.ctx.config
        cfg.ffmpeg_path = ""
        cfg.ffprobe_path = ""
        cfg.output_mode = "source"
        cfg.output_dir = ""
        cfg.filename_suffix = ""
        cfg.overwrite = False
        cfg.open_when_done = False
        cfg.concurrency = 2
        cfg.background_mode = False
        cfg.theme = "light"
        self.ctx.save_config()
        self._load()
        self.themeChanged.emit(cfg.theme)
        self.env_detail.setText(self.env_detail.text() + "\n已恢复默认。")
