#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$SCRIPT_DIR/.tools/bin:$PATH"
CONFIG_FILE=""
PLATFORM_OVERRIDE=""
ENV_NAME="cragent"
BOOTSTRAP_ONLY=""
TIMEOUT_S=""

usage() {
  echo "用法: $0 --config <workspace中的config.toml路径> [--platform <gitlab|infcode>] [--env-name <name>] [--bootstrap-only] [--timeout-s <seconds>]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_FILE="$2"
      shift 2
      ;;
    --platform)
      PLATFORM_OVERRIDE="$2"
      shift 2
      ;;
    --env-name)
      ENV_NAME="$2"
      shift 2
      ;;
    --bootstrap-only)
      BOOTSTRAP_ONLY="1"
      shift
      ;;
    --timeout-s)
      TIMEOUT_S="$2"
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

if [[ -z "$CONFIG_FILE" ]]; then
  echo "错误: 必须提供 --config 参数"
  usage
  exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "错误: 配置文件不存在: $CONFIG_FILE"
  exit 1
fi

if [[ -n "$PLATFORM_OVERRIDE" ]]; then
  case "$PLATFORM_OVERRIDE" in
    gitlab|infcode) ;;
    *)
      echo "错误: --platform 只支持 gitlab 或 infcode"
      exit 1
      ;;
  esac
fi

ABS_CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"

CONDA_ENV_DIR="$(conda env list | awk -v env="$ENV_NAME" '$1==env {print $NF}')"
if [[ -z "$CONDA_ENV_DIR" || ! -x "$CONDA_ENV_DIR/bin/python" ]]; then
  echo "错误: 未找到 conda 环境 $ENV_NAME，请先执行 INSTALL.sh"
  exit 1
fi

CMD=("$CONDA_ENV_DIR/bin/python" -m cr_agent.main --config "$ABS_CONFIG_FILE")
if [[ -n "$PLATFORM_OVERRIDE" ]]; then
  CMD+=(--platform "$PLATFORM_OVERRIDE")
fi
if [[ -n "$BOOTSTRAP_ONLY" ]]; then
  CMD+=(--bootstrap-only)
fi
if [[ -n "$TIMEOUT_S" ]]; then
  CMD+=(--timeout-s "$TIMEOUT_S")
fi

echo "📋 Config: $ABS_CONFIG_FILE"
if [[ -n "$PLATFORM_OVERRIDE" ]]; then
  echo "🌐 Platform override: $PLATFORM_OVERRIDE"
fi
echo "🚀 启动 CR-Agent SDK pipeline..."

cd "$SCRIPT_DIR"
"${CMD[@]}"
