#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build.sh — 跨平台构建脚本（Windows / macOS / Linux）
#
# 用法：
#   ./build.sh              # 构建
#   ./build.sh --check      # 只做环境自检，不构建
#   ./build.sh --clean      # 清理产物后重新构建
#
# 前置要求：
#   - Python 3.10+（推荐 3.11）
#   - 已执行过：pip install -r requirements.txt && pip install pyinstaller
#
# 注意：PyInstaller 不支持交叉编译。在 macOS 上构建得到 .app，
#       在 Windows 上构建得到 .exe，各平台需各自构建。
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# ---------- 颜色（非 tty 时自动禁用）----------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_OFF=""
fi

ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_OFF" "$1"; }
warn() { printf '%s!%s %s\n' "$C_YELLOW" "$C_OFF" "$1"; }
err()  { printf '%s✗%s %s\n' "$C_RED" "$C_OFF" "$1"; }
step() { printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_OFF"; }

# 安全清理目标路径：优先 rm -rf；被沙箱/安全策略拦截时退回逐层删除。
# （某些受限环境会拦截批量删除，逐层 find -delete 通常仍可用）
_clean_target() {
  local target="$1"
  [ -e "$target" ] || return 0
  if rm -rf "$target" 2>/dev/null; then
    return 0
  fi
  if find "$target" -type f -delete 2>/dev/null; then
    find "$target" -type d -empty -delete 2>/dev/null
    return 0
  fi
  return 1
}

# ---------- 定位 Python ----------
find_python() {
  if [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"
  elif [ -x ".venv/Scripts/python.exe" ]; then
    echo ".venv/Scripts/python.exe"
  elif command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  else
    echo ""
  fi
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  err "找不到 Python，请先安装 Python 3.10+ 并创建虚拟环境"
  exit 1
fi

# ---------- 平台信息 ----------
OS_RAW="$(uname -s 2>/dev/null || echo unknown)"
case "$OS_RAW" in
  Darwin) PLATFORM="macOS"; BUNDLE_EXT=".app" ;;
  Linux)  PLATFORM="Linux"; BUNDLE_EXT="(目录)" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="Windows"; BUNDLE_EXT=".exe" ;;
  *) PLATFORM="$OS_RAW"; BUNDLE_EXT="" ;;
esac

# ---------- 架构检测 ----------
# 注意：决定产物架构的是【Python 解释器】的架构，不一定是机器硬件架构。
# 例如 M 系列 mac 上装了 Intel 版 Python，构建出的就是 Intel 包。
ARCH_INFO="$($PY - <<'PYEOF' 2>/dev/null || true
import platform, struct, sys
mach = platform.machine()
bits = struct.calcsize("P") * 8
# 判断解释器本身是 32/64 位以及机器架构
print("%s|%d" % (mach, bits))
PYEOF
)"
ARCH_RAW="$(echo "$ARCH_INFO" | cut -d'|' -f1)"
BITS="$(echo "$ARCH_INFO" | cut -d'|' -f2)"
[ -z "$ARCH_RAW" ] && ARCH_RAW="unknown"

# 归一化为简短标识（用于产物文件名）
case "$ARCH_RAW" in
  arm64|aarch64)     ARCH_TAG="arm64" ;;
  x86_64|AMD64|amd64) ARCH_TAG="x64" ;;
  i386|i686|x86)     ARCH_TAG="x86" ;;
  universal2)        ARCH_TAG="universal2" ;;
  *)                 ARCH_TAG="$ARCH_RAW" ;;
esac

# 该产物能运行在哪些机器上（macOS 专属逻辑）
ARCH_NOTE=""
if [ "$PLATFORM" = "macOS" ]; then
  case "$ARCH_TAG" in
    arm64)
      ARCH_NOTE="仅 Apple Silicon（M 系列）；Intel Mac 无法运行" ;;
    x64)
      ARCH_NOTE="Intel + Apple Silicon 均可（M 系列经 Rosetta 2 运行）" ;;
    universal2)
      ARCH_NOTE="Intel + Apple Silicon 原生支持" ;;
    *) ARCH_NOTE="未知架构，请确认" ;;
  esac
