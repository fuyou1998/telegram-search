"""Build a self-contained Windows folder + zip that runs without installing Python.

    python build_portable.py

The output in dist/ has its own embedded interpreter, all dependencies, the app files and
two .cmd entry points. Credentials and sessions are never copied, so every copy starts clean.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
NAME = "Telegram搜索"
FOLDER = DIST / NAME
# Scratch tree; the finished build is swapped into FOLDER at the end.
RUNTIME = DIST / ".build"
PYTHON_VERSION = "3.13.5"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

APP_FILES = [
    "launcher.py", "server.py", "settings.py", "telegram_source.py", "search_quality.py",
    "history.py", "proxy_probe.py", "telegram_login.py", "compare.py", "test_server.py",
    "README.md", "LICENSE", ".env.example", "requirements.txt", "create-shortcuts.ps1",
    "screenshot.jpg",
]
APP_DIRS = ["static"]

LAUNCH_LOGIN = """@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Telegram 检索 - 登录
python\\python.exe telegram_login.py
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo 登录没有完成，请按上面的提示检查后重试。
)
echo.
pause
exit /b %RESULT%
"""

LAUNCH_SEARCH = """@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
title 极目
if not exist ".shortcuts-created" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "create-shortcuts.ps1" -Quiet > nul 2>&1
    echo ok > ".shortcuts-created"
)
if not exist ".telegram-data\\telegram.session" (
    echo 还没有登录 Telegram。
    echo 请先双击「1-登录.cmd」完成一次登录，再运行本程序。
    echo.
    pause
    exit /b 1
)
start "" "python\\pythonw.exe" launcher.py
exit /b 0
"""

LAUNCH_DIAGNOSTIC = """@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
title 极目 - 诊断模式
echo 诊断模式：窗口保持开启，出错信息会显示在这里。
echo 正常使用请直接点桌面或开始菜单的「极目」，那个不会弹黑窗口。
echo.
if not exist ".telegram-data\\telegram.session" (
    echo 还没有登录 Telegram。请先双击「1-登录.cmd」。
    echo.
    pause
    exit /b 1
)
python\\python.exe launcher.py
echo.
echo 搜索服务已停止。
pause
"""

LAUNCH_STOP = """@echo off
setlocal
chcp 65001 > nul
taskkill /f /im pythonw.exe > nul 2>&1
echo 已停止极目。
timeout /t 2 > nul
exit /b 0
"""

LAUNCH_SHORTCUT = """@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "create-shortcuts.ps1"
echo.
pause
"""

GUIDE = """极目 · Telegram 检索 - 使用说明
================================

这是一个本地运行的小工具：在你自己的电脑上搜索 Telegram 群组和频道。
所有登录信息和搜索结果都只保存在这台电脑上，不会上传到任何服务器。

整个过程只需要两个窗口：先「1-登录.cmd」，再「2-启动搜索.cmd」。


第一次使用
----------

第 1 步：申请 Telegram 应用凭据（只需一次，约 2 分钟）

  1. 浏览器打开 https://my.telegram.org/apps 并登录。
  2. 填写任意应用名，例如 my-search，提交。
  3. 页面上会显示 App api_id（一串数字）和 App api_hash（32 位字符），
     先复制下来，下一步会用到。


第 2 步：双击「1-登录.cmd」

  会弹出一个黑色窗口（这个窗口要输入东西，所以必须有），按提示一行一行输入：

  - App api_id     粘贴第 1 步复制的数字
  - App api_hash   粘贴第 1 步复制的 32 位字符

  - 代理           ★ 程序会自动检测，一般不用你操心 ★
                   它会扫描 Clash / v2rayN / FlClash 等常用端口，
                   检测到可用的会问一句「用这个代理吗」，直接回车就行。
                   如果没检测到（比如你的软件端口比较特殊），才需要手动填：
                   打开机场/加速器，找「本地端口」，常见 7890、7897、1080，
                   填成 socks5://127.0.0.1:端口；http 代理就填 http://127.0.0.1:端口。

  - 手机号         要带国际区号，例如 +8613800138000
  - 验证码         Telegram 会发到你已登录的 Telegram 客户端里（不是短信）

  看到「Telegram 登录完成」就可以关掉这个窗口了。只做一次，以后不用再登录。


第 3 步：双击「2-启动搜索.cmd」

  浏览器会自动打开 http://127.0.0.1:8787/ ，同时桌面和开始菜单会出现「极目」图标。

  以后直接点桌面上的「极目」就行，那个不会弹黑窗口。
  输入任意关键词，例如「漫画」。

  想关掉服务：在网页右上角「设置」最下面点「退出极目」，或者双击「停止极目.cmd」。


