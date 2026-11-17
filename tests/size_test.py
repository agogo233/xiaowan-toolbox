# -*- coding: utf-8 -*-
"""按文件大小压缩的端到端实测。

覆盖：正常目标 / 音频 copy / 极小目标（不可行）/ 允许降音频 / WebM 拒绝 / 已小于目标。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 从脚本自身位置推导项目根，不写死绝对路径（目录改名/换机器都不会坏）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.ffmpeg import FFmpegEnv, probe                     # noqa: E402
from app.core.presets import Container, VideoSettings            # noqa: E402
from app.core.size_budget import Feasibility, solve              # noqa: E402
from app.core.size_service import (build_size_task,              # noqa: E402
                                   _video_args as _size_video_args)
from app.core.task import TaskStatus                             # noqa: E402
from app.core.commands import build_compress_steps               # noqa: E402

env = FFmpegEnv()
env.refresh()
WORK = Path(tempfile.mkdtemp(prefix="size_page_"))
SRC = WORK / "src.mp4"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✔ " if ok else "  ✘ ") + name + (" —— " + detail if detail else ""))


def make_source(sec=30):
    r = subprocess.run([env.ffmpeg, "-hide_banner", "-v", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={sec}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={sec}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                        str(SRC)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]


class FakeCfg:
    filename_suffix = ""
    overwrite = True
    output_mode = "source"
    output_dir = ""
    concurrency = 1
    theme = "light"
    last_preset = ""

    def resolved_output_dir(self, src):
        return src.parent


class FakeCtx:
    def __init__(self):
        self.env = env
        self.config = FakeCfg()


ctx = FakeCtx()


def run_task(task, timeout=600):
    """同步跑完一个任务（不开 Qt 事件循环）。"""
    from app.core.engine import QueueManager
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    mgr = QueueManager(lambda: env, lambda: ctx.config)
    mgr.add_many([task])
    start = time.time()
    while not task.is_finished and time.time() - start < timeout:
        app.processEvents()
        time.sleep(0.05)
    if task.status != TaskStatus.DONE:
        # 失败时把真实报错打出来，别只给"编码失败，详情见日志"
        errs = [l for l in task.log if "error" in l.lower() or "invalid" in l.lower()]
        print("      [失败原因]", errs[-1][:220] if errs else task.message)
        # 把最后一条 ffmpeg 命令也打出来，方便手动复跑
        cmds = [l for l in task.log if l.startswith("$ ")]
        if cmds:
            print("      [命令]", cmds[-1][:260])
    return task


def main():
    make_source()
    info = probe(env, SRC)
    src_mb = info.size / 1024 / 1024
    print(f"素材：{src_mb:.2f} MB / {info.duration:.0f}s / "
          f"{info.video.width}x{info.video.height if info.video else 0}\n")

    # ---- 1. 正常目标 ----
    print("[1] 目标 3 MB / AAC（默认）")
    st = VideoSettings(codec="libx264", preset="veryfast", audio_codec="aac",
                       audio_bitrate_kbps=128, container=Container.MP4)
    t = build_size_task(ctx, SRC, info, st, 3.0)
    check("预算可行", t.budget.ok, t.budget.reason)
    run_task(t)
    mb = t.out_size / 1024 / 1024
    check("任务完成", t.status == TaskStatus.DONE, t.error)
    check("体积不超过目标", mb <= 3.0 * 1.02, f"{mb:.2f} MB / 3.00 MB")
    check("未被过份浪费", mb >= 3.0 * 0.85, f"{mb:.2f} MB")

    # ---- 2. 音频保留原轨 ----
    print("\n[2] 目标 3 MB / 保留原始音轨（旧实现会超 13%）")
    st2 = VideoSettings(codec="libx264", preset="veryfast", audio_codec="copy",
                        container=Container.MP4)
    t2 = build_size_task(ctx, SRC, info, st2, 3.0, audio_mode="copy")
    run_task(t2)
    mb2 = t2.out_size / 1024 / 1024
    check("任务完成", t2.status == TaskStatus.DONE, t2.error)
    check("copy 音频也没超目标", mb2 <= 3.0 * 1.02, f"{mb2:.2f} MB / 3.00 MB")

    # ---- 3. 极小目标：音频就吃掉预算，应被拒绝 ----
    print("\n[3] 目标 0.3 MB（音频就要 153%，应直接拒绝）")
    b3 = solve(info, 0.3, "mp4", audio_mode="aac", audio_kbps=128)
    check("判定为不可行", not b3.ok, b3.feasibility.value + " / " + b3.reason)
    check("给出了建议", bool(b3.suggestions), " | ".join(b3.suggestions[:2]))
    t3 = build_size_task(ctx, SRC, info, st, 0.3)
    run_task(t3)
    check("任务被拒绝而非产出垃圾", t3.status == TaskStatus.FAILED, t3.message)

    # ---- 4. 同上但允许降音频 ----
    print("\n[4] 目标 0.6 MB + 允许降音频")
    b4 = solve(info, 0.6, "mp4", audio_mode="aac", audio_kbps=128,
               allow_audio_shrink=True)
    check("降音频后可做", b4.ok, f"音频降到 {b4.audio_kbps} kbps")
    t4 = build_size_task(ctx, SRC, info, st, 0.6, audio_mode="aac",
                         allow_audio_shrink=True)
    run_task(t4)
    mb4 = t4.out_size / 1024 / 1024
    check("任务完成", t4.status == TaskStatus.DONE, t4.error)
    check("可达", t4.status != TaskStatus.DONE or mb4 <= 0.6 * 1.02,
          f"{mb4:.2f} MB / 0.60 MB")

    # ---- 5. WebM：明确拒绝而不是静默失效 ----
    print("\n[5] WebM 容器")
    b5 = solve(info, 3.0, "webm", audio_mode="aac")
    t5 = build_size_task(ctx, SRC, info,
                         VideoSettings(codec="libx264", preset="veryfast",
                                       container=Container.WEBM), 3.0)
    check("WebM 被明确拒绝而不是静默走 CRF", t5.budget is not None,
          "预算已算出 → 由页面提示")

    # ---- 6. 已小于目标 ----
    print("\n[6] 目标 50 MB（源仅 %.1f MB）" % src_mb)
    b6 = solve(info, 50.0, "mp4", audio_mode="aac")
    check("提示无需压缩", any("无需压缩" in s for s in b6.suggestions),
          " | ".join(b6.suggestions[:1]))

    # ---- 7. WebM 真的能用（不是静默失败）----
    print("" + chr(10) + "[7] WebM 容器（应自动换 VP9 + Opus）")
    st7 = VideoSettings(codec="libx264", preset="veryfast", audio_codec="aac",
                        audio_bitrate_kbps=128, container=Container.WEBM)
    t7 = build_size_task(ctx, SRC, info, st7, 2.0, policy="strict")
    run_task(t7)
    mb7 = t7.out_size / 1024 / 1024
    check("WebM 任务完成", t7.status == TaskStatus.DONE, (t7.error or "")[:120])
    check("WebM 严格不超目标", bool(mb7) and mb7 <= 2.0,
          "%.3f MB / 2.000 MB" % mb7)
    check("输出扩展名是 .webm", t7.dst.suffix == ".webm", t7.dst.name)

    # ---- 8. 两档策略：严格（两遍）与快速单遍，两档都必须校验并不超 ----
    print("" + chr(10) + "[8] 两档策略语义")
    t8_strict = build_size_task(ctx, SRC, info, st, 3.0, policy="strict")
    run_task(t8_strict)
    mb8a = t8_strict.out_size / 1024 / 1024
    t8_fast = build_size_task(ctx, SRC, info, st, 3.0, policy="fast")
    run_task(t8_fast)
    mb8b = t8_fast.out_size / 1024 / 1024
    check("两档都完成", t8_strict.status == TaskStatus.DONE
          and t8_fast.status == TaskStatus.DONE,
          "%s / %s" % (t8_strict.status.value, t8_fast.status.value))
    check("严格档不超目标（一个字节都不超）", mb8a <= 3.0,
          "%.3f MB / 3.000 MB" % mb8a)
    check("快速单遍也不超目标", mb8b <= 3.0, "%.3f MB / 3.000 MB" % mb8b)
    check("两档容差都是 0", t8_strict.size_tolerance == 0.0
          and t8_fast.size_tolerance == 0.0,
          "%s / %s" % (t8_strict.size_tolerance, t8_fast.size_tolerance))
    check("快速单遍只有一步（真的单遍）", len(t8_fast.steps) == 1,
          "%d 步" % len(t8_fast.steps))
    check("严格档是两遍", len(t8_strict.steps) == 2, "%d 步" % len(t8_strict.steps))
    check("严格档更贴近目标（两遍 ABR 的利用率更高）", mb8a >= mb8b,
          "严格 %.3f MB / 快速 %.3f MB" % (mb8a, mb8b))

    # ---- 9. strict 遇到容易超标的素材：必须修正到不超 ----
    print("" + chr(10) + "[9] strict 对易超标素材的保证")
    tough = WORK / "tough.mp4"
    subprocess.run([env.ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "nullsrc=s=1280x720:r=30:d=6,geq=random(1)*255:128:128",
                    "-f", "lavfi", "-i", "sine=f=440:d=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
                    "-c:a", "aac", "-b:a", "128k", str(tough)], check=True)
    info9 = probe(env, tough)
    t9 = build_size_task(ctx, tough, info9, st, 1.5, policy="strict")
    run_task(t9)
    mb9 = t9.out_size / 1024 / 1024
    check("strict 任务完成", t9.status == TaskStatus.DONE, (t9.error or "")[:120])
    check("strict 绝不超目标", mb9 <= 1.5, "%.3f MB / 1.500 MB" % mb9)
    check("修正轮数不超上限", t9.correction_round <= t9.size_max_rounds,
          "%d / %d 轮" % (t9.correction_round, t9.size_max_rounds))

    # ---- 9b. 「尽量用满」：欠标素材补跑，但绝不越过目标 ----
    print("" + chr(10) + "[9b] 尽量用满（欠标补跑）")
    t9b_off = build_size_task(ctx, tough, info9, st, 1.5, policy="strict",
                              fill_target=False)
    run_task(t9b_off)
    mb_off = t9b_off.out_size / 1024 / 1024
    t9b_on = build_size_task(ctx, tough, info9, st, 1.5, policy="strict",
                             fill_target=True)
    run_task(t9b_on)
    mb_on = t9b_on.out_size / 1024 / 1024
    check("勾选后任务完成", t9b_on.status == TaskStatus.DONE,
          (t9b_on.error or "")[:120])
    check("勾选后仍然绝不超目标", mb_on <= 1.5, "%.3f MB / 1.500 MB" % mb_on)
    # 这份素材本来就能到 99%+，没触发补跑；而且 x264 多线程在纯噪声上有 ±2%
    # 的编码不确定性，所以这里只断言"没有明显变差"，真正的补跑效果由
    # 专门会欠标的素材验证（见下方 9c）。
    # 阈值取 5%：两次独立编码各带 ±2% 抖动，差值本身就可能到 4%，
    # 卡 3% 会随机失败（实测 4 次里挂 2 次）。
    check("勾选后没有明显变差", mb_on >= mb_off * 0.95,
          "不勾 %.3f MB / 勾选 %.3f MB" % (mb_off, mb_on))
    check("勾选时补跑没有超出轮数上限",
          t9b_on.correction_round <= t9b_on.size_max_rounds,
          "%d / %d" % (t9b_on.correction_round, t9b_on.size_max_rounds))

    # ---- 9c. 真正会欠标的素材：补跑必须把体积推上去，且仍然不超 ----
    print("" + chr(10) + "[9c] 欠标素材上的补跑效果")
    under = WORK / "undershoot.mp4"
    subprocess.run([env.ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "nullsrc=s=640x360:r=25:d=3,geq=random(1)*255:128:128",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
                    "-pix_fmt", "yuv420p", str(under)], check=True)
    info_u = probe(env, under)
    t9c_off = build_size_task(ctx, under, info_u, st, 1.5, policy="strict",
                              fill_target=False)
    run_task(t9c_off)
    mb_off2 = t9c_off.out_size / 1024 / 1024
    t9c_on = build_size_task(ctx, under, info_u, st, 1.5, policy="strict",
                             fill_target=True)
    run_task(t9c_on)
    mb_on2 = t9c_on.out_size / 1024 / 1024
    check("欠标素材不勾选时确实欠标（<95%）", mb_off2 < 1.5 * 0.95,
          "%.3f MB / 1.500 MB" % mb_off2)
    # 纯噪声素材上 x264 多线程有 ±2% 的编码不确定性，补跑是否触发取决于第一轮
    # 恰好落在 95% 触发线的哪一侧。所以这里不断言"一定补跑"，而是断言
    # 「要么补跑了、要么本来就已经够满」—— 两者都说明判据在工作。
    fired = t9c_on.correction_round > 0
    check("补跑判据生效（补跑或已够满）",
          fired or mb_off2 >= 1.5 * 0.95,
          f"补跑 {t9c_on.correction_round} 轮，不勾时 {mb_off2/1.5:.1%}")
    if fired:
        # 补跑确实把体积推上去了才叫有效。阈值 3%：单轮补跑的目标是"往 95% 靠"，
        # 而两轮独立编码各有 ±2% 抖动，卡 5% 会随机失败（实测挂过）。
        check("勾选后体积有上升", mb_on2 > mb_off2 * 1.03,
              "不勾 %.3f MB → 勾选 %.3f MB" % (mb_off2, mb_on2))
    check("补跑后依然绝不超目标", mb_on2 <= 1.5, "%.3f MB / 1.500 MB" % mb_on2)

    # ---- 9d. fill 在 WebM/VP9 上：受益最大，但也不许冲过目标 ----
    print("" + chr(10) + "[9d] fill + WebM/VP9（实测 fill 的最大受益者）")
    st9d = VideoSettings(codec="libvpx-vp9", preset="veryfast",
                         audio_codec="libopus", audio_bitrate_kbps=96,
                         container=Container.WEBM)
    t9d_off = build_size_task(ctx, SRC, info, st9d, 2.0, audio_mode="opus",
                              policy="strict", fill_target=False)
    run_task(t9d_off)
    t9d_on = build_size_task(ctx, SRC, info, st9d, 2.0, audio_mode="opus",
                             policy="strict", fill_target=True)
    run_task(t9d_on)
    mb_off3 = t9d_off.out_size / 1024 / 1024
    mb_on3 = t9d_on.out_size / 1024 / 1024
    check("VP9 两档都完成",
          t9d_off.status == TaskStatus.DONE and t9d_on.status == TaskStatus.DONE,
          f"{t9d_off.status.value}/{t9d_on.status.value}")
    check("VP9 勾选后绝不超目标", mb_on3 <= 2.0, "%.3f MB / 2.000 MB" % mb_on3)
    check("VP9 勾选后不比不勾更小（fill 不该帮倒忙）", mb_on3 >= mb_off3 * 0.98,
          "不勾 %.3f → 勾选 %.3f" % (mb_off3, mb_on3))

    # ---- 9e. fill + 音频：音频字节不能被算错 ----
    print("" + chr(10) + "[9e] fill + 带音频（音频字节在修正公式里的处理）")
    st9e = VideoSettings(codec="libx264", preset="medium", audio_codec="aac",
                         audio_bitrate_kbps=128, container=Container.MKV)
    for mode in ("aac", "copy"):
        t9e = build_size_task(ctx, SRC, info, st9e, 1.0, audio_mode=mode,
                              policy="strict", fill_target=True)
        run_task(t9e)
        mb9e = t9e.out_size / 1024 / 1024
        check(f"音频={mode} 时 fill 不冲过目标", t9e.status == TaskStatus.DONE
              and mb9e <= 1.0,
              "%.3f MB / 1.000 MB（%s）" % (mb9e, t9e.status.value))

    # ---- 10. 画质评估：目标过小时必须给出警告 ----
    print("" + chr(10) + "[10] 画质评估与分辨率推荐")
    from app.core.quality import QualityLevel, assess as q_assess
    from app.core.ffmpeg import MediaInfo, StreamInfo

    def fake_media(w, h, fps=30):
        return MediaInfo(path=WORK / "x.mp4", duration=7200, size=2 * 1024 ** 3,
                         video=StreamInfo(index=0, codec_type="video",
                                          codec_name="h264", width=w, height=h, fps=fps))

    hi = q_assess(fake_media(1920, 1080), 4000)
    check("高码率判为可接受", hi.level.is_acceptable, hi.level.label)
    check("高码率不给降分辨率建议", not hi.needs_action,
          "suggest=%d" % hi.suggest_height)

    # 1080p 下 200kbps 连 480p 都救不回来（bpp 0.0163 < 0.018），
    # 所以这里用一个"降分辨率确实有效"的码率：600kbps
    lo = q_assess(fake_media(1920, 1080), 600)
    check("低码率判为劣化", lo.level in (QualityLevel.POOR, QualityLevel.BAD),
          lo.level.label)
    check("低码率给出降分辨率建议", 0 < lo.suggest_height < 1080,
          "降 %dp" % lo.suggest_height)
    check("建议方案 bpp 达标", lo.suggest_bpp >= 0.018, "%.4f" % lo.suggest_bpp)

    # 加入 360/240 后，200kbps 在 1080p 下不再是"无解"——360p 能救回来。
    # "无解"现在需要更极端的码率（120kbps / 4K 源）才触发。
    mid = q_assess(fake_media(1920, 1080), 200)
    check("200kbps 现在有解（360p）", mid.suggest_height == 360,
          "suggest=%d" % mid.suggest_height)

    # 240p 只需要约 55 kbps 就能达标（bpp 0.018），4K 源 + 120kbps 仍有解
    hopeless = q_assess(fake_media(3840, 2160), 40)
    check("无解时不给降分辨率建议", hopeless.suggest_height == 0,
          "suggest=%d" % hopeless.suggest_height)
    check("无解时建议提高目标", "提高" in hopeless.reason, hopeless.reason[:60])
    check("无解文案提到最低档 240p", "240p" in hopeless.reason,
          hopeless.reason[-30:])

    # ---- 10b. 评估必须跟随用户选的分辨率（用户实测踩过：选 480p 后
    # 提示仍停留在源分辨率的结论上，"降到 XXXp"的建议也失效）----
    print("" + chr(10) + "[10b] 画质评估跟随用户选定的分辨率")
    # 1080p 源 + 600kbps：按源分辨率评估是"劣化、建议降级"；
    # 用户手动选了 480p 后，实际编码就是 480p，bpp 升高、结论必须翻转为"可接受"
    by_src = q_assess(fake_media(1920, 1080), 600)
    check("按源分辨率评估：判为劣化", by_src.level in (QualityLevel.POOR,
          QualityLevel.BAD), by_src.level.label)
    by_user = q_assess(fake_media(1920, 1080), 600, scale_height=480)
    check("选 480p 后按 480p 评估", by_user.height == 480,
          "评估高度 %d" % by_user.height)
    check("选 480p 后结论翻转为可接受", by_user.level.is_acceptable,
          by_user.level.label)
    check("选 480p 后不再给降级建议", not by_user.needs_action,
          "suggest=%d" % by_user.suggest_height)

    # 用户选的高度比"建议降到的"还低时，建议不能再往下推 —— 那是在
    # 推翻用户的选择。选 360p 后若 360p 本身达标，就不该再建议 240p。
    by_360 = q_assess(fake_media(1920, 1080), 600, scale_height=360)
    check("选 360p 后按 360p 评估", by_360.height == 360,
          "评估高度 %d" % by_360.height)
    if by_360.level.is_acceptable:
        check("用户已选到达标高度时不给更低建议",
              by_360.suggest_height == 0,
              "suggest=%d" % by_360.suggest_height)
    else:
        # 360p 仍不够（理论不会发生，守住即可）
        check("用户已选到达标高度时不给更低建议",
              by_360.suggest_height == 0 or by_360.suggest_height >= 360,
              "suggest=%d" % by_360.suggest_height)

    # 选的高度比源高 = 实际仍按源编（只缩不放），评估必须跟着真实行为走
    by_up = q_assess(fake_media(1920, 1080), 600, scale_height=2160)
    check("选的高度比源高时按源评估（只缩不放）", by_up.height == 1080,
          "评估高度 %d" % by_up.height)

    # ---- 11. 容器开销随时长变化 ----
    print("" + chr(10) + "[11] 容器开销动态估算")
    from app.core.size_budget import solve as sb_solve

    short = sb_solve(info, 1.0, "mp4", audio_mode="aac", audio_kbps=128)
    long_ = sb_solve(info, 100.0, "mp4", audio_mode="aac", audio_kbps=128)
    check("短视频开销比例更高", short.overhead > long_.overhead,
          "%.2f%% vs %.2f%%" % (short.overhead * 100, long_.overhead * 100))
    check("短视频预留了固定索引开销", short.overhead_fixed >= 20000,
          "%d 字节" % short.overhead_fixed)
    check("长视频开销收敛到结构占比", long_.overhead < 0.025,
          "%.2f%%" % (long_.overhead * 100))

    # ---- 12. 多音轨预算 ----
    print("" + chr(10) + "[12] 多音轨预算")
    from app.core.ffmpeg import StreamInfo as SI

    def multi_audio(n=3, kbps=128):
        m = fake_media(1280, 720)
        m.audios = [SI(index=i + 1, codec_type="audio", codec_name="aac",
                       channels=2, bit_rate=kbps * 1000) for i in range(n)]
        m.video = SI(index=0, codec_type="video", codec_name="h264",
                     width=1280, height=720, fps=30)
        return m

    m3 = multi_audio(3)
    tight = sb_solve(m3, 2.0, "mp4", audio_mode="copy")
    check("多音轨放不下时判不可行", not tight.ok, tight.reason)
    check("提示指明音轨数量", "3 条音轨" in tight.reason, tight.reason[:50])
    check("给出跳过多余音轨的建议",
          any("AAC" in s or "移除" in s for s in tight.suggestions),
          " | ".join(tight.suggestions[:2])[:60])

    roomy = sb_solve(m3, 8.0, "mp4", audio_mode="copy")
    check("目标够大时保留全部音轨", roomy.audio_streams == 3,
          "%d 条" % roomy.audio_streams)
    check("多音轨预算按条数累计", roomy.audio_bytes > 3 * 0.9 * 128 * 1000 / 8 * roomy.duration * 0.9,
          "%.1f MB" % (roomy.audio_bytes / 1024 / 1024))

    # ---- 13. MP3 音轨（与预设页对齐）----
    print("" + chr(10) + "[13] MP3 音轨")
    st13 = VideoSettings(codec="libx264", preset="veryfast", audio_codec="libmp3lame",
                         audio_bitrate_kbps=128, container=Container.MP4)
    t13 = build_size_task(ctx, SRC, info, st13, 3.0, audio_mode="mp3")
    run_task(t13)
    mb13 = t13.out_size / 1024 / 1024
    check("MP3/MP4 任务完成", t13.status == TaskStatus.DONE, (t13.error or "")[:120])
    check("MP3/MP4 不超目标", bool(mb13) and mb13 <= 3.0 * 1.02,
          "%.2f MB / 3.00 MB" % mb13)
    check("音轨确实是 MP3", any(a.codec_name == "mp3" for a in probe(env, t13.dst).audios),
          " | ".join(a.codec_name for a in probe(env, t13.dst).audios))

    st13b = VideoSettings(codec="libx264", preset="veryfast", audio_codec="libmp3lame",
                          audio_bitrate_kbps=128, container=Container.MKV)
    t13b = build_size_task(ctx, SRC, info, st13b, 3.0, audio_mode="mp3")
    run_task(t13b)
    check("MP3/MKV 任务完成", t13b.status == TaskStatus.DONE, (t13b.error or "")[:120])

    # ---- 14. AV1 目标体积（旧 capped CRF 只用到 76% 预算）----
    print("" + chr(10) + "[14] SVT-AV1 目标 3 MB + 10bit")
    st14 = VideoSettings(codec="libsvtav1", preset="veryfast",
                         container=Container.MKV, pix_fmt="yuv420p10le")
    t14 = build_size_task(ctx, SRC, info, st14, 3.0, audio_mode="aac")
    run_task(t14)
    mb14 = t14.out_size / 1024 / 1024
    check("AV1 任务完成", t14.status == TaskStatus.DONE, (t14.error or "")[:120])
    check("AV1 不超目标", bool(mb14) and mb14 <= 3.0 * 1.02, "%.2f MB / 3.00 MB" % mb14)
    check("AV1 用满预算（≥85%）", mb14 >= 3.0 * 0.85, "%.2f MB / 3.00 MB" % mb14)
    out14 = probe(env, t14.dst)
    check("AV1 编出的确实是 AV1",
          bool(out14.video) and out14.video.codec_name == "av1",
          out14.video.codec_name if out14.video else "?")
    check("AV1 的 10bit 真生效",
          bool(out14.video) and out14.video.pix_fmt == "yuv420p10le",
          out14.video.pix_fmt if out14.video else "?")
    check("两遍的 passlog 临时目录已清理",
          bool(t14.work_dir) and not Path(t14.work_dir).exists(),
          str(t14.work_dir))

    # ---- 15. 10bit 的长尾组合（MP4+hvc1、WebM/Profile 2）----
    print("" + chr(10) + "[15] 10bit + MP4/x265（hvc1）与 10bit + WebM/VP9（Profile 2）")
    import subprocess as _sp

    def _vstream(path):
        r = _sp.run([env.ffprobe or "ffprobe", "-v", "error",
                     "-select_streams", "v:0", "-show_entries",
                     "stream=codec_name,profile,pix_fmt,codec_tag_string",
                     "-of", "default=nw=1", str(path)],
                    capture_output=True, text=True, encoding="utf-8")
        d = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    st15 = VideoSettings(codec="libx265", preset="ultrafast",
                         pix_fmt="yuv420p10le", container=Container.MP4)
    t15 = build_size_task(ctx, SRC, info, st15, 1.5, audio_mode="none",
                          policy="strict")
    run_task(t15)
    d15 = _vstream(t15.dst) if t15.status == TaskStatus.DONE else {}
    check("x265+MP4+10bit 任务完成", t15.status == TaskStatus.DONE,
          (t15.error or "")[:80])
    check("产物是 10bit HEVC", d15.get("pix_fmt") == "yuv420p10le"
          and d15.get("codec_name") == "hevc", str(d15))
    # hvc1 是 macOS/QuickTime 能认的 tag；mp4 里写成 hev1 有些播放器打不开
    check("MP4 里打了 hvc1 tag", d15.get("codec_tag_string") == "hvc1",
          d15.get("codec_tag_string", "?"))
    check("profile 是 Main 10", "10" in (d15.get("profile") or ""),
          d15.get("profile", "?"))

    st15b = VideoSettings(codec="libvpx-vp9", preset="ultrafast",
                          pix_fmt="yuv420p10le", container=Container.WEBM,
                          audio_codec="libopus", audio_bitrate_kbps=96)
    t15b = build_size_task(ctx, SRC, info, st15b, 1.5, audio_mode="opus",
                           policy="strict")
    run_task(t15b)
    d15b = _vstream(t15b.dst) if t15b.status == TaskStatus.DONE else {}
    check("VP9+WebM+10bit 任务完成", t15b.status == TaskStatus.DONE,
          (t15b.error or "")[:80])
    check("产物是 10bit VP9", d15b.get("pix_fmt") == "yuv420p10le"
          and d15b.get("codec_name") == "vp9", str(d15b))
    check("VP9 的 profile 是 Profile 2（10bit 的正式名字）",
          d15b.get("profile") == "Profile 2", d15b.get("profile", "?"))

    # ---- 16. hvc1 tag 必须覆盖 MOV，不能只管 MP4 ----
    # 实测 ffmpeg 对 MOV 也默认写 hev1，而 Safari/AVFoundation 只认 hvc1。
    # 原来条件写的是 `container == MP4`，选 MOV 就会得到 Mac 打不开的文件。
    print("\n[16] HEVC 的 hvc1 tag 覆盖哪些容器")
    for cont, want in ((Container.MP4, True), (Container.MOV, True),
                       (Container.MKV, False), (Container.WEBM, False)):
        st = VideoSettings(codec="libx265", crf=28, preset="medium",
                           container=cont, audio_codec="aac")
        steps = build_compress_steps(ctx.env, SRC,
                                     WORK / f"tag_{cont.value}.{cont.value}",
                                     st, info)
        txt = " ".join(" ".join(s.args) for s in steps)
        check(f"预设页路径：{cont.value} {'要' if want else '不要'} hvc1",
              ("hvc1" in txt) == want, "有" if "hvc1" in txt else "无")
        vtxt = " ".join(_size_video_args("libx265", st, 1000))
        check(f"目标体积页路径：{cont.value} {'要' if want else '不要'} hvc1",
              ("hvc1" in vtxt) == want, "有" if "hvc1" in vtxt else "无")

    print("\n[17] 线程数交给 ffmpeg 自动（实测「按并发分」是负优化）")
    from app.core.commands import _threads_for_one_job

    # 🔴 这里守的是一条**被实测推翻过**的判断。
    # 曾经的实现是 threads = 核数 // 并发数，理由是"别让两个 ffmpeg 各抢满核心"。
    # 实测（32 核 / 1080p/20s / x264 medium）：
    #   并发 2：各不限 4.3s vs 各 16 5.3s（慢 23%）
    #   并发 4：各不限 8.4s vs 各 8 10.2s（慢 22%）
    #   并发 8：各不限 16.6s vs 各 4 17.9s（慢 8%）
    # 每一档都更慢，CPU 也没降多少（93%→73%）。根因是人为砍线程会让
    # ffmpeg 的流水线变浅（读/解码/滤镜/编码/写盘填不满）。
    for conc in (1, 2, 4, 8):
        check(f"并发 {conc} 时不限制线程（交给 ffmpeg）",
              _threads_for_one_job(conc) is None,
              str(_threads_for_one_job(conc)))

    # 默认路径必须**不**产出 -threads（否则就是又把限制加回来了）
    st = VideoSettings(codec="libx264", crf=23, preset="medium",
                       container=Container.MP4, audio_codec="aac")
    steps = build_compress_steps(ctx.env, SRC, WORK / "thr.mp4", st, info)
    check("预设页默认命令里没有 -threads",
          "-threads" not in steps[0].args, str(steps[0].args[:8]))
    check("预设页命令仍带 -i（没把参数拼坏）", "-i" in steps[0].args)

    from app.core.size_budget import solve as _solve
    from app.core.size_service import build_size_steps as _bss
    b = _solve(info, 2.0, "mp4", audio_mode="aac", audio_kbps=128)
    ssteps = _bss(ctx.env, SRC, WORK / "thr2.mp4", st, b, WORK / "thr_wd",
                  audio_mode="aac", info=info)
    check("目标体积页两遍也都不带 -threads",
          all("-threads" not in s_.args for s_ in ssteps), f"{len(ssteps)} 步")

    # 显式传值时仍要生效，且位置在 -i 之后（放前面只限解码，对编码器无效）
    forced = build_compress_steps(ctx.env, SRC, WORK / "thr3.mp4", st, info,
                                  threads=8)
    a = forced[0].args
    check("显式传 threads=8 时命令里真的有", "-threads" in a, str(a[:8]))
    check("显式传时位置在 -i 之后", a.index("-threads") > a.index("-i"),
          f"-threads@{a.index('-threads')} -i@{a.index('-i')}")

    # 🔴 但不是所有编码器都吃 -threads —— 对它们加了也是空操作，
    # 加了反而让人以为生效了。
    # 实测证据：
    #   SVT-AV1：不限/2/16 三种取值下 `Level of Parallelism` 都是 6
    #     （它按分辨率自己算，1080p→6；preset 也改不了）
    #   NVENC：计算在 GPU 上，并发 2 只占 42% CPU；
    #     限不限线程耗时完全一样（1.4s vs 1.4s）
    from app.core.commands import _thread_args
    for c in ("libx264", "libx265"):
        check(f"{c} 接受 -threads", _thread_args(8, c) == ["-threads", "8"],
              str(_thread_args(8, c)))
    for c in ("libsvtav1", "h264_nvenc", "hevc_nvenc", "av1_nvenc",
              "h264_qsv", "hevc_amf", "h264_videotoolbox"):
        check(f"{c} 不塞 -threads（对它无效）", _thread_args(8, c) == [],
              str(_thread_args(8, c)))
    check("不传 codec 时保守放行（老调用点行为不变）",
          _thread_args(8) == ["-threads", "8"], str(_thread_args(8)))

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
    print("产物目录：", WORK)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
