"""应用配置的读写（存 JSON，Mac 上落在 ~/Library/Application Support）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .utils import config_path


@dataclass
class AppConfig:
    # 二进制
    ffmpeg_path: str = ""          # 留空 = 自动检测
    ffprobe_path: str = ""

    # 输出
    output_mode: str = "source"    # source = 与源文件同目录 / custom = 指定目录
    output_dir: str = ""           # 只在 custom 模式下使用；默认留空 = 与源文件同目录
    filename_suffix: str = ""      # 输出文件名后缀，如 "_压缩"
    overwrite: bool = False

    # 队列
    concurrency: int = 2
    open_when_done: bool = False
    remove_source: bool = False    # 成功后删除源文件（默认关闭）
    # 后台运行：把 ffmpeg 进程降到「低于正常」优先级，让系统在别人需要
    # CPU 时先挪开它。不减线程，所以吞吐不受影响（详见 engine._set_background_priority）
    background_mode: bool = False

    # 界面
    theme: str = "light"
    last_preset: str = "balanced"
    last_downmix_preset: str = "standard"
    downmix_merge: bool = False           # 下混页是否处于「多轨合并」模式
    downmix_merge_layout: str = "auto"    # 合并时的声道布局
    window_geometry: str = ""

    # 高级
    hw_checked: bool = False
    available_encoders: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls) -> "AppConfig":
        # 默认输出位置 = 与源文件相同目录（output_mode 的默认值），
        # 所以不再预填、也不再自动创建 ~/Videos/小丸输出 那种"默认目录"：
        # 输出目录留空，只有用户真的选了「始终输出到指定目录」才会用到。
        path = config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        valid = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in valid}
        return cls(**kwargs)

    def save(self) -> None:
        try:
            config_path().write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def resolved_output_dir(self, source: Path) -> Path:
        if self.output_mode == "custom" and self.output_dir:
            out = Path(self.output_dir)
        else:
            out = source.parent
        out.mkdir(parents=True, exist_ok=True)
        return out