可选的额外设置（都在网页里点「连接设置」）
------------------------------------------

  - 网络代理：和登录时填的是同一个，换网络环境后可以在这里改。
  - 关键词扩展 AI：填了之后会生成更多语言的关键词变体，搜得更全。
    不填也能用，只是跨语言变体少一些。


常见问题
--------

Q: 提示「Telegram 未连接」怎么办？
   A: 打开网页右上角的「设置 › 网络代理」，确认代理地址和端口对得上，
      然后点「保存代理」。国内网络基本都必须走代理。

Q: 代理端口在哪看？
   A: 一般不用看——登录时程序会自动检测。
      没检测到再去机场/加速器里找「本地端口 / 混合端口」，常见 7890、7897、1080，
      填成 socks5://127.0.0.1:端口 或 http://127.0.0.1:端口。
      网页「设置 › 网络代理」里也有一个「自动检测」按钮，随时可以再试一次。

Q: 登录时一直报错连不上？
   A: 先确认代理软件已经打开，再重新运行「1-登录.cmd」，把代理地址填进去。

Q: 怎么关掉它？
   A: 网页右上角「设置」最下面点「退出极目」，或者双击「停止极目.cmd」。

Q: 双击「极目」图标没反应？
   A: 可能已经在后台运行了（直接看浏览器 http://127.0.0.1:8787/）。
      如果确实没启动，双击「4-诊断启动.cmd」看具体报错。

Q: 搜索很慢或者没有结果？
   A: 极搜（@jisou）有时会限流，等几分钟再试。

Q: 关键词扩展显示「AI 暂时不可用」？
   A: 正常降级。在「连接设置 › 关键词扩展」里填 Base URL、模型和 API Key 就会变好。

Q: 想换一个 Telegram 账号？
   A: 关掉搜索窗口，删掉 .telegram-data 文件夹，重新运行「1-登录.cmd」。

Q: 买来的号 / 收不到验证码怎么办？
   A: 不要走登录流程。找一台这个号已经登录着的电脑（或者能登进去的 Telegram 客户端），
      把那边的 .telegram-data 文件夹和 .env 一起复制到本目录，直接双击「极目」即可，
      不需要验证码。验证码在那种情况下是发到已登录设备的 Telegram 里，不会发短信。
      复制过来的会话等于账号本身，不要发给别人；建议登录后在「设置 › 设备」里
      退出不认识的设备，并开启两步验证。

Q: 怎么给别人用？
   A: 把这个文件夹（或压缩包）整个发给对方，让他按第 1-3 步自己操作。
      api_id、手机号、验证码全都在他自己电脑上完成，你不需要、也拿不到他的任何东西。


文件夹里都是什么
----------------

  1-登录.cmd        登录 Telegram（只需一次）
  2-启动搜索.cmd    启动搜索服务并打开网页（首次顺便创建快捷方式）
  3-创建快捷方式.cmd 在桌面和开始菜单放一个「极目」图标
  4-诊断启动.cmd    出问题时用，窗口保持开启，能看到报错
  停止极目.cmd      关闭后台运行的服务
  python\\           内置的 Python，不用另外安装
  static\\           网页界面
  .env              配置（登录时自动生成，也可以手动改）
  .telegram-data\\   登录后的会话，首次登录后自动生成
  search.db          历史记录，自动生成


注意
----

日常使用直接点桌面或开始菜单的「极目」，后台运行，没有黑窗口。
黑窗口只在「1-登录」「4-诊断启动」时出现，因为要输入内容或显示错误。

把文件夹换位置后，重新运行「3-创建快捷方式.cmd」更新路径。

.telegram-data 里的会话文件等同于你的账号，不要发给别人。
换电脑时如果复制了整个文件夹，等于把登录状态也带过去了。


卡住了？把这一页丢给 AI
-----------------------

如果哪一步看不懂，或者一直连不上，可以把这个文件夹（或这一份使用说明）
直接发给任意 AI 助手，让它按你的系统一步步教你操作。

连不上最常见的原因就是代理端口不对，AI 看到上面的说明也能帮你判断该填哪个。
也可以在网页「设置 › 网络代理」里点「自动检测」，让程序自己找。

