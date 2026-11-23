import asyncio
from dataclasses import replace
import getpass
from http.client import HTTPConnection, HTTPException
import json
import re
import sqlite3
import time

from telethon import TelegramClient, errors, functions
from telethon.tl import types as tl_types

from settings import (
    SESSION_PATH, load_settings, save_local_settings, save_telegram_settings, telegram_proxy_options,
)
from proxy_probe import describe, detect, direct_works


def save_proxy_choice(settings, proxy: str):
    """Persist a proxy choice, but don't let a Windows file lock abort login."""
    try:
        save_local_settings({"TELEGRAM_PROXY_URL": proxy})
        return load_settings()
    except PermissionError:
        print("无法更新 .env（文件可能被编辑器或安全软件占用）。")
        print("本次登录会临时使用刚才选择的网络方式；登录后请在网页设置里保存代理。")
        return replace(settings, telegram_proxy_url=proxy)


def stop_local_app_before_login() -> bool:
    """Avoid deleting a Telethon session while the local search service has it open."""
    connection = HTTPConnection("127.0.0.1", 8787, timeout=1)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        data = json.loads(response.read(512))
        if response.status != 200 or data.get("app") != "telegram-search-local":
            return True
    except (HTTPException, OSError, ValueError, json.JSONDecodeError):
        return True
    finally:
        connection.close()

    print("\n检测到极目搜索服务仍在运行，它会占用 Telegram 会话文件。")
    answer = input("先自动退出极目，再继续登录？[Y/n]: ").strip().lower()
    if answer not in {"", "y", "yes", "是", "好", "对"}:
        print("已取消登录。请先退出极目，再重新运行「1-登录.cmd」。")
        return False

    connection = HTTPConnection("127.0.0.1", 8787, timeout=3)
    try:
        connection.request("POST", "/api/shutdown", body=b"{}", headers={"Content-Type": "application/json"})
        connection.getresponse().read()
    except (HTTPException, OSError):
        print("无法自动退出极目。请从网页「设置 → 退出极目」，再重新运行登录脚本。")
        return False
    finally:
        connection.close()

    for _ in range(30):
        probe = HTTPConnection("127.0.0.1", 8787, timeout=0.3)
        try:
            probe.request("GET", "/api/health")
            probe.getresponse().read()
        except (HTTPException, OSError):
            print("极目已退出，可以继续登录。\n")
            return True
        finally:
            probe.close()
        time.sleep(0.2)
    print("极目还在关闭，请稍等几秒后重新运行登录脚本。")
    return False


def normalize_phone(value: str) -> str:
    """Strip spaces, dashes and brackets people paste from a contact card."""
    cleaned = re.sub(r"[\s\-()（）.]", "", value)
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith("+") else "+" + cleaned.lstrip("+")


def ask_credentials(settings):
    """Collect app credentials on first run, so no file needs editing."""
    if settings.telegram_configured:
        return settings
    print("首次使用需要 Telegram 应用凭据：https://my.telegram.org/apps")
    print("登录后点「API development tools」，把显示的 App api_id 和 api_hash 填到下面。\n")
    while True:
        api_id = input("App api_id（纯数字）: ").strip()
        api_hash = input("App api_hash（32 位字符）: ").strip()
        if api_id.isdigit() and len(api_hash) == 32:
            break
        print("格式不对：api_id 应全是数字，api_hash 应是 32 位字符。再试一次。\n")
    save_telegram_settings(api_id, api_hash)
    print()
    return load_settings()


