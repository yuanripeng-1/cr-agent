#!/bin/bash

set -euo pipefail

ENV_NAME="cragent"
PYTHON_VERSION="3.11"

usage() {
  echo "用法: $0 [--env-name <name>] [--python-version <version>]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-name)
      ENV_NAME="$2"
      shift 2
      ;;
    --python-version)
      PYTHON_VERSION="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1"
      usage
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

if [[ ! -f "$REQ_FILE" ]]; then
  echo "requirements.txt 不存在: $REQ_FILE"
  exit 1
fi

if ! command -v conda >/dev/null 2>&1; then
  echo "未检测到 conda，请先安装 Miniconda/Anaconda。"
  exit 1
fi

if conda env list | awk -v env="$ENV_NAME" '$1 == env {found=1} END {exit !found}'; then
  conda env remove -n "$ENV_NAME" -y
fi

conda create -n "$ENV_NAME" python="$PYTHON_VERSION" -y
conda run -n "$ENV_NAME" pip install --upgrade pip
conda run -n "$ENV_NAME" pip install -r "$REQ_FILE"

if command -v npm >/dev/null 2>&1; then
  echo "检测到 npm，安装 claude-code-router 到当前目录..."
  mkdir -p "$SCRIPT_DIR/bin"
  if ! npm install --prefix "$SCRIPT_DIR/bin" @musistudio/claude-code-router; then
    echo "警告: claude-code-router 安装失败，请检查 npm 网络或包名。后续如需代理能力请手动安装。"
  fi
fi

echo "安装完成。"
echo "运行方式:"
echo "  conda activate $ENV_NAME"
echo "  bash RUN.sh --config workspace/<task>/agent_config.toml --platform gitlab"
