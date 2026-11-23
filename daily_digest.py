"""Generate a daily, review-first Telegram group/channel digest.

This script never publishes. It searches through the local Jimu service and writes a
copy-ready draft under .telegram-data/drafts/. Review the links and content before posting.

    python daily_digest.py
    python daily_digest.py --topic "开源 AI 工具"
    python daily_digest.py --topic "独立开发者" --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from search_quality import canonical_target, scripts_of
from settings import ROOT
import channel_card


BASE_URL = "http://127.0.0.1:8787"
DEFAULT_TOPICS = (
    "开源 AI 工具",
    "独立开发者",
    "Telegram 机器人",
    "Python 开源项目",
    "视频生成 AI",
    "设计工具",
    "跨境电商",
)
TOPICS_FILE = ROOT / "daily_topics.txt"
TOPICS_EXAMPLE = ROOT / "daily_topics.example.txt"
DRAFT_DIR = ROOT / ".telegram-data" / "drafts"
SEEN_FILE = ROOT / ".telegram-data" / "daily_seen.json"
BRAND = "极目"
GROUP_URL = "https://t.me/jimu_group"
CHANNEL_URL = "https://t.me/jimu_feed"
SEEN_DAYS = 14

LANGUAGES = (
    ("cjk", "中文"),
    ("kana", "日文"),
    ("hangul", "韩文"),
    ("cyrillic", "俄语"),
    ("arabic", "阿拉伯语 / 波斯语"),
    ("hebrew", "希伯来语"),
    ("greek", "希腊语"),
    ("armenian", "亚美尼亚语"),
    ("georgian", "格鲁吉亚语"),
    ("devanagari", "天城文"),
    ("bengali", "孟加拉语"),
    ("gurmukhi", "旁遮普语"),
    ("gujarati", "古吉拉特语"),
    ("tamil", "泰米尔语"),
    ("telugu", "泰卢固语"),
    ("kannada", "卡纳达语"),
    ("malayalam", "马拉雅拉姆语"),
    ("sinhala", "僧伽罗语"),
    ("thai", "泰语"),
    ("lao", "老挝语"),
    ("tibetan", "藏语"),
    ("myanmar", "缅甸语"),
    ("ethiopic", "阿姆哈拉语"),
    ("khmer", "高棉语"),
    ("syriac", "叙利亚语"),
    ("thaana", "迪维希语"),
)


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def clean_text(value: object, limit: int = 220) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:limit]


def get_topics() -> list[str]:
    path = TOPICS_FILE if TOPICS_FILE.is_file() else TOPICS_EXAMPLE
    if not path.is_file():
        return list(DEFAULT_TOPICS)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return list(DEFAULT_TOPICS)
    topics = [clean_text(line, 120) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return list(dict.fromkeys(topic for topic in topics if topic)) or list(DEFAULT_TOPICS)


def topic_for_day(topics: list[str], today: date | None = None) -> str:
    if not topics:
        raise ValueError("No daily topics configured")
    day = today or date.today()
    return topics[day.toordinal() % len(topics)]


def read_seen() -> dict[str, str]:
    try:
        value = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {str(url): str(day) for url, day in value.items()} if isinstance(value, dict) else {}


def write_seen(value: dict[str, str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    pending = SEEN_FILE.with_suffix(".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(pending, SEEN_FILE)


def select_candidates(results: list[dict], seen: dict[str, str], limit: int, today: date | None = None) -> list[dict]:
    day = today or date.today()
    cutoff = (day - timedelta(days=SEEN_DAYS)).isoformat()
    fresh_seen = {url: stamp for url, stamp in seen.items() if stamp >= cutoff}
    selected: list[dict] = []
    selected_urls: set[str] = set()

    # First pass: prefer channels not used in the previous two weeks.
    for row in results:
        url = canonical_target(row.get("url"))
        title = clean_text(row.get("title"), 140)
        if not url or not title or url in fresh_seen or url in selected_urls:
            continue
        selected.append({**row, "url": url, "title": title})
        selected_urls.add(url)
        if len(selected) >= limit:
            return selected

    # If the topic is narrow, fill remaining slots with the best current matches rather
    # than producing an empty digest. Duplicates are still removed within this draft.
    for row in results:
        url = canonical_target(row.get("url"))
        title = clean_text(row.get("title"), 140)
        if not url or not title or url in selected_urls:
            continue
        selected.append({**row, "url": url, "title": title})
        selected_urls.add(url)
        if len(selected) >= limit:
            break
    return selected


def language_label(title: str, description: str = "") -> str:
    scripts = scripts_of(f"{title} {description}")
    for code, label in LANGUAGES:
        if code in scripts:
            return label
    return "拉丁文字 / 未识别"


def source_label(sources: list[str]) -> str:
    labels = []
    for source in sources:
        if source == "telegram":
            label = "Telegram 官方搜索"
        elif source.lower() == "@jisou":
            label = "极搜"
        else:
            label = source.lstrip("@")
        if label not in labels:
            labels.append(label)
    return "、".join(labels) if labels else "公开搜索"


def render_draft(topic: str, candidates: list[dict], day: date | None = None) -> str:
    day = day or date.today()
    lines = [
        f"🔎 极目每日找群｜{day.isoformat()}",
        "",
        f"今日关键词：{topic}",
        "",
        "以下是公开搜索中发现的候选群组 / 频道，发布前请人工确认内容、链接与群规：",
        "",
    ]
    for index, row in enumerate(candidates, 1):
        title = clean_text(row.get("title"), 140)
        kind = {"group": "群组", "channel": "频道"}.get(row.get("kind"), "群组 / 频道")
        members = row.get("members")
        member_text = f"{members:,} 人" if isinstance(members, int) and members > 0 else "人数未知"
        language = language_label(title, clean_text(row.get("description"), 180))
        description = clean_text(row.get("description"), 180)
        lines.append(f"{index}. {title}")
        lines.append(f"   {kind} · {language} · {member_text}")
        if description:
            lines.append(f"   {description}")
        lines.append(f"   {row['url']}")
        lines.append(f"   来源：{source_label(row.get('sources') or [])}")
        lines.append("")
    lines.extend([
        "以上为公开搜索结果，不代表极目对群组内容作背书；加入前请自行确认安全性与群规。",
        "",
        f"本地多语言 Telegram 群组搜索工具：{ 'https://github.com/fuyou1998/telegram-search' }",
        f"交流反馈：{GROUP_URL}",
        f"更新频道：{CHANNEL_URL}",
    ])
    return "\n".join(lines) + "\n"


def _request(path: str, method: str = "GET", body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"} if data is not None else {},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"本地搜索服务返回 HTTP {error.code}: {detail}") from None
    except URLError as error:
        raise RuntimeError("本地搜索服务未运行。先双击「极目」启动，再生成每日草稿。") from error


def run_search(topic: str, timeout: int = 240) -> dict:
    started = _request("/api/search", "POST", {"query": topic})
    job_id = started.get("id")
    if not job_id:
        raise RuntimeError("本地搜索服务没有返回任务编号。")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _request(f"/api/search/{job_id}", timeout=30)
        if snapshot.get("state") != "running":
            return snapshot
        time.sleep(1.5)
    raise RuntimeError(f"搜索超过 {timeout} 秒仍未完成。")


def write_draft(topic: str, candidates: list[dict], day: date | None = None, force: bool = False) -> Path:
    day = day or date.today()
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    path = DRAFT_DIR / f"{day.isoformat()}.txt"
    if path.exists() and not force:
        raise FileExistsError(f"今天的草稿已存在：{path}")
    path.write_text(render_draft(topic, candidates, day), encoding="utf-8")
    return path


def main() -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description="生成极目每日找群草稿（只生成，不自动发布）")
    parser.add_argument("--topic", help="今天搜索的主题；省略时按 daily_topics.txt 轮换")
    parser.add_argument("--limit", type=int, default=3, help="候选数量，1–5，默认 3")
    parser.add_argument("--timeout", type=int, default=240, help="搜索最长等待秒数")
    parser.add_argument("--card", action="store_true", help="把首选候选深入分析成单频道推荐卡片")
    parser.add_argument("--ai", action="store_true", help="频道卡片使用 AI 摘要；会将公开帖子样本发给配置的 AI 服务")
    parser.add_argument("--force", action="store_true", help="覆盖当天同主题已有草稿")
    args = parser.parse_args()
    if not 1 <= args.limit <= 5:
        parser.error("--limit 必须在 1 到 5 之间")
    settings = load_settings()
    if args.ai and not args.card:
        parser.error("--ai 仅与 --card 一起使用。")
    if args.ai and not settings.ai_configured:
        parser.error("尚未配置 AI；请先在网页设置中配置，或不加 --ai 使用本地摘录。")

    topics = get_topics()
    topic = clean_text(args.topic, 120) if args.topic else topic_for_day(topics)
    if not topic:
        raise SystemExit("主题不能为空。")
    day = date.today()
    print(f"今日主题：{topic}")
    print("正在搜索并整理候选；不会自动发送到频道。")

    snapshot = run_search(topic, timeout=args.timeout)
    if snapshot.get("state") == "error":
        raise SystemExit(snapshot.get("error") or "搜索失败。")
    candidates = select_candidates(snapshot.get("results") or [], read_seen(), args.limit, day)
    if not candidates:
        raise SystemExit("没有找到可用候选，未生成草稿。")

    if args.card:
        candidate = candidates[0]
        try:
            review, ai = asyncio.run(channel_card.review(candidate["url"], 30, args.ai))
            draft = channel_card.write_card(review, ai, args.force)
        except (RuntimeError, FileExistsError, OSError) as error:
            raise SystemExit(str(error)) from None
        seen = read_seen()
        seen[candidate["url"]] = day.isoformat()
        cutoff = (day - timedelta(days=SEEN_DAYS * 3)).isoformat()
        write_seen({url: stamp for url, stamp in seen.items() if stamp >= cutoff})
        print(f"已生成频道推荐卡片：{draft}")
        if draft.with_suffix(".jpg").is_file():
            print(f"配图素材：{draft.with_suffix('.jpg')}")
        print("未自动发布；请检查摘要、推广信号和链接后手动发送。")
        return 0

    draft = write_draft(topic, candidates, day, args.force)

    seen = read_seen()
    for candidate in candidates:
        seen[candidate["url"]] = day.isoformat()
    cutoff = (day - timedelta(days=SEEN_DAYS * 3)).isoformat()
    write_seen({url: stamp for url, stamp in seen.items() if stamp >= cutoff})

    print(f"\n已生成 {len(candidates)} 个候选：{draft}")
    print("请检查频道链接、群规与内容，再手动复制发布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
