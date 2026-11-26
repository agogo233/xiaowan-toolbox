#!/usr/bin/env python3
"""封面子卡的 UI 行为测试（需要 QApplication）。

**为什么单独一个文件**：`cover_test.py` 只跑纯逻辑（QCoreApplication 都不需要），
而在那种环境里造 QWidget 会直接崩（"Cannot create a QWidget without
QApplication"）。UI 断言必须放进有 QApplication 的套件。

这一组钉的是用户实测踩到的那个坑：
  选了 MKV + 旁边有同名图 + 封装完 → 缩略图没变，而界面上只是一行灰字。
所以这里测的是**告警会不会在该出现的时候出现**：
  · 没封面 → 安静（默认路径不该一进页面就报红）
  · 有封面 + MKV → 橙色警告 + 「改用 MP4」按钮
  · 切到 MP4 → 警告消失
  · 主开关关掉 → 整块收起、封面参数不参与
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.context import AppContext  # noqa: E402
from app.core.presets import Container  # noqa: E402
from app.ui.main_window import NAV_ITEMS, MainWindow  # noqa: E402
from app.ui.style import apply_theme  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


def pump(app, times: int = 3) -> None:
    for _ in range(times):
        app.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app, "light")
    win = MainWindow(AppContext())
    win.resize(1180, 900)
    win.show()
    win.go_to([n for n, _ in NAV_ITEMS].index("封装混流"))
    page = win.remux_page
    cover = page.cover
    pump(app)

    print("[1] 主开关：默认**关**（加封面会改变输出，用户没要求就不该发生）")
    check("默认不勾「添加封面」", not cover.chk_enable.isChecked())
    check("默认勾选「自动用同名的图片文件」", cover.chk_auto.isChecked())
    check("主开关关着时子区块隐藏", not cover._body.isVisible())
    check("默认主开关视为未启用", not cover.enabled())

    print("\n[2] 刚进页面（MKV、无文件）———— 不应该报警")
    # 🔴 默认容器是 MKV。用户还没做任何事就先看到橙色警告，
    # 会让"正常路径"看起来像出错。告警只在真的会有封面时点亮。
    check("容器是 MKV", page.current_container() == Container.MKV,
          page.current_container().value)
    check("告警样式是普通的 Hint（不是 Warn）",
          cover.lbl_container.objectName() == "Hint",
          cover.lbl_container.objectName())
    check("「改用 MP4」按钮隐藏", not cover.btn_switch_mp4.isVisible())

    print("\n[3] 拖入带同名图的文件（仍 MKV）———— 告警点亮")
    work = Path(tempfile.mkdtemp(prefix="cover_ui_"))
    env = win.ctx.env
    video = work / "课时2.mp4"
    img = work / "课时2.jpg"
    subprocess.run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                   capture_output=True)
    subprocess.run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=320x180",
                    "-frames:v", "1", str(img)], capture_output=True)
    # 先拖文件（此时开关还关着），再打开开关 —— 这条路更像真实操作
    page.table.add_paths([str(video)])
    for _ in range(25):
        app.processEvents()
    check("开关关着时操作栏不提封面",
          "封面" not in page.bar.hint.text(), page.bar.hint.text()[:80])
    cover.chk_enable.setChecked(True)
    pump(app)
    check("检测到同名图", cover.has_same_name_cover([video]))
    check("这批文件会加封面", cover.cover_for(video) is not None,
          str(cover.cover_for(video)))
    pump(app)
    check("告警升级为 Warn", cover.lbl_container.objectName() == "Warn",
          cover.lbl_container.objectName())
    check("「改用 MP4」按钮出现", cover.btn_switch_mp4.isVisible())
    check("告警文案点名缩略图不会变",
          "缩略图" in cover.lbl_container.text(), cover.lbl_container.text())
    # 操作栏也要把后果顶到按钮旁边
    check("操作栏提示里带警告",
          "缩略图不变" in page.bar.hint.text() or "⚠️" in page.bar.hint.text(),
          page.bar.hint.text()[:80])

    print("\n[4] 点「改用 MP4」———— 一键纠正")
    cover.requestContainer.emit(Container.MP4)
    pump(app)
    check("容器真的切到了 MP4", page.current_container() == Container.MP4,
          page.current_container().value)
    check("告警退回 Hint", cover.lbl_container.objectName() == "Hint",
          cover.lbl_container.objectName())
    check("「改用 MP4」按钮消失", not cover.btn_switch_mp4.isVisible())
    check("文案改成两个地方都会变",
          "资源管理器" in cover.lbl_container.text(), cover.lbl_container.text())
    check("操作栏不再有封面警告",
          "缩略图不变" not in page.bar.hint.text(), page.bar.hint.text()[:80])

    print("\n[5] 关掉主开关 ———— 整块收起、参数不参与")
    cover.chk_enable.setChecked(False)
    pump(app)
    check("子区块隐藏", not cover._body.isVisible())
    check("enabled() = False", not cover.enabled())
    check("auto_cover() = False", not cover.auto_cover())
    check("global_cover() = None（存着但不用）", cover.global_cover() is None)
    check("cover_for() = None（这个文件不加封面）",
          cover.cover_for(video) is None, str(cover.cover_for(video)))
    check("操作栏完全不提封面",
          "封面" not in page.bar.hint.text(), page.bar.hint.text()[:80])

    print("\n[6] 重新打开主开关 ———— 状态恢复")
    cover.chk_enable.setChecked(True)
    pump(app)
    check("子区块恢复可见", cover._body.isVisible())
    check("同名图又被用上", cover.cover_for(video) is not None,
          str(cover.cover_for(video)))

    print("\n[7] 手动选全局封面（MKV 下应保持告警）")
    page._switch_container(Container.MKV)
    pump(app)
    cover._global_cover = img
    cover._sync()
    page._update_hint()
    pump(app)
    check("有全局封面 → 告警点亮", cover.lbl_container.objectName() == "Warn",
          cover.lbl_container.objectName())

    print("\n[8] 不支持的容器（MOV / WebM）")
    for cont in (Container.MOV, Container.WEBM):
        page._switch_container(cont)
        pump(app)
        check(f"{cont.value.upper()} 下「选择图片」被禁用",
              not cover.btn_pick.isEnabled())
        check(f"{cont.value.upper()} 告警说「完全不支持」（与 MKV 区分）",
              "完全不支持" in cover.lbl_container.text(),
              cover.lbl_container.text())
        check(f"{cont.value.upper()} 下不会加封面",
              cover.cover_for(video) is None)

    print("\n[9] 封面子卡不会把操作栏挤出窗口")
    page._switch_container(Container.MKV)
    pump(app)
    bar = page.bar
    bottom = bar.geometry().y() + bar.geometry().height()
    # 页面本身可以滚动，但操作栏必须在页面的可视高度内
    check("操作栏底边在页面高度内", bottom <= page.height(),
          f"{bottom} <= {page.height()}")

    win.close()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print(f"  失败：{n}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())