#!/usr/bin/env bash
# One-click setup for macOS and Linux.
#   bash install.sh
# It creates a local virtualenv, installs dependencies and makes the launchers executable.
set -euo pipefail

cd "$(dirname "$0")" || exit 1
say() { printf '%s\n' "$*"; }

say "极目 · Telegram 检索 — 安装"
say "----------------------------------------"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    say "没有找到 Python 3.11 或更新版本。"
    say "macOS:    brew install python@3.12     （或者到 https://www.python.org/downloads/ 下载）"
    say "Linux:    sudo apt install python3-venv    /    sudo dnf install python3"
    exit 1
fi
say "使用 $("$PYTHON" -V 2>&1)"

if [ ! -d .venv ]; then
    say "创建虚拟环境 .venv ..."
    "$PYTHON" -m venv .venv
fi

say "安装依赖（第一次会比较慢）..."
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt --quiet

if [ ! -f .env ]; then
    cp .env.example .env
    say "已生成 .env"
fi

chmod +x start.command login.command 2>/dev/null || true

say ""
say "安装完成。接下来："
say "  1. 双击 login.command（或在终端运行 ./login.command）完成一次登录"
say "  2. 双击 start.command 打开搜索页面"
say ""
say "如果双击没有反应，先在终端里执行：chmod +x *.command"
