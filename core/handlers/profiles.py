# -*- coding: utf-8 -*-
"""core/handlers/profiles.py —— 画像 / 人物接口（/profiles /profile /persons /person*）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:  # 包模式
    from ..web_response import (confirm_required, confirmed, fail, int_field, missing, ok,
                                result, text_field)
except ImportError:  # 顶层导入（测试）
    from core.web_response import (confirm_required, confirmed, fail, int_field, missing, ok,
                                   result, text_field)

try:  # Phase 17：结构化事件日志（自动脱敏）
    from ..observability import log_event
except ImportError:  # 顶层导入（测试）
    from core.observability import log_event


class ProfileHandlersMixin:
    """画像与人物：读写画像、跨平台身份绑定/解绑/合并。"""

    if TYPE_CHECKING:  # 宿主属性/方法由 main.py 的其他模块提供
        _body: Any
        _bot_id: Any
        _forget_in_amo: Any
        memory: Any
        persons: Any
        profiles: Any

    async def api_profiles(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        keyword = text_field(payload, "keyword", "q")
        return ok({"items": self.profiles.list_profiles(bot_id, keyword, 500)})

    async def api_profile(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = text_field(payload, "uid")
        if not uid:
            return missing("uid")
        if not bot_id:
            # 面板默认聚合所有 bot，先找到这条画像属于哪个 bot，否则会读出空对象
            found = self.profiles.find_profile(uid)
            bot_id = str(found.get("bot_id") or "")
        return ok({
            "bot_id": bot_id,
            "profile": self.profiles.get_profile(bot_id, uid),
            "memories": self.memory.list_memories(bot_id, uid, 200),
        })

    async def api_profile_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        uid = text_field(payload, "uid")
        if not uid:
            return missing("uid")
        profile = self.profiles.save_profile(
            bot_id, uid,
            name=payload.get("name"),
            alias=payload.get("alias"),
            relation=payload.get("relation"),
            remark=payload.get("remark"),
        )
        platform = text_field(payload, "platform")
        if platform:
            self.persons.resolve_person(bot_id, platform, uid, str(profile.get("name") or ""))
        return ok(profile, "已保存")

    async def api_profile_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        uid = text_field(payload, "uid")
        if not uid:
            return missing("uid")
        if not text_field(payload, "bot_id"):
            found = self.profiles.find_profile(uid)
            bot_id = str(found.get("bot_id") or bot_id)
        if not confirmed(payload):
            return confirm_required("删除")
        log_event("info", "profile.delete.request", uid=uid, bot_id=bot_id or "-")
        counts = self.profiles.delete_profile(bot_id, uid)
        # 只删数据侧没用：陪伴插件（amo）本地还留着画像，它每 10 分钟会把
        # 「本地有、数据侧缺」的画像重新推回来，面板上看起来就是「删不掉」。
        self._forget_in_amo(uid)
        return ok(counts, "已删除")

    async def api_persons(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        keyword = text_field(payload, "keyword", "q")
        return ok({"items": self.persons.list_persons(bot_id, keyword, 300)})

    async def api_person(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        pid = int_field(payload, "pid")
        platform = text_field(payload, "platform")
        uid = text_field(payload, "uid")
        if not pid and platform and uid:
            detail = self.persons.person_of(bot_id, platform, uid)
        else:
            detail = self.persons.person_detail(bot_id, pid) if pid else {}
        if not detail:
            return fail("没找到这个人")
        return ok(detail)

    async def api_person_link(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        platform = text_field(payload, "platform")
        uid = text_field(payload, "uid")
        pid = int_field(payload, "pid")
        if not uid:
            return missing("uid")
        new_pid = self.persons.link_identity(
            bot_id, platform, uid, pid, str(payload.get("name") or "")
        )
        return ok({"pid": new_pid}, "已绑定")

    async def api_person_unlink(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        platform = text_field(payload, "platform")
        uid = text_field(payload, "uid")
        if not uid:
            return missing("uid")
        unlinked = self.persons.unlink_identity(bot_id, platform, uid)
        return result(unlinked, {"ok": unlinked}, "已解绑", "没找到这个身份")

    async def api_person_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        pid = int_field(payload, "pid")
        if not pid:
            return missing("pid")
        try:
            log_event("info", "person.delete.probe", pid=pid, bot_id=bot_id or "-",
                      confirm=confirmed(payload))
        except Exception:
            pass
        preview = self.persons.delete_person(bot_id, pid, dry_run=True)
        if not preview.get("ok"):
            return fail("没找到这个人")
        counts = preview.get("counts") or {}
        if not confirmed(payload):
            return fail(
                "删除需要确认：会删掉 %d 条聊天、%d 条记忆、%d 个画像。带 confirm=1 再发一次才执行。"
                % (int(counts.get("messages") or 0), int(counts.get("memories") or 0),
                   int(counts.get("profiles") or 0)))
        log_event("info", "person.delete", pid=pid, bot_id=bot_id or "-",
                  messages=int(counts.get("messages") or 0),
                  memories=int(counts.get("memories") or 0))
        out = self.persons.delete_person(bot_id, pid)
        if not out.get("ok"):
            return fail("删除失败")
        for uid in counts.get("uids") or []:
            try:
                self._forget_in_amo(uid)
            except Exception:
                pass
        return ok(out.get("counts") or {}, "已删除")

    async def api_person_merge(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        keep_pid = int_field(payload, "keep_pid")
        drop_pid = int_field(payload, "drop_pid")
        if not keep_pid or not drop_pid or keep_pid == drop_pid:
            return fail("keep_pid 和 drop_pid 必须是两个不同的人物")
        merged = self.persons.merge_persons(bot_id, keep_pid, drop_pid)
        return result(merged, {"ok": merged}, "已合并", "合并失败")
