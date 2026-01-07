#!/bin/bash

# CR-Agent 运行脚本
# 用法: ./RUN.sh --config <config.toml路径>

set -e  # 遇到错误立即退出

# 默认配置文件路径
CONFIG_FILE=""

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法: $0 --config <config.toml路径>"
            echo ""
            echo "参数说明:"
            echo "  --config    指定 config.toml 配置文件的路径"
            echo "  -h, --help  显示帮助信息"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            echo "使用 -h 或 --help 查看帮助"
            exit 1
            ;;
    esac
done

# 检查是否提供了配置文件
if [ -z "$CONFIG_FILE" ]; then
    echo "错误: 必须提供 --config 参数"
    echo "使用 -h 或 --help 查看帮助"
    exit 1
fi

# 检查配置文件是否存在
if [ ! -f "$CONFIG_FILE" ]; then
    echo "错误: 配置文件不存在: $CONFIG_FILE"
    exit 1
fi

echo "📋 使用配置文件: $CONFIG_FILE"

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 从 config.toml 中提取配置
# 使用 grep 和 sed 提取配置值
CONTEXT_JSON_PATH=$(grep -E "^json_path" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')
PROJECT_LANGUAGE=$(grep -E "^language" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')
PROJECT_STATUS=$(grep -E "^status" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')
GUIDELINES_PATH=$(grep -E "^guidelines_path" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')
REQUIREMENTS_PATH=$(grep -E "^requirements_path" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')
LLM_MODEL=$(grep -E "^model" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')
LLM_API_KEY=$(grep -E "^api_key" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')
LLM_API_BASE=$(grep -E "^api_base" "$CONFIG_FILE" | sed 's/.*= *"\(.*\)".*/\1/')

# 处理相对路径
if [[ ! "$CONTEXT_JSON_PATH" = /* ]]; then
    CONTEXT_JSON_PATH="$(dirname "$CONFIG_FILE")/$CONTEXT_JSON_PATH"
fi

if [[ ! "$GUIDELINES_PATH" = /* ]]; then
    GUIDELINES_PATH="$(dirname "$CONFIG_FILE")/$GUIDELINES_PATH"
fi

if [[ ! "$REQUIREMENTS_PATH" = /* ]]; then
    REQUIREMENTS_PATH="$(dirname "$CONFIG_FILE")/$REQUIREMENTS_PATH"
fi

echo "📄 Context JSON: $CONTEXT_JSON_PATH"
echo "🌐 LLM Model: $LLM_MODEL"
echo "📖 Requirements: $REQUIREMENTS_PATH"

# 检查 context.json 是否存在
if [ ! -f "$CONTEXT_JSON_PATH" ]; then
    echo "错误: Context JSON 文件不存在: $CONTEXT_JSON_PATH"
    exit 1
fi

# 从 context.json 中提取 mr_iid 和 head_sha
MR_IID=$(grep -o '"mr_iid"[[:space:]]*:[[:space:]]*[0-9]*' "$CONTEXT_JSON_PATH" | grep -o '[0-9]*$')
HEAD_SHA=$(grep -o '"head_sha"[[:space:]]*:[[:space:]]*"[^"]*"' "$CONTEXT_JSON_PATH" | grep -o '"[^"]*"$' | tr -d '"')

# 提取 head_sha 的前8位
HEAD_SHA_SHORT="${HEAD_SHA:0:8}"

echo "🔀 MR IID: $MR_IID"
echo "🔐 Head SHA: $HEAD_SHA (前8位: $HEAD_SHA_SHORT)"

# 构建输出目录路径
# 检查是否在 Docker 容器内（通过检查 /workspace 是否可写）
if [ -w "/workspace" ] 2>/dev/null; then
    # 在 Docker 容器内，使用 /workspace 路径
    OUTPUT_DIR="/workspace/cr-result/${MR_IID}-${HEAD_SHA_SHORT}"
else
    # 在本地开发环境，使用项目根目录下的 results 文件夹
    OUTPUT_DIR="${SCRIPT_DIR}/results/${MR_IID}-${HEAD_SHA_SHORT}"
fi

OUTPUT_FILE="${OUTPUT_DIR}/cr_result.md"

echo "📁 输出目录: $OUTPUT_DIR"
echo "📝 输出文件: $OUTPUT_FILE"

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 设置环境变量
export OPENAI_API_KEY="$LLM_API_KEY"
export OPENAI_API_BASE="$LLM_API_BASE"

# 运行 cr-agent
echo ""
echo "🚀 开始运行 CR-Agent..."
echo "========================================"

cd "$SCRIPT_DIR"

# 运行 Python 脚本
python3 -m agent.main

# 检查是否生成了 CR_REPORT.md
if [ -f "CR_REPORT.md" ]; then
    echo ""
    echo "✅ CR-Agent 运行完成"
    echo "📦 移动结果到: $OUTPUT_FILE"
    
    # 移动结果文件到指定位置
    mv "CR_REPORT.md" "$OUTPUT_FILE"
    
    echo ""
    echo "========================================"
    echo "✨ 审查完成！结果已保存到: $OUTPUT_FILE"
    echo "========================================"
else
    echo ""
    echo "❌ 错误: 未生成 CR_REPORT.md"
    exit 1
fi
