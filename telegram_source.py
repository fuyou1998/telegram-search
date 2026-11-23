from __future__ import annotations

import asyncio
import base64
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from telethon import TelegramClient, errors, events, functions, types
from telethon.tl.functions.channels import GetFullChannelRequest

from search_quality import channel_scripts, order_queries, scripts_of


HitCallback = Callable[[dict], None]
SourceCallback = Callable[[str, str, str], None]

_HANDLE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{2,31}\Z")
_INVITE = re.compile(r"[a-zA-Z0-9_-]{4,128}\Z")
_TELEGRAM_LINK = re.compile(
    r"(?<![\w.])(?:https?://)?(?:t\.me|telegram\.me)/[^\s<>\u3000【】\[\]{}()。！，、；：？'\"]+",
    flags=re.IGNORECASE,
)
_RESERVED = {
    "addemoji", "addlist", "addstickers", "apps", "auth", "bg", "boost", "c", "confirmphone",
    "contact", "giftcode", "invoice", "iv", "joinchat", "login", "m", "me", "nft", "oauth",
    "premium_multigift", "proxy", "s", "setlanguage", "share", "socks", "topup",
}
_ADVERTISEMENT = re.compile(
    r"(?:[【\[(（]\s*(?:广告|推广|赞助|ad|sponsored)\s*[】\])）])"
    r"|^\s*(?:广告合作|广告投放|广告位招租|联系广告|赞助商|sponsored by)\b",
    re.IGNORECASE,
)
_COUNT = re.compile(r"([\d,.]+)\s*([KMB万亿]?)\s*(?:位?成员|订阅(?:者|人数)?|members?|subscribers?)", re.I)
_NEXT = re.compile(r"(?:下[一1]页|下一页\s*[›»>→➡️]+|next(?:\s+page)?(?:\s*[›»>→➡️]+)?|[›»>→➡️]+)\Z", re.I)
_EMPTY = re.compile(r"没有(?:找到|搜索到)|未找到|暂无(?:相关|结果)|no (?:matching |search )?results", re.I)
_JOIN_REQUIRED = re.compile(r"(?:加入|关注|订阅).{0,20}(?:后|才).{0,12}(?:搜索|使用|查看)|join .{0,30}(?:before|to (?:search|continue))", re.I)
_BOT_LIMIT = re.compile(r"人机验证|真人验证|(?:请|完成|输入).{0,8}验证码|captcha|verify (?:that )?you are (?:a )?human", re.I)
# @jisou answers a query it cannot match with a fixed paid-promo block instead of an empty
# reply. Two or more of these markers means the keyword found nothing, not that these are hits.
_BOT_PROMO = re.compile(r"账号批发|号商|白号|包上号|一手号|批发出售|批发国内账号|成品号|实名号", re.I)
_PROMO_MARKERS = 2
_BOT_VARIANTS = 4
_CALLBACK_BUTTONS = tuple(
    button for name in ("KeyboardButtonCallback", "InlineButtonTypeCallback") if (button := getattr(types, name, None))
)
_URL_BUTTONS = tuple(
    button for name in ("KeyboardButtonUrl", "InlineButtonTypeUrl") if (button := getattr(types, name, None))
)


def _short_query(query: str, words: int = 2) -> str:
    """Official search is AND: every extra word removes results. Two words scores best."""
    parts = query.split()
    return " ".join(parts[:words]) if len(parts) > words else query


def _is_promo_block(text: str) -> bool:
    """@jisou answers an unmatched keyword with a paid-promo block instead of an empty reply."""
    return len(_BOT_PROMO.findall(text)) >= _PROMO_MARKERS and not _TELEGRAM_LINK.search(text)


def normalize_target(value: str) -> str | None:
    """Return a canonical Telegram peer or invite link without opening it."""
    text = value.strip().rstrip(".,;，。；！!、")
    if text.startswith("@"):
        text = f"https://t.me/{text[1:]}"
    elif text.lower().startswith(("t.me/", "telegram.me/")):
        text = f"https://{text}"

    try:
        parts = urlsplit(text)
        query = parse_qs(parts.query, keep_blank_values=True)
        if parts.scheme == "tg" and parts.netloc == "resolve":
            if set(query) - {"domain", "post", "thread", "comment"}:
                return None
            return normalize_target(f"https://t.me/{query.get('domain', [''])[0]}")
        if parts.scheme == "tg" and parts.netloc == "join":
            return normalize_target(f"https://t.me/+{query.get('invite', [''])[0]}")
        if parts.scheme not in {"http", "https"} or parts.netloc.lower() not in {"t.me", "telegram.me"}:
            return None
        if set(query) - {"single", "thread", "comment", "embed", "before", "after"}:
            return None
    except ValueError:
        return None

    path = [unquote(part) for part in parts.path.strip("/").split("/")]
    name = path[0]
    if name.lower() == "joinchat" and len(path) == 2:
        name = f"+{path[1]}"
        path = [name]
    if name.startswith("+"):
        return f"https://t.me/{name}" if len(path) == 1 and _INVITE.fullmatch(name[1:]) else None
    if name.lower() == "s" and len(path) >= 2:
        path = path[1:]
        name = path[0]
    if not _HANDLE.fullmatch(name) or name.lower() in _RESERVED:
        return None
    if len(path) > 2 or (len(path) == 2 and not path[1].isdecimal()):
        return None
    return f"https://t.me/{name.lower()}"


