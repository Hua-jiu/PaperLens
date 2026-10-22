#!/bin/zsh
set -e
PAPER_PROJECT_DIR="${0:A:h}"
exec /bin/zsh "$PAPER_PROJECT_DIR/app/launch.zsh"
