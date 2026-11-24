#!/usr/bin/env python3
"""帧率选项测试：档位完整性 / 精确分数 / UI 行为。

三个已经踩过或差点踩的点：
  1. 23.976 / 29.970 / 59.940 **必须写成精确分数**（24000/1001 等）。
     实测 fps=23.976 会让 ffmpeg 生成 2997/125，剪辑软件认成「自定义帧率」。
  2. `fps` 从 float 改成 str 之后，所有 `if s.fps:` 的真值判断要核对
     —— "" 表示保持原始，不能顺手写成 `if s.fps != "0"`。
  3. 「调高帧率不会更流畅」的提示只在**目标高于源**时出现，
     否则几乎每次压缩都挂一句警告。
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
from app.core.commands import build_compress_steps  # noqa: E402
from app.core.ffmpeg import FFmpegEnv  # noqa: E402
from app.core.presets import (FPS_CUSTOM_LABEL, FPS_OPTIONS,  # noqa: E402
                              VideoSettings)
from app.ui.main_window import NAV_ITEMS, MainWindow  # noqa: E402
from app.ui.pages.compress import _fps_value  # noqa: E402
from app.ui.style import apply_theme  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    print("[1] 档位完整性：原版 9 项都要有")
    values = [v for v, _ in FPS_OPTIONS]
    check("第一项是「保持原始」（空字符串）", values[0] == "", repr(values[0]))
    for need, label in (("24000/1001", "23.976"), ("24", "24"), ("25", "25"),
                        ("30000/1001", "29.970"), ("30", "30"), ("50", "50"),
                        ("60000/1001", "59.940"), ("60", "60")):
        check(f"有 {label} fps", need in values, str(values))
    check(f"共 9 项（8 档 + 保持原始）", len(FPS_OPTIONS) == 9, str(len(FPS_OPTIONS)))
    check("自定义标签存在", bool(FPS_CUSTOM_LABEL), FPS_CUSTOM_LABEL)

    print("\n[2] 非整数帧率必须是精确分数（不是小数）")
    for value, label in FPS_OPTIONS:
        if "." in label and "fps" in label:
            check(f"{label} 用分数写法", "/" in value, f"value={value!r}")

    print("\n[3] 按数值升序排列（用户能按顺序找）")
    nums = [_fps_value(v) for v in values]
    asc = [n for n in nums if n > 0]
    check("递增", asc == sorted(asc), str([round(x, 3) for x in asc]))

    print("\n[4] _fps_value：字符串转 float（用于比较，不用于传给 ffmpeg）")
    check("空串 → 0", _fps_value("") == 0.0, str(_fps_value("")))
    check("'25' → 25", _fps_value("25") == 25.0)
    check("'24000/1001' ≈ 23.976",
          abs(_fps_value("24000/1001") - 23.976) < 0.001, f"{_fps_value('24000/1001'):.6f}")
    check("'60000/1001' ≈ 59.94",
          abs(_fps_value("60000/1001") - 59.94) < 0.001)

    print("\n[5] 命令构造：fps 字符串原样进滤镜，空串不产生滤镜")
    env = FFmpegEnv().refresh()
    # 实测编码需要 ffprobe 读回帧率；只有 ffmpeg 时就跳过这一段
    if env.ready and env.has_probe:
        for value, label in (("", "保持原始"), ("24000/1001", "23.976"),
                             ("25", "25"), ("60000/1001", "59.94")):
            s = VideoSettings(fps=value)
            args = build_compress_steps(env, Path("a.mp4"), Path("b.mp4"), s)[0].args
            fps_args = [a for a in args if str(a).startswith("fps=")]
            if value == "":
                check(f"{label} 不产生 fps 滤镜", fps_args == [], str(fps_args))
            else:
                # 🔴 必须是原样的字符串，不能被 float 化再格式化
                check(f"{label} 原样传入", fps_args == [f"fps={value}"], str(fps_args))

    print("\n[6] 实际编码：输出帧率必须是标准值")
    if env.ready:
        work = Path(tempfile.mkdtemp(prefix="fps_test_"))
        src = work / "src.mp4"
        subprocess.run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30", "-t", "2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
                       capture_output=True)
        for value, want in (("24000/1001", "24000/1001"),
                            ("30000/1001", "30000/1001"),
                            ("60000/1001", "60000/1001"),
                            ("25", "25/1")):
            out = work / f"o{value.replace('/', '_')}.mp4"
            s = VideoSettings(fps=value)
            args = build_compress_steps(env, src, out, s)[0].args
            r = subprocess.run(args, capture_output=True)
            if r.returncode != 0:
                check(f"{value} 编码成功", False, "rc!=0")
                continue
            got = subprocess.run(
                # 🔴 用 env 找到的 ffprobe，不写死 bin/ 路径 ——
                # bin/ 下的二进制不入库，开源用户 clone 下来没有它
                [env.ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(out)],
                capture_output=True).stdout.decode("utf-8", "replace").strip()
            check(f"{value} → 输出 {want}", got == want, got)

    print("\n[7] UI：下拉项数与「保持原始」文案随源变化")
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app, "light")
    win = MainWindow(AppContext())
    win.show()
    win.go_to([n for n, _ in NAV_ITEMS].index("视频压缩"))
    page = win.compress_page
    for _ in range(4):
        app.processEvents()

    for name, combo in (("预设页", page.cmb_fps), ("大小页", page.size_fps)):
        check(f"{name} 有 10 项（9 档 + 自定义）", combo.count() == 10,
              str(combo.count()))
        check(f"{name} 最后一项是自定义",
              "自定义" in combo.itemText(combo.count() - 1),
              combo.itemText(combo.count() - 1))

    # 没有文件时不该显示帧率
    page._refresh_fps_labels()
    for _ in range(3):
        app.processEvents()
    check("无文件时是「保持原始帧率」",
          page.cmb_fps.itemText(0) == "保持原始帧率",
          page.cmb_fps.itemText(0))

    # 拖一个 25fps 的源进去，等探测完
    import time
    work2 = Path(tempfile.mkdtemp(prefix="fps_ui_"))
    src2 = work2 / "v25.mp4"
    subprocess.run([env.ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25", "-t", "2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src2)],
                   capture_output=True)
    page.table.add_paths([str(src2)])
    deadline = time.time() + 25
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if page.table.info_for(src2) is not None:
            break
    for _ in range(5):
        app.processEvents()
    check("探测到了源帧率",
          page.table.info_for(src2) is not None
          and page.table.info_for(src2).video is not None
          and page.table.info_for(src2).video.fps == 25.0,
          str(page.table.info_for(src2).video.fps if page.table.info_for(src2)
              and page.table.info_for(src2).video else None))
    check("下拉显示「保持原始帧率（25 fps）」",
          "25 fps" in page.cmb_fps.itemText(0), page.cmb_fps.itemText(0))
    check("大小页同步显示", "25 fps" in page.size_fps.itemText(0),
          page.size_fps.itemText(0))

    print("\n[8] 帧率调高的警告：只在目标 > 源时出现")
    page.cmb_fps.setCurrentIndex(page.cmb_fps.findData("60"))
    page._update_estimate()
    for _ in range(3):
        app.processEvents()
    check("60fps vs 源25 → 有警告", "调高不会让画面更流畅" in page.bar.hint.text(),
          page.bar.hint.text()[-60:])
    check("用的是 Warn（橙色）", page.bar.hint.objectName() == "Warn",
          page.bar.hint.objectName())

    page.cmb_fps.setCurrentIndex(page.cmb_fps.findData("25"))
    page._update_estimate()
    for _ in range(3):
        app.processEvents()
    check("25fps = 源 → 不警告", "调高不会" not in page.bar.hint.text(),
          page.bar.hint.text()[-60:])

    page.cmb_fps.setCurrentIndex(page.cmb_fps.findData("24"))
    page._update_estimate()
    for _ in range(3):
        app.processEvents()
    check("24fps < 源25 → 不警告（降帧率是正常操作）",
          "调高不会" not in page.bar.hint.text(), page.bar.hint.text()[-60:])

    print("\n[9] 29.97 → 30 这种边界不该误报")
    # 源 29.97、目标 30：只高 0.1%，留了 1% 容差，不该警告
    check("容差存在（30 vs 29.97 不报）",
          abs(_fps_value("30") - _fps_value("30000/1001")) / _fps_value("30000/1001") < 0.01,
          f"{abs(_fps_value('30') - _fps_value('30000/1001')) / _fps_value('30000/1001'):.4f}")

    # 收尾：等探测线程跑完再销毁窗口
    for _ in range(40):
        app.processEvents()
    win.close()

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print(f"  失败：{n}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())