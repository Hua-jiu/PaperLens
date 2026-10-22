#!/bin/zsh
set -euo pipefail

PAPER_APP_DIR="${0:A:h}"
PAPER_PROJECT_DIR="${PAPER_APP_DIR:h}"
PAPER_OS="$(/usr/bin/uname -s)"
PAPER_ARCH="$(/usr/bin/uname -m)"
if [[ "$PAPER_OS" != Darwin ]]; then
  print -u2 '此启动入口适用于 macOS，请使用对应操作系统的独立运行时。'
  exit 1
fi
case "$PAPER_ARCH" in
  arm64) PAPER_RUNTIME="$PAPER_PROJECT_DIR/envs/python-macos-arm64" ;;
  x86_64) PAPER_RUNTIME="$PAPER_PROJECT_DIR/envs/python-macos-x86_64" ;;
  *) print -u2 "暂不支持当前处理器架构：$PAPER_ARCH"; exit 1 ;;
esac
PAPER_PYTHON="$PAPER_RUNTIME/bin/python3"
cd "$PAPER_APP_DIR"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_COMPILE=1
unset PYTHONHOME PYTHONPATH PYTHONEXECUTABLE PYTHONSAFEPATH __PYVENV_LAUNCHER__ DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH

paper_runtime_valid() {
  [[ -x "$PAPER_PYTHON" ]] && "$PAPER_PYTHON" -I -B -c '
import pathlib, sqlite3, ssl, sys
root = pathlib.Path(sys.argv[1]).resolve()
assert sys.version_info[:3] == (3, 13, 9)
assert pathlib.Path(sys.prefix).resolve() == root
assert pathlib.Path(sys.base_prefix).resolve() == root
assert pathlib.Path(sys.executable).resolve().is_relative_to(root)
assert pathlib.Path(ssl.__file__).resolve().is_relative_to(root)
assert pathlib.Path(sqlite3.__file__).resolve().is_relative_to(root)
' "$PAPER_RUNTIME" >/dev/null 2>&1
}

if ! paper_runtime_valid; then
  if [[ "${PAPERLENS_DRYRUN:-0}" == 1 ]]; then
    print -u2 'dryrun 模式下内置 Python 不可用；不会下载或安装运行时。'
    exit 1
  fi
  /bin/zsh "$PAPER_APP_DIR/setup_runtime.zsh"
  if ! paper_runtime_valid; then
    print -u2 '内置 Python 自检未通过，请重新运行启动入口。'
    exit 1
  fi
fi

if ! "$PAPER_PYTHON" -I -B -c 'import pymupdf; from markitdown.converters import PdfConverter; from markitdown import StreamInfo' >/dev/null 2>&1; then
  if [[ "${PAPERLENS_DRYRUN:-0}" == 1 ]]; then
    print -u2 'dryrun 模式下项目依赖不完整；不会安装或修改环境。'
    exit 1
  fi
  print '正在使用项目内置 Python 准备依赖，首次配置可能需要联网…'
  if ! "$PAPER_PYTHON" -I -B -m pip --version >/dev/null 2>&1; then
    PIP_BREAK_SYSTEM_PACKAGES=1 "$PAPER_PYTHON" -I -B -m ensurepip --upgrade
  fi
  "$PAPER_PYTHON" -B -E -s -c 'from bootstrap import main; main(dryrun=False)'
fi

export PATH="$PAPER_RUNTIME/bin:$PATH"
exec "$PAPER_PYTHON" -B -E -s "$PAPER_APP_DIR/start.py"