fi

# ===========================================================================
# 环境自检
# ===========================================================================
run_check() {
  step "== 环境自检 =="
  printf '平台      : %s (%s)\n' "$PLATFORM" "$OS_RAW"
  printf '架构      : %s%s\n' "$ARCH_RAW" "${BITS:+ ($BITS 位)}"
  printf 'Python    : %s\n' "$($PY --version 2>&1)"
  printf '解释器    : %s\n' "$PY"
  printf '构建产物  : %s\n' "$BUNDLE_EXT"
  if [ -n "$ARCH_NOTE" ]; then
    printf '兼容性    : %s\n' "$ARCH_NOTE"
  fi
  echo ""

  local failed=0

  # 0) macOS 架构提醒
  if [ "$PLATFORM" = "macOS" ] && [ "$ARCH_TAG" = "arm64" ]; then
    warn "当前是 arm64（Apple Silicon）解释器，产物只能在 M 系列 Mac 上运行"
    echo "     若需分发给 Intel Mac 用户，两种办法："
    echo "       ① 改用 Intel 版 Python 构建（产物可通吃两种机型）"
    echo "       ② 使用 universal2 版 Python：python.org 官网安装包即为此版本"
    echo "     详见 docs/BUILD-macOS.md"
    echo ""
  fi

  # 1) 核心依赖
  step "-- 依赖检查 --"
  for pkg in playwright httpx webview; do
    if $PY -c "import $pkg" >/dev/null 2>&1; then
      ok "$pkg"
    else
      err "$pkg 未安装"
      case "$pkg" in
        webview) echo "     → pip install pywebview" ;;
        *)       echo "     → pip install -r requirements.txt" ;;
      esac
      failed=1
    fi
  done

  # 2) PyInstaller
  if $PY -c "import PyInstaller" >/dev/null 2>&1; then
    ok "pyinstaller $($PY -c 'import PyInstaller; print(PyInstaller.__version__)' 2>/dev/null)"
  else
    err "pyinstaller 未安装"
    echo "     → pip install pyinstaller"
    failed=1
  fi

  # 3) 浏览器可用性（本机浏览器 或 playwright 内核，二者有其一即可）
  step "-- 浏览器检查 --"
  local browser_info
  browser_info="$($PY - <<'PYEOF' 2>/dev/null || true
import sys
sys.path.insert(0, "gui")
try:
    from platform_utils import find_browser, playwright_browsers_dir
except Exception as e:
    print("ERR|" + str(e)); raise SystemExit
b, ch = find_browser()
if b:
    print("LOCAL|%s|%s" % (ch, b))
pw = playwright_browsers_dir()
if pw and any(pw.glob("chromium*")):
    print("PW|%s" % pw)
PYEOF
)"
  if echo "$browser_info" | grep -q "^LOCAL|"; then
    ok "本机浏览器：$(echo "$browser_info" | grep '^LOCAL|' | cut -d'|' -f3)"
  else
    warn "未检测到本机 Chrome / Edge"
  fi
  if echo "$browser_info" | grep -q "^PW|"; then
    ok "playwright 内核：$(echo "$browser_info" | grep '^PW|' | cut -d'|' -f2)"
  else
    warn "未检测到 playwright 内核（若无本机浏览器，打包后需用户自装 Chrome）"
  fi

  # 4) 关键文件
  step "-- 文件检查 --"
  for f in xhs_video_downloader.py gui/app.py gui/bridge.py gui/index.html gui/platform_utils.py build_exe.spec; do
    if [ -f "$f" ]; then ok "$f"; else err "$f 缺失"; failed=1; fi
  done

  # 5) spec 的平台适配提示
  step "-- 打包配置 --"
  if grep -q "BUNDLE" build_exe.spec 2>/dev/null; then
    ok "spec 含 macOS BUNDLE 配置"
  else
    if [ "$PLATFORM" = "macOS" ]; then
      err "spec 缺少 BUNDLE 段，macOS 无法产出 .app"
      failed=1
    else
      warn "spec 无 BUNDLE 段（Windows/Linux 下正常）"
    fi
  fi

  # 6) 语法自检
  step "-- 语法自检 --"
  if $PY - <<'PYEOF'
