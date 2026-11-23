import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import history
import proxy_probe
import server
import telegram_login
from search_quality import build_prompt, channel_scripts, order_queries, rank_candidates, scripts_of
from settings import DEFAULT_LANGUAGES, Settings, parse_languages
from telegram_source import _is_promo_block, _short_query


class SearchSource:
    def __init__(self):
        self.joins = []
        self.message_searches = []

    async def status(self):
        return {"connected": True, "message": "Connected"}

    async def close(self):
        pass

    async def discover(self, variants, on_candidate, on_source):
        on_source("telegram", "running", "")
        on_candidate({
            "title": "Wan 3.0 API discussion", "url": "https://t.me/search_example",
            "kind": "group", "source": "telegram", "query": variants[0],
        })
        await asyncio.sleep(0.04)
        on_candidate({
            "title": "Wan 3.0 API discussion", "url": "https://t.me/s/SEARCH_EXAMPLE/72",
            "kind": "group", "source": "@jisou", "query": variants[-1],
        })
        on_candidate({
            "title": "Video API news", "url": "https://t.me/video_api_example",
            "kind": "channel", "source": "@jisou", "query": variants[0],
        })
        on_source("telegram", "done", "")
        on_source("@jisou", "done", "")

    async def join(self, url):
        self.joins.append(url)
        return {"state": "pending", "message": "Waiting for approval"}

    async def search_in(self, url, variants):
        self.message_searches.append((url, variants))
        return {"state": "done", "message": "", "results": [{
            "text": "Original post", "url": f"{url}/72", "date": "2026-11-23T06:00:00+00:00",
        }]}


class SearchServerTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(0, "", ("@jisou",), "", "https://api.openai.com/v1", "")
        patched_settings = patch.object(server, "load_settings", return_value=settings)
        self.addCleanup(patched_settings.stop)
        patched_settings.start()
        self.client = self.enterContext(TestClient(server.app, base_url="http://127.0.0.1:8787"))

    def test_missing_login_and_cross_site_requests(self):
        self.assertEqual(self.client.get("/api/health").json()["app"], "telegram-search-local")
        self.assertFalse(self.client.get("/api/status").json()["telegram"]["connected"])
        self.assertEqual(self.client.post("/api/search", json={"query": "Wan"}).status_code, 503)
        self.assertEqual(self.client.post(
            "/api/search", json={"query": "Wan"}, headers={"Origin": "https://outside.example"},
        ).status_code, 403)

    def test_settings_save_does_not_return_credentials(self):
        valid_body = {"api_id": "12345", "api_hash": "a" * 32}
        with patch.object(server, "save_telegram_settings") as save:
            path = "/api/settings/telegram"
            denied = self.client.post(path, json=valid_body, headers={"Origin": "https://outside.example"})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(self.client.post(path, json={**valid_body, "api_hash": "x"}).status_code, 400)
            save.assert_not_called()
            stored = self.client.post(path, json=valid_body)
            self.assertEqual(stored.json(), {"saved": True})
            save.assert_called_once_with("12345", "a" * 32)

    def test_search_deduplication_explicit_batch_join_and_original_post_search(self):
        source = SearchSource()
        server.app.state.source = source
        server.app.state.settings = Settings(1, "a" * 32, ("@jisou",), "", "", "")
        query = "Wan 3.0 API 报价"
        started = self.client.post("/api/search", json={"query": query})
        self.assertEqual(started.status_code, 202)
        job_path = f"/api/search/{started.json()['id']}"
        for _ in range(50):
            job = self.client.get(job_path).json()
            if job["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "done")
        self.assertEqual(len(job["results"]), 2)
        first = job["results"][0]
        self.assertEqual(first["sources"], ["telegram", "@jisou"])
        self.assertEqual(source.joins, [])
        self.assertEqual(self.client.post(f"{job_path}/join", json={"ids": ["fake"]}).status_code, 400)
        self.assertEqual(source.joins, [])

        messages = self.client.post(f"{job_path}/messages", json={"id": first["id"]}).json()
        self.assertEqual(messages["results"][0]["url"], f"{first['url']}/72")
        self.assertEqual(source.message_searches[0][1][0], query)
        self.assertEqual(source.joins, [])

        for _ in range(2):
            joined = self.client.post(f"{job_path}/join", json={"ids": [first["id"]]}).json()
            self.assertEqual(joined["results"][0]["state"], "pending")
        self.assertEqual(source.joins, [first["url"]])
        self.assertEqual(self.client.get(job_path).json()["results"][0]["join_state"], "pending")


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = Settings(1, "a" * 32, ("@jisou",), "", "https://api.openai.com/v1", "")
        patcher_settings = patch.object(server, "load_settings", return_value=settings)
        patcher_settings.start()
        self.addCleanup(patcher_settings.stop)
        self.saved = {}
        patcher_save = patch.object(server, "save_local_settings", side_effect=self.saved.update)
        patcher_save.start()
        self.addCleanup(patcher_save.stop)
        self.client = self.enterContext(TestClient(server.app, base_url="http://127.0.0.1:8787"))

    def test_invalid_bot_name_is_rejected(self):
        response = self.client.post("/api/settings", json={"search_bots": ["不是机器人"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("jisou", response.json()["detail"])
        self.assertEqual(self.saved, {})

    def test_bots_are_saved_with_at_sign(self):
        response = self.client.post("/api/settings", json={"search_bots": ["@Jisou", "other_bot"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved["TELEGRAM_SEARCH_BOTS"], "@jisou,@other_bot")

    def test_search_option_bounds(self):
        self.assertEqual(self.client.post("/api/settings", json={"max_variants": 99}).status_code, 422)
        self.assertEqual(self.client.post("/api/settings", json={"bot_variants": 0}).status_code, 422)
        self.assertEqual(self.client.post("/api/settings", json={"max_variants": 16}).status_code, 200)
        self.assertEqual(self.saved["TELEGRAM_MAX_VARIANTS"], "16")

    def test_invalid_proxy_is_rejected(self):
        response = self.client.post("/api/settings", json={"telegram_proxy": "not a proxy"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.saved, {})


class ProxyProbeTests(unittest.TestCase):
    def test_proxy_permission_error_falls_back_to_in_memory_choice(self):
        settings = Settings(
            1, "a" * 32, ("@jisou",), "", "https://api.openai.com/v1", "",
            telegram_proxy_url="socks5://127.0.0.1:7897",
        )
        with patch.object(telegram_login, "save_local_settings", side_effect=PermissionError), \
                patch.object(telegram_login, "load_settings", return_value=settings):
            changed = telegram_login.save_proxy_choice(settings, "")
        self.assertEqual(changed.telegram_proxy_url, "")

    def test_closed_port_is_rejected(self):
        # Port 9 (discard) is not listening on a normal Windows machine.
        self.assertFalse(proxy_probe._port_open("127.0.0.1", 9))

    def test_describe(self):
        self.assertEqual(proxy_probe.describe([]), "未检测到")
        self.assertEqual(proxy_probe.describe(["socks5://127.0.0.1:7897"]), "socks5://127.0.0.1:7897")

    def test_ask_proxy_accepts_the_detected_port(self):
        import telegram_login

        settings = Settings(1, "a" * 32, ("@jisou",), "", "https://api.openai.com/v1", "")
        saved = {}
        with patch.object(telegram_login, "direct_works", return_value=False), \
                patch.object(telegram_login, "detect", return_value=["socks5://127.0.0.1:7897"]), \
                patch.object(telegram_login, "input", return_value=""), \
                patch.object(telegram_login, "save_local_settings", side_effect=saved.update), \
                patch.object(telegram_login, "load_settings", return_value=settings):
            telegram_login.ask_proxy(settings)
        self.assertEqual(saved["TELEGRAM_PROXY_URL"], "socks5://127.0.0.1:7897")

    def test_ask_proxy_skips_when_direct_works(self):
        import telegram_login

        settings = Settings(1, "a" * 32, ("@jisou",), "", "https://api.openai.com/v1", "")
        with patch.object(telegram_login, "direct_works", return_value=True), \
                patch.object(telegram_login, "detect") as detect, \
                patch.object(telegram_login, "save_local_settings") as save:
            telegram_login.ask_proxy(settings)
        detect.assert_not_called()
        save.assert_not_called()


class RankingTests(unittest.TestCase):
    def test_preferred_source_wins_when_results_are_otherwise_equal(self):
        hits = [
            {"url": "https://t.me/a_one", "title": "video api chat", "kind": "group",
             "members": 10, "source": "telegram", "query": "video api"},
            {"url": "https://t.me/b_one", "title": "video api chat 2", "kind": "group",
             "members": 10, "source": "@jisou", "query": "video api"},
        ]
        self.assertEqual(rank_candidates("video api", hits)[0]["url"], "https://t.me/a_one")
        self.assertEqual(rank_candidates("video api", hits, "@jisou")[0]["url"], "https://t.me/b_one")
        self.assertEqual(rank_candidates("video api", hits, "telegram")[0]["url"], "https://t.me/a_one")

    def test_title_match_still_outranks_the_preferred_source(self):
        hits = [
            {"url": "https://t.me/titled", "title": "video api chat", "kind": "group",
             "members": 10, "source": "telegram", "query": "video api"},
            {"url": "https://t.me/unrelated", "title": "random name", "kind": "group",
             "members": 10, "source": "@jisou", "query": "video api"},
        ]
        self.assertEqual(rank_candidates("video api", hits, "@jisou")[0]["url"], "https://t.me/titled")

    def test_duplicate_titles_collapse_into_the_largest_copy(self):
        hits = [
            {"url": "https://t.me/small", "title": "Free VPN 推荐", "kind": "channel",
             "members": 10, "source": "@jisou", "query": "free vpn"},
            {"url": "https://t.me/large", "title": "Free VPN 推荐", "kind": "channel",
             "members": 5000, "source": "telegram", "query": "free vpn"},
        ]
        results = rank_candidates("free vpn", hits)
        self.assertEqual([row["url"] for row in results], ["https://t.me/large"])
        self.assertEqual(results[0]["sources"], ["telegram", "@jisou"])


class LanguageTests(unittest.TestCase):
    def test_prompt_uses_the_configured_languages(self):
        prompt = build_prompt(("Russian", "Persian", "Greek"), 3)
        self.assertIn("Russian, Persian, Greek", prompt)
        self.assertIn("up to 3", prompt)
        self.assertNotIn("{", prompt)

    def test_empty_language_list_falls_back_to_english(self):
        prompt = build_prompt((), 5)
        self.assertIn("English", prompt)
        self.assertIn("up to 1", prompt)

    def test_language_list_parses_chinese_separators(self):
        self.assertEqual(
            parse_languages("俄语、波斯语，阿拉伯语; Greek|Thai"),
            ("俄语", "波斯语", "阿拉伯语", "Greek", "Thai"),
        )
        self.assertEqual(parse_languages(""), DEFAULT_LANGUAGES)

    def test_channel_language_is_detected_from_its_own_text(self):
        self.assertIn("cyrillic", channel_scripts("Криптовалюта новости трейдинг сообщество каждый день"))
        self.assertIn("cjk", channel_scripts("开源视频生成接口价格交流群每天更新最新的模型"))
        self.assertIsNone(channel_scripts("ok"))

    def test_queries_follow_the_channel_language_plus_english(self):
        variants = ["开源视频 推荐", "open source video", "генерация видео", "مشاهدة فيلم"]
        russian = order_queries(variants, {"cyrillic", "latin"}, 3)
        self.assertIn("генерация видео", russian)
        self.assertIn("open source video", russian)
        self.assertNotIn("مشاهدة فيلم", russian)

        chinese = order_queries(variants, {"cjk", "kana", "latin"}, 3)
        self.assertEqual(chinese[0], "开源视频 推荐")

    def test_unknown_language_keeps_every_query(self):
        variants = ["一二三", "four", "пять"]
        self.assertEqual(order_queries(variants, None, 5), variants)

    def test_shortest_queries_come_first_inside_a_chat(self):
        variants = ["VPN 推荐 翻墙", "VPN", "VPN бесплатно"]
        ordered = order_queries(variants, {"cyrillic", "latin"}, 3, shortest_first=True)
        self.assertEqual(ordered, ["VPN", "VPN бесплатно", "VPN 推荐 翻墙"])


class QueryLengthTests(unittest.TestCase):
    def test_official_search_keeps_at_most_two_words(self):
        self.assertEqual(_short_query("VPN 推荐 翻墙 工具"), "VPN 推荐")
        self.assertEqual(_short_query("crypto"), "crypto")
        self.assertEqual(_short_query("开源视频生成 API 价格"), "开源视频生成 API")

    def test_promo_block_is_told_apart_from_real_results(self):
        promo = ("🎖【账号批发】QQ号/月亮太阳-皇冠-白号-全网最全QQ包上号售后 "
                 "🎖丫丫源头号商 售QQ、月亮太阳、皇冠包上号 🎖可乐一手号商 批发出售国内APP账号")
        self.assertTrue(_is_promo_block(promo))
        real = "🏞Crypto 週線級別走勢圖 📄crypto 是不是玩几把蛋了 https://t.me/cryptorover"
        self.assertFalse(_is_promo_block(real))
        # A real result that merely mentions a reseller is still a result.
        self.assertFalse(_is_promo_block("账号批发 群 https://t.me/somechannel"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(history, "DB_PATH", Path(self.directory.name) / "search.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_connection = patch.object(history, "_connection", None)
        patcher_connection.start()
        self.addCleanup(patcher_connection.stop)

    def tearDown(self):
        # Windows keeps the file locked while the connection is open.
        if history._connection is not None:
            history._connection.close()
            history._connection = None

    def test_expansion_cache_round_trip(self):
        self.assertIsNone(history.read_expansion("query", "model"))
        history.write_expansion("query", "model", ["a", "b"], True)
        self.assertEqual(history.read_expansion("query", "model"), (["a", "b"], True))
        self.assertIsNone(history.read_expansion("other", "model"))

    def test_search_is_stored_and_reloaded(self):
        hits = [{"url": "https://t.me/one", "title": "One", "query": "one"}]
        history.record_search("job1", "one", "done", ["one", "two"], hits)
        self.assertEqual(history.recent_searches()[0]["id"], "job1")
        stored = history.load_search("job1")
        self.assertEqual(stored["query"], "one")
        self.assertEqual(stored["hits"], hits)
        self.assertIsNone(history.load_search("missing"))

    def test_barren_variants_sink_to_the_end(self):
        hits = [{"query": "good"}, {"query": "good"}]
        for _ in range(history.MIN_USES_BEFORE_DEMOTION):
            history.record_hits(["good", "bad"], hits)
        self.assertEqual(history.demote_barren(["bad", "good"]), ["good", "bad"])
        self.assertEqual(history.demote_barren(["good", "bad"]), ["good", "bad"])


if __name__ == "__main__":
    unittest.main()