def _kind(text: str) -> str:
    if re.search(r"频道|订阅者|\bchannel\b|\bsubscribers?\b", text, re.I):
        return "channel"
    if re.search(r"群组|群聊|交流群|讨论群|\bgroup\b", text, re.I):
        return "group"
    return "unknown"


def _label(text: str, fallback: str) -> str:
    label = _TELEGRAM_LINK.sub("", text)
    label = _COUNT.sub("", label)
    label = re.sub(r"^\s*\d+[.、)）\s]+", "", label).strip(" \r\n\t|–—-·:：")
    return (label or fallback)[:120]


def _members(text: str) -> int | None:
    match = _COUNT.search(text)
    if not match:
        return None
    number, unit = match.groups()
    mult = {"": 1, "K": 1000, "M": 1000000, "B": 1000000000, "万": 10000, "亿": 100000000}
    try:
        return int(float(number.replace(",", "")) * mult[unit.upper()])
    except (ValueError, OverflowError):
        return None


def _buttons(message):
    for row in getattr(getattr(message, "reply_markup", None), "rows", ()):
        yield from row.buttons


def _next_button(message):
    for button in _buttons(message):
        action = getattr(button, "type", button)
        if (isinstance(action, _CALLBACK_BUTTONS) and action.data
                and not getattr(action, "requires_password", False) and _NEXT.fullmatch(button.text.strip())):
            return action
    return None


def _bot_hits(message, source: str, query: str, ignored: set[str]) -> list[dict]:
    text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
    hits: dict[str, dict] = {}

    def add(raw_url: str, label: str, context: str):
        url = normalize_target(raw_url)
        if not url or url in hits or _ADVERTISEMENT.search(label) or _ADVERTISEMENT.search(context):
            return
        name = url.rsplit("/", 1)[-1]
        username = None if name.startswith("+") else name
        if username and (username in ignored or username.endswith("bot")):
            return
        if re.search(r"(?:^|[【\[(（])\s*(?:机器人|bot)\s*(?:$|[】\])）])", context, re.I):
            return
        display = f"@{username}" if username else "Telegram 邀请链接"
        hits[url] = {
            "title": _label(label, display),
            "url": url,
            "kind": _kind(context),
            "username": username,
            "members": _members(context),
            "description": "",
            "source": source,
            "query": query,
        }

    for entity, label in message.get_entities_text():
        if isinstance(entity, types.MessageEntityTextUrl):
            raw_url = entity.url
        elif isinstance(entity, types.MessageEntityUrl):
            raw_url = label
        else:
            continue
        context = next((line for line in text.splitlines() if label in line), label)
        add(raw_url, context if label == raw_url else label, context)

    for line in text.splitlines():
        for match in _TELEGRAM_LINK.finditer(line):
            add(match.group(), line, line)

    for button in _buttons(message):
        action = getattr(button, "type", button)
        if isinstance(action, _URL_BUTTONS) and not _NEXT.fullmatch(button.text.strip()):
            add(action.url, button.text, button.text)

    return list(hits.values())[:30]


def _channel_hit(chat, source: str, query: str) -> dict | None:
    if not isinstance(chat, types.Channel):
        return None
    username = getattr(chat, "username", None)
    if not username:
        username = next((item.username for item in getattr(chat, "usernames", None) or () if item.active), None)
    url = normalize_target(f"@{username}") if username else None
    if not url:
        return None
    result = {
        "title": chat.title,
        "url": url,
        "username": username,
        "kind": "group" if chat.megagroup or getattr(chat, "gigagroup", False) else "channel",
        "members": getattr(chat, "participants_count", None),
        "description": "",
        "source": source,
        "query": query,
    }
    if not getattr(chat, "min", False):
        result["joined"] = not bool(chat.left)
    return result


