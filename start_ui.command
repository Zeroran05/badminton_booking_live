#!/bin/zsh

set -eu
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "尚未找到项目虚拟环境：$PROJECT_DIR/.venv"
  echo "请先按照 README 完成环境安装。"
  echo
  read "?按回车键关闭窗口……"
  exit 1
fi

exec .venv/bin/python ui_server.py
