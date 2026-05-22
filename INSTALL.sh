#!/bin/bash

# CR-Agent 安装脚本
# 用法: ./INSTALL.sh [环境名称] [Python版本]

set -e  # 遇到错误立即退出

# 默认参数
ENV_NAME="cragent"
PYTHON_VERSION="3.11"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --env-name)
            ENV_NAME="$2"
            shift 2
            ;;
        --python-version)
            PYTHON_VERSION="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项说明:"
            echo "  --env-name <名称>      指定 Conda 环境名称 (默认: cragent)"
            echo "  --python-version <版本> 指定 Python 版本 (默认: 3.9)"
            echo "  -h, --help             显示帮助信息"
            echo ""
            echo "示例:"
            echo "  $0                                    # 使用默认配置"
            echo "  $0 --env-name myenv --python-version 3.10"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            echo "使用 -h 或 --help 查看帮助"
            exit 1
            ;;
    esac
done

echo "========================================"
echo "  CR-Agent 安装脚本"
echo "========================================"
echo ""
echo "配置信息:"
echo "  环境名称: $ENV_NAME"
echo "  Python 版本: $PYTHON_VERSION"
echo ""

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements.txt"

# 检查 requirements.txt 是否存在
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "❌ 错误: requirements.txt 文件不存在: $REQUIREMENTS_FILE"
    exit 1
fi

# 检查 conda 是否已安装
echo "🔍 检查 Conda 是否已安装..."
if ! command -v conda &> /dev/null; then
    echo "❌ 错误: 未找到 Conda"
    echo ""
    echo "请先安装 Conda:"
    echo "  1. 使用 Homebrew 安装:"
    echo "     brew install --cask miniconda"
    echo ""
    echo "  2. 或从官网下载安装:"
    echo "     https://docs.conda.io/en/latest/miniconda.html"
    echo ""
    echo "安装完成后，请重新运行此脚本"
    exit 1
fi

echo "✅ Conda 已安装: $(conda --version)"
echo ""

# 检查环境是否已存在
echo "🔍 检查 Conda 环境..."
if conda env list | grep -q "^${ENV_NAME} "; then
    echo "⚠️  警告: Conda 环境 '$ENV_NAME' 已存在，正在自动删除并重新创建..."
    echo "🗑️  删除现有环境..."
    conda env remove -n "$ENV_NAME" -y
    echo "✅ 环境已删除"
fi

# 创建 Conda 环境
echo ""
echo "📦 创建 Conda 环境: $ENV_NAME (Python $PYTHON_VERSION)..."
conda create -n "$ENV_NAME" python="$PYTHON_VERSION" -y

echo "✅ Conda 环境创建成功"
echo ""

# 激活环境并安装依赖
echo "📥 安装项目依赖..."
echo "依赖文件: $REQUIREMENTS_FILE"
echo ""

# 使用 conda run 在指定环境中执行命令
conda run -n "$ENV_NAME" pip install --upgrade pip
conda run -n "$ENV_NAME" pip install -r "$REQUIREMENTS_FILE"

echo ""
echo "✅ Python 依赖安装完成"
echo ""

# 显示安装的包
echo "📋 已安装的包:"
conda run -n "$ENV_NAME" pip list
echo ""

# 完成
echo "========================================"
echo "✨ 安装完成！"
echo "========================================"
echo ""
echo "使用方法:"
echo ""
echo "1. 激活 Conda 环境:"
echo "   conda activate $ENV_NAME"
echo ""
echo "2. 运行 CR-Agent:"
echo "   cd $SCRIPT_DIR"
echo "   bash RUN.sh --config config.toml"
echo ""
echo "3. 退出 Conda 环境:"
echo "   conda deactivate"
echo ""
echo "========================================"
