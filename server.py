import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
import logging
import os
import re
import threading
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import history
import proxy_probe
from search_quality import canonical_target, expand_query, rank_candidates
from settings import (
    APP_NAME, MAX_BOTS, ROOT, SESSION_PATH, Settings, load_settings, parse_languages,
    save_local_settings, save_telegram_settings, telegram_proxy_options,
)
from telegram_source import TelegramSource


LOGGER = logging.getLogger(__name__)
MAX_RETAINED_SEARCHES = 12


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class JoinRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=25)


class CandidateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class TelegramCredentials(BaseModel):
    api_id: str = Field(max_length=30)
    api_hash: str = Field(max_length=64)


class SettingsUpdate(BaseModel):
    telegram_proxy: str | None = Field(default=None, max_length=200)
    ai_key: str | None = Field(default=None, max_length=400)
    ai_model: str | None = Field(default=None, max_length=100)
    ai_base_url: str | None = Field(default=None, max_length=300)
    ai_languages: str | None = Field(default=None, max_length=800)
    ai_language_count: int | None = Field(default=None, ge=1, le=10)
    search_bots: list[str] | None = Field(default=None, max_length=MAX_BOTS)
    official_search: bool | None = None
    bot_variants: int | None = Field(default=None, ge=1, le=8)
    max_variants: int | None = Field(default=None, ge=4, le=20)
    enrich: bool | None = None
    brand: str | None = Field(default=None, max_length=40)
    promo_title: str | None = Field(default=None, max_length=80)
    promo_text: str | None = Field(default=None, max_length=200)
    promo_url: str | None = Field(default=None, max_length=300)
    promo_channel: str | None = Field(default=None, max_length=300)


@dataclass
class SearchJob:
    query: str
    sources: dict[str, dict]
    id: str = field(default_factory=lambda: uuid4().hex)
    state: str = "running"
    variants: list[str] = field(default_factory=list)
    used_ai: bool | None = None
    expansion_message: str = ""
    error: str = ""
    hits: list[dict] = field(default_factory=list)
    joined: dict[str, dict] = field(default_factory=dict)
    task: asyncio.Task | None = None

    def set_source(self, name: str, state: str, message: str = "") -> None:
        self.sources[name] = {"state": state, "message": message}

    def results(self, prefer: str = "") -> list[dict]:
        results = rank_candidates(self.query, self.hits, prefer)
        for candidate in results:
            outcome = self.joined.get(candidate["id"])
            if outcome:
                candidate["join_state"] = outcome["state"]
                candidate["join_message"] = outcome["message"]
                if outcome["state"] in {"joined", "already_joined"}:
                    candidate["joined"] = True
        return results

    def snapshot(self, prefer: str = "") -> dict:
        return {
            "id": self.id,
            "query": self.query,
            "state": self.state,
            "variants": self.variants,
            "used_ai": self.used_ai,
            "expansion_message": self.expansion_message,
            "sources": self.sources,
            "results": self.results(prefer),
            "error": self.error,
        }


async def run_search(job: SearchJob, source: TelegramSource, settings: Settings) -> None:
    try:
        # The cached variants depend on the prompt, which depends on the language list.
        cache_key = "|".join((settings.ai_model, str(settings.ai_language_count), ",".join(settings.ai_languages)))
        cached = history.read_expansion(job.query, cache_key)
        if cached is not None:
            job.variants, job.used_ai = cached
            job.expansion_message = "使用了 30 分钟内的相同关键词变体。"
        else:
            job.variants, job.used_ai, job.expansion_message = await expand_query(
                job.query,
                api_key=settings.ai_key,
                base_url=settings.ai_base_url,
                model=settings.ai_model,
                proxy_url=settings.ai_proxy_url,
                reasoning_effort=settings.ai_reasoning_effort,
                languages=settings.ai_languages,
                language_count=settings.ai_language_count,
            )
            history.write_expansion(job.query, cache_key, job.variants, job.used_ai)
        job.variants = history.demote_barren(job.variants)
        await source.discover(job.variants, job.hits.append, job.set_source)
        if not job.hits and all(item["state"] == "error" for item in job.sources.values()):
            job.state = "error"
            job.error = "各检索来源都未能完成。可查看下方的来源状态。"
        else:
            job.state = "done"
    except asyncio.CancelledError:
        job.state = "cancelled"
        for name, status in job.sources.items():
            if status["state"] in {"waiting", "running"}:
                job.set_source(name, "cancelled", "已停止搜索")
        raise
    except Exception as error:
        LOGGER.error("Search failed (%s)", type(error).__name__)
        job.state = "error"
        job.error = "检索中断。已取得的候选仍可以查看。"
        for name, status in job.sources.items():
            if status["state"] in {"waiting", "running"}:
                job.set_source(name, "error", "检索中断")
    finally:
        history.record_search(job.id, job.query, job.state, job.variants, job.hits)
        history.record_hits(job.variants, job.hits)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.jobs = {}
    app.state.join_lock = asyncio.Lock()
    app.state.source = make_source(settings)
    try:
        yield
    finally:
        tasks = [job.task for job in app.state.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if app.state.source:
            await app.state.source.close()


def make_source(settings: Settings) -> TelegramSource | None:
    if settings.telegram_configured and SESSION_PATH.with_suffix(".session").is_file():
        try:
            proxy = telegram_proxy_options(settings.telegram_proxy_url)
        except ValueError:
            return None
        return TelegramSource(
            settings.api_id, settings.api_hash, SESSION_PATH, settings.bots, proxy=proxy,
            official=settings.official_search, bot_variants=settings.bot_variants,
            max_variants=settings.max_variants, enrich=settings.enrich,
        )
    return None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _error: RequestValidationError):
    return JSONResponse({"detail": "提交的内容格式有误，请检查后再试。"}, status_code=422)


