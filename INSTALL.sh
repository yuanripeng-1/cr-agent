#!/bin/bash

set -euo pipefail

ENV_NAME="cragent"
CRG_ENV_NAME="crg"
PYTHON_VERSION="3.11"

usage() {
  echo "用法: $0 [--env-name <name>] [--crg-env-name <name>] [--python-version <version>]"
}

upgrade_pip_tooling() {
  local env_name="$1"
  conda run -n "$env_name" python -m pip install --upgrade pip setuptools wheel
}

conda_cmd_exists() {
  local env_name="$1"
  local cmd="$2"
  conda run -n "$env_name" python -c "import shutil, sys; sys.exit(0 if shutil.which('${cmd}') else 1)" 2>/dev/null
}

has_command_group() {
  shift 2

  for cmd in "$@"; do
    if command -v "$cmd" >/dev/null 2>&1; then
      return 0
    fi
    if conda_cmd_exists "$ENV_NAME" "$cmd"; then
      return 0
    fi
    if conda_cmd_exists "$CRG_ENV_NAME" "$cmd"; then
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
    echo "警告: 未检测到 ${label}（候选命令: $*）。${install_hint}"
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

create_tool_wrapper() {
  local wrapper_path="$1"
  local target_bin="$2"

  if [[ -z "$target_bin" || ! -x "$target_bin" ]]; then
    echo "警告: 无法创建 wrapper，目标命令不可执行: $target_bin"
    return 1
  fi

  mkdir -p "$(dirname "$wrapper_path")"
  cat > "$wrapper_path" <<EOF
#!/usr/bin/env bash
exec "$target_bin" "\$@"
EOF
  chmod +x "$wrapper_path"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-name)
      ENV_NAME="$2"
      shift 2
      ;;
    --crg-env-name)
      CRG_ENV_NAME="$2"
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
  echo "错误: 未找到 Conda"
  echo ""
  echo "请先安装 Conda:"
  echo "  1. 使用 Homebrew 安装:"
  echo "     brew install --cask miniconda"
  echo ""
  echo "  2. 或从官网下载安装:"
  echo "     https://docs.conda.io/en/latest/miniconda.html"
  echo ""
  echo "安装完成后初始化 shell 并重新运行此脚本，例如:"
  echo "  conda init zsh && source ~/.zshrc"
  exit 1
fi

if [[ -d "$SCRIPT_DIR/.venv" ]]; then
  echo "警告: 发现 $SCRIPT_DIR/.venv，建议删除以避免 pip 环境混淆: rm -rf .venv"
fi

if conda env list | awk -v env="$ENV_NAME" '$1 == env {found=1} END {exit !found}'; then
  conda env remove -n "$ENV_NAME" -y
fi

conda create -n "$ENV_NAME" python="$PYTHON_VERSION" -y
upgrade_pip_tooling "$ENV_NAME"
conda run -n "$ENV_NAME" python -m pip install -r "$REQ_FILE"

if conda env list | awk -v env="$CRG_ENV_NAME" '$1 == env {found=1} END {exit !found}'; then
  conda env remove -n "$CRG_ENV_NAME" -y
fi

conda create -n "$CRG_ENV_NAME" python="$PYTHON_VERSION" -y
upgrade_pip_tooling "$CRG_ENV_NAME"
conda run -n "$CRG_ENV_NAME" python -m pip install code-review-graph

echo "检查外部工具..."
ensure_system_tool "git" "请安装 git；缺失时 git 工具会降级。" git git git git git
ensure_system_tool "ripgrep/rg" "请安装 ripgrep；缺失时 grep_text 会降级。" ripgrep ripgrep ripgrep ripgrep rg
ensure_system_tool "ast-grep" "请安装 ast-grep；缺失时 ast_grep_search 会降级。" ast-grep ast-grep "" ast-grep ast-grep sg
ensure_pip_tool "Semble" "请安装 semble；缺失时 semble_search 会降级。" semble semble

TOOLS_BIN_DIR="$SCRIPT_DIR/.tools/bin"
SEMBLE_BIN="$(conda run -n "$ENV_NAME" python -c 'import shutil; print(shutil.which("semble") or "")')"
CRG_BIN="$(conda run -n "$CRG_ENV_NAME" python -c 'import shutil; print(shutil.which("code-review-graph") or "")')"
create_tool_wrapper "$TOOLS_BIN_DIR/semble" "$SEMBLE_BIN"
create_tool_wrapper "$TOOLS_BIN_DIR/code-review-graph" "$CRG_BIN"

PATH="$TOOLS_BIN_DIR:$PATH"
check_command_group "Semble wrapper" "请检查 $TOOLS_BIN_DIR/semble。" semble
check_command_group "code-review-graph wrapper" "请检查 $TOOLS_BIN_DIR/code-review-graph。" code-review-graph

echo "安装完成。"
echo "运行方式:"
echo "  conda activate $ENV_NAME"
echo "  bash RUN.sh --config workspace/<task>/agent_config.toml --platform gitlab"
