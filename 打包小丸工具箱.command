#!/bin/bash
# =============================================================================
# 小丸工具箱 · Mac 一键打包
#
# 用法：双击本文件即可（或在终端 bash 打包小丸工具箱.command）
#
# 它会自动完成：
#   1. 建构建用虚拟环境（.buildenv），装依赖 + PyInstaller
#   2. 准备 ffmpeg / ffprobe（优先用 bin/ 里已有的；没有就从系统/Homebrew 复制）
#   3. 生成图标与 .icns
#   4. PyInstaller 打出 小丸工具箱.app
#   5. 临时签名（codesign -s -）：不做公证也能在本机双击打开
#   6. 把 .app 压成 zip（直接传 .app 文件夹会丢执行权限）
#
# 产物：dist/小丸工具箱.app  与  dist/小丸工具箱.zip
# =============================================================================

set -uo pipefail

# 双击运行时工作目录是 $HOME，必须先切到脚本所在目录
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
APP_NAME="小丸工具箱"
ZIP_NAME="${APP_NAME}.zip"

# ---------- 小工具 ----------------------------------------------------------
info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }
die()  {
  printf '\n\033[1;31m✗ %s\033[0m\n\n' "$*"
  # 双击运行时窗口会立刻关掉，必须停下来让人看到错误
  if [ -t 0 ]; then
    read -r -p "按回车键关闭…" _
  else
    osascript -e "display alert \"打包失败\" message \"$*\" as critical" 2>/dev/null || true
  fi
  exit 1
}

trap 'die "第 $LINENO 行出错，打包中断"' ERR

# ---------- 0. 环境检查 -----------------------------------------------------
[ "$(uname)" = "Darwin" ] || die "这个脚本只能在 macOS 上运行。Windows 请用 packaging/build_windows.py"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "找不到 python3。请先安装 Python 3.12：brew install python@3.12"

# 🔴 Python 版本检查 —— 必须在装依赖**之前**。
# requirements.txt 把 PySide6 锁在 6.9.x（Python 3.9+ / macOS 12+ 可用），
# 所以这里的下限是 3.9。版本不够时 pip 只会报「找不到匹配的版本」，
# 脚本原来把那个当成"网络问题"提示去换镜像 —— 完全指错了方向，
# 用户实测就是卡在这里。
PY_OK="$("$PY" -c 'import sys; print("1" if sys.version_info >= (3, 9) else "0")' 2>/dev/null || echo 0)"
if [ "$PY_OK" != "1" ]; then
  die "Python 版本太低：$("$PY" -V 2>&1)
     本项目需要 Python 3.9 或更高。
     请安装 Homebrew 的 Python 并用它重跑：
         brew install python@3.12
         PYTHON=/opt/homebrew/bin/python3.12 bash 打包小丸工具箱.command
     或者双击前在终端里先执行：
         export PYTHON=/opt/homebrew/bin/python3.12"
fi

info "环境"
ok "macOS $(sw_vers -productVersion) / $(uname -m)"
ok "$("$PY" -V 2>&1)  →  $PY"

# 🔴 macOS 版本也要检查：PySide6 的 wheel 带最低系统标记。
# 6.9.x 的 wheel 是 macosx_12_0 —— macOS 11 及更低装不上（pip 会报
# 「No matching distribution」）。同样必须提前说清楚。
MACOS_VER="$(sw_vers -productVersion)"
MACOS_MAJOR="$(printf '%s' "$MACOS_VER" | cut -d. -f1)"
if [ "${MACOS_MAJOR:-0}" -lt 12 ] 2>/dev/null; then
  die "macOS 版本太低：$MACOS_VER
     PySide6 6.9 的预编译包要求 macOS 12 或更高。
     可选方案：
       · 升级 macOS 到 12+（推荐）
       · 或在旧系统上自行从源码构建 Qt（很麻烦，不推荐）"
fi