@app.middleware("http")
async def protect_local_session(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin is not None and origin != f"http://{request.headers.get('host', '')}"
        ):
            return JSONResponse({"detail": "请在本地搜索页面操作。"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    )
    if request.url.path.startswith("/api/") or request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {"app": APP_NAME}


@app.post("/api/shutdown")
async def shutdown():
    """Stop the local service. Needed because the packaged app runs without a console."""
    def stop() -> None:
        # Let the response reach the browser before the process disappears.
        threading.Timer(0.4, lambda: os._exit(0)).start()

    stop()
    return {"stopped": True}


async def telegram_status() -> dict:
    latest = load_settings()
    if app.state.source is None:
        app.state.settings = latest
        app.state.source = make_source(latest)
    else:
        app.state.settings = replace(
            app.state.settings, ai_key=latest.ai_key,
            ai_base_url=latest.ai_base_url, ai_model=latest.ai_model, ai_proxy_url=latest.ai_proxy_url,
            ai_reasoning_effort=latest.ai_reasoning_effort,
        )
    if not app.state.settings.telegram_configured:
        return {"connected": False, "message": "请先在 .env 中填写 Telegram API ID 和 API Hash。"}
    try:
        telegram_proxy_options(app.state.settings.telegram_proxy_url)
    except ValueError:
        return {"connected": False, "message": "请检查 .env 中的 TELEGRAM_PROXY_URL 格式。"}
    if not app.state.source:
        return {"connected": False, "message": "请运行 telegram-login.cmd 登录 Telegram，再刷新此页。"}
    try:
        return await asyncio.wait_for(app.state.source.status(), timeout=18)
    except (TimeoutError, ConnectionError, OSError):
        return {"connected": False, "message": "暂时连不上 Telegram。检查网络后刷新页面。"}


@app.get("/api/status")
async def status():
    telegram = await telegram_status()
    settings = app.state.settings
    return {
        "telegram": {**telegram, "credentials_saved": settings.telegram_configured},
        "ai": {
            "configured": settings.ai_configured,
            "model": settings.ai_model,
            "base_url": settings.ai_base_url,
        },
        "proxy": settings.telegram_proxy_url,
        "bots": settings.bots,
        "brand": settings.brand,
        "promo": {
            "title": settings.promo_title,
            "text": settings.promo_text,
            "url": settings.promo_url,
            "channel": settings.promo_channel,
        },
        "search": {
            "official": settings.official_search,
            "bot_variants": settings.bot_variants,
            "max_variants": settings.max_variants,
            "enrich": settings.enrich,
            "max_bots": MAX_BOTS,
            "languages": list(settings.ai_languages),
            "language_count": settings.ai_language_count,
        },
    }


@app.post("/api/settings")
async def update_settings(body: SettingsUpdate):
    """Update the fields the settings dialog can change, leaving the rest untouched."""
    entries: dict[str, str] = {}
    if body.telegram_proxy is not None:
        proxy = body.telegram_proxy.strip()
        try:
            telegram_proxy_options(proxy)
        except ValueError:
            raise HTTPException(400, "代理地址格式有误，应形如 socks5://127.0.0.1:7897") from None
        entries["TELEGRAM_PROXY_URL"] = proxy
    if body.ai_base_url is not None:
        entries["AI_BASE_URL"] = body.ai_base_url.strip()
    if body.ai_model is not None:
        entries["AI_MODEL"] = body.ai_model.strip()
    if body.ai_key:
        # An empty box means "keep the saved key"; only a typed value replaces it.
        entries["AI_API_KEY"] = body.ai_key.strip()
    if body.search_bots is not None:
        bots: list[str] = []
        for raw in body.search_bots:
            name = raw.strip().lower().lstrip("@")
            if not name:
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", name):
                raise HTTPException(400, f"「{raw}」不是有效的机器人用户名，应形如 @jisou")
            if name not in bots:
                bots.append(name)
        if len(bots) > MAX_BOTS:
            raise HTTPException(400, f"最多只能添加 {MAX_BOTS} 个搜索机器人。")
        entries["TELEGRAM_SEARCH_BOTS"] = ",".join(f"@{name}" for name in bots)
    if body.official_search is not None:
        entries["TELEGRAM_OFFICIAL_SEARCH"] = "1" if body.official_search else "0"
    if body.bot_variants is not None:
        entries["TELEGRAM_BOT_VARIANTS"] = str(body.bot_variants)
    if body.max_variants is not None:
        entries["TELEGRAM_MAX_VARIANTS"] = str(body.max_variants)
    if body.enrich is not None:
        entries["TELEGRAM_ENRICH"] = "1" if body.enrich else "0"
    if body.ai_languages is not None:
        languages = parse_languages(body.ai_languages, default=())
        if not languages:
            raise HTTPException(400, "至少保留一种语言，用逗号分隔，例如 Russian, Persian")
        entries["AI_LANGUAGES"] = ", ".join(languages)
    if body.ai_language_count is not None:
        entries["AI_LANGUAGE_COUNT"] = str(body.ai_language_count)
    if body.brand is not None and body.brand.strip():
        entries["APP_BRAND"] = body.brand.strip()
    if body.promo_title is not None:
        entries["PROMO_TITLE"] = body.promo_title.strip()
    if body.promo_text is not None:
        entries["PROMO_TEXT"] = body.promo_text.strip()
    if body.promo_url is not None:
        promo_url = body.promo_url.strip()
        if promo_url and not re.match(r"^https?://", promo_url):
            raise HTTPException(400, "推广链接需要以 http:// 或 https:// 开头。")
        entries["PROMO_URL"] = promo_url
    if body.promo_channel is not None:
        channel = body.promo_channel.strip()
        if channel and not re.match(r"^https?://", channel):
            raise HTTPException(400, "频道链接需要以 http:// 或 https:// 开头。")
        entries["PROMO_CHANNEL"] = channel
    if not entries:
        return {"saved": False}
    try:
        save_local_settings(entries)
    except OSError:
        raise HTTPException(500, "无法保存配置。请关闭正在编辑 .env 的程序后重试。") from None
    app.state.settings = load_settings()
    if entries.keys() & {"TELEGRAM_PROXY_URL", "TELEGRAM_SEARCH_BOTS", "TELEGRAM_OFFICIAL_SEARCH",
                         "TELEGRAM_BOT_VARIANTS", "TELEGRAM_MAX_VARIANTS", "TELEGRAM_ENRICH"}:
        # These are baked into the client when it is created, so rebuild it.
        if app.state.source is not None:
            await app.state.source.close()
            app.state.source = None
    return {"saved": True}


@app.post("/api/settings/telegram")
async def configure_telegram(body: TelegramCredentials):
    api_id = body.api_id.strip()
    api_hash = body.api_hash.strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", api_id) or int(api_id) >= 2**31:
        raise HTTPException(400, "API ID 应是一串数字，请复制 Telegram 页面显示的 App api_id。")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        raise HTTPException(400, "API Hash 应是 32 位字符，请复制完整的 App api_hash。")
    if app.state.source:
        raise HTTPException(409, "已有登录会话，请先结束搜索程序再更换凭证。")
    try:
        save_telegram_settings(api_id, api_hash)
    except OSError:
        raise HTTPException(500, "无法保存配置。请关闭正在编辑配置文件的程序，再重试。") from None
    return {"saved": True}


def get_job(job_id: str) -> SearchJob:
    job = app.state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "该次搜索已过期，请重新搜索。")
    return job


