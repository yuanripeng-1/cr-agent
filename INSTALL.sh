#!/bin/bash

set -euo pipefail

ENV_NAME="cragent"
PYTHON_VERSION="3.11"

usage() {
  echo "用法: $0 [--env-name <name>] [--python-version <version>]"
}

has_command_group() {
  shift 2

  for cmd in "$@"; do
    if command -v "$cmd" >/dev/null 2>&1; then
      return 0
    fi
    if conda run -n "$ENV_NAME" "$cmd" --version >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

check_command_group() {
  local label="$1"
  local install_hint="$2"
  shift 2

  if has_command_group "$label" "$install_hint" "$@"; then
    echo "检测到 $label"
  else
    echo "警告: 未检测到 $label（候选命令: $*）。$install_hint"
  fi
}

install_system_package() {
  local label="$1"
  local brew_pkg="$2"
  local apt_pkg="$3"
  local conda_pkg="$4"

  echo "尝试安装 $label..."
  if command -v brew >/dev/null 2>&1; then
    brew install "$brew_pkg"
    return $?
  fi
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y "$apt_pkg"
    return $?
  fi
  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y "$apt_pkg"
    return $?
  fi
  if command -v yum >/dev/null 2>&1; then
    sudo yum install -y "$apt_pkg"
    return $?
  fi
  if command -v apk >/dev/null 2>&1; then
    sudo apk add --no-cache "$apt_pkg"
    return $?
  fi
  if [[ -n "$conda_pkg" ]]; then
    conda install -n "$ENV_NAME" -c conda-forge "$conda_pkg" -y
    return $?
  fi
  return 1
}

ensure_system_tool() {
  local label="$1"
  local install_hint="$2"
  local brew_pkg="$3"
  local apt_pkg="$4"
  local conda_pkg="$5"
  shift 5

  if has_command_group "$label" "$install_hint" "$@"; then
    echo "检测到 $label"
    return 0
  fi

  if ! install_system_package "$label" "$brew_pkg" "$apt_pkg" "$conda_pkg"; then
    echo "警告: $label 自动安装失败。$install_hint"
  fi
  check_command_group "$label" "$install_hint" "$@"
}

ensure_pip_tool() {
  local label="$1"
  local install_hint="$2"
  local pip_pkg="$3"
  shift 3

  if has_command_group "$label" "$install_hint" "$@"; then
    echo "检测到 $label"
    return 0
  fi

  echo "尝试安装 $label..."
  if ! conda run -n "$ENV_NAME" python -m pip install "$pip_pkg"; then
    echo "警告: $label 自动安装失败。$install_hint"
  fi
  check_command_group "$label" "$install_hint" "$@"
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

echo "检查外部工具..."
ensure_system_tool "git" "请安装 git；缺失时 git 工具会降级。" git git git git
ensure_system_tool "ripgrep/rg" "请安装 ripgrep；缺失时 grep_text 会降级。" ripgrep ripgrep ripgrep ripgrep rg
ensure_system_tool "ast-grep" "请安装 ast-grep；缺失时 ast_grep_search 会降级。" ast-grep ast-grep "" ast-grep ast-grep sg
ensure_pip_tool "Semble" "请安装 semble；缺失时 semble_search 会降级。" semble semble
ensure_pip_tool "code-review-graph" "请安装 code-review-graph；缺失时 crg_* 工具会降级。" code-review-graph code-review-graph crg

echo "安装完成。"
echo "运行方式:"
echo "  conda activate $ENV_NAME"
echo "  bash RUN.sh --config workspace/<task>/agent_config.toml --platform gitlab"
