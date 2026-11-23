"""视频压缩页。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                               QInputDialog, QLabel, QMenu,
                               QDoubleSpinBox, QFrame, QGridLayout,
                               QHBoxLayout, QPushButton, QRadioButton, QScrollArea,
                               QSizePolicy, QSlider,
                               QSpinBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from ...core.commands import (SAMPLE_SECONDS, SAMPLE_SECONDS_OPTIONS,
                             estimate_output_size_mb, sample_span,
                             truncated_info)
from ...core.pipeline import build_compress_task
from ...core.presets import (AUDIO_CODEC_OPTIONS, CONTAINER_LABEL, FPS_OPTIONS,
                             PIX_FMT_OPTIONS, PRESETS, PRESETS_BY_KEY,
                             SCALE_OPTIONS, TUNE_OPTIONS,
                             X264_PRESETS, Container, Preset, RateMode,
                             VideoSettings, codec_available, codec_labels,
                             is_hw_encoder, resolve_codec, ten_bit_note)
from ...core.quality import assess, describe as describe_quality
from ...core.size_budget import (Feasibility, container_overhead,
                                 describe_budget, estimate_output_bytes, solve)
from ...core.size_service import build_size_task
from ...core.utils import VIDEO_EXTS, human_size
from ..widgets import Card, Combo, FileArea, label
from .shared import ActionBar, OutputRow, Page, PresetChip, make_file_toolbar

MODE_ITEMS = [
    (RateMode.CRF, "恒定质量 CRF（推荐）"),
    (RateMode.BITRATE, "指定视频码率"),
]

# 目标大小单位：key -> (显示名, 每个 MB 折合多少该单位, 小数位, 步长, 最小, 最大)
# 内部规范值一律用 MB，单位只影响显示与输入；上界给得宽松，切单位时不会被钳掉
_UNIT_SPECS = {
    # key: (显示名, 每 MB 折合多少, 基准小数位, 步长, 最小, 最大)
    # 最小/最大给得宽松：切单位时只做换算，不该因为区间把用户的值钳掉
    "kb": ("KB", 1024.0, 0, 10.0, 1.0, 100_000_000.0),
    "mb": ("MB", 1.0, 0, 1.0, 0.01, 1_000_000.0),
    "gb": ("GB", 1.0 / 1024.0, 2, 0.1, 0.0001, 1000.0),
}


# 「速度档」在各家编码器上的去向（commands._speed_args 负责真正的翻译）。
# 除了 QSV，速度档都会被真正用到，所以只有 QSV 该禁用 —— 禁错会让用户以为坏了。
_SPEED_TIP = {
    "nvenc": "NVIDIA 硬件编码：这里选的档位会自动映射到 p1–p7（p1 最快，p7 最省体积）",
    "amf": "AMD 硬件编码：会自动映射到 speed / balanced / quality 三档",
    "videotoolbox": "VideoToolbox 只区分「是否画质优先」：选 medium 及以上即画质优先",
    "qsv": "Intel QSV 不使用速度档，此项不生效",
    "svtav1": "AV1：档位映射到 SVT 的 13~2（数值越大越快）",
}
_TUNE_TIP = "-tune 只对 x264 / x265 软编生效"

# 「按文件大小」页签的速度档多一句说明：目标体积要跑两遍编码
#
# 这一页码率被目标钉死，所以速度档是**唯一能用时间换画质的控件** ——
# 实测（10s 720p / 1500k）：ultrafast SSIM 0.961 → veryslow 0.974，
# 代价是 0.4s → 2.3s。它比在预设页更重要，文案要把这层意思说透。
_SIZE_SPEED_NOTE = ("目标体积要跑两遍编码（第一遍分析、第二遍出片），"
                    "所以耗时约为「按常用预设」里同一档位的两倍。\n"
                    "这一页码率被目标钉死，速度档是唯一能「用时间换画质」的控件："
                    "同样的体积，越慢画质越好（实测 ultrafast→veryslow "
                    "SSIM 0.961→0.974，耗时约 6 倍）。")
_SIZE_SPEED_NOTE_AV1 = ("AV1 走单遍「恒定质量 + 体积上限」，不需要两遍；"
                        "体积精度靠编码后自动修正。")

# 快速单遍档用不上的速度档：慢档的收益来自"第二遍按第一遍的统计分配比特"，
# 单遍拿不到。实测单遍+veryslow（3.5s）比两遍+medium（2.0s）还慢。
_SLOW_PRESETS = {"slow", "slower", "veryslow"}

# 试压：只压中间一小段，产物名带这个后缀（永不与正常输出撞名）
SAMPLE_SECONDS = 20
SAMPLE_SUFFIX = "_试压"

# 「用途」一键配置：把编码器 + 容器 + 色深 + 音频一次配好。
#
# 🔴 设计原则：**按"在哪播"分档，档数必须等于真正的结论数**。
#
# 第一版列了三档（发微信/放网页/自己存），被用户当场指出两个问题：
#   ① 「发微信」和「放网页」参数一模一样 —— 纯重复，只会让人困惑；
#   ② 「自己存着」给的是 H.265，但那不是"效率最高"，只是"比 H.264 高"。
#
# 从第一性原理看，决定参数的其实只有**一个问题**：这段视频要在哪播？
#   - 要在**别人的设备**上播（手机/电视/电脑/浏览器/上传平台）→ 兼容性优先
#     → H.264 8bit + MP4 + AAC。这是唯一"谁都能播"的组合。
#       上传到抖音/B站/淘宝也一样：平台自己转码成 H.264 交付，
#       在这条链路上纠结编码器是白纠结，H.264 + 高码率留出的画质余量才值钱。
#       浏览器同理（只硬解 H.265、不软解，老设备/Linux 桌面直接黑屏），
#       所以"发别人"和"放网页"是**同一个答案**，合并成一档。
#   - 只在**自己这台电脑**上看（播放器自己装、能软解）→ 效率优先
#     → SVT-AV1 8bit + MP4 + AAC。没有兼容性包袱，直接吃最高的压缩率。
#       实测 1080p/6s：SVT-AV1 preset 6（1505ms）比 x265 medium（1697ms）
#       **还快**；低码率段 SSIM 更高（300k 时 0.892 vs x265 0.884 vs x264 0.849）。
#
# 色深一律 8bit：兼容档自不必说；本地档虽是软解，但 8bit 省得纠结
# （10bit 的收益只有 8~12% 码率，而"能不能在这台机器上放"是不确定项）。
USE_CASES = [
    ("share", "要发给别人 / 放网页", "libx264", "mp4", "", "aac",
     "H.264 8bit + MP4 + AAC：手机、电视、电脑、浏览器全都能播。"
     "上传到抖音/B站/淘宝时平台会自己转码成 H.264，所以用 H.264 + 高码率"
     "反而留出最大画质余量。浏览器也只认 H.264（对 H.265 只硬解不软解，"
     "老设备和 Linux 桌面会黑屏），放网页同样用这个。"),
    ("local", "只在自己电脑看", "libsvtav1", "mp4", "", "aac",
     "AV1 + MP4 + AAC：本地播放器（mpv / PotPlayer / VLC）都能软解，"
     "不用管兼容性，直接拿最高的压缩率——同画质下比 H.264 省一半左右"
     "（实测四段素材 47~64%），比 H.265 再省三成半。"
     "1080p 下编码速度还比 H.265 快得多。"
     "代价：拷给别人或传到手机可能播不了（下拉里那句「需较新设备播放」说的就是这个）。"),
]


def _speed_available(real: str) -> bool:
    """速度档对当前编码器是否有效。QSV 的 -preset 会被 ffmpeg 忽略。"""
    return not real.endswith("_qsv")


def _speed_tip(real: str) -> str:
    if real == "libsvtav1":
        return _SPEED_TIP["svtav1"]
    for key, text in _SPEED_TIP.items():
        if real.endswith("_" + key):
            return text
    return ""


# 色深提示：分「编码器能不能编 10 位」（实测）与「编出来能不能播」（格式兼容）
# 两件事说，别混成一句"兼容性稍差"。
_PIXFMT_TIP_BASE = ("10bit 提高编码器内部精度：即使源是 8bit，也能减轻天空/暗场的"
                    "色带。实测同画质下省约 8~12% 码率（x264 8.0% / x265 8.8% / "
                    "AV1 11.8%），代价是编码慢 20~50%（x264 最明显）。")


def _pixfmt_state(env, real: str) -> tuple[bool, str]:
    """当前编码器能不能编 10 位色深；不能时给出人话原因。

    env 为空（还没检测到 ffmpeg）时按"能用"处理——真正的失败会由编码
    本身报出来，不该在界面上先禁掉一个可能可用的选项。
    """
    if env is None or not real or not getattr(env, "ready", False):
        return True, ""
    if env.encoder_usable_10bit(real):
        return True, ""
    why = env.ten_bit_reason(real) or "实测编码失败"
    return False, why


def _set_label_role(lbl, role: str) -> None:
    """运行时换 QSS 角色（#Warn / #Hint）并让它立刻生效。

    🔴 只改 objectName 不够：Qt 在控件显示过之后不会重新匹配选择器，
    颜色还停在旧角色上（实测「💡 提示」照样是警告的橙色，两级提示看不出区别）。
    必须 unpolish/polish 一遍才重算。
    """
    if lbl.objectName() == role:
        return
    lbl.setObjectName(role)
    style = lbl.style()
    if style is not None:
        style.unpolish(lbl)
        style.polish(lbl)
    lbl.update()


def _gate_codec_items(combo: Combo, env) -> None:
    """把本机用不了的编码器项灰掉（仍留在列表里，并写明原因）。

    沿用「速度档」「10bit」那条规矩：禁用必须给原因，别让选项凭空消失。
    这里比它们更要紧 —— 用不了的硬件项**不会报错**：resolve_codec 挑不出硬件
    就静默回退到 libx264，用户以为在跑硬件加速，实际出片慢好几倍，界面上
    一点异样都看不出来。灰掉它，这个陷阱才关得上。

    ⚠️ 别拿 resolve_codec() 的结果判断可用性（它对硬件项永远返回一个能跑的
    编码器），要用 codec_available()。
    """
    model = combo.model()
    for i in range(combo.count()):
        value = combo.itemData(i)
        ok, why = codec_available(value, env)
        item = model.item(i)
        if item is None:
            continue
        item.setEnabled(ok)
        item.setToolTip("" if ok else f"本机用不了：{why}")


_CRF_HINT_BASE = ("CRF 数值越小画质越好、体积越大，范围 1–63、支持一位小数；"
                  "23 是通用起点，追求体积可用 26–28，追求画质用 18–20。"
                  "（H.264 上限 51，超出会自动钳制）")

# CRF 刻度跨位深**不可比**（多个来源都强调这点，实测也确认）：
# x265 CRF 26 下 10bit 反而比 8bit **大 3.2%** —— 因为同样的 CRF 在 10bit 下
# 代表更高画质。10bit 的效率优势只在"同画质"下成立（实测省 8~12% 码率）。
# 这段只在 10bit 时追加在 CRF 说明后面。⚠️ 别和色深提示行说同一件事：
# 两句都讲"同 CRF 下 10bit 更大"，用户会以为软件在复读（原来就是这样）。
_CRF_HINT_10BIT = ("\n注意：10bit 下同样的 CRF 数值代表「更高画质、更大文件」"
                   "（实测 x265 CRF 26：10bit 比 8bit 大 3.2%）。")


def _pixfmt_tip(real: str, ok: bool, why: str) -> str:
    if not ok:
        return (f"⚠ 当前编码器（{real}）不支持 10 位色深，已回退 8bit：{why}")
    return _PIXFMT_TIP_BASE + "\n" + ten_bit_note(real)


def _pixfmt_plain(real: str, crf_note: bool) -> tuple[str, bool]:
    """选中 10bit 时那句**看得见**的大白话；第二个返回值表示是不是警告。

    为什么必须看得见、且必须说人话：10bit 的信息原来全在 tooltip 里，
    而小白用户不会去悬停。他们要判断的其实只有三件事 ——
    ① 值不值（好处）② 会不会变慢（代价）③ 能不能播（兼容）。
    这里就按这三件事写，术语（Hi10P / 色带 / bpp）一律换成生活化说法。
    """
    is_h264 = real == "libx264" or real.startswith("h264_")
    if is_h264:
        # 兼容性是硬伤，压过一切好处：说清"打不开"比说清"画质好"重要
        return ("⚠ 这个组合手机和电视打不开：H.264 的 10bit（Hi10P）没有设备"
                "能硬解，只能电脑软解。要发给别人、要在手机上看，请改回 8bit；"
                "想留住 10bit 的好处，把编码器换成 H.265 或 AV1。", True)
    if crf_note:
        # 预设页按 CRF 定画质：同 CRF 在 10bit 下是更高画质、更大文件
        return ("💡 天空、暗部的条纹会变少，画面更干净。注意这一页按 CRF 定画质，"
                "同样的 CRF 在 10bit 下等于更高画质、文件也更大——想压小就调高 CRF，"
                "或者用「按文件大小」页（那边体积固定，10bit 是白赚的）。", False)
    # 「按文件大小」页：体积被目标钉死，10bit 只赚不亏
    return ("💡 体积不会变大：天空、暗部的条纹会变少，同样大小下画面更干净。"
            "代价是编码慢两三成。", False)


# 「按文件大小」页签的音频策略。
# 这里**只放编码器选择**，不放策略：「预算不够时压不压音频」是另一个问题，
# 由 cb_shrink 那个复选框统一表达。（原来第一项叫「自动（预算紧时降码率）」，
# 名字描述的是复选框的语义，实际效果又和「重编码为 AAC」完全一样 —— 实测各容器下
# 两者解析出的编码器逐个相同，纯属重复选项。）
SIZE_AUDIO_OPTIONS = [
    ("copy", "保留原始音轨"),
    ("aac", "重编码为 AAC"),
    ("mp3", "MP3"),
    ("opus", "Opus（体积最小）"),
    ("none", "移除音轨"),
]
# 各容器能承载哪些音频选项：Opus 放 MP4/MOV 里播放器支持太差，不提供；
# MP3 塞不进 WebM（会报 Could not write header），WebM 下也不提供
CONTAINER_AUDIO_MODES = {
    "mp4": ("copy", "aac", "mp3", "none"),
    "mov": ("copy", "aac", "mp3", "none"),
    "mkv": ("copy", "aac", "mp3", "opus", "none"),
    "webm": ("copy", "opus", "none"),
}
# 换容器后默认选哪个：必须显式给，否则会落到列表第一项「保留原始音轨」，
# 那等于默认不压缩音频（体积最大），跟这个页签"压到目标以内"的定位相反
CONTAINER_DEFAULT_AUDIO = {
    "mp4": "aac", "mov": "aac", "mkv": "aac", "webm": "opus",
}

# MP4/MOV 里**浏览器和多数播放器放不出声**的音频编码。
# ffmpeg 能把这些流原样封进 MP4（实测 AC3 成功、不报错），但封进去不等于能播：
# 网页 <video> 只认 AAC/MP3，拿到 AC3/DTS 就是「有画面没声音」，DTS 甚至可能
# 直接黑屏。源是这些格式时选「保留原始音轨」必须警告。
_POOR_MP4_AUDIO = {
    "ac3": "AC3（杜比数字）",
    "eac3": "E-AC3",
    "dts": "DTS",
    "truehd": "TrueHD",
    "flac": "FLAC",
    "vorbis": "Vorbis",
    "opus": "Opus",
    "pcm_s16le": "未压缩 PCM",
    "pcm_s24le": "未压缩 PCM",
    "wmav2": "WMA",
}
# 这些容器才需要考虑上面的兼容性（MKV 用户一般自己播，不在此列）
_STRICT_AUDIO_CONTAINERS = {"mp4", "mov"}


def audio_compat_warning(container: str, audio_mode: str,
                         info) -> str:
    """「保留原始音轨」进 MP4/MOV 时，源音频可能放不出声 → 返回警告文本。

    为什么要管这件事：ffmpeg 会**成功**把 AC3/DTS 封进 MP4，任务显示"完成"，
    用户拿去发微信/传网页才发现没声音 —— 比直接报错还难受。
    只在用户明确选了流复制时提示（重编码成 AAC 就没这个问题）。
    """
    if audio_mode != "copy" or container not in _STRICT_AUDIO_CONTAINERS:
        return ""
    if info is None or not getattr(info, "audios", None):
        return ""
    bad = [(a.codec_name, _POOR_MP4_AUDIO.get(a.codec_name, a.codec_name))
           for a in info.audios if a.codec_name in _POOR_MP4_AUDIO]
    if not bad:
        return ""
    names = "、".join(sorted({label for _, label in bad}))
    return (f"⚠ 源音轨是 {names}，原样放进 {container.upper()} 后网页和很多播放器"
            f"会「只有画面没有声音」。改成「重编码为 AAC」就正常了。")


def _fdk_option(env) -> tuple[str, str] | None:
    """libfdk_aac 可用时返回音频下拉的追加项；否则 None。

    fdk 是非自由库，常用构建（gyan/BtbN）都不带 —— 换了带 fdk 的 ffmpeg
    后这里会自动出现该选项。
    """
    if env is not None and env.encoder_available("libfdk_aac"):
        return ("libfdk_aac", "AAC-FDK（低码率音质更优）")
    return None


class FpsCombo(Combo):
    """帧率下拉：预置档位 + 「自定义…」让用户直接填帧率。

    自定义项的 data 恒为 None —— 这样再次选择它会重新弹出输入框；
    实际数值存在 _custom_value，currentData() 对外返回它，
    下游 `currentData() or 0.0` 的取值逻辑完全不用改。
    取消输入则回退到上一个档位。
    """

    CUSTOM_LABEL = "自定义…"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._custom_value: float | None = None
        self._prev_index = 0            # 上一个非自定义选择（取消时回退）
        self.currentIndexChanged.connect(self._remember_prev)

    def add_fps_options(self, options) -> None:
        for value, text in options:
            self.addItem(text, value)
        self.addItem(self.CUSTOM_LABEL, None)

    def _remember_prev(self, index: int) -> None:
        if index >= 0 and self.itemData(index) is not None:
            self._prev_index = index

    def _on_item_clicked(self, index) -> None:      # noqa: N802
        row = index.row()
        if not (0 <= row < self.count()) or self.itemData(row) is not None:
            super()._on_item_clicked(index)
            return

        was_custom = self.currentIndex() == row
        value, ok = QInputDialog.getDouble(
            self.window(), "自定义帧率", "帧率（fps，1–240）：",
            self._custom_value if self._custom_value else 29.97,
            1.0, 240.0, 2)
        if ok:
            self._custom_value = float(value)
            self.setItemText(row, f"自定义（{value:g} fps）")
            self.setCurrentIndex(row)
            self.hidePopup()
            # 索引可能没变（重复选择自定义项），手动通知页面重算
            self.currentIndexChanged.emit(row)
        elif was_custom:
            self.hidePopup()         # 已是自定义，取消 = 保留当前值
        else:
            self.hidePopup()
            self.setCurrentIndex(self._prev_index)

    def currentData(self, role=Qt.ItemDataRole.UserRole):       # noqa: N802
        row = self.currentIndex()
        if 0 <= row < self.count() and self.itemData(row) is None:
            return self._custom_value if self._custom_value is not None else 0.0
        return super().currentData(role)

    def findData(self, value, role=Qt.ItemDataRole.UserRole):   # noqa: N802
        idx = super().findData(value, role)
        if idx < 0 and self._custom_value is not None and value == self._custom_value:
            for r in range(self.count()):
                if self.itemData(r) is None:
                    return r
        return idx


class _SizeSpin(QDoubleSpinBox):
    """目标大小输入框：能输小数，但不显示多余的零。

    QDoubleSpinBox 的 decimals 同时决定「显示几位」和「能输入几位」：
    设 0 位就输不进 0.5，设 2 位又会让 100 显示成 100.00（假精度）。
    所以固定 2 位保证输入能力，再在显示时把末尾的零去掉。
    """

    def textFromValue(self, value: float) -> str:      # noqa: N802
        text = super().textFromValue(value)
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"


class _TabBar(QWidget):
    """分段控件样式的页签条。

    外观（轨道底 + 选中项浮起块）定义在 style.py 的 #TabBar / #TabButton 里，
    这样深色主题会自动跟随；这里只负责结构。
    轨道只包住按钮，所以不加内部 stretch，由外部布局负责左对齐。
    """

    changed = Signal(int)

    def __init__(self, titles: list[str], parent=None):
        super().__init__(parent)
        self.setObjectName("TabBar")
        # 纯 QWidget 要开这个属性，QSS 的 background 才会绘制
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        self._index = 0
        self._buttons: list[QPushButton] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for i, text in enumerate(titles):
            btn = QPushButton(text)
            btn.setObjectName("TabButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setChecked(i == 0)
            btn.clicked.connect(lambda _=False, idx=i: self.select(idx))
            group.addButton(btn, i)
            self._buttons.append(btn)
            lay.addWidget(btn)

    def index(self) -> int:
        return self._index

    def select(self, index: int) -> None:
        if index == self._index:
            return
        self._index = index
        for i, btn in enumerate(self._buttons):
            btn.setChecked(i == index)
        self.changed.emit(index)


class CompressPage(Page):
    requestQueue = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, "视频压缩",
                         "拖入视频，点一个预设就能开始。要指定输出大小，切到「按文件大小」。")
        self._loading = False
        self._quality = None       # 最近一次画质评估结果（QualityAssessment）
        self._page_heights = {}    # 各页签的内容高度缓存，用于取最大值

        # ---------------- 文件 ---------------- #
        from ..widgets import FileTable
        self.table = FileTable(ctx.env_provider, VIDEO_EXTS)
        self.root.addWidget(FileArea(
            self.table, make_file_toolbar(self.table, VIDEO_EXTS)), 1)

        # ---------------- 平级页签：按常用预设 / 按文件大小 ---------------- #
        # 文件区、输出行、操作栏共用；只有中间这块随页签切换。
        self.tab_bar = _TabBar(["按常用预设", "按文件大小"])
        self.tab_bar.changed.connect(self._on_tab_changed)
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.addWidget(self.tab_bar)
        tab_row.addStretch(1)      # 轨道只包住按钮，其余留给空白
        self.root.addLayout(tab_row)

        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self._build_preset_tab())
        self.content_stack.addWidget(self._build_size_tab())
        # 默认 QStackedWidget 高度取所有子页的最大值，会让「按文件大小」页
        # 底部留出一大块空白。改为跟随当前页高度。
        self._settle_stack_size()
        self.root.addWidget(self.content_stack)

        # ---------------- 输出 + 操作 ---------------- #
        self.bar = ActionBar("开始压缩", OutputRow(ctx))
        self.bar.button.clicked.connect(self._on_start)

        # 试压：用当前参数只压中间一小段，十几秒就能看到效果。
        #
        # 样式：用**默认 QPushButton**（和「添加文件」「清空」那些一致）。
        # 原来用了 Ghost（透明底 + 无边框 + 链接色），在操作栏里孤零零一个
        # 描边都没有，看着像漏了样式 —— 用户提的就是这个。
        #
        # 🔴 按钮文字**只写「试压」**，不带秒数。
        # 踩过的坑：先做成「试压 20 秒」（写死）→ 短片撒谎；改成动态显示
        # 「试压 5 秒」→ 操作栏里常驻一个秒数，用户会以为"开始压缩"也用这个
        # 长度，于是不知道怎么才能压完整片（用户报的就是这个）。
        # 根因是把**一次性动作的参数**做成了**常驻应用状态**。
        # 现在：秒数只出现在菜单里，选完菜单就关，操作栏不留痕迹。
        # 菜单第一项直接就是"试压 20 秒"这种带秒数的完整命令 —— 点哪项就
        # 用哪个长度压，语义是"选一个动作"而不是"设一个参数"。
        #
        # 秒数在短片上的收敛仍然保留（8 秒视频点"试压 20 秒"实际压 8 秒），
        # 用 tooltip 和状态栏说明，不占用按钮文字。
        self._sample_seconds = SAMPLE_SECONDS
        # 🔴 用 QPushButton 而不是 QToolButton：
        # QToolButton 在 QSS 里**没有任何规则**（style.py 只定义了 QPushButton），
        # 于是它既不像「开始压缩」，禁用时也只是文字变淡、底色边框都不变，
        # 跟旁边按钮完全不是一套。它必须和「开始压缩」长得像一家人。
        self.btn_sample = QPushButton("试压")
        self.btn_sample.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sample.setMinimumWidth(88)

        self.sample_menu = QMenu(self.btn_sample)
        self._sample_acts: list[QAction] = []
        for sec in SAMPLE_SECONDS_OPTIONS:
            act = self.sample_menu.addAction(f"试压 {sec} 秒")
            act.triggered.connect(
                lambda _=False, s=sec: self._start_sample_with(s))
            self._sample_acts.append(act)
        # 用 clicked → exec 弹菜单（同 OutputRow 的做法），不用 setMenu：
        # QPushButton.setMenu 会在文字右边自动留一块箭头区域，宽度和
        # 默认 padding 打架，反而和「开始压缩」对不齐。
        self.btn_sample.clicked.connect(self._show_sample_menu)
        self._refresh_sample_button()

        self.bar.layout().insertWidget(self.bar.layout().count() - 1, self.btn_sample)
        self.root.addWidget(self.bar)

        self.table.filesChanged.connect(self._update_estimate)
        self.table.filesChanged.connect(self._refresh_sample_button)
        self._select_preset("balanced")
        self._update_estimate()

    # ------------------------------------------------------------------ #
    # 页签
    # ------------------------------------------------------------------ #
    def _on_tab_changed(self, index: int) -> None:
        self.content_stack.setCurrentIndex(index)
        self._settle_stack_size()
        self._update_estimate()
        # 预设卡的流式布局高度要等一次布局才稳定，延后再贴合一次
        QTimer.singleShot(0, self._settle_stack_size)

    def resizeEvent(self, event) -> None:      # noqa: N802
        super().resizeEvent(event)
        # 宽度变化会改变预设卡换行数 / 说明文字折行数，容器的固定高度要跟着调
        QTimer.singleShot(0, self._settle_stack_size)

    def _settle_stack_size(self) -> None:
        """页签容器用「两页中较高的那个」的固定高度。

        两个约束要同时满足：
        1. 必须固定高度，不能只设最小高度 —— 容器的垂直策略是可扩展的，
           窗口拉高时多余空间会被它吸收，表现成「编码参数卡片下方一大片空白」。
           多余高度应该给文件列表（它在 Page.root 里带 stretch=1）。
        2. 取两页的最大值 —— 若按当前页高度收缩，切页签时上方文件区会跟着变高变矮，
           视觉上很跳。代价是较矮那页底部留一点空白（几十像素，可接受）。

        隐藏页不会被布局，sizeHint 可能是旧值，所以按页缓存「见过的最大高度」。
        """
        idx = self.content_stack.currentIndex()
        page = self.content_stack.currentWidget()
        if page is None:
            return
        # 先让内部布局重新计算（标签显隐后 sizeHint 才会更新）
        lay = page.layout()
        if lay is not None:
            lay.activate()
        hint = page.sizeHint().height()
        if hint > 0:
            self._page_heights[idx] = hint

        target = max(self._page_heights.values()) if self._page_heights else 0
        # 值没变就不重复设置：避免「设置高度 → 触发布局 → 再触发本函数」的递归
        if target > 0 and self.content_stack.height() != target:
            self.content_stack.setFixedHeight(target)

    def _current_tab(self) -> str:
        return "size" if self.tab_bar.index() == 1 else "preset"

    def _build_preset_tab(self) -> QWidget:
        """按常用预设页签：预设卡片 + 编码参数，与改造前一致。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(self._build_presets())
        lay.addWidget(self._build_params())
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    # 按文件大小页签
    # ------------------------------------------------------------------ #
    def _build_size_tab(self) -> QWidget:
        """目标体积是硬约束：编码器/分辨率等仍可选，CRF 与码率不再出现。"""
        card = Card()
        card.add(label("目标大小", "SectionTitle"))

        # 数字与单位分开：单位是"选择"，不是让用户敲的内容
        self.spin_target = _SizeSpin()
        # 4 位小数：任何单位的换算都不会被舍成 0（显示时末尾的零会被裁掉）
        self.spin_target.setDecimals(4)
        # 必须先设范围：QDoubleSpinBox 默认是 0~99.99，会让 100 以上被悄悄钳住
        self.spin_target.setRange(*_UNIT_SPECS["mb"][4:6])
        self.spin_target.setSingleStep(1.0)
        self.spin_target.setValue(100.0)
        self.spin_target.setMinimumWidth(90)
        self.spin_target.setMaximumWidth(120)   # KB 单位下可能到 7 位数字
        self.spin_target.valueChanged.connect(self._on_target_value_changed)

        self._unit = "mb"             # 当前显示单位
        self.cmb_unit = Combo()
        for key in ("kb", "mb", "gb"):
            self.cmb_unit.addItem(_UNIT_SPECS[key][0], key)
        self.cmb_unit.setCurrentIndex(1)          # 默认 MB
        self.cmb_unit.setFixedWidth(66)
        self.cmb_unit.currentIndexChanged.connect(self._on_unit_changed)

        target_input = QWidget()
        ti = QHBoxLayout(target_input)
        ti.setContentsMargins(0, 0, 0, 0)
        ti.setSpacing(6)
        ti.addWidget(self.spin_target)
        ti.addWidget(self.cmb_unit)
        ti.addStretch(1)

        # 左侧标签列 + 右侧内容，和下方「编码参数」卡片同一种排法。
        # 只有两档：耗时（两遍 / 单遍）与体积利用率（~98% / ~93%）两个维度，
        # 两个点就够；原「尽量不超过」档花同样的两遍时间却不做校验，已删除。
        self.rb_strict = QRadioButton("严格不超过（默认）")
        self.rb_strict.setChecked(True)
        self.rb_strict.setToolTip(
            "跑两遍：第一遍分析全片复杂度，第二遍按目标码率分配比特。\n"
            "编码后实测体积，一旦超过目标就自动降码率重跑，保证不大于目标。\n"
            "最贴近目标（实测约 98%），耗时约为单遍的两倍。")
        self.rb_fast = QRadioButton("快速单遍（最快）")
        self.rb_fast.setToolTip(
            "不跑第一遍分析，按预算码率打折直接编码，耗时约为两遍方案的一半。\n"
            "体积精度靠修正循环兜底：超出目标会自动降码率重编，保证不超。\n"
            "用掉的目标空间略少（实测约 93%），画质的码率分配不如两遍精准。")
        self.policy_group = QButtonGroup(self)
        self.policy_group.addButton(self.rb_strict, 0)
        self.policy_group.addButton(self.rb_fast, 1)
        self.policy_group.buttonClicked.connect(self._on_policy_changed)

        self.cb_shrink = QCheckBox("允许压缩音频（预算不够时自动降音频码率）")
        self.cb_shrink.setToolTip(
            "勾上：目标是硬指标，预算不够时连音频一起降，优先保住画面。\n"
            "不勾：音频码率固定（AAC/MP3 128 kbps，Opus 96 kbps），\n"
            "　　　预算不够只会压视频；音频本身就超预算时会提示「做不到」。")
        self.cb_shrink.toggled.connect(lambda _: self._update_estimate())

        self.cb_fill = QCheckBox("尽量用满目标（低于目标时继续加码率）")
        # 默认勾上：用户要的是「按文件大小」，用满是这个诉求的自然期待。
        # 不需要时它不会动 —— 只有低于目标 5% 才触发（普通素材两遍 ABR
        # 稳定在 96~98%，压根到不了这条线）。
        self.cb_fill.setChecked(True)
        self.cb_fill.setToolTip(
            "勾上（默认）：编出来比目标小很多时，自动加码率重跑，把剩余空间用掉。\n"
            "不勾：一次成型。普通素材两遍编码已经能到目标的 96~98%，"
            "为剩下几个百分点多跑一轮通常不值。\n"
            "纯色、静止这类内容加码率也不涨体积，会自动停手（会多花一点时间）。")
        self.cb_fill.toggled.connect(lambda _: self._update_estimate())

        self.lbl_policy_hint = label("", "Hint", wrap=True)

        # 右侧内容竖排：单选一行 → 模式说明
        # （「允许压缩音频」不放这里：它约束的是音频编码器，挪到「编码参数」里
        #   音频那一行下面，跟它真正影响的控件挨在一起）
        radio_row = QHBoxLayout()
        radio_row.setContentsMargins(0, 0, 0, 0)
        radio_row.setSpacing(18)
        radio_row.addWidget(self.rb_strict)
        radio_row.addWidget(self.rb_fast)
        radio_row.addStretch(1)

        opts_holder = QWidget()
        opts_col = QVBoxLayout(opts_holder)
        opts_col.setContentsMargins(0, 0, 0, 0)
        opts_col.setSpacing(7)
        opts_col.addLayout(radio_row)
        # 「尽量用满」紧跟体积控制：它约束的就是"要不要把目标用干净"
        opts_col.addWidget(self.cb_fill)
        opts_col.addWidget(self.lbl_policy_hint)

        target_grid = QGridLayout()
        target_grid.setContentsMargins(0, 0, 0, 0)
        target_grid.setHorizontalSpacing(10)
        target_grid.setVerticalSpacing(10)
        target_grid.addWidget(label("输出不超过"), 0, 0)
        target_grid.addWidget(target_input, 0, 1)
        target_grid.addWidget(label("体积控制"), 1, 0, Qt.AlignmentFlag.AlignTop)
        target_grid.addWidget(opts_holder, 1, 1)
        target_grid.setColumnStretch(1, 1)
        target_holder = QWidget()
        target_holder.setLayout(target_grid)
        card.add(target_holder)

        # 预检结果：可行 / 勉强 / 做不到（无文件时隐藏，避免留出一片空白）
        self.size_status = label("", "Hint", wrap=True)
        self.size_status.setVisible(False)
        card.add(self.size_status)

        self.size_detail = label("", "Hint", wrap=True)
        self.size_detail.setVisible(False)
        card.add(self.size_detail)

        # 画质评估 + 一键采纳降分辨率
        self.lbl_quality = label("", "Hint", wrap=True)
        self.lbl_quality.setVisible(False)
        card.add(self.lbl_quality)

        self.btn_apply_res = QPushButton("")
        self.btn_apply_res.setObjectName("Primary")
        # 一键采纳是最该突出的动作：原来是无样式的默认灰按钮，宽 140、
        # 缩在提示下面 —— 用户反馈"一点儿都不明显"。
        self.btn_apply_res.setMinimumHeight(30)
        self.btn_apply_res.clicked.connect(self._apply_suggested_resolution)
        self.lbl_apply_hint = label("", "Hint", wrap=True)
        self.lbl_apply_hint.setVisible(False)
        res_row = QHBoxLayout()
        res_row.setContentsMargins(0, 0, 0, 0)
        res_row.setSpacing(10)
        res_row.addWidget(self.btn_apply_res)
        res_row.addWidget(self.lbl_apply_hint, 1)
        res_holder = QWidget()
        res_holder.setLayout(res_row)
        res_holder.setVisible(False)
        self._res_holder = res_holder
        card.add(res_holder)

        # --- 编码选项 ---
        self.size_codec = Combo()
        for value, text in codec_labels(self.ctx.env):
            self.size_codec.addItem(text, value)
        _gate_codec_items(self.size_codec, self.ctx.env)
        self.size_codec.currentIndexChanged.connect(lambda _: self._update_estimate())

        self.size_speed = Combo()
        # 与预设页同一套档位：目标体积模式下码率被目标钉死，
        # 慢档=同样的体积换更好的画质，正是这里最该提供的选项。
        # （代价是两遍编码，耗时约为预设页的单遍两倍 —— 在 tooltip 里说明。）
        for name in X264_PRESETS:
            self.size_speed.addItem(_speed_label(name), name)
        self.size_speed.setCurrentIndex(
            max(0, self.size_speed.findData("medium")))
        # 编码器一变就同步速度档的可用状态（同样只有 QSV 用不上）
        self.size_codec.currentIndexChanged.connect(
            self._on_size_codec_changed)

        self.size_scale = Combo()
        for value, text in SCALE_OPTIONS:
            self.size_scale.addItem(text, value)
        self.size_scale.currentIndexChanged.connect(lambda _: self._update_estimate())

        self.size_fps = FpsCombo()
        self.size_fps.add_fps_options(FPS_OPTIONS)
        self.size_fps.currentIndexChanged.connect(
            lambda _: self._update_estimate())

        self.size_container = Combo()
        for c, text in CONTAINER_LABEL.items():
            self.size_container.addItem(text, c.value)
        self.size_container.currentIndexChanged.connect(self._on_container_changed)

        self.size_audio = Combo()
        self.size_audio.currentIndexChanged.connect(self._on_size_audio_changed)
        # 不要在构造里先填一遍选项：那样第一项「保留原始音轨」会被下面当成
        # "用户已有的选择"，于是默认值永远落不到 _DEFAULT_AUDIO 上。
        # 交给 _on_container_changed 一次性按容器填好（它会选出该容器的默认项）。
        self._on_container_changed(self.size_container.currentIndex())

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        self.size_pixfmt = Combo()
        for value, text in PIX_FMT_OPTIONS:
            self.size_pixfmt.addItem(text, value)
        # 用户手动动过色深没有？动过就别再自动跟随编码器（见 _auto_pixfmt_for_size）
        self._pixfmt_user_set = False
        self.size_pixfmt.activated.connect(self._on_size_pixfmt_activated)
        # 警告标签必须在 _on_size_codec_changed() 之前存在（它会在构造途中被调）
        self.lbl_pixfmt_warn_size = label("", "Warn", wrap=True)
        self.lbl_pixfmt_warn_size.setVisible(False)
        # 切换色深本身也要重算警告（编码器没变，但选没选 10bit 变了）
        self.size_pixfmt.currentIndexChanged.connect(
            lambda _: self._update_pixfmt_warn(
                self.size_pixfmt,
                resolve_codec(self.size_codec.currentData() or "libx264",
                              self.ctx.env),
                self.lbl_pixfmt_warn_size))
        # 音频兼容性警告（源是 AC3/DTS 却选了「保留原始音轨」进 MP4）
        self.lbl_audio_warn = label("", "Warn", wrap=True)
        self.lbl_audio_warn.setVisible(False)
        # 首次同步放在色深下拉建好之后：_on_size_codec_changed 要按编码器
        # 开关「10bit」这一项（tooltip 也由它按编码器写，不再从预设页抄一句）
        self._on_size_codec_changed()

        pairs = [
            ("编码器", self.size_codec, "编码速度", self.size_speed),
            ("分辨率", self.size_scale, "帧率", self.size_fps),
            ("音频", self.size_audio, "输出格式", self.size_container),
        ]
        r = 0
        for l1, w1, l2, w2 in pairs:
            grid.addWidget(label(l1), r, 0)
            grid.addWidget(w1, r, 1)
            grid.addWidget(label(l2), r, 2)
            grid.addWidget(w2, r, 3)
            r += 1
            if l1 == "分辨率":
                grid.addWidget(label("色深"), r, 0)
                grid.addWidget(self.size_pixfmt, r, 1, 1, 3)
                r += 1
                # 10bit 的"能不能播"警告：tooltip 不够显眼，而 H.264+10bit
                # （Hi10P）几乎没有设备能硬解。目标体积页常用于发送/存档，
                # 压出没人能播的文件很糟。只在真的选了 10bit 时占位显示。
                grid.addWidget(self.lbl_pixfmt_warn_size, r, 1, 1, 3)
                r += 1
            if l1 == "音频":
                # 紧贴音频下拉：它的作用对象就是这一格选的编码器
                grid.addWidget(self.cb_shrink, r, 1, 1, 3)
                r += 1
                # 源音轨是 AC3/DTS 这类、又选了「保留原始音轨」进 MP4 时，
                # 封进去能成功但网页放不出声 —— 这一格就是警告那件事
                grid.addWidget(self.lbl_audio_warn, r, 1, 1, 3)
                r += 1
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        holder = QWidget()
        holder.setLayout(grid)

        # 编码参数单独成卡：与上面的「目标大小」平级，视觉上不再有一段空洞
        card2 = Card()
        card2.add(label("编码参数", "SectionTitle"))

        # 「用途」一键配置：先问"你要拿去干嘛"，再谈参数。
        # 放在参数之上，是因为小白真正知道的是用途，不是编码器。
        use_row = QHBoxLayout()
        use_row.setContentsMargins(0, 0, 0, 0)
        use_row.setSpacing(8)
        use_row.addWidget(label("我要拿去"))
        for key, text, *_ in USE_CASES:
            chip = QPushButton(text)
            chip.setObjectName("SizeChip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(next(u[6] for u in USE_CASES if u[0] == key))
            chip.clicked.connect(
                lambda _=False, k=key: self._apply_use_case(k))
            use_row.addWidget(chip)
        use_row.addStretch(1)
        use_holder = QWidget()
        use_holder.setLayout(use_row)
        card2.add(use_holder)

        self.lbl_use_note = label("", "Hint", wrap=True)
        self.lbl_use_note.setVisible(False)
        card2.add(self.lbl_use_note)

        card2.add(holder)
        card2.add(label(
            "目标体积是硬上限：编码后会校验实际大小，超出就按实际结果重算码率再压一次。"
            "源文件本来就小于目标时不会重新编码。", "Hint", wrap=True))

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(card)
        lay.addWidget(card2)
        lay.addStretch(1)      # 内容贴顶，多余高度交给下方，不再均匀摊在卡片之间
        return page

    def _on_container_changed(self, _index: int) -> None:
        """换容器就重建音频选项：WebM 只认 Opus，MP4/MOV 不提供 Opus。"""
        # 选项源含 fdk（可用时追加；WebM 的 allowed 不含它，自动被过滤）
        fdk = _fdk_option(self.ctx.env)
        self._size_audio_source = list(SIZE_AUDIO_OPTIONS) + ([fdk] if fdk else [])
        key = self.size_container.currentData() or "mp4"
        allowed = CONTAINER_AUDIO_MODES.get(key, ("copy", "aac", "none"))
        current = self.size_audio.currentData()

        self.size_audio.blockSignals(True)
        self.size_audio.clear()
        audio_source = getattr(self, "_size_audio_source", SIZE_AUDIO_OPTIONS)
        for value, text in audio_source:
            if value in allowed:
                self.size_audio.addItem(text, value)
        # 原选项在新容器里不可用 → 回到该容器的默认重编码编码器（不是列表第一项）
        idx = self.size_audio.findData(current) if current else -1
        if idx < 0:
            idx = self.size_audio.findData(CONTAINER_DEFAULT_AUDIO.get(key, "aac"))
        self.size_audio.setCurrentIndex(max(0, idx))
        self.size_audio.blockSignals(False)

        self._on_size_audio_changed(0)

    def _on_size_audio_changed(self, _index: int) -> None:
        mode = self.size_audio.currentData()
        # 只有重编码时才有"压不压音频"的余地：复制是字节照搬，移除更没有音频
        self.cb_shrink.setEnabled(mode in ("aac", "mp3", "opus"))
        # 音频守卫由 _update_estimate 统一刷新（它在本函数末尾会被调用）
        self._update_estimate()

    def _apply_use_case(self, key: str) -> None:
        """点「用途」→ 一次配好编码器 / 容器 / 色深 / 音频，并说明为什么。

        这里**故意覆盖**用户已有选择：他点这个按钮就是在说"按这个用途帮我配"。
        但会把"为什么这么配"写在下面那行，而不是默默改完就走 ——
        用户得能看懂自己拿到了什么，下次才能自己选。
        """
        case = next((u for u in USE_CASES if u[0] == key), None)
        if case is None:
            return
        _, _text, codec, container, pix_fmt, audio, note = case
        self._set_combo(self.size_codec, codec)
        self._set_combo(self.size_container, container)
        # 色深：先取消"用户已手动设置"，否则自动跟随不会生效；
        # 但用途本身要的是确定值，所以直接设并重新标记为手动（避免随后
        # 换编码器时被自动切换改掉，例如 archive 的 H.265 会被切成 10bit）
        self._set_combo(self.size_pixfmt, pix_fmt)
        self._pixfmt_user_set = True
        self._set_combo(self.size_audio, audio)
        self.lbl_use_note.setText(note)
        self.lbl_use_note.setVisible(True)
        self._update_estimate()
        if hasattr(self, "content_stack"):
            QTimer.singleShot(0, self._settle_stack_size)

    def _update_audio_warn(self) -> None:
        """源音轨放不进 MP4 的常见容器时，给一行看得见的警告。

        触发条件全在 `audio_compat_warning()` 里，这里只负责显示。
        ⚠️ 必须看**第一个**文件的音频流：批量任务里各文件可能不同，
        但界面只能提示一个，先用首个（与预估/按钮口径一致）。
        """
        lbl = getattr(self, "lbl_audio_warn", None)
        if lbl is None:
            return
        paths = self.table.paths or []
        info = self.table.info_for(paths[0]) if paths else None
        text = audio_compat_warning(
            self.size_container.currentData() or "mp4",
            self.size_audio.currentData() or "aac", info)
        lbl.setText(text)
        lbl.setVisible(bool(text))
        if hasattr(self, "content_stack"):
            QTimer.singleShot(0, self._settle_stack_size)

    def _size_settings(self) -> VideoSettings:
        codec = self.size_codec.currentData() or "libx264"
        audio_mode = self.size_audio.currentData() or "aac"
        audio_codec = {"copy": "copy", "none": "none", "aac": "aac",
                       "mp3": "libmp3lame", "opus": "libopus"}.get(audio_mode, "aac")
        return VideoSettings(
            codec=codec,
            preset=self.size_speed.currentData() or "medium",
            scale_height=int(self.size_scale.currentData() or 0),
            fps=float(self.size_fps.currentData() or 0.0),
            pix_fmt=self.size_pixfmt.currentData() or "",
            audio_codec=audio_codec,
            # Opus 在同样听感下比 AAC 省 ~30%，默认 96 就够，把比特留给视频
            audio_bitrate_kbps=96 if audio_codec == "libopus" else 128,
            container=Container(self.size_container.currentData() or "mp4"),
        )

    def _size_audio_mode(self) -> str:
        return self.size_audio.currentData() or "aac"

    def _on_target_value_changed(self, _value: float) -> None:
        self._update_estimate()

    def _on_unit_changed(self, _index: int) -> None:
        """切换 MB/GB：只换显示，规范值不动。

        这样 100 MB → 切 GB（显示 0.10 GB）→ 切回 MB，仍是 100 MB，
        不会因为两位小数舍入漂成 102 MB。
        """
        old_unit = self._unit
        new_unit = self.cmb_unit.currentData()
        mb = self.spin_target.value() / _UNIT_SPECS[old_unit][1]      # 按旧单位换回 MB
        _label, per_mb, _base, step, lo, hi = _UNIT_SPECS[new_unit]
        raw = mb * per_mb

        self.spin_target.blockSignals(True)
        self.spin_target.setRange(lo, hi)
        self.spin_target.setSingleStep(step)
        self.spin_target.setValue(min(hi, max(lo, raw)))
        self.spin_target.blockSignals(False)
        self._unit = new_unit
        self._update_estimate()

    def _target_mb(self) -> float:
        """目标大小（MB）。直接由界面显示换算 —— 显示什么就是什么。"""
        return self.spin_target.value() / _UNIT_SPECS[self._unit][1]

    def _target_text(self) -> str:
        """给按钮/提示用的可读文本，如「800 KB」「100 MB」「1.50 GB」。"""
        label = _UNIT_SPECS[self._unit][0]
        return "%s %s" % (self.spin_target.textFromValue(self.spin_target.value()), label)

    def _policy(self) -> str:
        if self.rb_fast.isChecked():
            return "fast"
        return "strict"

    def _on_policy_changed(self, _btn=None) -> None:
        """切换体积控制档位，联动下方几个控件。

        1. 快速单遍用不上「尽量用满」：该档起手只给 97% 码率，实测常落在
           93~94%，刚好低于 5% 的补跑触发线 → 几乎每次都多跑一轮
           （0.6s → 1.1s，只换 3 个百分点）。「快」正是这一档存在的全部意义。
        2. 快速单遍也用不上慢速档：实测「单遍 + veryslow」耗时 3.5s，
           **比「两遍 + medium」的 2.0s 还慢**，体积却只到目标的 66%。
           两个控件的目标直接打架，所以把 slow/slower/veryslow 灰掉。
        """
        fast = self._policy() == "fast"
        if fast:
            # 记住用户的选择再取消勾选：只灰掉不取消的话，勾还在，看起来像
            # "已经启用了但被锁住"，容易让人以为它仍然生效。
            self._fill_pref = self.cb_fill.isChecked()
            self.cb_fill.setChecked(False)
        else:
            self.cb_fill.setChecked(getattr(self, "_fill_pref", True))
        self.cb_fill.setEnabled(not fast)
        if fast:
            self.cb_fill.setToolTip(
                "快速单遍下不可用：这一档起手只给 97% 码率，压出来常在 93~94%，"
                "几乎每次都会触发补跑 —— 耗时翻倍却只多换 3 个百分点，"
                "等于把「快速」变成了「两遍」。要压满请改用「严格不超过」。")
        else:
            self.cb_fill.setToolTip(
                "勾上（默认）：编出来比目标小很多时，自动加码率重跑，把剩余空间用掉。\n"
                "不勾：一次成型。普通素材两遍编码已经能到目标的 96~98%，"
                "为剩下几个百分点多跑一轮通常不值。\n"
                "纯色、静止这类内容加码率也不涨体积，会自动停手（会多花一点时间）。")
        self._gate_slow_presets(fast)
        self._update_estimate()

    def _gate_slow_presets(self, fast: bool) -> None:
        """快速单遍时禁掉慢速档（它们是两遍编码才划算的选项）。

        实测（混合复杂度 10s / 目标 1500k）：
          单遍 + veryslow  3.5s  → 997 KB（目标的 66%）
          两遍 + medium    2.0s  → 1713 KB
        慢档的收益来自"第二遍按第一遍的统计分配比特"，单遍拿不到这个收益，
        只剩编码变慢的代价。留着它等于让用户把快速档调得比两遍还慢。
        """
        model = self.size_speed.model()
        cur = self.size_speed.currentData()
        for i in range(self.size_speed.count()):
            name = self.size_speed.itemData(i)
            item = model.item(i)
            if item is None:
                continue
            slow = name in _SLOW_PRESETS
            item.setEnabled(not (fast and slow))
            item.setToolTip(
                "快速单遍下不可用：慢档的收益来自「第二遍按第一遍的统计分配比特」，"
                "单遍拿不到，只剩编码更慢。实测单遍+veryslow 比两遍+medium 还慢。"
                if (fast and slow) else "")
        # 当前选中的正好被禁 → 退到 medium
        if fast and cur in _SLOW_PRESETS:
            self.size_speed.setCurrentIndex(
                max(0, self.size_speed.findData("medium")))

    def _apply_suggested_resolution(self) -> None:
        """采纳画质评估给出的降分辨率建议。"""
        qa = getattr(self, "_quality", None)
        if not qa or not qa.suggest_height:
            return
        idx = self.size_scale.findData(qa.suggest_height)
        if idx < 0:
            return
        self.size_scale.setCurrentIndex(idx)
        self._update_estimate()

    def _update_policy_hint(self) -> None:
        real = resolve_codec(self.size_codec.currentData() or "libx264",
                             self.ctx.env)
        if self._policy() == "fast":
            text = ("快速单遍：跳过第一遍分析直接编码，耗时约为两遍方案的一半；"
                    "超出目标会自动降码率重编，仍保证不超目标。")
        elif is_hw_encoder(real):
            # 硬件编码在目标体积模式下走固定 QP，**没有"两遍"这回事** ——
            # 说成两遍会让用户以为它和软编一样准
            text = ("严格模式（硬件编码）：单遍固定 QP 编码，编完逐个字节核对，"
                    "超出目标会自动调高 QP 重跑，保证输出不大于目标。"
                    "QP 档位跨度较大，可能比目标小一些。")
        else:
            text = ("严格模式：两遍编码，编完逐个字节核对，一旦超过目标就自动降码率重跑，"
                    "保证输出不大于目标。这是最贴近目标的档位。")
        # 三行都跟"目标里有多少留给音视频"有关，放一起：
        # 模式说明 / 音频占掉多少 / 容器占掉多少
        lines = [text, self._audio_cost_text(), self._container_cost_text()]
        self.lbl_policy_hint.setText("\n".join(x for x in lines if x))

    def _audio_cost_text(self) -> str:
        """音频吃掉了目标里的多少 —— 目标体积模式下它和视频抢同一块预算。

        实测 2MB / 12s 目标下，128k 音频占约 190 KB ≈ 9%。用户选「重编码为
        AAC 128k」时，界面必须告诉他"你刚从画面预算里挖走了多少"。
        数值一律来自 solve()，不在 UI 里另算（既定约定）。
        """
        paths = self.table.paths
        if not paths:
            return ""      # 没文件就没时长，算不出来
        info = self.table.info_for(paths[0])
        if info is None or not info.duration:
            return ""
        target_mb = max(self._target_mb(), 0.0)
        if target_mb <= 0:
            return ""
        # 目标体积页没有"音频码率"输入框，用默认值（与 solve 的默认一致）；
        # 真正生效的码率由 allow_audio_shrink 的阶梯决定，solve 会算出来
        try:
            budget = solve(info, target_mb,
                           self.size_container.currentData() or "mp4",
                           audio_mode=self._size_audio_mode(),
                           audio_kbps=128,
                           allow_audio_shrink=self.cb_shrink.isChecked())
        except Exception:
            return ""
        if budget.audio_bytes <= 0:
            return "音频：已移除，整个目标都留给画面"
        ratio = budget.audio_bytes / budget.target_bytes * 100
        return ("音频会占掉约 %s（目标的 %.0f%%）：这部分先扣掉，剩下的 %d kbps "
                "才是画面的" % (human_size(budget.audio_bytes), ratio,
                                budget.video_kbps))

    def _container_cost_text(self) -> str:
        key = self.size_container.currentData() or "mp4"
        target_mb = max(self._target_mb(), 0.0)
        cost, ratio = container_overhead(key, target_mb)
        try:
            # 下拉里的名字带说明（"MP4（兼容性最好）"），这里只要短名
            name = CONTAINER_LABEL[Container(key)].split("（")[0]
        except KeyError:
            name = key.upper()
        return ("容器 %s 会占掉约 %s（目标的 %.1f%%）：索引与结构开销，"
                "不计入音视频" % (name, human_size(cost), ratio * 100))

    # ------------------------------------------------------------------ #
    # 预设区
    # ------------------------------------------------------------------ #
    def _build_presets(self) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        # 不再放「按常用预设」区块标题：上方页签已经写了，重复

        from .shared import PresetFlowRow

        flow = PresetFlowRow()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(PresetFlowRow.CHIP_H + 2)
        flow.heightChanged.connect(
            lambda h: scroll.setFixedHeight(h + 2))

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._chips: dict[str, PresetChip] = {}

        for preset in PRESETS:
            chip = PresetChip(preset)
            chip.clicked.connect(lambda _=False, key=preset.key: self._select_preset(key))
            self._group.addButton(chip)
            self._chips[preset.key] = chip
            flow.addChip(chip)

        scroll.setWidget(flow)
        lay.addWidget(scroll)
        return holder

    def _select_preset(self, key: str) -> None:
        preset: Preset | None = PRESETS_BY_KEY.get(key)
        chip = self._chips.get(key)
        if chip:
            chip.setChecked(True)
        if not preset:
            return

        s = preset.video
        panel = self._panel
        panel.show()
        if s.codec == "copy":
            self._set_combo(self.cmb_codec, "copy")
            # 音频也要跟着预设走：预设定义了 audio_codec="copy"（流复制）。
            # 早退分支原来漏了这一项，若之前选过别的预设（如 AV1 的 Opus），
            # 只换壳会带着旧音频编码去"换壳"，音频被重新编码，违背零损失承诺
            self._set_combo(self.cmb_audio, s.audio_codec)
            self.spin_audio_br.setValue(s.audio_bitrate_kbps)
            self._set_combo(self.cmb_container, s.container.value)
            # 色深同样回默认：流复制不碰像素格式
            self._set_combo(self.cmb_pixfmt, s.pix_fmt or "")
            self._mode_index(0)
            self._update_estimate()
            return

        idx = self.cmb_codec.findData(s.codec)
        if idx >= 0:
            self.cmb_codec.setCurrentIndex(idx)
        self._mode_index({
            RateMode.CRF: 0, RateMode.BITRATE: 1,
        }.get(s.rate_mode, 0))
        self.slider_crf.setValue(int(s.crf * 10))
        self.spin_crf.setValue(s.crf)
        self.spin_bitrate.setValue(s.bitrate_kbps)
        self._set_combo(self.cmb_speed, s.preset)
        self._set_combo(self.cmb_tune, s.tune)
        self._set_combo(self.cmb_scale, s.scale_height)
        self._set_combo(self.cmb_fps, s.fps)
        self._set_combo(self.cmb_audio, s.audio_codec)
        self.spin_audio_br.setValue(s.audio_bitrate_kbps)
        self._set_combo(self.cmb_container, s.container.value)
        self._set_combo(self.cmb_pixfmt, s.pix_fmt or "")
        self.ctx.config.last_preset = key
        self.ctx.save_config()
        self._update_estimate()

    # ------------------------------------------------------------------ #
    # 参数区
    # ------------------------------------------------------------------ #
    def _build_params(self) -> QWidget:
        card = Card()
        card.add(label("编码参数", "SectionTitle"))

        # 编码器
        self.cmb_codec = Combo()
        for value, text in codec_labels(self.ctx.env):
            self.cmb_codec.addItem(text, value)
        _gate_codec_items(self.cmb_codec, self.ctx.env)
        self.cmb_codec.currentIndexChanged.connect(self._on_codec_changed)

        self.cmb_speed = Combo()
        for name in X264_PRESETS:
            self.cmb_speed.addItem(_speed_label(name), name)

        self.cmb_tune = Combo()
        for value, text in TUNE_OPTIONS:
            self.cmb_tune.addItem(text, value)

        # 质量控制
        self.cmb_mode = Combo()
        for mode, text in MODE_ITEMS:
            self.cmb_mode.addItem(text, mode)
        self.cmb_mode.currentIndexChanged.connect(
            lambda i: self._quality_stack.setCurrentIndex(i) or self._update_estimate())

        # 滑杆以 0.1 为步进（内部整数 10~630 = 1.0~63.0）
        self.slider_crf = QSlider(Qt.Orientation.Horizontal)
        self.slider_crf.setRange(10, 630)
        self.slider_crf.setValue(230)
        self.spin_crf = QDoubleSpinBox()
        self.spin_crf.setRange(1.0, 63.0)
        self.spin_crf.setDecimals(1)
        self.spin_crf.setSingleStep(0.1)
        self.spin_crf.setValue(23.0)
        self.slider_crf.valueChanged.connect(
            lambda v: self.spin_crf.setValue(v / 10.0))
        self.spin_crf.valueChanged.connect(
            lambda v: self.slider_crf.setValue(round(v * 10)))
        self.slider_crf.valueChanged.connect(lambda _: self._update_estimate())

        crf_row = QWidget()
        crf_lay = QHBoxLayout(crf_row)
        crf_lay.setContentsMargins(0, 0, 0, 0)
        crf_lay.setSpacing(10)
        crf_lay.addWidget(self.slider_crf, 1)
        crf_lay.addWidget(self.spin_crf)
        self.crf_hint = label("", "Hint")
        self.crf_hint.setMinimumWidth(190)
        crf_lay.addWidget(self.crf_hint)

        self.spin_bitrate = QSpinBox()
        self.spin_bitrate.setRange(100, 200000)
        self.spin_bitrate.setSingleStep(100)
        self.spin_bitrate.setValue(2000)
        self.spin_bitrate.setSuffix(" kbps")
        self.spin_bitrate.valueChanged.connect(lambda _: self._update_estimate())
        br_row = QWidget()
        br_lay = QHBoxLayout(br_row)
        br_lay.setContentsMargins(0, 0, 0, 0)
        br_lay.addWidget(self.spin_bitrate)
        br_lay.addWidget(label("越高越清晰、文件越大；1080p 一般 2000–6000", "Hint"), 1)

        self._quality_stack = QStackedWidget()
        self._quality_stack.addWidget(crf_row)
        self._quality_stack.addWidget(br_row)

        # 画面
        self.cmb_scale = Combo()
        for value, text in SCALE_OPTIONS:
            self.cmb_scale.addItem(text, value)
        self.cmb_scale.currentIndexChanged.connect(lambda _: self._update_estimate())

        self.cmb_fps = FpsCombo()
        self.cmb_fps.add_fps_options(FPS_OPTIONS)
        self.cmb_fps.currentIndexChanged.connect(
            lambda _: self._update_estimate())

        # 音频
        self.cmb_audio = Combo()
        for value, text in AUDIO_CODEC_OPTIONS:
            self.cmb_audio.addItem(text, value)
        fdk = _fdk_option(self.ctx.env)
        if fdk:
            self.cmb_audio.addItem(fdk[1], fdk[0])
        self.spin_audio_br = QSpinBox()
        self.spin_audio_br.setRange(32, 512)
        self.spin_audio_br.setValue(128)
        self.spin_audio_br.setMinimumWidth(80)
        self.spin_audio_br.valueChanged.connect(lambda _: self._update_estimate())
        audio_row = QWidget()
        au_lay = QHBoxLayout(audio_row)
        au_lay.setContentsMargins(0, 0, 0, 0)
        au_lay.setSpacing(6)
        au_lay.addWidget(self.cmb_audio, 1)
        au_lay.addWidget(self.spin_audio_br)
        # 单位不塞进数值框（suffix 会在数字右侧挤着、和光标抢位置），单独放一个标签
        au_lay.addWidget(label("kbps", "Hint"))
        self.cmb_audio.currentIndexChanged.connect(self._on_audio_changed)

        # 容器
        self.cmb_container = Combo()
        for c, text in CONTAINER_LABEL.items():
            self.cmb_container.addItem(text, c.value)
        self.cmb_container.currentIndexChanged.connect(lambda _: self._update_estimate())

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        self.cmb_pixfmt = Combo()
        for value, text in PIX_FMT_OPTIONS:
            self.cmb_pixfmt.addItem(text, value)
        # 切换色深本身也要重算警告（编码器没变，但选没选 10bit 变了）
        self.cmb_pixfmt.currentIndexChanged.connect(
            lambda _: self._update_pixfmt_warn(
                self.cmb_pixfmt,
                resolve_codec(self.cmb_codec.currentData() or "libx264",
                              self.ctx.env),
                self.lbl_pixfmt_warn, crf_note=True))
        # 初始状态也走同一套门控（按当前编码器试编 10bit 并写 tooltip），
        # 不在这里另写一句静态说明 —— 否则启动时那句话和实际能力可能不一致
        # 预设页自己的警告标签（与 size 页的同名会互相覆盖，故用 _preset 后缀）
        self.lbl_pixfmt_warn = label("", "Warn", wrap=True)
        self.lbl_pixfmt_warn.setVisible(False)
        self._apply_pixfmt_gate(
            self.cmb_pixfmt,
            resolve_codec(self.cmb_codec.currentData() or "libx264", self.ctx.env),
            self.lbl_pixfmt_warn, crf_note=True)

        two_col_rows = [
            ("编码器", self.cmb_codec, "编码速度", self.cmb_speed),
            ("质量模式", self.cmb_mode, "场景优化", self.cmb_tune),
            ("分辨率", self.cmb_scale, "帧率", self.cmb_fps),
            ("音频", audio_row, "输出格式", self.cmb_container),
            ("色深", self.cmb_pixfmt, "", QLabel()),
        ]
        r = 0
        for l1, w1, l2, w2 in two_col_rows:
            grid.addWidget(label(l1), r, 0)
            grid.addWidget(w1, r, 1)
            if l2:
                grid.addWidget(label(l2), r, 2)
                grid.addWidget(w2, r, 3)
            r += 1
            if l1 == "质量模式":
                grid.addWidget(label("质量参数"), r, 0)
                grid.addWidget(self._quality_stack, r, 1, 1, 3)
                r += 1
            if l1 == "色深":
                # 与「按文件大小」页同一套警告：H.264+10bit（Hi10P）无硬解
                grid.addWidget(self.lbl_pixfmt_warn, r, 1, 1, 3)
                r += 1
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._panel = QWidget()
        self._panel.setLayout(grid)
        card.add(self._panel)

        # wrap=True：这行文字很长，不折行会把整个页面的最小宽度顶到 890+，
        # 窗口一窄就被右边缘拦腰截断（横向滚动条是关的，只能裁）
        self.lbl_crf_hint = label(_CRF_HINT_BASE, "Hint", wrap=True)
        card.add(self.lbl_crf_hint)
        return card

    # ------------------------------------------------------------------ #
    # 联动
    # ------------------------------------------------------------------ #
    def _mode_index(self, index: int) -> None:
        self.cmb_mode.setCurrentIndex(index)
        self._quality_stack.setCurrentIndex(index)

    def _codec_usable_or_base(self, combo: Combo) -> None:
        """当前编码器项要是被灰了，就切回 H.264 软编，并说明为什么。

        灰掉的项鼠标点不到，但**代码**能选中它：预设卡片走的是
        `findData` + `setCurrentIndex`（如没硬件的机器上点「硬件加速」卡片）。
        那时下拉会显示「H.265 硬件加速」而实际跑 libx264 —— 界面在说谎。
        切回去至少显示的是真的。（真要让卡片也跟着灰，是另一件事。）
        """
        model = combo.model()
        item = model.item(combo.currentIndex())
        if item is None or item.isEnabled():
            combo.setToolTip("")     # 编码器下拉只在这里用 tooltip，顺手清掉过期的
            return
        base = combo.findData("libx264")
        if base >= 0 and base != combo.currentIndex():
            combo.setCurrentIndex(base)
        combo.setToolTip("原来选的硬件加速项本机用不了，已切回 H.264 软编")

    def _on_size_pixfmt_activated(self, _index: int = 0) -> None:
        """用户亲手选了色深 —— 从此不再自动跟随编码器。

        用 `activated`（只有用户交互才发）而不是 `currentIndexChanged`
        （代码 setCurrentIndex 也发）：否则自动切换自己会把自己标记成
        "用户选过了"，下一次换编码器就不再跟随。
        """
        self._pixfmt_user_set = True

    def _auto_pixfmt_for_size(self, real: str) -> None:
        """目标体积页：换编码器时自动跟着切色深。

        规则（用户 2026-11-12 定）：体积被目标钉死时 10bit 是白赚的，所以
        **能用 10bit 就开 10bit**；但 H.264 的 10bit 是 Hi10P，手机电视全打不开，
        所以 H.264 保持 8bit。

        ⚠️ 只跟随「用户手动改过没有」：用户一旦自己动过色深下拉，就不再插手。
        否则他特意选了 8bit（比如就是要发给老人机），换个编码器又被改回 10bit。
        """
        if getattr(self, "_pixfmt_user_set", False):
            return
        is_h264 = real == "libx264" or real.startswith("h264_")
        want = "" if is_h264 else "yuv420p10le"
        if not self._pixfmt_supported(real, want):
            want = ""
        if self.size_pixfmt.currentData() != want:
            self._set_combo(self.size_pixfmt, want)

    def _pixfmt_supported(self, real: str, pix_fmt: str) -> bool:
        """该编码器能不能真的编这个色深（8bit 一律算能）。"""
        if pix_fmt != "yuv420p10le":
            return True
        return _pixfmt_state(self.ctx.env, real)[0]

    def _on_size_codec_changed(self, _index: int = 0) -> None:
        """按文件大小页签：编码器变了，速度档 / 色深 / 提示都跟着变。

        这里不刷预估 —— 编码器下拉本身已经连了 _update_estimate，
        而且本函数会在「编码参数」卡构建途中被调一次，那时别的控件还没建好。
        """
        self._codec_usable_or_base(self.size_codec)
        real = resolve_codec(self.size_codec.currentData() or "libx264",
                             self.ctx.env)
        self._auto_pixfmt_for_size(real)
        self.size_speed.setEnabled(_speed_available(real))
        tip = _speed_tip(real)
        if is_hw_encoder(real):
            # 硬件编码在目标体积模式下走「固定 QP + 二分」，不是两遍 ABR ——
            # 因为它不产生 passlog（实测 pass1 日志 0 字节），两遍名存实亡、
            # 体积会超 1.2~2.4 倍。代价是整数 QP 分辨率有限，可能撞不准目标。
            note = ("硬件编码在目标体积模式下改用「固定 QP」：它不产生两遍分析用的"
                    "统计文件，无法按码率精确分配。实测能在 1~2 轮内做到不超目标，"
                    "但可能比目标小一些（相邻 QP 档位跨度较大）。")
        elif real == "libsvtav1":
            note = _SIZE_SPEED_NOTE_AV1
        else:
            note = _SIZE_SPEED_NOTE
        self.size_speed.setToolTip(f"{tip}\n{note}" if tip else note)
        self._apply_pixfmt_gate(self.size_pixfmt, real,
                                self.lbl_pixfmt_warn_size)

    def _on_codec_changed(self, _index: int) -> None:
        self._codec_usable_or_base(self.cmb_codec)
        codec = self.cmb_codec.currentData()
        real = resolve_codec(codec or "", self.ctx.env)
        is_soft = codec in ("libx264", "libx265")
        # 速度档对硬件编码器同样有效（nvenc → p1–p7、amf → speed/balanced/quality、
        # videotoolbox → 画质优先开关），只有 QSV 不吃 -preset。
        # 原来一律 setEnabled(codec in ("libx264","libx265"))，等于把 NVENC 也禁掉了：
        # 用户点到「H.264 硬件加速(NVIDIA NVENC)」就发现编码速度点不动 —— 而且那个档位
        # 本来是真的会被用上的。
        self.cmb_speed.setEnabled(_speed_available(real))
        self.cmb_speed.setToolTip(_speed_tip(real))
        self.cmb_tune.setEnabled(is_soft)
        self.cmb_tune.setToolTip("" if is_soft else _TUNE_TIP)
        if is_hw_encoder(real):
            self.cmb_mode.setItemText(0, "质量档位（硬件加速）")
        else:
            self.cmb_mode.setItemText(0, "恒定质量 CRF（推荐）")
        self._apply_pixfmt_gate(self.cmb_pixfmt, real,
                                self.lbl_pixfmt_warn, crf_note=True)
        self._update_estimate()

    def _on_audio_changed(self, _index: int) -> None:
        codec = self.cmb_audio.currentData()
        self.spin_audio_br.setEnabled(codec not in ("copy", "none"))
        self._update_estimate()

    def _set_combo(self, combo: Combo, value) -> None:
        """按 itemData 选中一项。找不到就**什么都不做** —— 调用方常传
        "用户没选过"的空值（如 pix_fmt=""），那是合法输入。

        🔴 但正因为静默，写错值（如把 "libx264" 写成 "h264"）会**毫无反应**：
        按钮点了、界面没变、也没有报错。踩过 ——「我要拿去」那三个按钮里
        两个写的 "h264"，于是只有 H.265 那个看起来"能用"。
        所以约定：传进来的值必须来自下拉本身，测试里用 findData 反查校验。
        """
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _apply_pixfmt_gate(self, combo: Combo, real: str,
                           warn_label=None, crf_note: bool = False) -> None:
        """按当前编码器开关「10bit」这一项，并给出分编码器的提示。

        实测（2026-11-11）：h264_amf 编 10bit 直接报 `Conversion failed!`，
        用户完全看不出是色深的问题；而 ffmpeg 声明的格式列表里还写着 p010le，
        所以只能靠试编判断（`env.encoder_usable_10bit`，结果按编码器缓存）。
        """
        ok, why = _pixfmt_state(self.ctx.env, real)
        model = combo.model()
        idx = combo.findData("yuv420p10le")
        if idx >= 0:
            item = model.item(idx)
            if item is not None:
                # 灰掉但**留在列表里**：直接删掉的话用户不知道这个选项存在过，
                # 也不知道为什么没了（沿用速度档那条"禁用必须给原因"的教训）
                item.setEnabled(ok)
                item.setToolTip("" if ok else f"当前编码器不支持：{why}")
        if not ok and combo.currentData() == "yuv420p10le":
            self._set_combo(combo, "")
        combo.setToolTip(_pixfmt_tip(real, ok, why))
        self._update_pixfmt_warn(combo, real, warn_label, crf_note)

    def _update_pixfmt_warn(self, combo: Combo, real: str,
                            warn_label=None, crf_note: bool = False) -> None:
        """选中 10bit 后，在两个页签各出一行**看得见**的大白话说明。

        为什么不能只靠 tooltip：10bit 的信息原来全在悬停里，而小白用户不会
        去悬停。H.264 的 10bit（Hi10P）**至今没有任何设备能硬解**，压出一个
        手机打不开的文件代价很大 —— 这种必须自己跳出来。

        分成两级：H.264+10bit 是真警告（红色 Warn），其余是提示（蓝色 Hint），
        别让「体积不会变大」这种好消息也顶着红色感叹号吓人。
        """
        if warn_label is None:
            return
        chosen = combo.currentData() == "yuv420p10le"
        if chosen:
            text, is_warn = _pixfmt_plain(real, crf_note)
            _set_label_role(warn_label, "Warn" if is_warn else "Hint")
            warn_label.setText(text)
            warn_label.setVisible(True)
        else:
            warn_label.setText("")
            warn_label.setVisible(False)
        # 预设页的 CRF 说明随色深变化：CRF 刻度跨位深不可比，
        # 选了 10bit 要把"同 CRF ≠ 同画质"讲清楚（否则用户按老 CRF 值压，
        # 文件反而变大还以为软件算错了）
        if crf_note:
            crf_lbl = getattr(self, "lbl_crf_hint", None)
            if crf_lbl is not None:
                crf_lbl.setText(_CRF_HINT_BASE + (_CRF_HINT_10BIT if chosen else ""))
        # 🔴 标签文字变了 → 内容高度变了 → 页签的**固定高度**必须跟着重算，
        # 否则多出来的那一行会被容器切掉（实测 10bit 时面板需要 295px 却只有
        # 240px，最后一行只剩上半截）。延后一拍是因为 setText 之后要等一次
        # 布局才能拿到新的 sizeHint。
        if hasattr(self, "content_stack"):
            QTimer.singleShot(0, self._settle_stack_size)

    # ------------------------------------------------------------------ #
    # 读取 / 估算
    # ------------------------------------------------------------------ #
    def current_settings(self) -> VideoSettings:
        mode = self.cmb_mode.currentData() or RateMode.CRF
        return VideoSettings(
            codec=self.cmb_codec.currentData() or "libx264",
            rate_mode=mode,
            crf=self.spin_crf.value(),
            bitrate_kbps=self.spin_bitrate.value(),
            pix_fmt=self.cmb_pixfmt.currentData() or "",
            preset=self.cmb_speed.currentData() or "medium",
            tune=self.cmb_tune.currentData() or "",
            scale_height=int(self.cmb_scale.currentData() or 0),
            fps=float(self.cmb_fps.currentData() or 0.0),
            audio_codec=self.cmb_audio.currentData() or "aac",
            audio_bitrate_kbps=self.spin_audio_br.value(),
            container=Container(self.cmb_container.currentData() or "mp4"),
        )

    def target_size_mb(self) -> float:
        # 「按文件大小」是独立页签，不走这里的固定码率路径。
        return 0.0

    def _update_estimate(self) -> None:
        bar = getattr(self, "bar", None)
        if bar is None:      # 构造过程中参数区可能先于操作栏初始化
            return
        # 音频守卫挂在这里：本函数是"参数变了 / 文件信息到位了"的统一出口，
        # 单独在容器/音频下拉上连一次会漏掉"文件刚解析完"这个时机
        self._update_audio_warn()
        if self._current_tab() == "size":
            self._update_size_estimate()
            return

        self.bar.button.setText("开始压缩")
        settings = self.current_settings()
        crf = self.spin_crf.value()
        if settings.codec == "copy":
            self.crf_hint.setText("不重新编码，秒级完成")
        else:
            quality = ("画质很好" if crf <= 20 else "画质较好" if crf <= 24
                       else "体积优先" if crf <= 28 else "画质损失明显")
            note = ""
            if settings.codec == "libx264" and crf > 51:
                note = " · x264 上限 51，按 51 执行"
            self.crf_hint.setText(f"{quality}（CRF {crf:g}{note}）")

        paths = self.table.paths
        if not paths:
            self.bar.set_hint("还没有添加文件 —— 把视频拖进上面的列表即可开始")
            self.bar.button.setEnabled(False)
            return
        self.bar.button.setEnabled(self.ctx.env.ready)
        if not self.ctx.env.ready:
            self.bar.set_hint("未找到 ffmpeg，请到「功能设置」里指定 ffmpeg 路径", "Danger")
            return

        info = self.table.info_for(paths[0])
        est = estimate_output_size_mb(settings, info, self.target_size_mb())
        count = len(paths)
        prefix = f"共 {count} 个任务 · " if count > 1 else ""
        if est and info:
            src_mb = info.size / 1024 / 1024
            ratio = (est / src_mb * 100) if src_mb else 0
            self.bar.set_hint(
                f"{prefix}首个文件预计 {human_size(est * 1024 * 1024)}"
                f"（原 {human_size(info.size)}，约 {ratio:.0f}%）")
        else:
            self.bar.set_hint(f"{prefix}文件信息读取中…")

    # ------------------------------------------------------------------ #
    # 按文件大小：预检与提示
    # ------------------------------------------------------------------ #
    def _update_size_estimate(self) -> None:
        target = self._target_mb()
        self.bar.button.setText("按 %s 压缩" % self._target_text())
        self._update_policy_hint()

        paths = self.table.paths
        if not paths:
            self.size_status.setVisible(False)
            self.size_status.setText("")
            self.size_detail.setVisible(False)
            self.size_detail.setText("")
            self.lbl_quality.setVisible(False)
            self._res_holder.setVisible(False)
            self.bar.set_hint("拖入视频，填一个目标大小即可")
            self.bar.button.setEnabled(False)
            return
        if not self.ctx.env.ready:
            self.bar.set_hint("未找到 ffmpeg，请到「功能设置」里指定 ffmpeg 路径", "Danger")
            self.bar.button.setEnabled(False)
            return

        info = self.table.info_for(paths[0])
        if info is None:
            self.size_status.setVisible(False)
            self.size_status.setText("")
            self.size_detail.setVisible(False)
            self.size_detail.setText("")
            self.lbl_quality.setVisible(False)
            self._res_holder.setVisible(False)
            self.bar.set_hint("正在读取文件信息…")
            self.bar.button.setEnabled(False)
            return

        # 源文件本来就小于目标：无需压缩，优先级最高
        if info.size and info.size <= target * 1024 * 1024:
            self._set_size_status(
                "✔ 源文件 %s 已经小于目标 %s，无需压缩"
                % (human_size(info.size), human_size(int(target * 1024 * 1024))),
                "Hint")
            self.size_detail.setVisible(False)
            self.size_detail.setText("")
            self.lbl_quality.setVisible(False)
            self._res_holder.setVisible(False)
            self.bar.button.setEnabled(False)
            self.bar.set_hint("源文件已经小于目标，无需压缩")
            return

        settings = self._size_settings()
        budget = solve(info, target, settings.container.value,
                       audio_mode=self._size_audio_mode(),
                       audio_kbps=settings.audio_bitrate_kbps,
                       allow_audio_shrink=self.cb_shrink.isChecked())

        if budget.feasibility == Feasibility.IMPOSSIBLE:
            text = "✘ 做不到：" + budget.reason
            if budget.suggestions:
                text += "\n" + "\n".join("   · " + s for s in budget.suggestions)
            self._set_size_status(text, "Danger")
        elif budget.feasibility == Feasibility.AUDIO_WINS:
            text = "⚠ " + budget.reason
            if budget.suggestions:
                text += "\n" + "\n".join("   · " + s for s in budget.suggestions)
            self._set_size_status(text, "Danger")
        elif budget.feasibility == Feasibility.TIGHT:
            self._set_size_status(
                "⚠ 目标偏紧" + ("：" + budget.reason if budget.reason else "")
                + "，画质会比较勉强", "Hint")
        else:
            self._set_size_status(
                "✔ 可以做" + ("（" + budget.reason + "）" if budget.reason else ""),
                "Hint")

        est = estimate_output_bytes(budget)
        self.size_detail.setVisible(True)
        self.size_detail.setText(
            "%s\n源文件 %s → 目标 %s（预计 %s）" % (
                describe_budget(budget), human_size(info.size),
                human_size(int(target * 1024 * 1024)), human_size(est)))

        # ---- 画质评估：目标"能完成"不代表"结果能看" ----
        # 必须传用户选的分辨率：选了 480p 时评估的就是 480p 的编码，
        # 按源分辨率算会给出过时的警告（实测踩过：选 480p 后提示仍停留在
        # 1280p 的结论上，建议的"降到 XXXp"也失效）
        real = resolve_codec(settings.codec, self.ctx.env)
        qa = assess(info, budget.video_kbps, settings.scale_height,
                    codec=real)
        self._quality = qa
        if qa.level.is_acceptable:
            self.lbl_quality.setText(describe_quality(qa))
            self.lbl_quality.setObjectName("Hint")
        else:
            text = "⚠ " + describe_quality(qa) + "\n   " + qa.reason
            self.lbl_quality.setText(text)
            self.lbl_quality.setObjectName("Danger")
        self.lbl_quality.setVisible(True)
        self.lbl_quality.style().unpolish(self.lbl_quality)
        self.lbl_quality.style().polish(self.lbl_quality)

        # 推荐降分辨率：只在真的能改善时出现，并给出按钮
        if qa.needs_action:
            self.btn_apply_res.setText(
                f"改用 {qa.suggest_height}p（画质更好）")
            # 按钮旁边说清"为什么值得点"：同体积下 bpp 升多少
            self.lbl_apply_hint.setText(
                "同样的体积下，%dp 的画面每像素能分到 %.3f 的比特"
                "（现在只有 %.3f），色块和糊感会明显减少"
                % (qa.suggest_height, qa.suggest_bpp, qa.bpp))
            self.lbl_apply_hint.setVisible(True)
            self._res_holder.setVisible(True)
        else:
            self.btn_apply_res.setText("")
            self.lbl_apply_hint.setText("")
            self.lbl_apply_hint.setVisible(False)
            self._res_holder.setVisible(False)

        # 标签显隐改变了内容高度，容器要重新贴合，否则会被压扁
        self._settle_stack_size()

        count = len(paths)
        prefix = f"共 {count} 个任务 · " if count > 1 else ""
        if budget.ok:
            self.bar.button.setEnabled(True)
            extra = ""
            if settings.container == Container.WEBM:
                extra = "（WebM 自动用 VP9 + Opus）"
            self.bar.set_hint(
                prefix + f"视频 {budget.video_kbps} kbps · 编码后会自动校验体积" + extra)
        else:
            self.bar.button.setEnabled(False)
            self.bar.set_hint("目标不可行，请调大目标或放宽音频设置", "Danger")

    def _set_size_status(self, text: str, role: str) -> None:
        self.size_status.setVisible(bool(text))
        self.size_status.setText(text)
        self.size_status.setObjectName(role)
        self.size_status.style().unpolish(self.size_status)
        self.size_status.style().polish(self.size_status)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _show_sample_menu(self) -> None:
        """在按钮**正上方**弹出时长菜单。

        🔴 不能直接 `exec(按钮底部左角)`：
        操作栏贴着窗口底部，按钮往往就在屏幕边缘。传底部坐标时 Qt 发现
        下方放不下，会自己往上翻 —— 但翻到的位置和按钮之间会留一大段空隙
        （实测：菜单落在按钮上方 157px，两者完全不挨着）。
        而且弹出方向忽上忽下，看着像 bug。

        所以显式算：优先向上展开（菜单底边对齐按钮顶边），上方也确实放不下
        时才向下。这样菜单永远紧贴按钮，方向也稳定。
        """
        menu = self.sample_menu
        menu.adjustSize()
        size = menu.sizeHint()

        top_left = self.btn_sample.mapToGlobal(self.btn_sample.rect().topLeft())
        bottom_left = self.btn_sample.mapToGlobal(
            self.btn_sample.rect().bottomLeft())

        # 按钮所在屏幕的可用区域（多显示器时不能用 primaryScreen）
        screen = self.btn_sample.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None

        above_room = top_left.y() - (avail.top() if avail else 0)
        below_room = ((avail.bottom() if avail else 10000) - bottom_left.y())

        if above_room >= size.height() or above_room >= below_room:
            # 向上：菜单底部贴按钮顶部
            x = top_left.x()
            y = top_left.y() - size.height()
        else:
            x = bottom_left.x()
            y = bottom_left.y()

        # 🔴 必须夹进屏幕：窗口比屏幕高时（或贴边时）按钮可能在屏幕外，
        # 上面算出来的位置会把菜单推出去，用户看不到。夹回来至少保证可点。
        if avail is not None:
            x = max(avail.left(), min(x, avail.right() - size.width() + 1))
            y = max(avail.top(), min(y, avail.bottom() - size.height() + 1))

        menu.exec(QPoint(x, y))

    def _refresh_sample_button(self) -> None:
        """刷新试压按钮的可用性与提示。

        🔴 **不往按钮上写秒数**。写上去就成了常驻参数，用户会以为
        「开始压缩」也受它影响（踩过）。秒数只在菜单项里出现。
        """
        btn = getattr(self, "btn_sample", None)
        if btn is None:
            return
        paths = self.table.paths
        info = self.table.info_for(paths[0]) if paths else None
        info_ready = bool(info is not None and info.duration)

        if not paths:
            tip = ("用当前全部参数只压中间一段，产物名带「_试压」后缀。\n"
                   "先拖入视频。")
        elif not info_ready:
            tip = "视频信息还在读取，稍等一下就能试压。"
        else:
            dur = info.duration
            tip = (
                "用来调参数：用当前**全部**参数只压中间一段，"
                "产物名带「_试压」后缀，不会影响「开始压缩」的结果。\n"
                f"原片 {dur:.0f} 秒；选菜单里的秒数决定试压多长"
                f"（源比它短就按整片压）。\n"
                "取中间段而不是片头：片头常是黑场/标题，压出来好看但不代表全片。\n"
                "满意了再点「开始压缩」压完整片。")

        btn.setEnabled(info_ready)
        btn.setToolTip(tip)
        # 秒数比源还长时标注实际值，避免"选了 60 秒却只出 8 秒"的困惑
        for act, sec in zip(self._sample_acts, SAMPLE_SECONDS_OPTIONS):
            if info_ready and sec > dur:
                act.setText(f"试压 {sec} 秒（实际 {dur:.0f}）")
            else:
                act.setText(f"试压 {sec} 秒")

    def _start_sample_with(self, seconds: int) -> None:
        """菜单里点了「试压 N 秒」：用这个长度立刻开跑。

        不用记住"上次用的长度" —— 每次试压都是**从菜单里点一次动作**，
        菜单本身就是唯一入口，没有"再试一次"这种需要记忆的路径。
        """
        self._sample_seconds = seconds
        self._on_start_sample()

    def _on_start_sample(self) -> None:
        """试压：用当前**全部**参数，只压中间一段（默认 20 秒），出带"_试压"的样本。

        为什么值得做：调参数最难的不是"知道 CRF 是什么"，而是"改了之后
        效果到底怎么样" —— 完整压一遍动辄半小时，用户根本不会去试第二遍。
        20 秒的样本能把反馈时间压到十几秒，于是"试 → 改 → 再试"变得可行。

        设计上的三个决定：
        1. **只处理第一个文件**。试压是给你看效果用的，压十个样本毫无意义。
        2. **取中间一段**。片头常是黑场/标题/静止画面，压出来又小又好看，
           完全不能代表全片难度；中间段才是正片。
        3. **产物带"_试压"后缀**，绝不碰正常输出，也不改动用户的目标设置。
        """
        paths = self.table.paths
        if not paths:
            return
        if not self.ctx.env.ready:
            self.bar.set_hint("未找到 ffmpeg，请到「功能设置」里指定路径", "Danger")
            return

        path = paths[0]
        info = self.table.info_for(path)
        if info is None or not info.duration:
            self.bar.set_hint("视频信息还在读取，稍等一下再试压", "Warn")
            return

        # 秒数取用户设置的（默认 20），sample_span 会自动收敛到源时长
        clip = sample_span(info, self._sample_seconds)
        if clip[1] <= 0:
            self.bar.set_hint("读不到时长，无法试压", "Danger")
            return
        # 🔴 必须用截断后的 info：下游按 duration 算码率/进度，
        # 用完整时长会让样本码率算成"压完整片"的量，体积对不上
        sample_info = truncated_info(info, self._sample_seconds)

        if self._current_tab() == "size":
            # 目标体积页的预算按 duration 反推码率，截断后自动按样本时长算，
            # 补跑循环也就不会把样本往死里冲（那是照抄参考实现最大的坑）
            task = build_size_task(
                self.ctx, path, sample_info, self._size_settings(),
                self._target_mb(),
                audio_mode=self._size_audio_mode(),
                allow_audio_shrink=self.cb_shrink.isChecked(),
                policy=self._policy(), fill_target=self.cb_fill.isChecked(),
                suffix=SAMPLE_SUFFIX, clip=clip)
        else:
            task = build_compress_task(
                self.ctx, path, sample_info, self.current_settings(),
                self.target_size_mb(), suffix=SAMPLE_SUFFIX, clip=clip)

        self.ctx.queue.add_many([task])
        seg = f"{clip[0]:.0f}s 起 {clip[1]:.0f} 秒"
        self.bar.set_hint(f"试压已加入队列（{seg}），看完效果再决定要不要压完整片…")
        self.requestQueue.emit()

    def _on_start(self) -> None:
        if self._current_tab() == "size":
            self._on_start_size()
            return

        paths = self.table.paths
        if not paths:
            return
        if not self.ctx.env.ready:
            self.bar.set_hint("未找到 ffmpeg，请到「功能设置」里指定路径", "Danger")
            return

        settings = self.current_settings()
        target = self.target_size_mb()
        tasks = []
        for path in paths:
            info = self.table.info_for(path)
            tasks.append(build_compress_task(self.ctx, path, info, settings, target))

        self.ctx.queue.add_many(tasks)
        self.table.clear_all()
        self.bar.set_hint(f"已加入 {len(tasks)} 个任务，正在跳转到队列…")
        self.requestQueue.emit()

    def _on_start_size(self) -> None:
        paths = self.table.paths
        if not paths or not self.ctx.env.ready:
            return

        settings = self._size_settings()
        audio_mode = self._size_audio_mode()
        allow_shrink = self.cb_shrink.isChecked()
        target = self._target_mb()

        tasks = []
        skipped = 0
        for path in paths:
            info = self.table.info_for(path)
            if info is not None and info.size and info.size <= target * 1024 * 1024:
                skipped += 1
                continue
            tasks.append(build_size_task(
                self.ctx, path, info, settings, target,
                audio_mode=audio_mode, allow_audio_shrink=allow_shrink,
                policy=self._policy(), fill_target=self.cb_fill.isChecked()))

        if not tasks:
            self.bar.set_hint("所有文件都已小于目标，无需压缩")
            return

        self.ctx.queue.add_many(tasks)
        self.table.clear_all()
        note = f"已按 {target:g} MB 加入 {len(tasks)} 个任务"
        if skipped:
            note += f"（{skipped} 个已小于目标，已跳过）"
        self.bar.set_hint(note + "，正在跳转到队列…")
        self.requestQueue.emit()


def _speed_label(name: str) -> str:
    table = {
        "ultrafast": "ultrafast（最快）",
        "superfast": "superfast",
        "veryfast": "veryfast",
        "faster": "faster",
        "fast": "fast",
        "medium": "medium（默认）",
        "slow": "slow",
        "slower": "slower",
        "veryslow": "veryslow（最慢最省）",
    }
    return table.get(name, name)