@app.post("/api/search", status_code=202)
async def start_search(body: SearchRequest):
    query = " ".join(body.query.split())
    if not query:
        raise HTTPException(400, "请输入要搜索的内容。")
    status = await telegram_status()
    if not status["connected"]:
        raise HTTPException(503, status["message"])
    jobs = app.state.jobs
    if any(job.task and not job.task.done() for job in jobs.values()):
        raise HTTPException(409, "还有一次检索正在运行，请等它结束或先停止它。")
    if len(jobs) >= MAX_RETAINED_SEARCHES:
        del jobs[next(iter(jobs))]
    names = ("telegram", *app.state.settings.bots)
    job = SearchJob(query, {name: {"state": "waiting", "message": ""} for name in names})
    jobs[job.id] = job
    job.task = asyncio.create_task(run_search(job, app.state.source, app.state.settings))
    return {"id": job.id}


@app.get("/api/search/{job_id}")
async def search_status(job_id: str, prefer: str = ""):
    job = get_job(job_id)
    allowed = {"telegram", *app.state.settings.bots}
    return job.snapshot(prefer if prefer in allowed else "")


@app.get("/api/history")
async def history_list():
    return {"searches": history.recent_searches()}


@app.get("/api/proxy/detect")
async def proxy_detect():
    """Probe common local proxy ports so nobody has to hunt for the number."""
    found = await asyncio.to_thread(proxy_probe.detect)
    direct = await asyncio.to_thread(proxy_probe.direct_works)
    return {
        "found": found,
        "suggest": found[0] if found else "",
        "direct": direct,
        "message": "可以直连，无需代理。" if direct else (
            f"检测到 {proxy_probe.describe(found)}" if found else "没有检测到可用代理。"
        ),
    }