但无论如何：不要把自己的验证码、api_hash、或者 .telegram-data 文件夹
发给任何人或任何 AI——那些等同于你的账号。
"""


def download(url: str, target: Path) -> None:
    print(f"下载 {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "portable-build/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def run(command: list[str], cwd: Path) -> None:
    print("运行", " ".join(command))
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        raise SystemExit(f"命令失败：{command[0]}")


def build() -> Path:
    # Build into a scratch folder first: a half-cleared output is worse than a stale one.
    global RUNTIME
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME, ignore_errors=True)
    if RUNTIME.exists():
        # A running copy of a previous build still holds the folder; use another name.
        RUNTIME = DIST / f".build-{time.strftime('%H%M%S')}"
    RUNTIME.mkdir(parents=True, exist_ok=True)

    python_dir = RUNTIME / "python"
    archive = DIST / "python-embed.zip"
    download(PYTHON_URL, archive)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(python_dir)
    archive.unlink()

    # A venv is not relocatable; an embedded interpreter is. Only site.py support and a
    # writable site-packages directory are missing from the embeddable distribution.
    pth = next(python_dir.glob("python*._pth"))
    stdlib_zip = pth.read_text(encoding="utf-8").splitlines()[0].strip()
    # Entries resolve relative to python.exe, so ".." is the application folder.
    pth.write_text(
        f"{stdlib_zip}\n.\n..\nLib\\site-packages\n\nimport site\n",
        encoding="utf-8",
    )
    (python_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    python_exe = python_dir / "python.exe"

    get_pip = DIST / "get-pip.py"
    download(GET_PIP_URL, get_pip)
    run([str(python_exe), str(get_pip), "--no-warn-script-location"], cwd=RUNTIME)
    get_pip.unlink()
    # Telethon depends on pyaes, which only ships as source and needs setuptools to build.
    run([str(python_exe), "-m", "pip", "install", "--no-warn-script-location",
         "--disable-pip-version-check", "setuptools", "wheel"], cwd=RUNTIME)

    for name in APP_FILES:
        shutil.copy2(ROOT / name, RUNTIME / name)
    for name in APP_DIRS:
        shutil.copytree(ROOT / name, RUNTIME / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    run([str(python_exe), "-m", "pip", "install", "--no-warn-script-location",
         "--disable-pip-version-check", "-r", "requirements.txt"], cwd=RUNTIME)

    (RUNTIME / "1-登录.cmd").write_text(LAUNCH_LOGIN, encoding="utf-8")
    (RUNTIME / "2-启动搜索.cmd").write_text(LAUNCH_SEARCH, encoding="utf-8")
    (RUNTIME / "3-创建快捷方式.cmd").write_text(LAUNCH_SHORTCUT, encoding="utf-8")
    (RUNTIME / "4-诊断启动.cmd").write_text(LAUNCH_DIAGNOSTIC, encoding="utf-8")
    (RUNTIME / "停止极目.cmd").write_text(LAUNCH_STOP, encoding="utf-8")
    (RUNTIME / "使用说明.txt").write_text(GUIDE, encoding="utf-8")

    # The .ps1 is copied file-to-file, so keep its UTF-8 BOM: Windows PowerShell 5.1
    # decodes BOM-less scripts as ANSI and would garble the Chinese shortcut name.
    script = RUNTIME / "create-shortcuts.ps1"
    if script.exists() and not script.read_bytes().startswith(b"\xef\xbb\xbf"):
        script.write_bytes(b"\xef\xbb\xbf" + script.read_bytes())

    # Never ship the builder's own credentials or session.
    for leftover in (RUNTIME / ".env", RUNTIME / ".telegram-data"):
        if leftover.is_dir():
            shutil.rmtree(leftover)
        elif leftover.exists():
            leftover.unlink()

    shutil.rmtree(RUNTIME / "python" / "Lib" / "site-packages" / "__pycache__", ignore_errors=True)

    result = subprocess.run(
        [str(python_exe), "-c", "import telethon, fastapi, uvicorn, httpx, dotenv, python_socks; print('deps ok')"],
        cwd=RUNTIME, capture_output=True, text=True,
    )
    print(result.stdout.strip() or result.stderr.strip()[-500:])
    if result.returncode != 0:
        raise SystemExit("内置 Python 无法导入依赖")

    # Swap the finished tree into place. Rename first, so a running copy of the previous
    # build keeps working and a failure leaves the old folder untouched.
    backup = DIST / f"{NAME}.old"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    archive_path = DIST / f"{NAME}-便携版.zip"
    if FOLDER.exists():
        try:
            FOLDER.rename(backup)
        except OSError:
            print(f"{FOLDER.name} 正在被使用；保留现有文件夹，直接从临时构建目录打包。")
            if archive_path.exists():
                archive_path.unlink()
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for path in sorted(RUNTIME.rglob("*")):
                    if path.is_file():
                        archive.write(path, (Path(NAME) / path.relative_to(RUNTIME)).as_posix())
            return archive_path
    shutil.move(str(RUNTIME), str(FOLDER))
    shutil.rmtree(backup, ignore_errors=True)

    shutil.make_archive(str(archive_path.with_suffix("")), "zip", DIST, NAME)
    return archive_path


if __name__ == "__main__":
    path = build()
    size = path.stat().st_size / 1024 / 1024
    print(f"\n完成：{path}  ({size:.1f} MB)")
    print(f"文件夹：{FOLDER}")
    sys.exit(0)
