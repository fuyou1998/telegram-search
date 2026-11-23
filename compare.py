"""Compare this tool against a manual Telegram search.

A manual search is reproduced exactly: one keyword typed into Telegram's search box is a
single `contacts.Search` call, which is what `/api/baseline` returns. Every topic is also
written out with the tool's own ranking so the two lists can be read side by side.

    python compare.py "开源视频生成 API 价格" "VPN 推荐"
    python compare.py --topics topics.txt --out reports/compare.md
    python compare.py --baseline manual.json --topics topics.txt

`manual.json` holds URLs collected by hand: {"关键词": ["https://t.me/a", ...]}.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE_URL = "http://127.0.0.1:8787"
POLL_SECONDS = 2.0
SCRIPT_LABELS = {
    "cjk": "中文/汉字", "kana": "日文", "hangul": "韩文", "cyrillic": "俄文",
    "arabic": "波斯/阿拉伯", "hebrew": "希伯来", "thai": "泰文",
    "devanagari": "印地", "bengali": "孟加拉", "myanmar": "缅甸", "latin": "拉丁",
}


def api(path: str, method: str = "GET", body: dict | None = None, timeout: float = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise SystemExit(f"请求 {path} 失败：HTTP {error.code} {detail}") from None
    except URLError as error:
        raise SystemExit(f"连不上本地服务 {BASE_URL}（{error.reason}）。先运行 start-search.cmd。") from None


def run_tool(query: str, prefer: str, timeout: float) -> dict:
    job = api("/api/search", "POST", {"query": query})
    path = f"/api/search/{job['id']}" + (f"?prefer={quote(prefer)}" if prefer else "")
    deadline = time.monotonic() + timeout
    snapshot = {}
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        snapshot = api(path)
        if snapshot.get("state") != "running":
            return snapshot
    raise SystemExit(f"「{query}」在 {timeout:.0f} 秒内没有搜索完成。")


def unique_urls(rows: list[dict]) -> list[str]:
    seen: list[str] = []
    for row in rows:
        url = row.get("url")
        if url and url not in seen:
            seen.append(url)
    return seen


def script_label(title: str) -> str:
    for char in title:
        code = ord(char)
        if 0x0400 <= code <= 0x04FF:
            return SCRIPT_LABELS["cyrillic"]
        if 0x0600 <= code <= 0x06FF:
            return SCRIPT_LABELS["arabic"]
        if 0x3040 <= code <= 0x30FF:
            return SCRIPT_LABELS["kana"]
        if 0xAC00 <= code <= 0xD7AF:
            return SCRIPT_LABELS["hangul"]
        if 0x4E00 <= code <= 0x9FFF:
            return SCRIPT_LABELS["cjk"]
        if 0x0E00 <= code <= 0x0E7F:
            return SCRIPT_LABELS["thai"]
    return SCRIPT_LABELS["latin"]


def describe(row: dict) -> str:
    members = row.get("members")
    count = f"{members:,} 位成员" if isinstance(members, int) else "成员数未知"
    sources = " + ".join(row.get("sources") or ["?"])
    return f"{row.get('title') or row.get('url')} — {count} — {sources} — {row.get('url')}"


def normalized_title(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def compare_topic(topic: str, tool: dict, baseline: dict, manual: list[str] | None) -> dict:
    rows = tool.get("results") or []
    tool_urls = unique_urls(rows)
    if manual is not None:
        baseline_urls = manual
        baseline_label = "手工记录的链接"
    else:
        baseline_urls = unique_urls(baseline.get("results") or [])
        baseline_label = "官方搜索一次（与手工等价）"

    tool_by_url = {row["url"]: row for row in rows}
    tool_titles = {normalized_title(row.get("title") or "") for row in rows}
    baseline_set = list(dict.fromkeys(baseline_urls))
    baseline_rows = {row["url"]: row for row in (baseline.get("results") or [])}
    both = [url for url in baseline_set if url in tool_by_url]
    # A duplicate-promoted copy is present under the merged title, not actually missing.
    merged = [
        url for url in baseline_set
        if url not in tool_by_url
        and normalized_title(baseline_rows.get(url, {}).get("title") or "") in tool_titles
    ]
    missing = [url for url in baseline_set if url not in tool_by_url and url not in merged]
    added = [url for url in tool_urls if url not in set(baseline_set)]
    recall = len(both) / len(baseline_set) if baseline_set else None
    ranks = [
        (tool_urls.index(url) + 1, baseline_set.index(url) + 1)
        for url in both
    ]
    return {
        "rows": rows,
        "tool_urls": tool_urls,
        "baseline_urls": baseline_set,
        "baseline_label": baseline_label,
        "both": both,
        "merged": merged,
        "missing": missing,
        "added": added,
        "recall": recall,
        "ranks": ranks,
        "baseline_rows": baseline_rows,
    }


def render(topic: str, result: dict, prefer: str) -> str:
    rows = result["rows"]
    total = len(result["tool_urls"])
    base_total = len(result["baseline_urls"])
    recall = result["recall"]
    recall_text = "—" if recall is None else f"{recall * 100:.0f}%"
    scripts = Counter(script_label(row.get("title") or "") for row in rows)
    language_text = " · ".join(
        f"{name} {count}" for name, count in scripts.most_common() if count
    ) or "—"
    improved = sum(1 for tool_rank, base_rank in result["ranks"] if tool_rank < base_rank)
    worse = sum(1 for tool_rank, base_rank in result["ranks"] if tool_rank > base_rank)

    lines = [
        f"## {topic}",
        "",
        f"- 权重：{prefer or '均衡'} · 耗时 {result.get('elapsed', 0):.1f} 秒 · "
        f"关键词变体 {len(result.get('variants') or [])} 个"
        f"（{result.get('expansion', '')}）",
        f"- 工具找到 **{total}** 个 · {result['baseline_label']} **{base_total}** 个 · "
        f"重合 **{len(result['both'])}** 个（召回 **{recall_text}**） · "
        f"工具新增 **{len(result['added'])}** 个 · 漏掉 **{len(result['missing'])}** 个"
        + (f" · 同名合并 **{len(result['merged'])}** 个" if result["merged"] else ""),
    ]
    if base_total == 0:
        lines.append(
            f"- 人工这一次搜不到任何结果，工具靠关键词变体和机器人补出 {total} 个"
        )
    lines.append(f"- 覆盖语言：{language_text}")
    if result["ranks"]:
        lines.append(
            f"- 重合项排名：比人工靠前 {improved} 个 · 靠后 {worse} 个 · "
            f"平均人工第 {sum(b for _, b in result['ranks']) / len(result['ranks']):.1f} 位 → "
            f"工具第 {sum(t for t, _ in result['ranks']) / len(result['ranks']):.1f} 位"
        )
    lines.append("")

    if result["missing"]:
        lines.append(f"**人工能看到、工具没找到（{len(result['missing'])} 个）**")
        lines.append("")
        for index, url in enumerate(result["missing"], 1):
            row = result["baseline_rows"].get(url)
            detail = describe(row) if row else url
            lines.append(f"{index}. 人工第 {result['baseline_urls'].index(url) + 1} 位 — {detail}")
        lines.append("")

    if result["added"]:
        lines.append(f"**工具新增、人工那一次没有的（前 {min(12, len(result['added']))} 个）**")
        lines.append("")
        by_url = {row["url"]: row for row in rows}
        for index, url in enumerate(result["added"][:12], 1):
            row = by_url[url]
            hits = row.get("matched_queries") or []
            matched = f" · 命中词：{hits[0]}" if hits else ""
            lines.append(f"{index}. {describe(row)}{matched}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="对比工具结果与人工 Telegram 搜索")
    parser.add_argument("topics", nargs="*", help="要对比的关键词，可给多个")
    parser.add_argument("--topics", dest="topic_file", help="每行一个关键词的文件")
    parser.add_argument("--prefer", default="", help="排序权重：留空均衡，或 @jisou / telegram")
    parser.add_argument("--baseline", help="手工收集的链接 JSON：{\"关键词\": [\"url\", ...]}")
    parser.add_argument("--out", help="Markdown 报告路径，默认写进 reports/")
    parser.add_argument("--csv", help="同时导出完整结果的 CSV 路径")
    parser.add_argument("--timeout", type=float, default=180.0, help="单个关键词最长等待秒数")
    args = parser.parse_args()

    topics = list(args.topics)
    if args.topic_file:
        topics += [
            line.strip() for line in Path(args.topic_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not topics:
        print("至少给一个关键词，或用 --topics 指定文件。", file=sys.stderr)
        return 2

    manual = json.loads(Path(args.baseline).read_text(encoding="utf-8")) if args.baseline else {}

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_path = Path(args.out or f"reports/compare-{stamp}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sections, csv_rows = [], []
    for topic in topics:
        started = time.monotonic()
        snapshot = run_tool(topic, args.prefer, args.timeout)
        elapsed = time.monotonic() - started
        baseline = api(f"/api/baseline?query={quote(topic)}", timeout=90)
        if baseline.get("state") != "done":
            print(f"跳过「{topic}」：{baseline.get('message') or '基线检索失败'}", file=sys.stderr)
            continue
        result = compare_topic(topic, snapshot, baseline, manual.get(topic))
        result.update(elapsed=elapsed, variants=snapshot.get("variants"), expansion=snapshot.get("expansion_message"))
        sections.append(render(topic, result, args.prefer))

        for rank, row in enumerate(result["rows"], 1):
            csv_rows.append({
                "关键词": topic, "工具排名": rank,
                "标题": row.get("title") or "", "链接": row.get("url") or "",
                "成员数": row.get("members") or "",
                "来源": " / ".join(row.get("sources") or []),
                "命中词": " / ".join((row.get("matched_queries") or [])[:3]),
                "语言": script_label(row.get("title") or ""),
                "人工也有": "是" if row.get("url") in result["baseline_urls"] else "否",
            })
        print(
            f"{topic}：工具 {len(result['tool_urls'])} 个 · 人工 {len(result['baseline_urls'])} 个 · "
            f"重合 {len(result['both'])} · 漏 {len(result['missing'])} · {elapsed:.1f}s"
        )

    header = [
        "# 工具 vs 人工 Telegram 搜索",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 权重：{args.prefer or '均衡'}",
        "- 「人工」= 同一个关键词在 Telegram 搜索框搜一次的结果（官方 `contacts.Search`）",
        "",
    ]
    out_path.write_text("\n".join(header) + "\n".join(sections), encoding="utf-8")
    print(f"报告：{out_path}")

    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]) if csv_rows else ["关键词"])
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"明细：{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