@app.get("/api/baseline")
async def baseline_search(query: str = ""):
    query = " ".join(query.split())[:200]
    if not query:
        raise HTTPException(400, "请输入用来对比的关键词。")
    if app.state.source is None:
        raise HTTPException(503, "请先登录 Telegram。")
    return await app.state.source.baseline(query)


@app.post("/api/history/{search_id}/restore")
async def restore_search(search_id: str):
    job = app.state.jobs.get(search_id)
    if job is None:
        stored = history.load_search(search_id)
        if stored is None:
            raise HTTPException(404, "这条历史搜索已不在本地记录中。")
        names = ("telegram", *app.state.settings.bots)
        job = SearchJob(
            stored["query"], {name: {"state": "done", "message": ""} for name in names},
            id=search_id, state="done", variants=stored["variants"], hits=stored["hits"],
            expansion_message="本地历史记录。",
        )
        if len(app.state.jobs) >= MAX_RETAINED_SEARCHES:
            del app.state.jobs[next(iter(app.state.jobs))]
        app.state.jobs[search_id] = job
    return {"id": job.id}


@app.post("/api/search/{job_id}/cancel")
async def cancel_search(job_id: str):
    job = get_job(job_id)
    if job.task and not job.task.done():
        job.task.cancel()
        with suppress(asyncio.CancelledError):
            await job.task
        if job.state == "running":
            job.state = "cancelled"
            for name in job.sources:
                job.set_source(name, "cancelled", "已停止搜索")
    return {"state": job.state}


@app.post("/api/search/{job_id}/join")
async def join_results(job_id: str, body: JoinRequest):
    job = get_job(job_id)
    if job.state == "running":
        raise HTTPException(409, "请等这次搜索结束或先停止搜索。")
    source = app.state.source
    if not source:
        raise HTTPException(503, "请先登录 Telegram。")

    candidates = {candidate["id"]: candidate for candidate in job.results()}
    selected = []
    for candidate_id in dict.fromkeys(body.ids):
        candidate = candidates.get(candidate_id)
        url = canonical_target(candidate["url"]) if candidate else None
        if not url:
            raise HTTPException(400, "选中的候选已失效，请刷新结果后再试。")
        selected.append((candidate_id, candidate, url))

    async with app.state.join_lock:
        results = []
        for candidate_id, candidate, url in selected:
            previous = job.joined.get(candidate_id)
            if previous and previous["state"] in {"joined", "already_joined", "pending"}:
                outcome = previous
            elif candidate.get("joined"):
                outcome = {"state": "already_joined", "message": "已在这个群组或频道中。"}
            else:
                try:
                    outcome = await source.join(url)
                except Exception as error:
                    LOGGER.error("Join failed (%s)", type(error).__name__)
                    outcome = {"state": "error", "message": "加入失败，请稍后重试。"}
            job.joined[candidate_id] = outcome
            results.append({"id": candidate_id, **outcome})
        return {"results": results}


@app.post("/api/search/{job_id}/messages")
async def search_messages(job_id: str, body: CandidateRequest):
    job = get_job(job_id)
    if job.state == "running":
        raise HTTPException(409, "请等这次搜索结束或先停止搜索。")
    if not app.state.source:
        raise HTTPException(503, "请先登录 Telegram。")

    candidate = next((item for item in job.results() if item["id"] == body.id), None)
    url = canonical_target(candidate["url"]) if candidate else None
    if not url:
        raise HTTPException(404, "该候选已失效，请重新搜索。")
    variants = list(dict.fromkeys([job.query, *candidate["matched_queries"], *job.variants]))
    return await app.state.source.search_in(url, variants)
