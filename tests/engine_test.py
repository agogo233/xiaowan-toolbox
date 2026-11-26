#!/usr/bin/env python3
"""引擎测试：任务队列 / 进度信号 / 并发 / 取消（不打开窗口）。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

from app.core.config import AppConfig  # noqa: E402
from app.core.engine import QueueManager  # noqa: E402
from app.core.ffmpeg import FFmpegEnv  # noqa: E402
from app.core.pipeline import build_audio_task, build_compress_task  # noqa: E402
from app.core.presets import PRESETS_BY_KEY  # noqa: E402
from app.core.task import TaskStatus  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✔' if ok else '✘'} {name}" + (f" —— {detail}" if detail else ""))


def make_sample(env, path: Path, seconds: int) -> bool:
    return subprocess.run([
        env.ffmpeg, "-hide_banner", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", str(path),
    ], capture_output=True).returncode == 0


def run_queue(mgr: QueueManager, timeout_s: float) -> None:
    """等队列跑空。返回后由调用方自己断言任务状态。

    🔴 timeout_s 必须给足：机器忙时（比如紧跟真跑编码的套件之后）
    实测 120 秒都不够 3 个任务。这里改成等**最后一个任务真的结束**
    而不是固定墙钟 —— 但保留上限防止死等。
    """
    loop = QCoreApplication.instance()
    holder = {"done": False}
    mgr.queueEmpty.connect(lambda: holder.update(done=True))
    deadline = time.time() + timeout_s
    while not holder["done"] and time.time() < deadline:
        loop.processEvents()
        time.sleep(0.02)
    if not holder["done"]:
        remaining = [t for t in mgr.tasks if t.is_active]
        print(f"  （等待超时 {timeout_s:.0f}s，仍有 {len(remaining)} 个任务未结束）")


def main() -> int:
    app = QCoreApplication(sys.argv[:1])
    env = FFmpegEnv().refresh()
    if not env.ready:
        print("找不到 ffmpeg")
        return 2
    print(f"ffmpeg: {env.ffmpeg}\n")

    work = Path(tempfile.mkdtemp(prefix="xiaowan_engine_"))
    src = work / "input.mp4"
    if not make_sample(env, src, 20):
        print("素材生成失败")
        return 2

    cfg = AppConfig()
    cfg.output_dir = str(work / "out")
    cfg.output_mode = "custom"
    cfg.concurrency = 2

    mgr = QueueManager(lambda: env, lambda: cfg)

    updates: list[float] = []
    mgr.taskUpdated.connect(lambda tid: updates.append(mgr.get(tid).progress)
                            if mgr.get(tid) else None)

    # ---------------- 场景 1：批量 + 进度 ---------------- #
    print("[1] 批量压缩 + 进度上报")
    settings = PRESETS_BY_KEY["fast"].video
    tasks = [build_compress_task(type("C", (), {"env": env, "config": cfg})(),
                                 src, None, settings) for _ in range(3)]
    for t in tasks:
        if t.info is None:
            from app.core.ffmpeg import probe
            t.info = probe(env, src)
    mgr.add_many(tasks)
    run_queue(mgr, 300)          # 机器忙时 120s 实测不够（批量回归里踩过）

    ok = all(t.status == TaskStatus.DONE for t in tasks)
    # 先单独断言"都跑完了"这个前提：否则下面几条全是它的连锁失败，
    # 报错看起来像功能坏了，实际只是没等到
    check("前提：3 个任务都已结束（非 running/waiting）",
          all(t.is_finished for t in tasks),
          "、".join(f"{t.status.value}" for t in tasks))
    check("3 个任务全部完成", ok,
          "、".join(f"{t.status.value}:{t.progress:.0%}" for t in tasks))
    check("输出文件都存在", all(t.dst.exists() for t in tasks),
          f"{sum(t.out_size for t in tasks) / 1024:.0f} KB 合计")
    check("进度最终到 100%", all(abs(t.progress - 1.0) < 1e-6 for t in tasks))
    check("进度是渐进上报的", len(updates) > 10, f"{len(updates)} 次刷新")
    check("速度信息已解析", any(t.speed for t in tasks),
          "、".join(sorted({t.speed for t in tasks if t.speed})))
    check("剩余时间已解析", any(t.eta >= 0 for t in tasks))
    check("压缩比合理", all(0.01 < t.ratio < 1.5 for t in tasks),
          "、".join(f"{t.ratio:.0%}" for t in tasks))

    # ---------------- 场景 2：音频任务混排 ---------------- #
    print("\n[2] 压缩 + 音频混合队列")
    from app.core.presets import AUDIO_FORMATS
    a_task = build_audio_task(type("C", (), {"env": env, "config": cfg})(),
                              src, None, "mp3", "libmp3lame", 192)
    mgr.add(a_task)
    run_queue(mgr, 60)
    check("音频任务完成", a_task.status == TaskStatus.DONE and a_task.dst.exists(),
          a_task.dst.name if a_task.dst.exists() else str(a_task.error))

    # ---------------- 场景 3：取消 ---------------- #
    print("\n[3] 运行中取消")
    # 直接构造慢速设置，不借用预设（预设的编码器可能变化）
    from app.core.presets import VideoSettings as _VS
    slow = _VS(codec="libx265", crf=28, preset="veryslow", audio_codec="aac",
               audio_bitrate_kbps=96)
    big = work / "big.mp4"
    make_sample(env, big, 60)
    cancel_task = build_compress_task(type("C", (), {"env": env, "config": cfg})(),
                                      big, None, slow)
    from app.core.ffmpeg import probe
    cancel_task.info = probe(env, big)
    mgr.add(cancel_task)
    deadline = time.time() + 20
    while time.time() < deadline and cancel_task.status != TaskStatus.RUNNING:
        app.processEvents()
        time.sleep(0.02)
    time.sleep(1.0)
    app.processEvents()
    mgr.cancel(cancel_task.id)
    run_queue(mgr, 30)
    check("任务被取消", cancel_task.status == TaskStatus.CANCELED,
          cancel_task.status.value)
    check("半成品文件已清理", not cancel_task.dst.exists(),
          cancel_task.dst.name)

    # ---------------- 场景 3b：临时件清理（.best 不许残留） ---------------- #
    print("\n[3b] 取消 / 预检失败都不许留 .best 副本")
    from app.core.engine import _best_path
    from app.core.size_service import build_size_task
    from app.core.presets import Container, VideoSettings

    ctx_like = type("C", (), {"env": env, "config": cfg})()
    # 取消路径：手动放一个 .best，模拟修正阶段留下的副本
    tc = build_size_task(ctx_like, big, None,
                         VideoSettings(codec="libx264", preset="veryslow",
                                       container=Container.MKV),
                         1.0, audio_mode="none", policy="strict")
    tc.info = probe(env, big)
    best_c = _best_path(tc.dst)
    best_c.parent.mkdir(parents=True, exist_ok=True)
    best_c.write_bytes(b"fake")
    mgr.add_many([tc])
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    mgr.cancel(tc.id)
    deadline = time.time() + 30
    while time.time() < deadline and not tc.is_finished:
        app.processEvents()
        time.sleep(0.02)
    check("取消后 .best 已清理", not best_c.exists(), str(best_c))

    # 预检失败路径：不可行时是 return 早退，不走 except 分支
    tf = build_size_task(ctx_like, big, None,
                         VideoSettings(codec="libx264", preset="ultrafast",
                                       container=Container.MKV),
                         0.0001, audio_mode="none", policy="strict")
    tf.info = probe(env, big)
    best_f = _best_path(tf.dst)
    best_f.parent.mkdir(parents=True, exist_ok=True)
    best_f.write_bytes(b"fake")
    mgr.add_many([tf])
    deadline = time.time() + 60
    while time.time() < deadline and not tf.is_finished:
        app.processEvents()
        time.sleep(0.02)
    check("预检失败后 .best 已清理", not best_f.exists(), str(best_f))

    # ---------------- 场景 4：并发限制 ---------------- #
    print("\n[4] 并发数限制")
    cfg.concurrency = 1
    t1 = build_compress_task(type("C", (), {"env": env, "config": cfg})(), big, None, slow)
    t2 = build_compress_task(type("C", (), {"env": env, "config": cfg})(), big, None, slow)
    for t in (t1, t2):
        t.info = probe(env, big)
    mgr.add_many([t1, t2])
    deadline = time.time() + 15
    while time.time() < deadline and mgr.running_count() < 1:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    check("并发=1 时只跑一个任务",
          mgr.running_count() == 1 and mgr.waiting_count() == 1,
          f"running={mgr.running_count()} waiting={mgr.waiting_count()}")
    mgr.clear_all()
    run_queue(mgr, 10)

    # ---------------- 场景 5：原子输出 ---------------- #
    print("\n[5] 原子输出：目标路径要么不存在、要么完整")
    from app.core.engine import _part_path, _best_path
    from app.core.presets import Container, VideoSettings

    ctx_like = type("C", (), {"env": env, "config": cfg})()
    st5 = VideoSettings(codec="libx264", preset="ultrafast",
                        container=Container.MKV)
    t5 = build_size_task(ctx_like, big, probe(env, big), st5, 2.0,
                         audio_mode="none", policy="strict")
    part5 = _part_path(t5.dst)
    mgr.add_many([t5])
    run_queue(mgr, 300)
    check("任务完成", t5.status == TaskStatus.DONE, t5.error or "")
    check("产物在正式路径上", t5.dst.exists())
    check("中间产物 .part 已改名，不残留", not part5.exists(), str(part5))
    check("产物不是空文件", t5.dst.exists() and t5.dst.stat().st_size > 1024,
          f"{t5.dst.stat().st_size if t5.dst.exists() else 0} 字节")

    # 同名文件在编码期间不许被破坏：用一个已知内容的文件占住目标路径
    t6 = build_size_task(ctx_like, big, probe(env, big), st5, 2.0,
                         audio_mode="none", policy="strict")
    original = b"ORIGINAL-CONTENT-MUST-SURVIVE"
    t6.dst.write_bytes(original)
    mgr.add_many([t6])
    saw_intact = False
    deadline = time.time() + 300
    while time.time() < deadline and not t6.is_finished:
        app.processEvents()
        time.sleep(0.02)
        if t6.dst.exists() and t6.dst.stat().st_size == len(original):
            saw_intact = True
    check("编码期间原同名文件保持完好", saw_intact)
    check("完成后才被原子替换成视频",
          t6.status == TaskStatus.DONE and t6.dst.stat().st_size > 1024,
          f"{t6.dst.stat().st_size if t6.dst.exists() else 0} 字节")

    # ---------------- 场景 6：日志不许重复，失败信息要立刻可读 ---------------- #
    print("\n[6] 日志：命令不重复、失败信息不用等信号")
    cmds = [str(l) for l in t5.log if str(l).startswith("$")]
    check("每条命令只出现一次", len(cmds) == len(set(cmds)),
          f"{len(cmds)} 条 / {len(set(cmds))} 条唯一")
    check("两遍编码 → 恰好 2 条命令", len(cmds) == 2, f"{len(cmds)} 条")

    # 失败时 task.error 必须立刻是人话（不能只给"详情见日志"）
    t7 = build_size_task(ctx_like, big, probe(env, big), st5, 2.0,
                         audio_mode="none", policy="strict")
    t7.steps[-1].args = [("bogus_encoder_xyz" if a == "libx264" else a)
                         for a in t7.steps[-1].args]
    mgr.add_many([t7])
    run_queue(mgr, 120)
    check("失败任务的状态是 FAILED", t7.status == TaskStatus.FAILED,
          t7.status.value)
    # 不额外 pump：验证日志在任务结束那一刻就已经完整
    err_text = (t7.error or "") + "\n" + "\n".join(str(l) for l in t7.log)
    check("错误信息不是笼统的「详情见日志」",
          "详情见日志" not in (t7.error or ""), (t7.error or "")[:50])
    check("日志里能看到编码器找不到", "ncoder" in err_text or "ncoding" in err_text,
          err_text[-90:].replace("\n", " | "))

    print("\n[8] 后台运行：降进程优先级，而不是减线程")
    from app.core.commands import _threads_for_one_job
    from app.core.engine import (_background_preexec, _set_background_priority)

    # 平台契约：Windows 用进程句柄改，POSIX 用 preexec —— 两者不能混
    if sys.platform.startswith("win"):
        check("Windows 上 preexec 返回 None（不该用 fork 钩子）",
              _background_preexec() is None)
    else:
        check("POSIX 上 preexec 是可调用对象",
              callable(_background_preexec()))

    # 🔴 核心：降优先级**不能**顺手减线程 —— 两者是不同的手段。
    # 减线程直接牺牲吞吐（实测慢 8~23%）；降优先级只在别人抢 CPU 时让路，
    # 空闲时全速（实测 0.76s vs 0.76s，完全一样）。
    # 所以线程数现在恒为 None（交给 ffmpeg），与 background_mode 无关。
    check("线程数不随并发设置变化（已交给 ffmpeg 自动）",
          all(_threads_for_one_job(c) is None for c in (1, 2, 4, 8)),
          str([_threads_for_one_job(c) for c in (1, 2, 4, 8)]))

    # 后台运行开关不碰任务参数 —— 它只改进程优先级
    _cfg = AppConfig()
    check("后台运行默认关闭（全速是默认预期）",
          _cfg.background_mode is False)

    # 真跑一次，确认进程优先级真的被改了。
    # ⚠️ psutil 不是项目依赖（只为这件事装一个库不值），所以拿不到就跳过
    # 这一项 —— 但它只是"验证手段"，被测的 _set_background_priority 本身
    # 只依赖标准库。
    try:
        import psutil
        HAS_PSUTIL = True
    except ImportError:
        HAS_PSUTIL = False

    if HAS_PSUTIL:
        env = FFmpegEnv()
        env.refresh()
        # 素材要够长：进程若在采样前就退出，psutil 会抛 NoSuchProcess
        probe_cmd = [env.ffmpeg or "ffmpeg", "-hide_banner", "-loglevel", "error",
                     "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=30",
                     "-c:v", "libx264", "-preset", "veryslow", "-crf", "18",
                     "-f", "null", "-"]
        proc = subprocess.Popen(probe_cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        ok = False
        try:
            time.sleep(1.2)
            before = psutil.Process(proc.pid).nice()
            _set_background_priority(proc)
            time.sleep(0.5)
            after = psutil.Process(proc.pid).nice()
            ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"      跳过优先级实测（进程提前退出：{exc}）")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if ok:
            if sys.platform.startswith("win"):
                check("后台运行把优先级降到 BELOW_NORMAL",
                      after == psutil.BELOW_NORMAL_PRIORITY_CLASS,
                      f"{before} -> {after}")
            else:
                check("后台运行把 nice 调高（降优先级）", after > before,
                      f"{before} -> {after}")
    else:
        print("      跳过优先级实测（未安装 psutil；实现本身只用标准库）")

    print("\n[9] 完成后关机：三重闸门（关机不可逆，默认从严）")
    from app.core.utils import (cancel_shutdown_command, should_power_off,
                                shutdown_command)
    from app.core.task import Task, TaskKind, TaskStatus as TS

    def _mk(status):
        t = Task(kind=TaskKind.COMPRESS, src=Path("a.mp4"), dst=Path("b.mp4"))
        t.status = status
        return t

    # 🔴 这一组是防误关的核心。每一条"不关"都对应一个真实会惹恼用户的场景：
    ok, why = should_power_off([])
    check("空队列不关机（空队列触发关机是偷袭）", not ok, why)

    ok, why = should_power_off([_mk(TS.DONE)] * 2)
    check("全部成功才关机", ok, why)

    ok, why = should_power_off([_mk(TS.DONE), _mk(TS.FAILED)])
    check("有任务失败就不关机（回来发现文件没出来、电脑却关了）", not ok, why)
    check("理由里点明失败数量", "1 个任务失败" in why, why)

    ok, why = should_power_off([_mk(TS.DONE), _mk(TS.CANCELED)])
    check("有任务被取消就不关机", not ok, why)

    ok, why = should_power_off([_mk(TS.DONE)], user_canceled=True)
    check("用户主动取消过就不关机（我取消了，你却关了电脑）", not ok, why)

    ok, why = should_power_off([_mk(TS.FAILED)] * 2)
    check("全部失败也不关机", not ok, why)

    ok, why = should_power_off([_mk(TS.DONE), _mk(TS.WAITING)])
    check("还有任务在等待时不关机（没跑完就关机是漏任务）", not ok, why)

    print("\n[9b] 关机命令按平台构造")
    cmd = shutdown_command(0)
    if sys.platform.startswith("win"):
        check("Windows 用 shutdown /s /t N",
              cmd[:3] == ["shutdown", "/s", "/t"], str(cmd))
        check("延迟秒数会传给 /t", "0" in cmd, str(cmd))
        check("取消关机用 shutdown /a（Windows 才有）",
              cancel_shutdown_command() == ["shutdown", "/a"],
              str(cancel_shutdown_command()))
    elif sys.platform == "darwin":
        check("macOS 走 osascript（shutdown 需要密码）",
              "osascript" in cmd[0], str(cmd))
        check("macOS 无需系统级取消（延迟在应用侧做）",
              cancel_shutdown_command() == [], str(cancel_shutdown_command()))
    else:
        check("Linux 用 shutdown -h", cmd[0] == "shutdown", str(cmd))

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print(f"  失败：{name}")
    print(f"测试目录：{work}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