def ask_proxy(settings):
    if settings.telegram_proxy_url:
        print(f"当前已配置本地代理：{settings.telegram_proxy_url}")
        print("如果你开启的是 Clash / FlClash 全局/TUN 模式，清空这里，避免双重代理。")
        print("注意：只有 Windows「系统代理」开关不一定代理 Telegram；g 适用于能接管所有流量的 TUN 模式。")
        choice = input("回车=保留本地代理，g=使用 TUN 全局模式，m=手动修改: ").strip().lower()
        if choice in {"g", "global", "全局"}:
            settings = save_proxy_choice(settings, "")
            print("已清空应用内代理，将使用系统全局网络。")
            return settings
        if choice not in {"m", "manual", "手动"}:
            return settings

    print("正在检查网络…")
    if direct_works():
        print("可以直连 Telegram，不需要代理。\n")
        return settings

    print("直连不通，正在自动检测本机代理端口（约 5 秒）…")
    found = detect()
    if found:
        print(f"检测到可用代理：{describe(found)}")
        print("若你已开启全局 / TUN 模式，请输入 g，避免让应用再套一层本地代理。")
        answer = input("回车=使用检测到的本地代理，g=使用 TUN 全局模式，n=手动填写 [Y/g/n]: ").strip().lower()
        if answer in {"g", "global", "全局"}:
            settings = save_proxy_choice(settings, "")
            print("已清空应用内代理，将使用系统全局网络。")
            return settings
        if answer in {"", "y", "yes", "是", "好", "对"}:
            settings = save_proxy_choice(settings, found[0])
            print(f"已保存 {found[0]}\n")
            return settings
    else:
        print("没有自动找到可用代理。")

    print("若使用全局 / TUN 模式（可接管其他软件流量），先确认它已开启，然后输入 g。")
    print("仅开启 Windows「系统代理」可能不够，Telegram 的连接不一定走 HTTP 系统代理。")
    print("若使用本地端口模式，打开你的机场/加速器（Clash、v2rayN、FlClash 等），")
    print("找「本地端口 / 混合端口」，常见 7890、7897、1080。")
    print("填成 socks5://127.0.0.1:端口；如果软件显示的是 http 代理，就填 http://127.0.0.1:端口。")
    while True:
        proxy = input("代理地址（g=使用系统全局代理，回车=不使用代理）: ").strip()
        if proxy.lower() in {"g", "global", "全局"}:
            settings = save_proxy_choice(settings, "")
            print("已清空应用内代理，将使用系统全局网络。")
            return settings
        if not proxy:
            return settings
        try:
            telegram_proxy_options(proxy)
        except ValueError:
            print("格式不对，应该带端口，例如 socks5://127.0.0.1:7897。再试一次。\n")
            continue
        return save_proxy_choice(settings, proxy)


def delivery_hint(sent) -> str:
    kind = type(getattr(sent, "type", None)).__name__
    if "App" in kind:
        return "验证码发到了你已登录的 Telegram 客户端里（找官方「Telegram」对话，不是短信）。"
    if "Call" in kind:
        return "验证码会通过电话朗读给你。"
    if "Sms" in kind or "Firebase" in kind:
        return "验证码已通过短信发送。"
    return "验证码已发送。"


def delivery_detail(sent) -> str:
    """Print what Telegram actually answered, so a delivery problem is visible."""
    kind = type(getattr(sent, "type", None)).__name__
    nxt = getattr(sent, "next_type", None)
    return (f"  [Telegram 返回] 通道={kind}  备选通道={type(nxt).__name__ if nxt else '无'}"
            f"  重发需等待={getattr(sent, 'timeout', '未给出')}秒"
            f"  验证码位数={getattr(sent, 'code_length', '未给出')}")


async def sign_in_with_prompts(client, phone: str) -> None:
    """Telethon's own prompts are English and give up after one wrong code."""
    try:
        sent = await client.send_code_request(phone)
    except errors.SendCodeUnavailableError:
        print("这个号现在发不出验证码：可能刚请求过，或该账号需要先在已登录设备上确认。")
        print("等 1–2 分钟再试；如果这个号是买来的，直接把已登录的会话复制过来更快。")
        raise
    print(delivery_hint(sent))
    print(delivery_detail(sent))
    print("验证码是纯数字，通常 5 位；输入 sms 可以再试短信通道；输入 quit 取消。\n")

    for attempt in range(5):
        code = input("Telegram 验证码: ").strip()
        if not code:
            continue
        if code.lower() in {"quit", "exit", "q", "退出"}:
            raise SystemExit(1)
        if code.lower() in {"sms", "短信", "call", "电话"}:
            # force_sms was removed from the API; ResendCode lets Telegram pick the channel.
            try:
                sent = await client(functions.auth.ResendCodeRequest(
                    phone_number=phone, phone_code_hash=sent.phone_code_hash,
                ))
                print(delivery_hint(sent))
                print(delivery_detail(sent))
            except errors.SendCodeUnavailableError:
                print("暂时不能重发验证码（可能刚发过，或这种方式不可用）。")
                print("请等 1–2 分钟，或直接去已登录的 Telegram 客户端里看。\n")
            except errors.FloodWaitError as error:
                print(f"请求太频繁，请等 {error.seconds} 秒。\n")
            continue
        try:
            await client.sign_in(phone=phone, code=code)
            return
        except errors.SessionPasswordNeededError:
            print("\n这个账号开启了两步验证，需要输入密码（不是验证码）。")
            password = getpass.getpass("两步验证密码: ")
            await client.sign_in(password=password)
            return
        except errors.PhoneNumberUnoccupiedError:
            print("\n这个手机号还没有注册 Telegram，需要先创建账号。")
            first = input("名字（First name，随便填）: ").strip() or "User"
            await client.sign_up(phone=phone, code=code, first_name=first)
            return
        except errors.PhoneNumberBannedError:
            print("这个号码已被 Telegram 封禁，换一个号。")
            raise
        except errors.PhoneCodeInvalidError:
            print(f"验证码不对（第 {attempt + 1} 次）。注意只要数字，不要带空格或字母。\n")
        except errors.PhoneCodeExpiredError:
            print("验证码已过期，正在重新发送…\n")
            sent = await client.send_code_request(phone)
            print(delivery_hint(sent))
        except errors.PhoneNumberInvalidError:
            print("手机号格式不对。要带国际区号，例如 +8613800138000。")
            raise
        except errors.FloodWaitError as error:
            print(f"请求太频繁，请等 {error.seconds} 秒后再运行本程序。")
            raise
    print("连续多次验证码不正确，请重新运行本程序。")
    raise SystemExit(1)