# 🔴 把 Python 的真实来源也打出来 —— macOS 自带的 /usr/bin/python3 是
# Xcode 命令行工具的转发壳，它在某些版本上 venv/pip 行为异常，
# 是「卡在 1/6」最常见的原因。让用户一眼能看出自己用的是哪一个。
PY_PATH="$(command -v "$PY")"
case "$PY_PATH" in
  /usr/bin/python3*)
    warn "你用的是 macOS 自带的 $PY_PATH"
    warn "它是 Xcode 命令行工具的转发壳，venv/pip 可能很慢或失败。"
    warn "如果卡住，建议改用 Homebrew 的 Python："
    warn "    brew install python@3.12"
    warn "    PYTHON=/opt/homebrew/bin/python3.12 bash 打包小丸工具箱.command"
    ;;
  *) ok "解释器路径：$PY_PATH" ;;
esac

# 网络连通性预检：pip 装不上时最常见的报错是网络，提前给出判断
echo "  检查 PyPI 连通性 …"
if curl -sS -o /dev/null --max-time 8 https://pypi.org/simple/ 2>/dev/null; then
  ok "PyPI 可达"
else
  warn "连不上 PyPI（https://pypi.org）。"
  warn "若后续 pip 安装失败/极慢，可换国内镜像，脚本失败时会给出命令。"
fi

# ---------- 1. 虚拟环境 -----------------------------------------------------
info "1/6 准备构建环境（.buildenv）"

# 🔴 .buildenv 目录存在但没有可用的 python（上次打包被中断留下的半成品，
# 或者建到一半就失败）—— 先清掉重建。否则脚本会以为"环境已就绪"，
# 后面每一步都报「找不到 .buildenv/bin/python」这种莫名其妙的错。
if [ -e ".buildenv" ] && [ ! -x ".buildenv/bin/python" ]; then
  warn "发现不完整的 .buildenv（可能是上次中断留下的），正在清理重来"
  rm -rf ".buildenv"
fi

if [ ! -x ".buildenv/bin/python" ]; then
  echo "  正在创建虚拟环境（首次约 10~60 秒）…"
  if ! "$PY" -m venv .buildenv; then
    die "创建虚拟环境失败。
     如果你用的是 macOS 自带的 python3（Xcode 命令行工具版），
     它可能缺少 venv 支持。请改用 Homebrew 的 Python：
         brew install python@3.12
     然后用明确的解释器重跑本脚本：
         PYTHON=/opt/homebrew/bin/python3.12 bash 打包小丸工具箱.command
     或双击时临时设：打开终端后先执行 export PYTHON=/opt/homebrew/bin/python3.12"
  fi
  ok "虚拟环境已创建"
fi

# 确认 pip 真的在（有些精简版 venv 不带 pip）
if ! .buildenv/bin/python -m pip --version >/dev/null 2>&1; then
  warn "虚拟环境里没有 pip，尝试补装"
  .buildenv/bin/python -m ensurepip --upgrade 2>/dev/null \
    || die "虚拟环境缺少 pip 且无法自动补装。请删掉 .buildenv 目录后重跑本脚本"
fi

# 🔴 pip 的三个安装步骤全部去掉 -q：
# 静默模式下用户看到的是一动不动（PySide6 下载 ~80MB，慢网要几分钟），
# 无法区分"在下载"和"卡死了"。这里必须让进度可见。
# 🔴 只存 python 的路径，不要把 "-m pip" 也塞进来：
# "$PIP" 带引号时 bash 把整串当成**一个命令名**去找，
# 于是报「.buildenv/bin/python -m pip: No such file or directory」
# —— 用户 mac 上实测就是它。调用处写成 "$PYV" -m pip … 才对。
PYV=".buildenv/bin/python"

