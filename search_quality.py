"""Expand user queries and rank Telegram candidates without uploading results."""

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import httpx


MAX_VARIANTS = 14
# Ranking bonus for the source the operator trusts most on this workspace.
PREFERRED_SOURCE_BONUS = 2.2
CJK = "\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
PUBLIC_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,31}\Z")
INVITE_TOKEN = re.compile(r"[A-Za-z0-9_-]{6,128}\Z")
TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
ACTION_PATHS = {
    "addemoji", "addlist", "addstickers", "addtheme", "boost", "c", "confirmphone",
    "giftcode", "invoice", "iv", "joinchat", "login", "m", "nft", "proxy", "s",
    "setlanguage", "share", "socks", "stars", "voicechat",
}
ACTION_QUERY_KEYS = {"start", "startapp", "startattach", "startchannel", "startgroup", "game"}
GENERIC_TERMS = {
    "and", "channel", "channels", "community", "for", "group", "groups", "search",
    "telegram", "the", "tg", "telegram群", "电报", "频道", "群组", "聊天", "讨论", "社区",
    # Telegram is used at scale outside Chinese and English; drop the equivalent
    # standalone channel/group words so a translation never becomes a bare keyword.
    "канал", "каналы", "группа", "группы", "чат", "сообщество",
    "کانال", "کانال‌ها", "گروه", "گروه‌ها", "چت",
    "قناة", "قنوات", "مجموعة", "مجموعات", "دردشة", "شات",
    "kanal", "kanallar", "grup", "gruplar", "guruh", "guruhlar", "saluran", "komunitas", "obrolan",
    "canal", "canais", "grupo", "grupos", "comunidade", "chat",
    "chaîne", "chaînes", "groupe", "groupes", "communauté",
    "kanäle", "gruppe", "gruppen", "gemeinschaft",
    "canale", "canali", "gruppo", "gruppi", "comunità",
    "kênh", "nhóm", "cộng đồng", "trò chuyện",
    "चैनल", "चैनलों", "समूह", "समूहों", "ग्रुप",
    "チャンネル", "グループ", "チャット", "コミュニティ",
    "채널", "그룹", "채팅", "커뮤니티",
}
QUERY_PROMPT = """Generate search keywords for finding Telegram groups and channels about the user's search topic.
Return exactly one JSON object with a key "variants" and an array of up to twelve strings.
Alternate precise queries with useful broader queries. Include the original language and English when different.
Prefer the subject's actual names, aliases, accepted abbreviations, common spelling variants, or useful translations.
Telegram is used at scale in {languages}, so also translate the core topic words into up to {count} of those
languages that this subject genuinely has communities in, writing each in its own script. Keep product names, brands,
model numbers, entities and acronyms unchanged inside those translations. Skip a language when its wording would be
identical to English or would not help find communities. Preserve important exclusions. No URLs, commands, user
handles, or generic words on their own such as group, channel or chat. Use 2–60 characters per variant. Strongest
candidates first. Do not answer the user's query."""


def build_prompt(languages: tuple[str, ...] | list[str] | None, count: int = 5) -> str:
    listed = [name for name in (languages or ()) if name][:40]
    if not listed:
        return QUERY_PROMPT.format(languages="English", count=1)
    return QUERY_PROMPT.format(languages=", ".join(listed), count=max(1, min(10, count)))


def _clean_text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = "".join(char for char in value if char.isprintable() or char.isspace())
    return " ".join(value.split())[:limit].strip()


def _is_generic(value: str) -> bool:
    return value.casefold() in GENERIC_TERMS or len(value) < 2


def _unique_variants(values: list[str]) -> list[str]:
    variants = []
    seen = set()
    for raw in values:
        value = _clean_text(raw)
        key = value.casefold()
        if value and key not in seen:
            variants.append(value)
            seen.add(key)
    return variants[:MAX_VARIANTS]


