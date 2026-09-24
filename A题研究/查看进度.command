#!/bin/zsh
TASK_DIR="${0:A:h}"
TASK_PYTHON="/Users/liyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$TASK_PYTHON" ]]; then
  TASK_PYTHON="$(command -v python3)"
fi
"$TASK_PYTHON" -B "$TASK_DIR/查看进度.py"
echo
read '?按回车关闭；下次双击会重新读取最新进度。'