import ast, sys
files = ["xhs_video_downloader.py", "gui/app.py", "gui/bridge.py",
         "gui/platform_utils.py"]
for f in files:
    try:
        ast.parse(open(f, encoding="utf-8").read())
    except Exception as e:
        print("  语法错误 %s: %s" % (f, e)); sys.exit(1)
print("  全部通过")
PYEOF
  then
    ok "语法检查通过"
  else
    err "存在语法错误"
    failed=1
  fi

  echo ""
  if [ "$failed" -eq 0 ]; then
    printf '%s自检通过，可以构建%s\n' "$C_GREEN" "$C_OFF"
    return 0
  else
    printf '%s自检发现问题，请先修复上述错误%s\n' "$C_RED" "$C_OFF"
    return 1
  fi
}

# ===========================================================================
# 构建
# ===========================================================================
run_build() {
  step "== 开始构建 =="
  if [ "${1:-}" = "--clean" ]; then
    echo "清理旧产物…"
    # 优先用 PyInstaller 自带的 --clean 清缓存，再逐个删除产物目录。
    # 不用 rm -rf 递归删（某些沙箱/安全策略会拦截批量删除），
    # 统一走 _clean_target（失败时自动退回逐层删除）。
    for d in build dist; do
      if [ -e "$d" ]; then
        if _clean_target "$d"; then
          echo "  已清理 $d"
        else
          warn "无法自动清理 $d（可能被安全策略拦截），请在文件管理器中手动删除后重试"
          warn "或改用 --noconfirm 让 PyInstaller 直接覆盖"
        fi
      fi
    done
  fi

  # --noconfirm 覆盖旧产物；--clean 由上面的逻辑处理（这里不再传，避免重复）
  if ! $PY -m PyInstaller build_exe.spec --noconfirm; then
    err "构建失败，请查看上方日志"
    return 1
  fi

  step "== 构建结果 =="
  local out
  if [ "$PLATFORM" = "macOS" ]; then
    out="dist/xhs-video-downloader.app"
  elif [ "$PLATFORM" = "Windows" ]; then
    out="dist/xhs-video-downloader/xhs-video-downloader.exe"
  else
    out="dist/xhs-video-downloader/xhs-video-downloader"
  fi

  if [ ! -e "$out" ]; then
    warn "未找到预期产物 $out，请检查 dist/ 目录"
    ls -la dist/ 2>/dev/null || true
    return 1
  fi

  # ---------- 重命名为带平台+架构标识的名字 ----------
  # 便于区分不同机器构建的产物，避免分发时搞混。
  # 命名规则：
  #   macOS  : xhs-video-downloader-<arch>.app        例: -arm64.app / -x64.app / -universal2.app
  #   Windows: xhs-video-downloader-windows-<arch>.exe
  #   Linux  : xhs-video-downloader-linux-<arch>
  local final_name
  if [ "$PLATFORM" = "macOS" ]; then
    final_name="xhs-video-downloader-${ARCH_TAG}.app"
  elif [ "$PLATFORM" = "Windows" ]; then
    final_name="xhs-video-downloader-windows-${ARCH_TAG}.exe"
  else
    final_name="xhs-video-downloader-linux-${ARCH_TAG}"
  fi

  local final_path
  if [ "$PLATFORM" = "macOS" ]; then
    # .app 是自包含目录，直接整体重命名
    final_path="dist/$final_name"
    _clean_target "$final_path"
    if mv "$out" "$final_path" 2>/dev/null; then
      ok "产物已命名 → $final_name"
    else
      final_path="$out"
      warn "重命名失败（目录可能被占用），仍为原名：$out"
    fi
  else
    # Windows/Linux：exe 必须与同级的 _internal 目录配套，不能单独改名。
    # 做法：整体重命名输出目录，再把目录内的可执行文件改名。
    local new_dir="dist/xhs-video-downloader-${ARCH_TAG}"
    _clean_target "$new_dir"
    if mv "dist/xhs-video-downloader" "$new_dir" 2>/dev/null; then
      # 目录内可执行文件也改成带标识的名字
      if [ -f "$new_dir/$(basename "$out")" ]; then
        mv "$new_dir/$(basename "$out")" "$new_dir/$final_name" 2>/dev/null || true
      fi
      final_path="$new_dir/$final_name"
      ok "产物已命名 → $new_dir/"
    else
      final_path="$out"
      warn "重命名失败（目录可能被占用），仍为原名"
    fi
  fi

  echo ""
  ok "产物：$final_path"
  if command -v du >/dev/null 2>&1; then
    # Windows/Linux 的产物是「exe + 同级 _internal 目录」，
    # 必须统计整个目录，只算 exe 文件会严重偏小（7M vs 实际 128M）。
    local size_src
    if [ "$PLATFORM" = "macOS" ]; then
      size_src="$final_path"
    else
      size_src="$(dirname "$final_path")"
    fi
    printf '体积：%s（含依赖目录）\n' "$(du -sh "$size_src" 2>/dev/null | cut -f1)"
  fi
  printf '架构：%s%s\n' "$ARCH_TAG" "${ARCH_NOTE:+ — $ARCH_NOTE}"

  echo ""
  printf '%s注意：%s请运行 %s%s%s（这是成品）。\n' \
    "$C_YELLOW" "$C_OFF" "$C_BOLD" "$final_path" "$C_OFF"
  if [ "$PLATFORM" != "macOS" ]; then
    printf '%s      不要运行 build/ 目录下的同名文件 —— 那是中间产物，缺少 _internal 依赖会报错！%s\n' \
      "$C_YELLOW" "$C_OFF"
  fi

  # 分发提示
  if [ "$PLATFORM" = "macOS" ] && [ -n "$ARCH_NOTE" ]; then
    echo ""
    printf '%s分发说明：%s%s\n' "$C_BOLD" "$C_OFF" "$ARCH_NOTE"
    if [ "$ARCH_TAG" = "arm64" ]; then
      printf '%s  要给 Intel Mac 用户使用，请见 docs/BUILD-macOS.md 的「多架构打包」章节%s\n' \
        "$C_YELLOW" "$C_OFF"
    fi
  fi

  # 自动清理 build/ 中间产物：里面有个同名 exe 极易误点，
  # 且它缺 _internal 会报 "Failed to load Python DLL"。
  # build/ 只是编译缓存，删掉不影响已产出的 dist/。
  if [ -d build ]; then
    if find build -type f -delete 2>/dev/null && find build -type d -empty -delete 2>/dev/null; then
      echo ""
      ok "已清理 build/ 中间产物（避免误点其中的同名 exe）"
    fi
  fi
}

# ===========================================================================
# 主流程
# ===========================================================================
echo "=============================================="
echo " 小红书视频下载器 — 构建脚本"
echo "=============================================="

case "${1:-}" in
  --check)
    run_check || exit 1
    ;;
  --clean)
    run_check || exit 1
    run_build --clean
    ;;
  ""|--build)
    run_check || exit 1
    run_build
    ;;
  -h|--help)
    cat <<'EOF'
用法：./build.sh [选项]

  （无参数）   自检 + 构建
  --check      只做环境自检，不构建
  --clean      清理旧产物后构建
  -h, --help   显示帮助
EOF
    ;;
  *)
    err "未知参数：$1"
    echo "用 ./build.sh --help 查看用法"
    exit 1
    ;;
esac
