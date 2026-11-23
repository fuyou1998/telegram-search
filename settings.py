from dataclasses import dataclass
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
SESSION_PATH = ROOT / ".telegram-data" / "telegram"
WEB_HOST = "127.0.0.1"
WEB_PORT = 8787
APP_NAME = "telegram-search-local"
MAX_BOTS = 5

# Languages Telegram is actually used in, weighted towards the large non-English communities.
DEFAULT_LANGUAGES = (
    "Russian", "Persian", "Arabic", "Indonesian", "Turkish", "Portuguese", "Spanish",
    "Hindi", "Urdu", "Bengali", "Vietnamese", "Ukrainian", "Uzbek", "French", "German",
    "Italian", "Filipino", "Burmese", "Korean", "Japanese",
)


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bots: tuple[str, ...]
    ai_key: str
    ai_base_url: str
    ai_model: str
    telegram_proxy_url: str = ""
    ai_proxy_url: str = ""
    ai_reasoning_effort: str = ""
    official_search: bool = True
    bot_variants: int = 4
    max_variants: int = 14
    enrich: bool = True
    ai_languages: tuple[str, ...] = DEFAULT_LANGUAGES
    ai_language_count: int = 5
    brand: str = "极目"
    promo_title: str = ""
    promo_text: str = ""
    promo_url: str = ""
    promo_channel: str = ""

    @property
    def telegram_configured(self) -> bool:
        return self.api_id > 0 and bool(re.fullmatch(r"[a-fA-F0-9]{32}", self.api_hash))

    @property
    def ai_configured(self) -> bool:
        return bool(self.ai_key and self.ai_base_url and self.ai_model)


def _flag(value: str, default: bool) -> bool:
    text = value.strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "off", "disabled"}


def _bounded(value: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def parse_languages(value: str, default: tuple[str, ...] = DEFAULT_LANGUAGES) -> tuple[str, ...]:
    """Accept "Russian, Persian, Greek" or "俄语、波斯语" as written by the operator."""
    entries = [part.strip() for part in re.split(r"[,，、;；|]+", value) if part.strip()]
    unique = tuple(dict.fromkeys(entry for entry in entries))
    return unique[:40] if unique else default


def load_settings() -> Settings:
    values = dotenv_values(ENV_FILE, interpolate=False) if ENV_FILE.is_file() else {}

    def get(name: str, default: str = "") -> str:
        return (os.environ.get(name, values.get(name, default)) or "").strip()

    try:
        api_id = int(get("TELEGRAM_API_ID", "0"))
    except ValueError:
        api_id = 0

    bots = []
    for configured_bot in get("TELEGRAM_SEARCH_BOTS", "@jisou").split(","):
        bot = configured_bot.strip().lower().lstrip("@")
        if re.fullmatch(r"[a-z][a-z0-9_]{3,31}", bot) and f"@{bot}" not in bots:
            bots.append(f"@{bot}")

    return Settings(
        api_id=api_id,
        api_hash=get("TELEGRAM_API_HASH"),
        bots=tuple(bots[:MAX_BOTS]),
        ai_key=get("AI_API_KEY"),
        ai_base_url=get("AI_BASE_URL", "https://api.openai.com/v1"),
        ai_model=get("AI_MODEL"),
        telegram_proxy_url=get("TELEGRAM_PROXY_URL"),
        ai_proxy_url=get("AI_PROXY_URL"),
        ai_reasoning_effort=get("AI_REASONING_EFFORT").lower(),
        official_search=_flag(get("TELEGRAM_OFFICIAL_SEARCH"), True),
        bot_variants=_bounded(get("TELEGRAM_BOT_VARIANTS"), 4, 1, 8),
        max_variants=_bounded(get("TELEGRAM_MAX_VARIANTS"), 14, 4, 20),
        enrich=_flag(get("TELEGRAM_ENRICH"), True),
        ai_languages=parse_languages(get("AI_LANGUAGES")),
        ai_language_count=_bounded(get("AI_LANGUAGE_COUNT"), 5, 1, 10),
        brand=get("APP_BRAND", "极目")[:40],
        promo_title=get("PROMO_TITLE")[:80],
        promo_text=get("PROMO_TEXT")[:200],
        promo_url=get("PROMO_URL")[:300],
        promo_channel=get("PROMO_CHANNEL")[:300],
    )


def telegram_proxy_options(url: str) -> dict | None:
    if not url:
        return None
    parsed = urlsplit(url)
    protocol = {"socks5": "socks5", "socks5h": "socks5", "http": "http"}.get(parsed.scheme)
    if not protocol or not parsed.hostname or not parsed.port or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Expected a SOCKS5 or HTTP proxy address with a port")
    return {
        "proxy_type": protocol,
        "addr": parsed.hostname,
        "port": parsed.port,
        "username": unquote(parsed.username) if parsed.username is not None else None,
        "password": unquote(parsed.password) if parsed.password is not None else None,
        "rdns": True,
    }


def save_local_settings(entries: dict[str, str]) -> None:
    template = ENV_FILE if ENV_FILE.exists() else ROOT / ".env.example"
    content = template.read_text(encoding="utf-8-sig")
    for key, value in entries.items():
        entry = f"{key}={value}"
        content, changed = re.subn(rf"(?m)^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=.*$", lambda _: entry, content)
        if not changed:
            content = f"{content.rstrip()}\n{entry}\n"
    with NamedTemporaryFile(dir=ROOT, prefix=".env.", mode="w", encoding="utf-8", delete=False) as handle:
        pending = Path(handle.name)
        handle.write(content)
    try:
        os.replace(pending, ENV_FILE)
    finally:
        pending.unlink(missing_ok=True)


def save_telegram_settings(api_id: str, api_hash: str) -> None:
    save_local_settings({"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash})
