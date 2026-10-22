#!/bin/zsh
set -euo pipefail

PAPER_SETUP_APP_DIR="${0:A:h}"
PAPER_SETUP_PROJECT_DIR="${PAPER_SETUP_APP_DIR:h}"
PAPER_SETUP_ENVS="$PAPER_SETUP_PROJECT_DIR/envs"
PAPER_SETUP_OS="$(/usr/bin/uname -s)"
PAPER_SETUP_ARCH="$(/usr/bin/uname -m)"
if [[ "$PAPER_SETUP_OS" != Darwin ]]; then
  print -u2 '此安装程序适用于 macOS。'
  exit 1
fi
case "$PAPER_SETUP_ARCH" in
  arm64)
    PAPER_SETUP_TARGET='python-macos-arm64'
    PAPER_SETUP_TRIPLE='aarch64-apple-darwin'
    PAPER_SETUP_SHA='52721745b0fa3196e4d0381fa5c06dda1d54343b90d49d90c3bba52d1171bd98'
    ;;
  x86_64)
    PAPER_SETUP_TARGET='python-macos-x86_64'
    PAPER_SETUP_TRIPLE='x86_64-apple-darwin'
    PAPER_SETUP_SHA='7c33b153a69c6255e6f2659cf39738f316b03969d6230d7bc47c73b7fde9a0d4'
    ;;
  *) print -u2 "暂不支持当前处理器架构：$PAPER_SETUP_ARCH"; exit 1 ;;
esac
PAPER_SETUP_RUNTIME="$PAPER_SETUP_ENVS/$PAPER_SETUP_TARGET"
PAPER_SETUP_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.13.9%2B20251014-$PAPER_SETUP_TRIPLE-install_only_stripped.tar.gz"

if [[ "${PAPERLENS_SETUP_DRYRUN:-0}" == 1 ]]; then
  print -r -- 'dryrun=True'
  print -r -- "runtime=$PAPER_SETUP_RUNTIME"
  print -r -- "url=$PAPER_SETUP_URL"
  print -r -- "sha256=$PAPER_SETUP_SHA"
  print -r -- 'plan=检查内置 Python；缺失时下载、校验、暂存自检后安装；不依赖本机 Python'
  exit 0
fi

unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE PYTHONSAFEPATH __PYVENV_LAUNCHER__ DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_COMPILE=1

paper_setup_runtime_valid() {
  local PAPER_CHECK_RUNTIME="$1"
  [[ -x "$PAPER_CHECK_RUNTIME/bin/python3" ]] && "$PAPER_CHECK_RUNTIME/bin/python3" -I -B -c '
import ensurepip, pathlib, sqlite3, ssl, sys
root = pathlib.Path(sys.argv[1]).resolve()
assert sys.version_info[:3] == (3, 13, 9)
assert pathlib.Path(sys.prefix).resolve() == root
assert pathlib.Path(sys.base_prefix).resolve() == root
assert pathlib.Path(sys.executable).resolve().is_relative_to(root)
assert pathlib.Path(ssl.__file__).resolve().is_relative_to(root)
assert pathlib.Path(sqlite3.__file__).resolve().is_relative_to(root)
' "$PAPER_CHECK_RUNTIME" >/dev/null 2>&1
}

if paper_setup_runtime_valid "$PAPER_SETUP_RUNTIME"; then
  exit 0
fi

PAPER_SETUP_STAGE=''
PAPER_SETUP_COMMITTED=0
paper_setup_cleanup() {
  if [[ -n "$PAPER_SETUP_STAGE" && -d "$PAPER_SETUP_STAGE" ]]; then
    if [[ "$PAPER_SETUP_COMMITTED" != 1 && ( -e "$PAPER_SETUP_STAGE/previous-runtime" || -L "$PAPER_SETUP_STAGE/previous-runtime" ) ]]; then
      /bin/rm -rf -- "$PAPER_SETUP_RUNTIME"
      if ! /bin/mv -- "$PAPER_SETUP_STAGE/previous-runtime" "$PAPER_SETUP_RUNTIME"; then
        print -u2 "旧运行时已保留在 $PAPER_SETUP_STAGE/previous-runtime，自动恢复未完成。"
        return 1
      fi
    fi
    /bin/rm -rf -- "$PAPER_SETUP_STAGE"
  fi
}
trap paper_setup_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

/bin/mkdir -p "$PAPER_SETUP_ENVS"
PAPER_SETUP_STAGE="$(/usr/bin/mktemp -d "$PAPER_SETUP_ENVS/.python-runtime.XXXXXXXX")"
PAPER_SETUP_ARCHIVE="$PAPER_SETUP_STAGE/runtime.tar.gz"
print '正在下载项目内置 Python，无需安装本机 Python 或 Conda…'
/usr/bin/curl --fail --location --retry 2 --connect-timeout 15 --max-time 300 --proto '=https' --tlsv1.2 --output "$PAPER_SETUP_ARCHIVE" "$PAPER_SETUP_URL"
PAPER_SETUP_ACTUAL_SHA="$(/usr/bin/shasum -a 256 < "$PAPER_SETUP_ARCHIVE")"
PAPER_SETUP_ACTUAL_SHA="${PAPER_SETUP_ACTUAL_SHA%% *}"
if [[ "$PAPER_SETUP_ACTUAL_SHA" != "$PAPER_SETUP_SHA" ]]; then
  print -u2 '内置 Python 下载校验失败，已有环境未修改，请重新启动。'
  exit 1
fi
/usr/bin/tar -xzf "$PAPER_SETUP_ARCHIVE" -C "$PAPER_SETUP_STAGE"
if ! paper_setup_runtime_valid "$PAPER_SETUP_STAGE/python"; then
  print -u2 '下载的内置 Python 自检失败，已有环境未修改。'
  exit 1
fi
if ! "$PAPER_SETUP_STAGE/python/bin/python3" -I -B -m pip --version >/dev/null 2>&1; then
  PIP_BREAK_SYSTEM_PACKAGES=1 "$PAPER_SETUP_STAGE/python/bin/python3" -I -B -m ensurepip --upgrade
fi

# A second launcher may have completed while this download was in progress.
if paper_setup_runtime_valid "$PAPER_SETUP_RUNTIME"; then
  exit 0
fi
if [[ -e "$PAPER_SETUP_RUNTIME" || -L "$PAPER_SETUP_RUNTIME" ]]; then
  /bin/mv -- "$PAPER_SETUP_RUNTIME" "$PAPER_SETUP_STAGE/previous-runtime"
fi
/bin/mv -- "$PAPER_SETUP_STAGE/python" "$PAPER_SETUP_RUNTIME"
if ! paper_setup_runtime_valid "$PAPER_SETUP_RUNTIME"; then
  print -u2 '内置 Python 迁移自检失败，正在恢复原有环境。'
  exit 1
fi
PAPER_SETUP_COMMITTED=1
print '项目内置 Python 已准备就绪。'
