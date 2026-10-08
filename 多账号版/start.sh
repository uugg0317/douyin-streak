#!/bin/bash
# 抖音续火花 - 单账号启动脚本
# 用法: bash start.sh

cd "$(dirname "$0")"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

# 检查虚拟环境，没有则创建
if [ ! -d "venv" ]; then
    echo "[1/3] 创建 Python 虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 安装依赖
echo "[2/3] 安装依赖..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# 安装 Playwright 浏览器（首次运行需要）
if [ ! -d "/root/.cache/ms-playwright" ] && [ ! -d "$HOME/.cache/ms-playwright" ]; then
    echo "  安装 Playwright Chromium 浏览器..."
    playwright install --with-deps chromium 2>/dev/null || playwright install chromium
fi

# 启动应用
echo "[3/3] 启动抖音续火花服务..."
echo "  管理后台: http://127.0.0.1:8000"
echo "  本机免登录：浏览器打开地址后直接进入管理后台"
echo "  按 Ctrl+C 停止"
echo ""

python app.py