# 装依赖时把输出同时存进日志，失败时从日志里提取关键错误直接显示。
# 原来只给一句「检查网络，或换镜像」—— 用户实测时真正的错误是
# 「找不到匹配的版本」（Python 版本太低），提示完全指错了方向。
PIP_LOG="$(mktemp)"
# 🔴 只在**成功**退出时清理日志。失败时 die 会把路径告诉用户，
# 那时删掉就等于"让你去看日志，日志已经没了"。
cleanup_pip_log() {
  if [ "${PIP_FAILED:-0}" != "1" ] && [ -f "$PIP_LOG" ]; then
    rm -f "$PIP_LOG"
  fi
}
trap 'cleanup_pip_log' EXIT
pip_install() {
  local desc="$1"; shift
  echo "  $desc"
  if "$@" --progress-bar on > >(tee -a "$PIP_LOG") 2>&1; then
    return 0
  fi
  # 失败：从日志里找最像原因的那几行
  local why=""
  for pat in "ERROR: Could not find a version" \
             "requires-python" \
             "No matching distribution" \
             "ERROR: No matching" \
             "Could not build wheels" \
             "Permission denied" \
             "Connection refused" \
             "Temporary failure in name resolution" \
             "Read timed out" \
             "SSL" ; do
    if grep -q "$pat" "$PIP_LOG" 2>/dev/null; then
      why="$(grep -m3 -E "$pat" "$PIP_LOG" | sed 's/^[[:space:]]*/           /')"
      break
    fi
  done
  printf '\n  \033[1;31m✗ pip 安装失败\033[0m\n'
  PIP_FAILED=1                       # 让 EXIT trap 保留日志
  if [ -n "$why" ]; then
    printf '  \033[1;33m关键错误：\033[0m\n%s\n' "$why"
  fi
  printf '  完整日志：%s\n' "$PIP_LOG"
  return 1
}

