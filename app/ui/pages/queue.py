"""任务队列页：进度、速度、剩余时间、日志。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QHBoxLayout, QHeaderView, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ...core.task import TaskStatus
from ...core.utils import (human_duration, human_size, open_in_file_manager,
                           power_off, should_power_off)
from ..style import palette
from ..widgets import Card, ProgressDelegate, StatCard, label
from .shared import Page

HEADERS = ["文件名", "类型", "进度", "速度", "剩余", "状态", "输出"]
COL_PROGRESS = 2


class QueuePage(Page):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, "任务队列",
                         "所有任务都排在这里。并发数可以在「功能设置」里调整。")
        self.queue = ctx.queue
        self._rows: dict[str, int] = {}
        self._last_logged: dict[str, int] = {}

        # ---------------- 统计 ---------------- #
        stats = QWidget()
        sl = QHBoxLayout(stats)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(10)
        self.stat_wait = StatCard("等待中", "0")
        self.stat_run = StatCard("进行中", "0")
        self.stat_done = StatCard("已完成", "0")
        self.stat_fail = StatCard("失败", "0")
        self.stat_saved = StatCard("累计节省", "—")
        for s in (self.stat_wait, self.stat_run, self.stat_done,
                  self.stat_fail, self.stat_saved):
            sl.addWidget(s, 1)
        self.root.addWidget(stats)

        # ---------------- 表格 ---------------- #
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(HEADERS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setHighlightSections(False)
        self.table.doubleClicked.connect(self._on_double_click)

        card = Card()
        card.layout().addWidget(self.table, 1)
        self.root.addWidget(card, 1)

        # ---------------- 操作条 ---------------- #
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        self.btn_start = QPushButton("继续派发任务")
        self.btn_pause = QPushButton("暂停派发")
        self.btn_cancel = QPushButton("取消所选")
        self.btn_retry = QPushButton("重试失败")
        self.btn_clear = QPushButton("清除已完成")
        self.btn_clearall = QPushButton("清空全部")
        self.btn_open = QPushButton("打开输出目录")

        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_retry.clicked.connect(self._on_retry)
        self.btn_clear.clicked.connect(self.queue.clear_finished)
        self.btn_clearall.clicked.connect(self.queue.clear_all)
        self.btn_open.clicked.connect(self._on_open_output)

        # 完成后关机：**一次性**开关（不是持久设置）。勾上只对本次队列生效，
        # 队列跑完 / 被取消后自动复位 —— 免得下次莫名其妙被关机。
        #
        # 🔴 用 QCheckBox 而不是可勾选的 QPushButton：后者看着就是个按钮，
        # 用户会以为「点一下 = 现在就关机」。勾选框才是"这是个开关"的视觉语言，
        # 而且本软件其他开关（覆盖文件、后台运行、防削波…）全是 QCheckBox。
        self.chk_poweroff = QCheckBox("全部完成后关机")
        self.chk_poweroff.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_poweroff.setToolTip(
            "勾上后，**本次队列全部成功完成**时会先弹 60 秒倒计时再关机。\n"
            "以下情况不关机：有任务失败、有任务被取消、队列为空。\n"
            "（关机不可逆，所以默认从严处理。）")
        self.chk_poweroff.toggled.connect(self._on_poweroff_toggled)
        self._poweroff_armed = False

        for b in (self.btn_start, self.btn_pause, self.btn_cancel, self.btn_retry,
                  self.btn_clear, self.btn_clearall):
            bl.addWidget(b)
        bl.addStretch(1)
        bl.addWidget(self.chk_poweroff)
        bl.addSpacing(8)
        bl.addWidget(self.btn_open)
        self.root.addWidget(bar)

        # ---------------- 日志 ---------------- #
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFixedHeight(130)
        self.log.setPlaceholderText("任务日志会显示在这里")
        self.root.addWidget(self.log)

        # ---------------- 信号 ---------------- #
        self.queue.taskAdded.connect(self._on_added)
        self.queue.taskUpdated.connect(self._on_updated)
        self.queue.taskRemoved.connect(self._on_removed)
        self.queue.queueEmpty.connect(self._on_empty)

        self._refresh_stats()
        self._sync_buttons()

    # ------------------------------------------------------------------ #
    def _on_poweroff_toggled(self, checked: bool) -> None:
        """「完成后关机」只对本次队列生效，且必须有任务才允许勾。

        勾上时若队列已经是空的，直接取消并说明 —— 否则用户勾完发现
        没反应（因为队列已经跑完了，queueEmpty 不会再发一次）。
        """
        self._poweroff_armed = checked
        if not checked:
            self.log.appendPlainText("─── 已取消「完成后关机」───")
            return
        if not self.queue.tasks:
            self.chk_poweroff.setChecked(False)
            self.log.appendPlainText("─── 队列是空的，「完成后关机」没有意义 ───")
            return
        self.log.appendPlainText(
            "─── 已启用「完成后关机」：本次队列全部成功后弹 60 秒倒计时 ───")

    def _maybe_power_off(self) -> None:
        """队列跑空时调用：过三重闸门，通过了才弹倒计时。"""
        if not self._poweroff_armed:
            return
        # 一次性：不管结果如何，这次触发后就复位，避免下次被误关
        self.chk_poweroff.setChecked(False)

        tasks = self.queue.tasks
        ok, reason = should_power_off(tasks)
        if not ok:
            self.log.appendPlainText(f"─── 不关机：{reason} ───")
            return

        self.log.appendPlainText(f"─── {reason}，{PowerOffDialog.SECONDS} 秒后关机 ───")
        dlg = PowerOffDialog(reason, self.window())
        dlg.start()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.log.appendPlainText("─── 已取消关机 ───")
            return
        done, msg = power_off(0)
        self.log.appendPlainText(
            f"─── 关机命令{('已下达' if done else '失败：')}{'' if done else msg} ───")

    def _on_added(self, task_id: str) -> None:
        task = self.queue.get(task_id)
        if not task:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[task_id] = row

        name = QTableWidgetItem(task.name)
        name.setToolTip(task.source_tooltip)
        name.setData(Qt.ItemDataRole.UserRole + 1, task_id)
        self.table.setItem(row, 0, name)
        self.table.setItem(row, 1, QTableWidgetItem(task.kind_label))
        self.table.setItem(row, COL_PROGRESS, QTableWidgetItem("0%"))
        self.table.setItem(row, 3, QTableWidgetItem("—"))
        self.table.setItem(row, 4, QTableWidgetItem("—"))
        self.table.setItem(row, 5, QTableWidgetItem(task.status_label))
        out = QTableWidgetItem(task.dst.name)
        out.setToolTip(str(task.dst))
        self.table.setItem(row, 6, out)

        self._on_updated(task_id)
        self.table.scrollToBottom()

    def _on_removed(self, task_id: str) -> None:
        row = self._rows.pop(task_id, None)
        self._last_logged.pop(task_id, None)
        if row is None:
            return
        self.table.removeRow(row)
        self._rows = {tid: self._find_row(tid) for tid in list(self._rows)}
        self._refresh_stats()
        self._sync_buttons()

    def _find_row(self, task_id: str) -> int:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.data(Qt.ItemDataRole.UserRole + 1) == task_id:
                return r
        return -1

    def _on_updated(self, task_id: str) -> None:
        row = self._rows.get(task_id)
        task = self.queue.get(task_id)
        if task is None:
            return
        if row is None or row >= self.table.rowCount():
            self._on_added(task_id)
            return

        pal = palette()
        ratio = max(0.0, min(1.0, task.progress))
        prog = self.table.item(row, COL_PROGRESS)
        if prog:
            prog.setText(f"{ratio * 100:.1f}%" if ratio < 1 else "100%")
            prog.setData(Qt.ItemDataRole.UserRole, ratio)

        speed = self.table.item(row, 3)
        if speed:
            speed.setText(task.speed or "—")

        eta = self.table.item(row, 4)
        if eta:
            if task.status == TaskStatus.RUNNING and task.eta >= 0:
                eta.setText(human_duration(task.eta))
            elif task.status in (TaskStatus.DONE, TaskStatus.CANCELED):
                eta.setText(human_duration(task.elapsed))
            else:
                eta.setText("—")

        st = self.table.item(row, 5)
        if st:
            st.setText(task.status_label)
            color = {
                TaskStatus.WAITING: pal["text_dim"],
                TaskStatus.RUNNING: pal["accent"],
                TaskStatus.DONE: pal["success"],
                TaskStatus.FAILED: pal["danger"],
                TaskStatus.CANCELED: pal["text_faint"],
            }.get(task.status, pal["text"])
            st.setForeground(QColor(color))
            detail = task.error or task.message or ""
            st.setToolTip(detail)

        if task.status == TaskStatus.DONE:
            out = self.table.item(row, 6)
            if out:
                out.setText(f"{task.dst.name}（{human_size(task.out_size)}）")
            self._append_log(task, f"✔ 完成：{task.dst.name} "
                                   f"→ {human_size(task.out_size)}"
                                   f"（{task.ratio * 100:.0f}% of source）")
        elif task.status == TaskStatus.FAILED:
            self._append_log(task, f"✘ 失败：{task.error}")
        elif task.status == TaskStatus.CANCELED:
            self._append_log(task, "－ 已取消")

        self._refresh_stats()
        self._sync_buttons()

    def _append_log(self, task, text: str) -> None:
        seen = self._last_logged.get(task.id, -1)
        key = (hash(text))
        if seen == key:
            return
        self._last_logged[task.id] = key
        self.log.appendPlainText(f"[{task.name}] {text}")

    def _on_empty(self) -> None:
        self.log.appendPlainText("─── 队列已全部处理完毕 ───")
        self._maybe_power_off()

    # ------------------------------------------------------------------ #
    def _refresh_stats(self) -> None:
        tasks = self.queue.tasks
        waiting = sum(1 for t in tasks if t.status == TaskStatus.WAITING)
        running = sum(1 for t in tasks if t.status == TaskStatus.RUNNING)
        done = [t for t in tasks if t.status == TaskStatus.DONE]
        failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)

        self.stat_wait.set_value(str(waiting))
        self.stat_run.set_value(str(running))
        self.stat_done.set_value(str(len(done)))
        self.stat_fail.set_value(str(failed))

        saved = 0
        for t in done:
            try:
                saved += max(0, t.src.stat().st_size - t.out_size)
            except OSError:
                continue
        self.stat_saved.set_value(human_size(saved) if done else "—")

    def _sync_buttons(self) -> None:
        tasks = self.queue.tasks
        active = any(t.status in (TaskStatus.WAITING, TaskStatus.RUNNING) for t in tasks)
        self.btn_pause.setEnabled(active and not self.queue.paused)
        self.btn_start.setEnabled(self.queue.paused or not active)
        self.btn_cancel.setEnabled(bool(tasks))
        self.btn_retry.setEnabled(any(t.status == TaskStatus.FAILED for t in tasks))
        self.btn_clear.setEnabled(any(t.is_finished for t in tasks))
        self.btn_clearall.setEnabled(bool(tasks))

    # ------------------------------------------------------------------ #
    def _selected_ids(self) -> list[str]:
        ids: list[str] = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item:
                tid = item.data(Qt.ItemDataRole.UserRole + 1)
                if tid:
                    ids.append(tid)
        return ids

    def _on_start(self) -> None:
        self.queue.start()
        self.log.appendPlainText("─── 继续派发任务 ───")
        self._sync_buttons()

    def _on_pause(self) -> None:
        self.queue.pause()
        self.log.appendPlainText("─── 已暂停派发（正在运行的任务会先做完）───")
        self._sync_buttons()

    def _on_cancel(self) -> None:
        for tid in self._selected_ids():
            self.queue.cancel(tid)

    def _on_retry(self) -> None:
        for tid in self._selected_ids():
            task = self.queue.get(tid)
            if task and task.status == TaskStatus.FAILED:
                self.queue.retry(tid)

    def _on_open_output(self) -> None:
        tasks = self.queue.tasks
        if tasks:
            open_in_file_manager(tasks[-1].dst)
        else:
            cfg = self.ctx.config
            if cfg.output_dir:
                open_in_file_manager(Path(cfg.output_dir))

    def _on_double_click(self, index) -> None:
        item = self.table.item(index.row(), 0)
        if not item:
            return
        tid = item.data(Qt.ItemDataRole.UserRole + 1)
        task = self.queue.get(tid) if tid else None
        if task:
            target = task.dst if task.dst.exists() else task.src
            open_in_file_manager(Path(target))


# --------------------------------------------------------------------------- #
# 完成后关机
# --------------------------------------------------------------------------- #

class PowerOffDialog(QDialog):
    """关机前的人机确认：60 秒倒计时，随时可取消。

    为什么必须有这一道：关机不可逆。用户可能在任务还剩几秒时才离开，
    回来只看到黑屏就不知道发生了什么。倒计时给了他"反悔"的机会。

    🔴 不用系统自带的 `shutdown /t N` 做倒计时：那个窗口用户可能看不到
    （被我们的主窗口挡住），而且取消要记命令行。应用内倒计时更可控。
    """

    SECONDS = 60

    def __init__(self, reason: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("即将关机")
        self.setMinimumWidth(380)
        self._left = self.SECONDS

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(10)

        head = label("全部任务已完成", "SectionTitle")
        lay.addWidget(head)
        lay.addWidget(label(reason, "Hint", wrap=True))

        self.lbl_count = label("", "HintStrong")
        lay.addWidget(self.lbl_count)
        lay.addWidget(label(
            "想继续用电脑就点下面的「取消关机」。倒计时结束后系统会"
            "自然关机，所有程序会收到关闭通知（该保存的会提示保存）。",
            "Hint", wrap=True))

        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("取消关机")
        btn_cancel.setObjectName("Primary")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        lay.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._update_text()

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._left -= 1
        self._update_text()
        if self._left <= 0:
            self._timer.stop()
            self.accept()

    def _update_text(self) -> None:
        self.lbl_count.setText(f"{self._left} 秒后关机…")