async def authorize() -> int:
    settings = ask_credentials(load_settings())
    if not stop_local_app_before_login():
        return 1
    settings = ask_proxy(settings)

    try:
        proxy = telegram_proxy_options(settings.telegram_proxy_url)
    except ValueError:
        print("TELEGRAM_PROXY_URL 格式有误，请填写带端口号的 SOCKS5 或 HTTP 地址。")
        return 1

    SESSION_PATH.parent.mkdir(exist_ok=True)
    session_file = SESSION_PATH.with_suffix(".session")

    def make_client() -> TelegramClient:
        return TelegramClient(
            str(SESSION_PATH), settings.api_id, settings.api_hash,
            proxy=proxy, timeout=8, connection_retries=1,
        )

    client = make_client()
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("\n这个会话已经是登录状态，可以直接双击「2-启动搜索.cmd」。")
            return 0

        # A session file left behind after another device terminated it still holds a dead
        # key, and reusing it stops Telegram from delivering anything. Any unauthorized
        # file is discarded so the login always starts from a pristine session.
        await client.disconnect()
        try:
            session_file.unlink(missing_ok=True)
        except PermissionError:
            print("无法重建 Telegram 会话文件：文件仍被另一个进程占用。")
            print("确认极目服务已经退出；如果任务管理器里还有 pythonw.exe，请先结束它，再重试。")
            return 1
        client = make_client()
        await client.connect()

        print("\n请在这个窗口完成 Telegram 登录。")
        print("如果这个号是买来的、收不到验证码：不用登录，直接把已经登录好的")
        print("  .telegram-data 文件夹和 .env 从原电脑复制过来即可。（按 Ctrl+C 退出）\n")
        while True:
            raw = input("手机号（含国际区号，例如 +8613800138000）: ").strip()
            phone = normalize_phone(raw)
            if len(phone) >= 8 and phone[1:].isdigit():
                break
            print("号码格式不对：要以 + 加国家码开头，只包含数字，例如 +8613800138000。\n")
        if phone != raw:
            print(f"已整理为 {phone}\n")

        await sign_in_with_prompts(client, phone)
        if not await client.is_user_authorized():
            print("登录尚未完成。")
            return 1
        print("\nTelegram 登录完成。现在双击「2-启动搜索.cmd」打开搜索页面。")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(authorize()))
    except KeyboardInterrupt:
        print("已取消登录。")
        raise SystemExit(1)
    except sqlite3.OperationalError:
        print("Telegram 会话文件无法打开。请先关闭正在运行的搜索程序再重试。")
        raise SystemExit(1)
    except PermissionError:
        print("本地文件无法访问（可能仍被其他程序占用或没有写权限）。")
        print("先退出所有「极目」窗口、关闭正在编辑 .env 的程序，再重试。")
        raise SystemExit(1)
    except (errors.RPCError, OSError, TimeoutError, EOFError) as error:
        print(f"Telegram 登录未完成（{type(error).__name__}）。")
        print("多半是网络问题：确认代理可用，或在「设置 › 网络代理」里填好代理后重试。")
        raise SystemExit(1)
