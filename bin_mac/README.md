# bin_mac/ —— 随 mac 分发包一起提供的 ffmpeg

这里放 **macOS 版**的 ffmpeg / ffprobe，让 mac 用户**不用装 Homebrew、不用联网**，
解压「打包小丸工具箱.command」的 zip 后双击就能出 .app。

## 需要哪几个文件

| 文件 | 架构 | 用于 |
| --- | --- | --- |
| `ffmpeg-arm64` | Apple Silicon（M1/M2/M3…） | 在 M 系列 Mac 上打包 |
| `ffprobe-arm64` | 同上 | 同上 |
| `ffmpeg-x64` | Intel Mac | 在 Intel Mac 上打包 |
| `ffprobe-x64` | 同上 | 同上 |

`打包小丸工具箱.command` 会按 `uname -m` 只复制**当前机器架构**那一份到
`bin/`，另一份不参与打包 —— 所以最终 .app 里只有一份 ffmpeg，不会翻倍。

## 来源

`ffmpeg-static` b6.1.1 的官方 darwin 构建，经 npmmirror 镜像获取：

```bash
BASE=https://cdn.npmmirror.com/binaries/ffmpeg-static/b6.1.1
for A in arm64 x64; do
  curl -L -o ffmpeg-$A.gz  "$BASE/ffmpeg-darwin-$A.gz"  && gzip -d ffmpeg-$A.gz
  curl -L -o ffprobe-$A.gz "$BASE/ffprobe-darwin-$A.gz" && gzip -d ffprobe-$A.gz
done
```

为什么不用 evermeet.cx：那家的 CDN 在部分网络下 TLS 握手会被拦
（`SEC_E_ILLEGAL_MESSAGE`），而 GitHub Releases 直连经常被限速/重置。
npmmirror 在国内可达性好且支持 Range 请求。

## 授权提醒

FFmpeg 遵循 LGPL / GPL，这份是 GPL 构建。对外分发内置 ffmpeg 的程序时，
请一并遵守对应许可（提供源码获取方式等）。

## 不放会怎样

`.command` 会回退到系统已安装的 ffmpeg（`brew install ffmpeg`）；
都没有就直接报错并提示，不会静默产出缺 ffmpeg 的包。
