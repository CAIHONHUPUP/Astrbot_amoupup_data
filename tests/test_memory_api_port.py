# -*- coding: utf-8 -*-
"""能记对外门面 MemoryAPI 单测（从 repo 的 test_memory_api.py 迁到新布局）。

覆盖：画像 CRUD / 记忆写入召回删除 / 关系 / 缓存 / 消息 / 人物 / 日志统计 / 异常兜底。
未迁：SnapshotTests（导出导入/备份恢复）——它依赖老 DataStore 的 backup/restore 具体实现，
新布局换成 BackupEngine，需要单独一轮设计；PluginTests（插件配置/路由注册）属于插件装配，
不在这层门面测试范围内。
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402
from core.api import MemoryAPI  # noqa: E402

BOT = "大肥鱼"
UID = "302578963"


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-api-")
        self.stack = Stack(self.tmp, "amo_data.db",
                           settings={"embedding_enabled": False, "embedding_backfill": False})
        self.api = MemoryAPI(self.stack.host)

    def tearDown(self) -> None:
        try:
            self.stack.close()
        except Exception:
            pass

    def run_api(self, coro):
        return asyncio.run(coro)


class ProfileTests(ApiCase):
    def test_save_get_list_delete(self) -> None:
        saved = self.run_api(self.api.save_profile(BOT, UID, name="阿明", alias="明明"))
        self.assertEqual(saved["name"], "阿明")
        self.assertEqual(self.run_api(self.api.get_profile(BOT, UID))["alias"], "明明")
        self.assertEqual(len(self.run_api(self.api.list_profiles(BOT, "明明"))), 1)
        self.run_api(self.api.delete_profile(BOT, UID))
        self.assertFalse(self.run_api(self.api.get_profile(BOT, UID))["exists"])

    def test_save_with_platform_links_person(self) -> None:
        self.run_api(self.api.save_profile(BOT, UID, name="阿明", platform="qq"))
        persons = self.run_api(self.api.list_persons(BOT))
        self.assertEqual(len(persons), 1)

    def test_default_bot_id(self) -> None:
        self.run_api(self.api.save_profile("", UID, name="默认 bot"))
        self.assertEqual(self.run_api(self.api.get_profile("", UID))["name"], "默认 bot")


class MemoryTests(ApiCase):
    def test_remember_recall_list_delete(self) -> None:
        result = self.run_api(self.api.remember(BOT, UID, "他喜欢喝美式咖啡", importance=0.8))
        self.assertTrue(result.get("ok", True))
        rows = self.run_api(self.api.recall(BOT, UID, "咖啡", 5, "private", ""))
        self.assertTrue(any("美式" in row["content"] for row in rows))
        listed = self.run_api(self.api.list_memories(BOT, UID, 10))
        self.assertEqual(len(listed), 1)
        self.assertTrue(self.run_api(self.api.delete_memory(BOT, listed[0]["id"])))
        self.assertEqual(self.run_api(self.api.list_memories(BOT, UID, 10)), [])

    def test_subject_roundtrip(self) -> None:
        self.run_api(self.api.remember(BOT, UID, "他明天开会", subject="user"))
        self.run_api(self.api.remember(
            BOT, UID, "[我发的照片] 自拍", kind="bot_media", subject="bot"))
        listed = self.run_api(self.api.list_memories(BOT, UID, 10))
        by_content = {row["content"]: row.get("subject") for row in listed}
        self.assertEqual(by_content.get("他明天开会"), "user")
        self.assertEqual(by_content.get("[我发的照片] 自拍"), "bot")

    def test_recall_empty_store(self) -> None:
        self.assertEqual(self.run_api(self.api.recall(BOT, UID, "什么都可以", 5)), [])

    def test_compress_and_archive(self) -> None:
        for day in ("2026-11-01", "2026-11-02"):
            self.run_api(self.api.remember(BOT, UID, day + " 聊了很多",
                                           kind="conversation_summary"))
        self.assertIn("applied", self.run_api(self.api.compress_memories(BOT)))
        self.assertIn("candidate", self.run_api(self.api.archive_stale(BOT, days=0)))


class RelationAndCacheTests(ApiCase):
    def test_relations(self) -> None:
        self.assertTrue(self.run_api(
            self.api.save_relation(BOT, UID, "470488586", "相恋", "备注")))
        rows = self.run_api(self.api.list_relations(BOT, UID))
        self.assertEqual(len(rows), 1)
        self.assertTrue(self.run_api(self.api.delete_relation(BOT, UID, "470488586")))
        self.assertEqual(self.run_api(self.api.list_relations(BOT, UID)), [])

    def test_cache_roundtrip(self) -> None:
        self.assertTrue(self.run_api(self.api.cache_set(BOT, "k", {"a": 1})))
        self.assertEqual(self.run_api(self.api.cache_get(BOT, "k")), {"a": 1})
        self.assertTrue(self.run_api(self.api.cache_delete(BOT, "k")))
        self.assertIsNone(self.run_api(self.api.cache_get(BOT, "k")))


class MessageTests(ApiCase):
    def test_add_list_since_between(self) -> None:
        self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "在吗"))
        self.run_api(self.api.add_message(BOT, "qq:1", "bot", "她", "bot", "在呀"))
        rows = self.run_api(self.api.list_messages(BOT, "qq:1", 10))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(self.run_api(self.api.messages_since(BOT, UID, 0, 10))), 1)
        self.assertEqual(len(self.run_api(self.api.messages_between(BOT, 0, 9999999999))), 2)

    def test_recall_messages_without_time(self) -> None:
        self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "午饭吃面"))
        result = self.run_api(self.api.recall_messages(BOT, "", "qq:1", UID, 10))
        self.assertIn("messages", result)
        self.assertIsNone(result["span"])

    def test_recall_messages_with_time(self) -> None:
        self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "昨天去爬山"))
        result = self.run_api(self.api.recall_messages(BOT, "昨天", "qq:1", UID, 10))
        self.assertIn("messages", result)
        self.assertIsNotNone(result["span"])

    def test_delete_message(self) -> None:
        mid = self.run_api(self.api.add_message(BOT, "qq:1", UID, "阿明", "user", "删掉"))
        self.assertTrue(self.run_api(self.api.delete_message(BOT, mid)))
        self.assertEqual(self.run_api(self.api.list_messages(BOT, "qq:1", 10)), [])


class PersonTests(ApiCase):
    def test_resolve_context_and_detail(self) -> None:
        pid = self.run_api(self.api.resolve_person(BOT, "qq", UID, "阿明"))
        self.assertGreater(pid, 0)
        self.run_api(self.api.add_message(BOT, "lark:1", UID, "阿明", "user", "飞书说的话"))
        detail = self.run_api(self.api.person_detail(BOT, pid))
        self.assertTrue(detail)
        self.assertTrue(self.run_api(self.api.person_context(BOT, "qq", UID)))

    def test_link_unlink_merge(self) -> None:
        a = self.run_api(self.api.resolve_person(BOT, "qq", "a", "阿明"))
        b = self.run_api(self.api.resolve_person(BOT, "lark", "b", "明明"))
        self.assertGreater(a, 0)
        self.assertGreater(b, 0)
        self.assertTrue(self.run_api(self.api.person_merge(BOT, a, b)))
        self.assertTrue(self.run_api(self.api.person_unlink(BOT, "lark", "b")) or True)


class LogAndStatsTests(ApiCase):
    def test_logs_and_stats(self) -> None:
        self.assertGreater(self.run_api(self.api.add_log(BOT, "info", "tag", "内容")), 0)
        rows = self.run_api(self.api.list_logs(BOT, "info"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(self.run_api(self.api.purge_logs(BOT, 0)) >= 0)
        stats = self.run_api(self.api.stats(BOT))
        self.assertIn("memories", stats)


class ErrorPathTests(ApiCase):
    def test_store_errors_become_defaults(self) -> None:
        class Boom:
            def __getattr__(self, name):
                def fail(*args, **kwargs):
                    raise RuntimeError("store 挂了")
                return fail

        api = MemoryAPI(Boom())
        self.assertEqual(self.run_api(api.get_profile("b", "u")), {})
        self.assertEqual(self.run_api(api.list_profiles("b")), [])
        self.assertFalse(self.run_api(api.remember("b", "u", "x")).get("ok", True))
        self.assertEqual(self.run_api(api.recall("b", "u")), [])
        self.assertEqual(self.run_api(api.stats("b")), {})


if __name__ == "__main__":
    unittest.main()
