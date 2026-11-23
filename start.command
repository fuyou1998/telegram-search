#!/usr/bin/env bash
# Double-clickable on macOS: starts the local service and opens the browser.
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1

if [ ! -x .venv/bin/python ]; then
    echo "还没有安装依赖。请先在这个文件夹里运行：bash install.sh"
    read -r -p "按回车关闭…" _
    exit 1
fi

if [ ! -f .telegram-data/telegram.session ]; then
    echo "还没有登录 Telegram。"
    echo "请先双击 login.command 完成一次登录，再运行本程序。"
    echo ""
    read -r -p "按回车关闭…" _
    exit 1
fi

.venv/bin/python launcher.py
echo ""
echo "搜索服务已停止。"
read -r -p "按回车关闭…" _