# 🔴 优先**离线安装**：wheels_mac/ 里预置了 mac 版的全部 wheel
# （PySide6/shiboken/pyinstaller/imageio-ffmpeg 等，两种架构各一份），
# 所以这里用 --no-index --find-links 完全不联网。
# 用户实测反馈："我还得去 mac 上下载依赖" —— 这些下载早就该由打包方做完。
WHEELS="wheels_mac"
OFFLINE_MODE=0
if [ -d "$WHEELS" ] && [ -n "$(ls -A "$WHEELS" 2>/dev/null)" ]; then
  n=$(ls "$WHEELS"/*.whl 2>/dev/null | wc -l | tr -d ' ')
  ok "发现离线依赖包：$n 个 wheel（无需联网）"
  OFFLINE_MODE=1
else
  warn "没有 wheels_mac/ 目录，将走联网安装（较慢，且可能因版本不兼容失败）"
fi

# ⚠️ macOS 自带的是 bash 3.2：`"${ARR[@]}"` 在数组为空 + set -u 时会报
# "unbound variable"。所以不用数组，用字符串拼参数。
OFFLINE_ARGS=""
if [ "$OFFLINE_MODE" = "1" ]; then
  OFFLINE_ARGS="--no-index --find-links $WHEELS"
fi

# pip 升级本身也要联网，离线时跳过（旧 pip 装 wheel 完全没问题）
if [ "$OFFLINE_MODE" = "1" ]; then
  echo "  跳过 pip 升级（离线模式，现有 pip 足够装 wheel）"
else
  echo "  正在升级 pip …"
  "$PYV" -m pip install --upgrade pip --progress-bar on \
    || warn "升级 pip 失败，继续（不影响后续安装）"
fi

echo "  正在安装运行依赖（PySide6 约 128MB）…"
# shellcheck disable=SC2086  （OFFLINE_ARGS 需要按空格拆成两个参数）
pip_install "  依赖：PySide6-Essentials + imageio-ffmpeg" \
  "$PYV" -m pip install \
  $OFFLINE_ARGS -r requirements.txt \
  || die "安装运行依赖失败（关键错误见上）。
     常见原因与对策：
       · 「Could not find a version / No matching distribution」
         → 本机 Python 或 macOS 版本与这些 wheel 不匹配。
           本包 wheel 要求 Python 3.10+ / macOS 12+。
           用 brew install python@3.12 后指定 PYTHON= 重跑。
       · 「Read timed out / Connection refused / SSL」
         → 说明在走联网安装（wheels_mac 缺失），换国内镜像：
            .buildenv/bin/python -m pip install -r requirements.txt \\
              -i https://pypi.tuna.tsinghua.edu.cn/simple"

echo "  正在安装打包工具（PyInstaller）…"
# shellcheck disable=SC2086
pip_install "  工具：PyInstaller" "$PYV" -m pip install $OFFLINE_ARGS pyinstaller \
  || die "安装 PyInstaller 失败（关键错误见上）。离线包装不上时可换镜像：
     .buildenv/bin/python -m pip install pyinstaller \\
       -i https://pypi.tuna.tsinghua.edu.cn/simple"
ok "依赖就绪"

# ---------- 2. ffmpeg -------------------------------------------------------
info "2/6 准备 ffmpeg / ffprobe"
mkdir -p bin

HOST_ARCH="$(uname -m)"
# bin_mac/ 里预置了两种架构的官方静态构建（从 npmmirror 镜像取的
# ffmpeg-static b6.1.1）。**优先用它** —— 这样使用者不需要装 Homebrew、
# 不需要联网，解压后双击就能出包。
case "$HOST_ARCH" in
  arm64)  MAC_SUFFIX="arm64" ;;
  x86_64) MAC_SUFFIX="x64" ;;
  *)      MAC_SUFFIX="" ;;
esac

bundled_ok=0
if [ -n "$MAC_SUFFIX" ] && [ -f "bin_mac/ffmpeg-$MAC_SUFFIX" ] \
   && [ -f "bin_mac/ffprobe-$MAC_SUFFIX" ]; then
  cp "bin_mac/ffmpeg-$MAC_SUFFIX" bin/ffmpeg  || die "复制内置 ffmpeg 失败"
  cp "bin_mac/ffprobe-$MAC_SUFFIX" bin/ffprobe || die "复制内置 ffprobe 失败"
  chmod +x bin/ffmpeg bin/ffprobe
  # Gatekeeper 会给从网上下载的二进制打隔离标记，去掉才允许执行
  xattr -c bin/ffmpeg bin/ffprobe 2>/dev/null || true
  bundled_ok=1
  ok "使用包内自带的 ffmpeg-${MAC_SUFFIX}（$(du -h bin/ffmpeg | cut -f1)）"
  ok "无需安装 ffmpeg / Homebrew"
fi

if [ "$bundled_ok" = "0" ]; then
  warn "包内没有匹配 $HOST_ARCH 的 ffmpeg，回退到系统已安装的版本"
  SRC=""
  for cand in "$(command -v ffmpeg 2>/dev/null)" \
              /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then SRC="$cand"; break; fi
  done
  if [ -z "$SRC" ]; then
    die "找不到可用的 ffmpeg，且包内没有 $HOST_ARCH 版本。
     请确认 bin_mac/ 目录完整（应含 ffmpeg-arm64/ffmpeg-x64/ffprobe-arm64/ffprobe-x64），
     或先安装：brew install ffmpeg"
  fi
  cp "$SRC" bin/ffmpeg || die "复制 ffmpeg 失败"
  DIR="$(dirname "$SRC")"
  if [ -x "$DIR/ffprobe" ]; then
    cp "$DIR/ffprobe" bin/ffprobe || warn "复制 ffprobe 失败（程序会自动降级）"
  else
    warn "同目录下没有 ffprobe，程序将降级用 ffmpeg -i 解析"
  fi
  chmod +x bin/ffmpeg bin/ffprobe 2>/dev/null || true
  ok "已从 $SRC 复制"
fi

# 🔴 架构一致性检查：跨架构的二进制打进去，用户机器上会直接跑不起来
if command -v lipo >/dev/null 2>&1 && [ -f bin/ffmpeg ]; then
  FF_ARCH="$(lipo -archs bin/ffmpeg 2>/dev/null || echo unknown)"
  ok "ffmpeg 架构：${FF_ARCH}（本机 ${HOST_ARCH}）"
  case " $FF_ARCH " in
    *" $HOST_ARCH "*) ;;
    *) die "ffmpeg 架构（${FF_ARCH}）与本机（${HOST_ARCH}）不一致，打出的 .app 无法运行" ;;
  esac
fi

# ---------- 3. 图标 ---------------------------------------------------------
info "3/6 生成图标"
.buildenv/bin/python packaging/make_icon.py >/dev/null 2>&1 || warn "图标生成失败，继续（用系统默认图标）"
if [ -f app/resources/icon.png ] && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$(mktemp -d)/icon.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z $size $size app/resources/icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1 || true
    dbl=$((size * 2))
    sips -z $dbl $dbl app/resources/icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
  done
  if iconutil -c icns "$ICONSET" -o packaging/icon.icns 2>/dev/null; then
    ok "packaging/icon.icns 已生成"
  else
    warn "icns 生成失败，继续"
  fi
fi

# ---------- 4. PyInstaller --------------------------------------------------
info "4/6 打包（首次较慢，约 3~8 分钟）"
rm -rf build dist/"${APP_NAME}.app" dist/"${APP_NAME}" 2>/dev/null || true
.buildenv/bin/python -m PyInstaller packaging/xiaowan.spec --noconfirm --clean \
  || die "PyInstaller 打包失败（看上面的报错）"

# onedir 产出的是普通目录，改成 .app
if [ -d "dist/${APP_NAME}" ] && [ ! -d "dist/${APP_NAME}.app" ]; then
  mv "dist/${APP_NAME}" "dist/${APP_NAME}.app" || die "重命名 .app 失败"
fi
[ -d "dist/${APP_NAME}.app" ] || die "没有找到产物 dist/${APP_NAME}.app"
ok "dist/${APP_NAME}.app 已生成"

# ---------- 5. 临时签名 -----------------------------------------------------
info "5/6 临时签名"
if command -v codesign >/dev/null 2>&1; then
  # ad-hoc 签名（-s -）：永不过期、只保完整性，本机双击可直接开。
  # 要"下载即开"必须用 Developer ID 公证，那是另一套流程。
  if codesign --force --deep --sign - "dist/${APP_NAME}.app" 2>/dev/null; then
    ok "ad-hoc 签名完成"
  else
    warn "签名失败（不影响本机运行）"
  fi
else
  warn "没有 codesign，跳过签名"
fi

# ---------- 6. 压缩 ---------------------------------------------------------
info "6/6 压缩成 zip"
rm -f "dist/${ZIP_NAME}" 2>/dev/null || true
# 🔴 必须 ditto 而不是 zip：ditto 保留符号链接、扩展属性、以及 .app 内的
#    执行权限。用普通 zip 压出来的 .app 解压后打不开（权限丢了）。
if command -v ditto >/dev/null 2>&1; then
  ( cd dist && ditto -c -k --sequesterRsrc --keepParent "${APP_NAME}.app" "${ZIP_NAME}" ) \
    || die "ditto 压缩失败"
else
  ( cd dist && zip -qry "${ZIP_NAME}" "${APP_NAME}.app" ) || die "zip 压缩失败"
fi
ok "dist/${ZIP_NAME}（$(du -h "dist/${ZIP_NAME}" | cut -f1)）"

# 顺手验证一下 zip 里 .app 的可执行权限还在
if unzip -Z "dist/${ZIP_NAME}" 2>/dev/null | grep -q 'Contents/MacOS/'; then
  ok "zip 内可执行文件权限正常"
fi

# ---------- 完成 ------------------------------------------------------------
SIZE="$(du -sh "dist/${APP_NAME}.app" | cut -f1)"
cat <<TIP

============================================================
  ✓ 打包完成
============================================================
  应用：dist/${APP_NAME}.app   （${SIZE}）
  分发：dist/${ZIP_NAME}

  分发说明
  --------
  · 只发 ${ZIP_NAME} 这一个文件。不要直接发 .app 文件夹 ——
    直接传文件夹会丢掉执行权限，对方解压后打不开。
  · 对方解压后双击即可。若被 Gatekeeper 拦下（未公证），
    右键 → 打开，或执行：xattr -cr "${APP_NAME}.app"
  · 架构不可跨：本机 $(uname -m) 打出的包只能在同架构 Mac 上运行。
    M 系列（arm64）与 Intel（x86_64）需要各自打包。

TIP

if [ -t 0 ]; then
  read -r -p "按回车键关闭…" _
fi
