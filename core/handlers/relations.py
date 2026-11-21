# -*- coding: utf-8 -*-
"""core/handlers/relations.py —— 关系图接口（/relations /relation/save /relation/delete）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:  # 包模式
    from ..web_response import fail, ok, result, text_field
except ImportError:  # 顶层导入（测试）
    from core.web_response import fail, ok, result, text_field


class RelationHandlersMixin:
    """人物关系：列表（带名字/画像关系覆盖）、保存、删除。"""

    if TYPE_CHECKING:  # 宿主属性/方法由 main.py 的其他模块提供
        _body: Any
        _bot_id: Any
        persons: Any
        profiles: Any

    async def api_relations(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = text_field(payload, "uid")
        items = self.persons.list_relations(bot_id, uid)
        # 面板上直接显名字，不要只给一串 uid
        names = {}
        profiles = []
        try:
            profiles = self.profiles.list_profiles(bot_id, "", 500)
            for row in profiles:
                key = str(row.get("uid") or "")
                if key:
                    names[key] = str(row.get("name") or row.get("alias") or "")
        except Exception:
            names = {}
        names.setdefault("bot", "你")

        # 画像里显式设过的关系也进图：不然把对方设成「恋人」，
        # 图上还停在自动沉淀出来的「熟人」。
        profile_rel = {}
        for row in profiles:
            key = str(row.get("uid") or "")
            rel = str(row.get("relation") or "").strip()
            if key and rel and rel not in ("普通",):
                profile_rel[key] = rel
        if profile_rel:
            pairs = {}
            for pair_item in items:
                pairs[frozenset((str(pair_item.get("a_uid") or ""), str(pair_item.get("b_uid") or "")))] = pair_item
            for key, rel in profile_rel.items():
                if key in ("bot", "self"):
                    continue
                item = pairs.get(frozenset(("bot", key)))
                if item is not None:
                    item["relation"] = rel
                    item["note"] = "画像：关系字段"
                    item["profile_relation"] = rel
                else:
                    items.append({
                        "a_uid": "bot", "b_uid": key, "relation": rel,
                        "note": "画像：关系字段", "profile_relation": rel,
                        "a_name": names.get("bot"), "b_name": names.get(key) or key,
                    })

        for item in items:
            for side in ("a", "b"):
                key = str(item.get(side + "_uid") or "")
                # 画像里没有就保留 store 用人物卡算出来的名字（别覆盖成空）
                if names.get(key):
                    item[side + "_name"] = names[key]
        return ok({"items": items})

    async def api_relation_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        a_uid = text_field(payload, "a_uid")
        b_uid = text_field(payload, "b_uid")
        if not a_uid or not b_uid:
            return fail("需要两个 uid")
        saved = self.persons.save_relation(
            bot_id, a_uid, b_uid,
            str(payload.get("relation") or "朋友"),
            str(payload.get("note") or ""),
        )
        return result(saved, None, "已保存", "保存失败")

    async def api_relation_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        a_uid = text_field(payload, "a_uid")
        b_uid = text_field(payload, "b_uid")
        if not a_uid or not b_uid:
            return fail("需要两个 uid")
        deleted = self.persons.delete_relation(bot_id, a_uid, b_uid)
        return ok(None, "已删除") if deleted else fail("没有这条关系")