class TelegramSource:
    def __init__(self, api_id: int, api_hash: str, session_path: Path,
                 bots: tuple[str, ...] = ("@jisou",), *, proxy: dict | None = None,
                 official: bool = True, bot_variants: int = _BOT_VARIANTS,
                 max_variants: int = 14, enrich: bool = True):
        self.api_id = api_id
        self.api_hash = api_hash
        self.proxy = proxy
        self.official = official
        self.bot_variants = max(1, min(8, bot_variants))
        self.max_variants = max(4, min(20, max_variants))
        self.enrich = enrich
        self.session_path = Path(session_path)
        if not self.session_path.name.endswith(".session"):
            self.session_path = self.session_path.with_name(f"{self.session_path.name}.session")
        self.bots = tuple(dict.fromkeys(
            f"@{bot.removeprefix('@').lower()}" for bot in bots if _HANDLE.fullmatch(bot.removeprefix("@"))
        ))[:3]
        self._client: TelegramClient | None = None
        self._me: types.User | None = None
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._cooldowns: dict[str, float] = {}
        self._no_pagination: set[str] = set()

    async def _discard(self) -> None:
        """Drop the client so the next attempt builds a fresh connection."""
        client, self._client, self._me = self._client, None, None
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), 3)
        except BaseException:
            # A half-open connection can block; the object is already dropped.
            pass

    async def _connect(self) -> TelegramClient | None:
        if not self.api_id or not self.api_hash or not self.session_path.is_file():
            return None
        async with self._connect_lock:
            if self._client is not None and self._client.is_connected() and self._me is not None:
                return self._client
            if self._client is not None:
                # Never reuse a client that lost its connection: its sender can hold a dead
                # socket and hang every later request until the caller times out.
                with suppress(BaseException):
                    await self._discard()
            try:
                self._client = TelegramClient(
                    str(self.session_path), self.api_id, self.api_hash,
                    proxy=self.proxy, timeout=9, connection_retries=1, request_retries=1,
                    flood_sleep_threshold=0,
                )
                await asyncio.wait_for(self._client.connect(), 9)
                self._me = await asyncio.wait_for(self._client.get_me(), 6)
                if self._me is None:
                    with suppress(BaseException):
                        await self._discard()
                    return None
                return self._client
            except BaseException:
                # Also covers cancellation from the caller's timeout, which would otherwise
                # leave a client half-connected forever.
                with suppress(BaseException):
                    await self._discard()
                raise

    async def status(self) -> dict:
        if not self.api_id or not self.api_hash:
            return {"connected": False, "message": "请配置 Telegram API ID 和 API Hash。"}
        if not self.session_path.is_file():
            return {"connected": False, "message": "请先在本机完成 Telegram 登录。"}
        try:
            if await self._connect():
                return {"connected": True, "message": "Telegram 已连接。"}
        except (OSError, TimeoutError, sqlite3.Error, errors.RPCError):
            return {"connected": False, "message": "Telegram 连接失败，请检查网络和登录会话。"}
        return {"connected": False, "message": "Telegram 会话未登录或已失效。"}

    async def close(self) -> None:
        async with self._connect_lock:
            with suppress(BaseException):
                await self._discard()

    async def baseline(self, query: str, limit: int = 20) -> dict:
        """What a manual search returns: one keyword, Telegram's own order."""
        async with self._operation_lock:
            try:
                client = await self._connect()
            except (OSError, TimeoutError, sqlite3.Error, errors.RPCError):
                client = None
            if client is None:
                return {"state": "error", "message": "Telegram 未连接。", "results": []}
            try:
                response = await client(functions.contacts.SearchRequest(q=query, limit=limit))
            except errors.FloodWaitError as error:
                return {"state": "limited", "message": f"限流，约 {error.seconds} 秒。", "results": []}
            except (errors.RPCError, OSError, TimeoutError, ValueError):
                return {"state": "error", "message": "基线检索失败。", "results": []}
            hits = [hit for chat in response.chats if (hit := _channel_hit(chat, "baseline", query))]
            return {"state": "done", "message": "", "results": hits}

    async def review_channel(self, url: str, sample_size: int = 30) -> dict:
        """Inspect a public channel/group and return factual metadata plus recent posts."""
        canonical = normalize_target(url)
        if not canonical or canonical.rsplit("/", 1)[-1].startswith("+"):
            return {"state": "error", "message": "请提供公开频道或公开群组链接。"}
        sample_size = max(5, min(50, int(sample_size)))
        async with self._operation_lock:
            try:
                client = await self._connect()
                if client is None:
                    return {"state": "error", "message": "请先连接 Telegram。"}
                username = canonical.rsplit("/", 1)[-1]
                entity = await client.get_entity(f"@{username}")
                if not isinstance(entity, types.Channel):
                    return {"state": "error", "message": "这个链接不是公开群组或频道。"}

                about = ""
                members = getattr(entity, "participants_count", None)
                try:
                    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
                    about = full.full_chat.about or ""
                    members = full.full_chat.participants_count or members
                except (errors.RPCError, OSError, TimeoutError):
                    pass

                posts_raw = [
                    message async for message in client.iter_messages(entity, limit=sample_size)
                    if message.raw_text
                ]
                posts = []
                for message in posts_raw:
                    post_date = message.date.isoformat() if message.date else ""
                    posts.append({
                        "id": message.id,
                        "date": post_date,
                        "text": " ".join((message.raw_text or "").split())[:900],
                        "url": f"{canonical}/{message.id}",
                        "views": getattr(message, "views", None),
                        "forwards": getattr(message, "forwards", None),
                    })
                dates = [message.date for message in posts_raw if message.date]
                views = [message.views for message in posts_raw if isinstance(message.views, int)]

                avatar_path = ""
                avatar_base64 = ""
                asset_dir = self.session_path.parent / "drafts" / "assets"
                try:
                    asset_dir.mkdir(parents=True, exist_ok=True)
                    avatar_file = asset_dir / f"{username}.jpg"
                    downloaded = await client.download_profile_photo(
                        entity, file=str(avatar_file), download_big=True,
                    )
                    if downloaded:
                        avatar_path = f"assets/{avatar_file.name}"
                        avatar_base64 = base64.b64encode(avatar_file.read_bytes()).decode("ascii")
                except (errors.RPCError, OSError, TimeoutError):
                    pass

                return {
                    "state": "done",
                    "channel": {
                        "title": entity.title,
                        "url": canonical,
                        "username": getattr(entity, "username", None) or username,
                        "kind": "group" if entity.megagroup else "channel",
                        "members": members,
                        "about": " ".join(about.split())[:1200],
                        "verified": bool(getattr(entity, "verified", False)),
                        "scam": bool(getattr(entity, "scam", False)),
                        "fake": bool(getattr(entity, "fake", False)),
                        "sampled_posts": len(posts),
                        "latest_post_at": dates[0].isoformat() if dates else "",
                        "oldest_sample_at": dates[-1].isoformat() if dates else "",
                        "active_days": len({value.date().isoformat() for value in dates}),
                        "average_views": round(sum(views) / len(views)) if views else None,
                        "avatar_path": avatar_path,
                        "avatar_base64": avatar_base64,
                    },
                    "posts": posts,
                }
            except errors.FloodWaitError as error:
                return {"state": "limited", "message": f"Telegram 限流，约 {error.seconds} 秒后重试。"}
            except errors.ChannelPrivateError:
                return {"state": "requires_join", "message": "当前账号无权读取此群组或频道。"}
            except (errors.RPCError, OSError, TimeoutError, ValueError):
                return {"state": "error", "message": "无法读取该频道；请检查链接、网络和访问权限。"}

    async def discover(self, variants: list[str], on_candidate: HitCallback, on_source: SourceCallback) -> None:
        variants = list(dict.fromkeys(
            " ".join(query.split())[:160] for query in variants if query.strip()
        ))[:self.max_variants]
        if not variants:
            return
        async with self._operation_lock:
            sources = (("telegram",) if self.official else ()) + self.bots
            for source in sources:
                on_source(source, "waiting", "等待检索")
            try:
                client = await self._connect()
            except (OSError, TimeoutError, sqlite3.Error, errors.RPCError):
                client = None
            if client is None:
                for source in sources:
                    on_source(source, "error", "Telegram 未连接，请检查网络和登录状态。")
                return
            collected: dict[str, dict] = {}

            def collect(hit: dict) -> None:
                collected.setdefault(hit.get("url") or hit.get("title") or "", hit)
                on_candidate(hit)

            calls = [
                *(self._run_source("telegram", self._official, client, variants, collect, on_source)
                  for _ in (1,) if self.official),
                *(self._run_source(bot, self._bot, client, variants, collect, on_source) for bot in self.bots),
            ]
            if not calls:
                return
            await asyncio.gather(*calls)
            if self.enrich:
                with suppress(TimeoutError, OSError, errors.RPCError):
                    await asyncio.wait_for(self._enrich(client, collected.values()), 8)

    async def _enrich(self, client, hits, limit: int = 8) -> None:
        """Fill in member counts and descriptions the search sources leave blank."""
        targets: dict[str, list[dict]] = {}
        for hit in hits:
            url = hit.get("url") or ""
            name = url.rsplit("/", 1)[-1]
            if not name or name.startswith("+") or (hit.get("members") and hit.get("description")):
                continue
            targets.setdefault(name, []).append(hit)
        if not targets:
            return
        gate = asyncio.Semaphore(6)

        async def fill(name: str) -> None:
            async with gate:
                try:
                    entity = await client.get_entity(f"@{name}")
                    if not isinstance(entity, types.Channel):
                        return
                    full = await client(GetFullChannelRequest(channel=entity))
                except (errors.RPCError, OSError, TimeoutError, ValueError):
                    return
                info = full.full_chat
                for hit in targets[name]:
                    if info.participants_count:
                        hit["members"] = info.participants_count
                    if info.about and not hit.get("description"):
                        hit["description"] = " ".join(info.about.split())[:400]

        await asyncio.gather(*(fill(name) for name in list(targets)[:limit]), return_exceptions=True)

    async def _run_source(self, source, search, client, variants, on_candidate, on_source):
        wait = int(self._cooldowns.get(source, 0) - time.monotonic() + 0.99)
        if wait > 0:
            on_source(source, "limited", f"Telegram 限流，约 {wait} 秒后可重试。")
            return
        on_source(source, "running", "正在检索")
        try:
            state, message = await search(client, source, variants, on_candidate)
        except errors.FloodWaitError as error:
            self._cooldowns[source] = time.monotonic() + error.seconds
            state, message = "limited", f"Telegram 限流，约 {error.seconds} 秒后可重试。"
        except (OSError, TimeoutError):
            state, message = "limited", "请求超时，已保留返回的结果。"
        except (errors.RPCError, ValueError) as error:
            if isinstance(error, errors.UnauthorizedError):
                self._me = None
            state, message = "error", "Telegram 拒绝了此来源的请求，已保留返回的结果。"
        on_source(source, state, message)

    async def _official(self, client, source, variants, on_candidate):
        emitted = 0
        searched = 0
        wanted_scripts = {script for query in variants for script in scripts_of(query)}
        seen_scripts: set[str] = set()

        def emit(chats, query):
            nonlocal emitted
            for chat in chats:
                hit = _channel_hit(chat, source, query)
                if hit:
                    emitted += 1
                    on_candidate(hit)

        for index, query in enumerate(variants):
            # One query already returns up to 18 chats; once the list is clearly populated,
            # the rest add little. Still run at least one query per writing system so the
            # other languages are not lost.
            if index >= 5 and emitted >= 24 and wanted_scripts <= seen_scripts:
                break
            searched += 1
            seen_scripts |= scripts_of(query)
            try:
                response = await client(functions.contacts.SearchRequest(q=_short_query(query), limit=18))
                emit(response.chats, query)
            except (errors.QueryTooShortError, errors.SearchQueryEmptyError):
                pass
            if index < 6:
                # Normal global search is separate from the paid public Posts action.
                try:
                    response = await client(functions.messages.SearchGlobalRequest(
                        q=query, filter=types.InputMessagesFilterEmpty(), min_date=None, max_date=None,
                        offset_rate=0, offset_peer=types.InputPeerEmpty(), offset_id=0, limit=18,
                    ))
                except errors.SearchQueryEmptyError:
                    continue
                matched = {
                    msg.peer_id.channel_id for msg in response.messages
                    if isinstance(getattr(msg, "peer_id", None), types.PeerChannel)
                }
                emit((chat for chat in response.chats if chat.id in matched), query)

        check = getattr(functions.channels, "CheckSearchPostsFloodRequest", None)
        search = getattr(functions.channels, "SearchPostsRequest", None)
        if check is not None and search is not None and getattr(self._me, "premium", False):
            query = variants[0]
            try:
                availability = await client(check(query=query))
                if availability.query_is_free or availability.remains > 0:
                    response = await client(search(
                        query=query, offset_rate=0, offset_peer=types.InputPeerEmpty(), offset_id=0, limit=20,
                    ))
                    emit(response.chats, query)
            except errors.FloodWaitError:
                raise
            except errors.RPCError:
                # Optional public discovery never requires a purchase or stops normal search.
                pass
        return "done", (
            f"已检索 {searched} 个关键词变体。"
            if searched >= len(variants)
            else f"已检索 {searched} 个关键词变体，结果足够，跳过其余。"
        )

    async def _bot(self, client, source, variants, on_candidate):
        bot = await client.get_entity(source)
        if not isinstance(bot, types.User) or not bot.bot:
            return "error", "该来源不是可用的 Telegram 机器人。"
        if getattr(bot, "send_paid_messages_stars", 0):
            return "limited", "此机器人要求付费发送，已跳过。"
        peer = await client.get_input_entity(bot)
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        ignored = {name.removeprefix("@") for name in self.bots}
        ignored.update({"jisou", "jisou0", "jisou1", "jisou2", "jisou3", "jisou1234"})

        async def receive(event):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event.message)

        client.add_event_handler(receive, events.NewMessage(chats=peer, incoming=True))
        client.add_event_handler(receive, events.MessageEdited(chats=peer, incoming=True))
        note = ""
        searched = 0
        promos = 0
        try:
            for query in variants[:self.bot_variants]:
                keyword = f"/search {_short_query(query)}" if source.startswith("@jisou") else (query.lstrip("/").strip() or query)
                if keyword.startswith("/") and not source.startswith("@jisou"):
                    continue
                sent = await client.send_message(peer, keyword, parse_mode=None)
                seen: set[tuple] = set()
                page, state = await self._bot_page(queue, sent.id, seen, source, query, ignored, on_candidate)
                if state == "promo":
                    promos += 1
                    await asyncio.sleep(0.35)
                    continue
                if state != "done":
                    return "limited", state
                searched += 1
                if page is not None and source not in self._no_pagination:
                    following = _next_button(page)
                    if following is not None:
                        try:
                            await client(functions.messages.GetBotCallbackAnswerRequest(
                                peer=peer, msg_id=page.id, data=following.data,
                            ))
                        except (errors.BotResponseTimeoutError, TimeoutError):
                            # Bots routinely skip callback queries; the extra page is optional,
                            # so keep what this keyword returned and try the remaining ones.
                            # Stop retrying: each unanswered callback costs a full timeout.
                            self._no_pagination.add(source)
                            note = "后续页码未响应，已保留已取得的结果。"
                            await asyncio.sleep(0.35)
                            continue
                        _, state = await self._bot_page(queue, sent.id, seen, source, query, ignored, on_candidate)
                        if state != "done":
                            return "limited", state
                await asyncio.sleep(0.35)
        finally:
            client.remove_event_handler(receive)
        message = f"已检索 {searched} 个关键词变体。"
        if promos:
            message += f"{promos} 个关键词机器人没有结果，只返回了推广位。"
        return "done", f"{note}{message}" if note else message

    async def _bot_page(self, queue, anchor, seen, source, query, ignored, on_candidate):
        deadline = time.monotonic() + 7
        page = None
        recognized = False
        while deadline > time.monotonic():
            try:
                message = await asyncio.wait_for(queue.get(), max(0, deadline - time.monotonic()))
            except TimeoutError:
                break
            reply_to = message.reply_to_msg_id
            if message.id <= anchor or (reply_to and reply_to != anchor and not any(item[0] == reply_to for item in seen)):
                continue
            signature = (message.id, message.raw_text, repr(message.entities), repr(message.reply_markup))
            if signature in seen:
                continue
            seen.add(signature)
            text = message.raw_text or ""
            if _BOT_LIMIT.search(text):
                return None, "机器人要求先完成验证，请在 Telegram 查看。"
            if _is_promo_block(text):
                # Paid promo block: this keyword found nothing, do not treat the ads as hits.
                return None, "promo"
            hits = _bot_hits(message, source, query, ignored)
            if _JOIN_REQUIRED.search(text) and not recognized:
                return None, "机器人要求先加入其他群或频道，已暂停此来源。"
            for hit in hits:
                on_candidate(hit)
            if hits or _EMPTY.search(text):
                recognized = True
                if hits and _next_button(message):
                    page = message
                deadline = min(deadline, time.monotonic() + 0.55)
        if not recognized:
            if seen:
                return None, "机器人未返回可识别的结果，已保留其他来源的结果。"
            return None, "机器人回复超时，已保留其他来源的结果。"
        return page, "done"

    async def _join_once(self, client, canonical: str) -> dict:
        name = canonical.rsplit("/", 1)[-1]
        if name.startswith("+"):
            invite_hash = name[1:]
            invite = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
            if isinstance(invite, types.ChatInviteAlready):
                return {"state": "already_joined", "message": "账号已加入。"}
            if getattr(invite, "subscription_pricing", None):
                return {"state": "error", "message": "此邀请需要付费，请在 Telegram 中查看。"}
            await client(functions.messages.ImportChatInviteRequest(hash=invite_hash))
        else:
            target = await client.get_entity(f"@{name}")
            if not isinstance(target, types.Channel):
                return {"state": "error", "message": "该链接不指向群组或频道。"}
            if not target.left:
                return {"state": "already_joined", "message": "账号已加入。"}
            await client(functions.channels.JoinChannelRequest(channel=target))
        return {"state": "joined", "message": "已加入。"}

    async def review_channel(self, url: str, sample_size: int = 30) -> dict:
        """Inspect a public channel and return verifiable facts plus recent public posts."""
        canonical = normalize_target(url)
        if not canonical or canonical.rsplit("/", 1)[-1].startswith("+"):
            return {"state": "error", "message": "请提供公开频道或公开群组链接。"}
        sample_size = max(5, min(50, int(sample_size)))

        async with self._operation_lock:
            try:
                client = await self._connect()
                if client is None:
                    return {"state": "error", "message": "请先连接 Telegram。"}
                username = canonical.rsplit("/", 1)[-1]
                entity = await client.get_entity(f"@{username}")
                if not isinstance(entity, types.Channel):
                    return {"state": "error", "message": "这个链接不是公开群组或频道。"}

                about = ""
                members = getattr(entity, "participants_count", None)
                try:
                    full = await client(GetFullChannelRequest(channel=entity))
                    about = full.full_chat.about or ""
                    members = full.full_chat.participants_count or members
                except (errors.RPCError, OSError, TimeoutError):
                    # Public channel metadata can still be useful when full info is unavailable.
                    pass

                messages = [
                    message async for message in client.iter_messages(entity, limit=sample_size)
                    if message.raw_text
                ]
                avatar_path = ""
                asset_dir = self.session_path.parent / "drafts" / "assets"
                asset_dir.mkdir(parents=True, exist_ok=True)
                avatar_file = asset_dir / f"{username}.jpg"
                try:
                    downloaded = await client.download_profile_photo(entity, file=str(avatar_file), download_big=True)
                    if downloaded:
                        avatar_path = f"assets/{avatar_file.name}"
                except (errors.RPCError, OSError, TimeoutError):
                    pass

                posts = []
                for message in messages:
                    post_text = " ".join((message.raw_text or "").split())[:900]
                    post_date = message.date.isoformat() if message.date else ""
                    post_url = f"{canonical}/{message.id}"
                    posts.append({
                        "id": message.id,
                        "date": post_date,
                        "text": post_text,
                        "url": post_url,
                        "views": getattr(message, "views", None),
                        "forwards": getattr(message, "forwards", None),
                    })

                dates = [message.date for message in messages if message.date]
                active_days = len({value.date().isoformat() for value in dates})
                view_values = [message.views for message in messages if isinstance(message.views, int)]
                return {
                    "state": "done",
                    "channel": {
                        "title": entity.title,
                        "url": canonical,
                        "username": getattr(entity, "username", None) or username,
                        "kind": "group" if entity.megagroup else "channel",
                        "members": members,
                        "about": " ".join(about.split())[:1200],
                        "verified": bool(getattr(entity, "verified", False)),
                        "scam": bool(getattr(entity, "scam", False)),
                        "fake": bool(getattr(entity, "fake", False)),
                        "sample_size": sample_size,
                        "sampled_posts": len(posts),
                        "latest_post_at": dates[0].isoformat() if dates else "",
                        "oldest_sample_at": dates[-1].isoformat() if dates else "",
                        "active_days": active_days,
                        "average_views": round(sum(view_values) / len(view_values)) if view_values else None,
                        "avatar_path": avatar_path,
                    },
                    "posts": posts,
                }
            except errors.FloodWaitError as error:
                return {"state": "limited", "message": f"Telegram 限流，约 {error.seconds} 秒后重试。"}
            except errors.ChannelPrivateError:
                return {"state": "requires_join", "message": "当前账号无权读取此群组或频道。"}
            except (errors.RPCError, OSError, TimeoutError, ValueError):
                return {"state": "error", "message": "无法读取该频道；请检查链接、网络和访问权限。"}

    async def join(self, url: str) -> dict:
        canonical = normalize_target(url)
        if not canonical:
            return {"state": "error", "message": "无效的 Telegram 群组或频道链接。"}
        async with self._operation_lock:
            wait = int(self._cooldowns.get("join", 0) - time.monotonic() + 0.99)
            if wait > 0:
                return {"state": "error", "message": f"Telegram 限流，约 {wait} 秒后可重试。"}
            try:
                client = await self._connect()
                if client is None:
                    return {"state": "error", "message": "请先连接 Telegram。"}
                for attempt in range(2):
                    try:
                        return await self._join_once(client, canonical)
                    except errors.FloodWaitError as error:
                        if attempt or error.seconds > 45:
                            raise
                        # A short cooldown is cheaper to wait out than to lose the batch.
                        await asyncio.sleep(error.seconds + 1)
                raise AssertionError("unreachable")
            except errors.InviteRequestSentError:
                return {"state": "pending", "message": "已提交申请，等待管理员批准。"}
            except errors.UserAlreadyParticipantError:
                return {"state": "already_joined", "message": "账号已加入。"}
            except errors.FloodWaitError as error:
                self._cooldowns["join"] = time.monotonic() + error.seconds
                return {"state": "error", "message": f"Telegram 限流，约 {error.seconds} 秒后可重试。"}
            except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
                return {"state": "error", "message": "邀请已失效。"}
            except (OSError, TimeoutError):
                return {"state": "error", "message": "请求超时，请在 Telegram 确认当前加入状态。"}
            except (errors.RPCError, sqlite3.Error, ValueError):
                return {"state": "error", "message": "Telegram 拒绝加入或此链接已失效。"}

    async def search_in(self, url: str, variants: list[str]) -> dict:
        found: dict[int, dict] = {}
        queries: list[str] = []

        def reply(state, message):
            # Most-hit messages first, then newest; the search box shows a short list.
            ordered = sorted(found.values(), key=lambda hit: hit["date"], reverse=True)
            ordered.sort(key=lambda hit: -hit.get("score", 0))
            results = [
                {"text": hit["text"], "url": hit["url"], "date": hit["date"]}
                for hit in ordered[:20]
            ]
            return {"state": state, "message": message, "results": results, "queries": queries}

        canonical = normalize_target(url)
        if not canonical:
            return reply("error", "无效的 Telegram 群组或频道链接。")
        queries = list(dict.fromkeys(" ".join(query.split())[:160] for query in variants if query.strip()))
        if not queries:
            return reply("error", "请输入用于查看群内消息的关键词。")

        async with self._operation_lock:
            wait = int(self._cooldowns.get("search_in", 0) - time.monotonic() + 0.99)
            if wait > 0:
                return reply("limited", f"Telegram 限流，约 {wait} 秒后可重试。")
            try:
                client = await self._connect()
                if client is None:
                    return reply("error", "请先连接 Telegram。")
                name = canonical.rsplit("/", 1)[-1]
                if name.startswith("+"):
                    invite = await client(functions.messages.CheckChatInviteRequest(hash=name[1:]))
                    if not isinstance(invite, (types.ChatInviteAlready, types.ChatInvitePeek)):
                        return reply("requires_join", "加入后可检索群内消息。")
                    chat = invite.chat
                else:
                    chat = await client.get_entity(f"@{name}")
                if not isinstance(chat, (types.Channel, types.Chat)):
                    return reply("error", "该链接不指向可读的群组或频道。")

                post_base = canonical if not name.startswith("+") else None
                if post_base is None and isinstance(chat, types.Channel):
                    public = _channel_hit(chat, "telegram", queries[0])
                    post_base = public["url"] if public else f"https://t.me/c/{chat.id}"

                # Search the chat with its own wording plus English, not with every language
                # the keyword expander produced.
                scripts = channel_scripts(chat.title or "")
                about = ""
                if isinstance(chat, types.Channel):
                    try:
                        full = await client(functions.channels.GetFullChannelRequest(channel=chat))
                        about = full.full_chat.about or ""
                    except (errors.RPCError, OSError, TimeoutError):
                        pass
                try:
                    sample = [about]
                    sample.extend(
                        message.raw_text for message in await client.get_messages(chat, limit=25)
                        if message.raw_text
                    )
                    sample_scripts = channel_scripts(" ".join(sample))
                except (errors.RPCError, OSError, TimeoutError):
                    sample_scripts = None
                if sample_scripts:
                    # The title states what the chat is about even when it mostly reposts English.
                    scripts = (scripts or set()) | sample_scripts
                queries = order_queries(queries, scripts, 6, shortest_first=True)

                async def collect(query: str) -> list:
                    return [message async for message in client.iter_messages(chat, search=query, limit=6)]

                # The variants describe the same topic, so issue them together instead of
                # paying a round trip for each one.
                batches = await asyncio.gather(*(collect(query) for query in queries), return_exceptions=True)
                if all(isinstance(batch, BaseException) for batch in batches):
                    raise batches[0]
                for batch in batches:
                    if isinstance(batch, BaseException):
                        continue
                    for message in batch:
                        if not message.raw_text:
                            continue
                        hit = found.get(message.id)
                        if hit is not None:
                            hit["score"] += 1
                            continue
                        sentence = " ".join(message.raw_text.split())[:360]
                        if sentence:
                            found[message.id] = {
                                "text": sentence,
                                "url": f"{post_base}/{message.id}" if post_base else None,
                                "date": message.date.isoformat() if message.date else "",
                                "score": 1,
                            }
                message = f"已找到 {len(found)} 条相关消息。" if found else "没有找到与当前关键词匹配的消息。"
                return reply("done", message)
            except errors.FloodWaitError as error:
                self._cooldowns["search_in"] = time.monotonic() + error.seconds
                return reply("limited", f"Telegram 限流，约 {error.seconds} 秒后可重试。")
            except (errors.ChannelPrivateError, errors.ChatAdminRequiredError):
                return reply("requires_join", "Telegram 限制了对此群组或频道的消息访问。")
            except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
                return reply("error", "邀请已失效。")
            except (OSError, TimeoutError):
                return reply("limited", "请求超时，已保留返回的结果。")
            except (errors.RPCError, sqlite3.Error, ValueError):
                return reply("error", "Telegram 无法读取此群组或频道的消息。")