def _fallback_variants(query: str) -> list[str]:
    words = re.sub(
        r"^(?:(?:请|帮我|我想)?(?:找一下|查一下|搜索|找|查|寻找)\s*|(?:please\s+)?(?:find|search for)\s+)",
        "", query, flags=re.IGNORECASE,
    )
    topic = re.sub(
        r"(?:\s+(?:telegram\s+)?(?:groups?|channels?)|\s*(?:电报|Telegram|TG)?(?:群组|频道|群))\s*$",
        "", words, flags=re.IGNORECASE,
    ).strip()
    segmented = re.sub(rf"([{CJK}])(?=[A-Za-z0-9])|([A-Za-z0-9])(?=[{CJK}])", r"\1\2 ", topic)
    compact_cjk = re.sub(rf"(?<=[{CJK}])\s+(?=[{CJK}])", "", topic)
    parts = [_clean_text(part) for part in re.split(r"[，,、；;|]+", topic)]
    parts = [part for part in parts if part and not _is_generic(part)]
    words_by_space = topic.split()
    shortened = []
    if len(words_by_space) >= 3:
        shortened.append(" ".join(words_by_space[:-1]))
        shortened.append(" ".join(words_by_space[:2]))
        shortened.append(" ".join(words_by_space[-2:]))
    if len(words_by_space) > 1 and re.fullmatch(rf"[{CJK}]{{4,}}", words_by_space[0]):
        shortened.append(words_by_space[0])
    # Add a compact versioned name when the query includes more detail after it.
    names = re.findall(
        r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_-]{1,24}\s?\d+(?:\.\d+)+(?![A-Za-z0-9_.])",
        topic,
    )
    return _unique_variants([query, words, topic, segmented, compact_cjk, *parts, *names, *shortened])


def _parse_variants(content: object) -> list[str]:
    if not isinstance(content, str) or len(content) > 12000:
        raise ValueError("Invalid keyword response")
    content = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.IGNORECASE | re.DOTALL)
    decoded = json.loads(fenced.group(1) if fenced else content)
    if not isinstance(decoded, dict) or not isinstance(decoded.get("variants"), list):
        raise ValueError("Expected a list of keywords")
    variants = [_clean_text(item, 120) for item in decoded["variants"][:16]]
    return [
        value for value in variants
        if value and not _is_generic(value) and not value.startswith(("/", "@"))
        and not re.search(r"https?://|(?:^|\s)(?:t\.me|telegram\.me)/", value, re.IGNORECASE)
    ]


async def expand_query(
    query: str, *, api_key: str, base_url: str, model: str, proxy_url: str = "",
    reasoning_effort: str = "", languages: tuple[str, ...] | list[str] | None = None,
    language_count: int = 5,
) -> tuple[list[str], bool, str]:
    original = _clean_text(query)
    fallback = _fallback_variants(original)
    if not api_key or not base_url or not model:
        return fallback, False, "AI 未配置，已使用本地关键词拆分。"

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_prompt(languages, language_count)},
            {"role": "user", "content": original},
        ],
        "stream": False,
    }
    if reasoning_effort:
        # One-shot task: extra reasoning only adds latency before the search starts.
        body["reasoning_effort"] = reasoning_effort
    try:
        async with asyncio.timeout(25):
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, proxy=proxy_url or None) as client:
                response = await client.post(url, json=body, headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "telegram-search-local/1.0",
                    # OpenCode Go requires a per-conversation session header for routing.
                    "x-opencode-session": f"ses_{uuid4().hex}",
                })
                response.raise_for_status()
                ai = _parse_variants(response.json()["choices"][0]["message"]["content"])
        if ai and any(value.casefold() != original.casefold() for value in ai):
            return _unique_variants([original, *ai, *fallback]), True, "已生成关键词变体。"
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError, KeyError, IndexError, TypeError):
        return fallback, False, "AI 暂时不可用，已使用本地关键词拆分。"
    return fallback, False, "AI 未返回有效变体，已使用本地关键词拆分。"


