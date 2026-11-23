#!/usr/bin/env bash
# Double-clickable on macOS. Falls back to a helpful message if setup has not run yet.
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1

if [ ! -x .venv/bin/python ]; then
    echo "还没有安装依赖。请先在这个文件夹里运行：bash install.sh"
    read -r -p "按回车关闭…" _
    exit 1
fi

.venv/bin/python telegram_login.py
status=$?
if [ "$status" -ne 0 ]; then
    echo ""
    echo "登录没有完成，请按上面的提示检查后重试。"
fi
echo ""
read -r -p "按回车关闭…" _
exit "$status"