def canonical_target(value: object) -> str | None:
    """Accept Telegram peer links and convert post links to their parent channel."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("@"):
        value = f"https://t.me/{value[1:]}"
    elif re.match(r"^(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", value, re.IGNORECASE):
        value = f"https://{value}"
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme not in {"https", "http"} or url.netloc.lower().removeprefix("www.") not in TELEGRAM_HOSTS:
        return None
    if any(key.lower() in ACTION_QUERY_KEYS for key, _ in parse_qsl(url.query, keep_blank_values=True)):
        return None

    parts = url.path.strip("/").split("/")
    if len(parts) == 1 and parts[0].startswith("+"):
        token = parts[0][1:]
        return f"https://t.me/+{token}" if INVITE_TOKEN.fullmatch(token) else None
    if len(parts) == 2 and parts[0].casefold() == "joinchat":
        return f"https://t.me/+{parts[1]}" if INVITE_TOKEN.fullmatch(parts[1]) else None
    if parts[0].casefold() == "s":
        parts = parts[1:]
    if not parts or len(parts) > 3 or any(not re.fullmatch(r"[0-9]+", part) for part in parts[1:]):
        return None
    username = parts[0].lower()
    if username in ACTION_PATHS or not PUBLIC_NAME.fullmatch(username):
        return None
    return f"https://t.me/{username}"


def _compact(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


SCRIPT_NAMES = {
    "cjk": "cjk", "kana": "kana", "hangul": "hangul", "cyrillic": "cyrillic",
    "arabic": "arabic", "hebrew": "hebrew", "thai": "thai",
    "devanagari": "devanagari", "bengali": "bengali", "myanmar": "myanmar",
    "latin": "latin",
}


def _script_of(char: str) -> str | None:
    """Name the writing system of one character; digits and punctuation return None."""
    code = ord(char)
    if 0x3040 <= code <= 0x30FF:
        return "kana"
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
        return "cjk"
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return "hangul"
    if 0x0400 <= code <= 0x04FF:
        return "cyrillic"
    if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0xFB50 <= code <= 0xFDFF:
        return "arabic"
    if 0x0590 <= code <= 0x05FF:
        return "hebrew"
    if 0x0700 <= code <= 0x074F:
        return "syriac"
    if 0x0780 <= code <= 0x07BF:
        return "thaana"
    if 0x0370 <= code <= 0x03FF or 0x1F00 <= code <= 0x1FFF:
        return "greek"
    if 0x0530 <= code <= 0x058F:
        return "armenian"
    if 0x10A0 <= code <= 0x10FF or 0x1C90 <= code <= 0x1CBF:
        return "georgian"
    if 0x0900 <= code <= 0x097F:
        return "devanagari"
    if 0x0980 <= code <= 0x09FF:
        return "bengali"
    if 0x0A00 <= code <= 0x0A7F:
        return "gurmukhi"
    if 0x0A80 <= code <= 0x0AFF:
        return "gujarati"
    if 0x0B00 <= code <= 0x0B7F:
        return "oriya"
    if 0x0B80 <= code <= 0x0BFF:
        return "tamil"
    if 0x0C00 <= code <= 0x0C7F:
        return "telugu"
    if 0x0C80 <= code <= 0x0CFF:
        return "kannada"
    if 0x0D00 <= code <= 0x0D7F:
        return "malayalam"
    if 0x0D80 <= code <= 0x0DFF:
        return "sinhala"
    if 0x0E00 <= code <= 0x0E7F:
        return "thai"
    if 0x0E80 <= code <= 0x0EFF:
        return "lao"
    if 0x0F00 <= code <= 0x0FFF:
        return "tibetan"
    if 0x1000 <= code <= 0x109F:
        return "myanmar"
    if 0x1200 <= code <= 0x137F:
        return "ethiopic"
    if 0x1780 <= code <= 0x17FF:
        return "khmer"
    return "latin" if char.isalpha() else None


def scripts_of(text: str) -> set[str]:
    """Every writing system used in this text."""
    return {script for script in (_script_of(char) for char in text) if script}


def channel_scripts(text: str) -> set[str] | None:
    """Scripts a chat actually writes in, or None when the sample is too mixed to judge.

    Latin is always allowed so English keywords keep working in every chat.
    """
    counts: dict[str, int] = {}
    for char in text:
        script = _script_of(char)
        if script:
            counts[script] = counts.get(script, 0) + 1
    total = sum(counts.values())
    if total < 20:
        return None
    dominant, seen = max(counts.items(), key=lambda item: item[1])
    if seen / total < 0.55:
        return None
    allowed = {"latin", dominant}
    if dominant in {"cjk", "kana"}:
        # Japanese mixes kanji and kana; keep both so neither loses its keywords.
        allowed |= {"cjk", "kana"}
    return allowed


def order_queries(variants: list[str], allowed: set[str] | None, limit: int,
                  shortest_first: bool = False) -> list[str]:
    """Keep the chat's own language and English first, drop languages it cannot hold.

    Search is AND across words, so inside a single chat the shortest keyword recalls the
    most; longer phrases rarely co-occur.
    """
    # A Latin-only result carries no information: it is the fallback for anything unclassified.
    if not allowed or allowed == {"latin"}:
        ordered = variants if not shortest_first else sorted(
            variants, key=lambda value: len(value.split())
        )
        return ordered[:limit]

    def rank(value: str) -> int:
        scripts = scripts_of(value)
        if not scripts or scripts <= allowed:
            return 0
        return 1 if "latin" in scripts else 2

    if shortest_first:
        ordered = sorted(
            range(len(variants)),
            key=lambda index: (rank(variants[index]), len(variants[index].split()), index),
        )
    else:
        ordered = sorted(range(len(variants)), key=lambda index: (rank(variants[index]), index))
    return [variants[index] for index in ordered[:limit]]


def _keywords(query: str) -> set[str]:
    terms = set()
    for part in re.findall(rf"[{CJK}]+|[^\W_]+(?:\.[0-9]+)*", query.casefold()):
        if _is_generic(part):
            continue
        if re.fullmatch(rf"[{CJK}]{{3,}}", part):
            terms.update(part[index:index + 2] for index in range(len(part) - 1))
        else:
            terms.add(_compact(part))
    return terms


def _match_score(query: str, headline: str, description: str) -> float:
    compact = _compact(query)
    if _is_generic(query) or not compact:
        return 0
    terms = _keywords(query)
    score = 8 if compact in headline else 0
    score += 2 if compact in description else 0
    if terms:
        score += 6 * sum(term in headline for term in terms) / len(terms)
        score += 2 * sum(term in description for term in terms) / len(terms)
    return score


def _is_better_copy(left: dict, right: dict) -> bool:
    left_members, right_members = left["members"] or 0, right["members"] or 0
    if left_members != right_members:
        return left_members > right_members
    left_official, right_official = "telegram" in left["sources"], "telegram" in right["sources"]
    if left_official != right_official:
        return left_official
    return left["url"] < right["url"]


def _absorb(target: dict, other: dict) -> None:
    """Move what the dropped duplicate knew into the copy that stays."""
    for source in other["sources"]:
        if source not in target["sources"]:
            target["sources"].append(source)
    for query in other["matched_queries"]:
        if query not in target["matched_queries"]:
            target["matched_queries"].append(query)
    target["joined"] |= other["joined"]
    if target["members"] is None and other["members"] is not None:
        target["members"] = other["members"]
    if not target["description"] and other["description"]:
        target["description"] = other["description"]
    if target["kind"] == "unknown" and other["kind"] != "unknown":
        target["kind"] = other["kind"]


def rank_candidates(query: str, hits: list[dict], prefer: str = "") -> list[dict]:
    peers = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = canonical_target(hit.get("url"))
        kind = _clean_text(hit.get("kind"), 20)
        if url is None or kind in {"bot", "person", "user"}:
            continue
        username = url.removeprefix("https://t.me/")
        is_invite = username.startswith("+")
        if not is_invite and username.endswith("bot") and kind not in {"channel", "group"}:
            continue

        title = _clean_text(hit.get("title"), 180)
        description = _clean_text(hit.get("description"), 420)
        source = _clean_text(hit.get("source"), 50)
        matched_query = _clean_text(hit.get("query"))
        official = source == "telegram"
        candidate = peers.setdefault(url, {
            "id": hashlib.sha256(url.encode()).hexdigest()[:24],
            "url": url, "username": None if is_invite else username,
            "title": "邀请链接" if is_invite else f"@{username}", "kind": "unknown",
            "members": None, "description": "", "sources": [], "matched_queries": [],
            "joined": False, "title_priority": -1, "description_priority": -1,
        })
        if title and title.removeprefix("@").casefold() != username:
            priority = 2 if official else 1
            if priority > candidate["title_priority"]:
                candidate["title"], candidate["title_priority"] = title, priority
        if description:
            priority = 2 if official else 1
            if priority > candidate["description_priority"]:
                candidate["description"], candidate["description_priority"] = description, priority
        if kind in {"channel", "group"} and (official or candidate["kind"] == "unknown"):
            candidate["kind"] = kind
        members = hit.get("members")
        if type(members) is int and 0 <= members <= 10_000_000_000:
            if official or candidate["members"] is None:
                candidate["members"] = members
        if source and source not in candidate["sources"]:
            candidate["sources"].append(source)
        if matched_query and matched_query not in candidate["matched_queries"]:
            candidate["matched_queries"].append(matched_query)
        candidate["joined"] |= hit.get("joined") is True

    # Promoters clone the same server under many usernames with an identical title, which
    # otherwise eats a large part of every result list. Keep the strongest copy of each title.
    clusters: dict[str, dict] = {}
    for candidate in peers.values():
        key = _compact(candidate["title"]) if candidate["title_priority"] >= 0 else ""
        if len(key) < 4:
            continue
        best = clusters.get(key)
        if best is None:
            clusters[key] = candidate
        elif _is_better_copy(candidate, best):
            _absorb(candidate, best)
            clusters[key] = candidate
        else:
            _absorb(best, candidate)
    duplicates = {
        candidate["url"]: cluster
        for key, cluster in clusters.items()
        for candidate in peers.values()
        if candidate is not cluster
        and candidate["title_priority"] >= 0
        and _compact(candidate["title"]) == key
    }
    peers = {url: candidate for url, candidate in peers.items() if url not in duplicates}

    original = _clean_text(query)

    def sort_key(candidate: dict) -> tuple:
        headline = _compact(candidate["title"]) + " " + _compact(candidate["username"] or "")
        description = _compact(candidate["description"])
        primary = _match_score(original, headline, description)
        alternate = max(
            (_match_score(term, headline, description) for term in candidate["matched_queries"]),
            default=0,
        )
        score = max(primary, alternate * 0.88) + min(primary, alternate) * 0.10
        score += min(2, max(0, len(candidate["sources"]) - 1)) * 0.7
        score += min(3, len(candidate["matched_queries"])) * 0.25
        score += 0.5 if candidate["kind"] != "unknown" else 0
        score += math.log10(1 + (candidate["members"] or 0)) * 0.10
        if prefer and prefer in candidate["sources"]:
            score += PREFERRED_SOURCE_BONUS
        return -score, candidate["title"].casefold(), candidate["url"]

    ordered = sorted(peers.values(), key=sort_key)
    for candidate in ordered:
        candidate.pop("title_priority")
        candidate.pop("description_priority")
        candidate["sources"].sort(key=lambda source: (source != "telegram", source.casefold()))
    return ordered
