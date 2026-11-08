"""我，deepseek —— WebUI 管理页面的数据接口。

宿主提供的两条扩展通道里，这里选的是**自定义页面**（``webui.json`` 里
``mode: "custom"``）：

- 声明式页面（declarative）的 ``input`` 节点只能写字面量、``select`` 的选项
  只能静态声明，做不到「选中一条就带出它的内容来改」；
- 自定义页面是独立 HTML，通过 ``window.maibot.query/action`` 调本文件里的
  ``@API`` 方法，可以做出带预填表单、搜索、分页的真正编辑器。

宿主侧的调用链（MaiBot 1.3.9 实测）：

    webui.json 里 queries/actions 的 api 短名
      → APIRegistry.get_api(plugin_id, name, version)   # 全名是 plugin_id.name
      → supervisor.invoke_api(plugin_id, handler_name, args)
      → Runner 侧 _handle_invoke：handler_method(**args)
      → 本文件的 async 方法

因此：

- 方法名不必等于 API 名（宿主用 ``handler_name`` 找方法），但为了可读性这里保持一致；
- 参数必须**逐个声明**在 ``webui.json`` 的 ``parameters`` 里，宿主会先校验；
  未声明的参数直接 422，方法签名对不上的 kwargs 会 TypeError；
- 返回值必须是 JSON 可序列化、且 ≤512 KiB（宿主 ``validate_json``）。

约定：业务错误**不抛异常**（抛异常会让宿主返回 502 并只给一句「插件 API 执行
失败」），而是返回 ``{"ok": false, "error": "..."}``，页面据此弹提示。
"""

from __future__ import annotations

import math
import time
from typing import Any

try:  # 正常安装环境
    from maibot_sdk import API
except Exception:  # pragma: no cover - 仅用于脱离 Runner 的本地导入/测试
    def API(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
        def decorator(func):
            return func

        return decorator

from .cg_keepsake import (
    KIND_LABELS as KEEPSAKE_KIND_LABELS,
    MAX_ITEMS as KEEPSAKE_MAX_ITEMS,
    STATUS_KEPT,
    STATUS_RELEASED,
)
from .cg_prefs import (
    KINDS,
    KIND_LABELS,
    SOURCE_LABELS,
    SOURCE_MANUAL,
    clamp_strength,
    normalize_kind,
    normalize_topic,
    prefs_key,
)
from .cg_proactive import KIND_LABELS as SPARK_KIND_LABELS
from .cg_store import CATEGORIES, CATEGORY_LABELS, STATUSES, STATUS_LABELS
from .cg_thought import KIND_DISSENT, STATUS_ACTIVE, STATUS_VOICED

#: 单页最多返回多少条。太大容易撞宿主的 512 KiB 返回值上限。
MAX_PAGE_SIZE = 200

DEFAULT_PAGE_SIZE = 50


def _clamp_page_size(value: Any) -> int:
    """把 limit 夹到 [1, MAX_PAGE_SIZE]。"""

    try:
        size = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if size <= 0:
        return DEFAULT_PAGE_SIZE
    return min(size, MAX_PAGE_SIZE)


def _clamp_offset(value: Any) -> int:
    try:
        offset = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, offset)


def _item_payload(row: dict[str, Any]) -> dict[str, Any]:
    """把一条 DB 行转成页面要的形状。

    时间戳保持 Unix 秒（float），由页面按浏览器本地时区格式化；这样插件侧
    不用猜宿主时区。
    """

    category = str(row.get("category") or "")
    status = str(row.get("status") or "")
    return {
        "id": int(row.get("id") or 0),
        "session_id": str(row.get("session_id") or ""),
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "content": str(row.get("content") or ""),
        "salience": round(float(row.get("salience") or 0.0), 3),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "first_seen_ts": float(row.get("first_seen_ts") or 0.0),
        "last_seen_ts": float(row.get("last_seen_ts") or 0.0),
        "expires_at": float(row.get("expires_at") or 0.0),
        "source_count": int(row.get("source_count") or 0),
        "created_at": float(row.get("created_at") or 0.0),
        "updated_at": float(row.get("updated_at") or 0.0),
        # 第 32 轮新增的三个：置顶、可信度、遗忘时间。
        "pinned": bool(row.get("pinned") or 0),
        "truth_score": round(float(row.get("truth_score") if row.get("truth_score") is not None else 1.0), 3),
        "doubted": float(row.get("truth_score") if row.get("truth_score") is not None else 1.0) < 0.5,
        "last_recalled_ts": float(row.get("last_recalled_ts") or 0.0),
        "forgotten_at": float(row.get("forgotten_at") or 0.0),
        # 注入轮换的三个账目（第 45 轮）：页面上要能看出「这条老被用」。
        # 没有它们，轮换就只能靠感受判断，出了问题也没法排查。
        "last_injected_ts": float(row.get("last_injected_ts") or 0.0),
        "inject_streak": int(row.get("inject_streak") or 0),
        "inject_count": int(row.get("inject_count") or 0),
    }


class MemoryAdminMixin:
    """给插件类提供 WebUI 管理接口。

    这些方法依赖宿主注入的 ``self._store``（``GapStore``）。写成 mixin 是为了
    不把 ``plugin.py`` 撑大：``collect_components`` 走 ``dir(instance)``，
    继承来的方法一样能被收集到。
    """

    # ── 内部工具 ──

    def _webui_store(self):
        """取存储层；未初始化时抛异常（宿主会记一条 error 日志）。"""

        store = getattr(self, "_store", None)
        if store is None:
            raise RuntimeError("存储尚未初始化，插件可能还没加载完成")
        return store

    def _webui_people_config(self):
        """取 ``[people]`` 配置（拿身份表与分级表用）。取不到就返回 ``None``。"""

        try:
            cfg = getattr(self, "config", None)
        except Exception:  # noqa: BLE001 - 配置未就绪时退回默认表
            return None
        return getattr(cfg, "people", None)

    def _webui_roles(self) -> list[dict[str, str]]:
        """当前生效的身份表（配置页可改，所以不能再用硬编码的四档）。"""

        from .cg_person import roles_from_config

        return roles_from_config(getattr(self._webui_people_config(), "roles", None))

    def _webui_levels(self) -> list[dict[str, Any]]:
        """当前生效的好感度分级表（阈值由用户自己定）。"""

        from .cg_person import intimacy_levels_from_config

        return intimacy_levels_from_config(
            getattr(self._webui_people_config(), "intimacy_levels", None)
        )

    def _webui_axes(self) -> list[dict[str, Any]]:
        """关系六维的定义表（页面画滑杆用）。"""

        from .cg_person import AXES

        return [dict(item) for item in AXES]

    # ── 查询 ──

    @API("mem_stats", description="记忆总量与状态分布", version="1")
    async def mem_stats(self) -> dict[str, Any]:
        """返回全局统计：条目按状态/类别分布、会话数、抽取与注入次数。"""

        try:
            store = self._webui_store()
            stats = store.global_stats()
            by_category: dict[str, int] = {}
            rows = store.conn.execute(
                "SELECT category, COUNT(*) AS n FROM gap_items GROUP BY category"
            ).fetchall()
            for row in rows:
                by_category[str(row["category"])] = int(row["n"])
            injections = store.conn.execute(
                "SELECT COUNT(*) AS n, AVG(elapsed_ms) AS avg_ms FROM gap_injections"
            ).fetchone()
            return {
                "ok": True,
                "sessions": int(stats.get("sessions") or 0),
                "items_by_status": {
                    str(key): int(value)
                    for key, value in (stats.get("items_by_status") or {}).items()
                },
                "items_by_category": by_category,
                "items_pinned": int(stats.get("items_pinned") or 0),
                "items_doubted": int(stats.get("items_doubted") or 0),
                "extractions": int(stats.get("extractions") or 0),
                "extractions_ok": int(stats.get("extractions_ok") or 0),
                "injections": int(injections["n"] or 0) if injections is not None else 0,
                "avg_injection_ms": round(float(injections["avg_ms"] or 0.0), 3) if injections is not None else 0.0,
                "categories": [
                    {"value": value, "label": CATEGORY_LABELS.get(value, value)} for value in CATEGORIES
                ],
                "statuses": [
                    {"value": value, "label": STATUS_LABELS.get(value, value)} for value in STATUSES
                ],
            }
        except Exception as exc:  # noqa: BLE001 - 业务错误回传给页面，不抛给宿主
            return {"ok": False, "error": f"读取统计失败：{exc}"}

    @API("mem_sessions", description="列出所有会话及其条目计数", version="1")
    async def mem_sessions(self) -> dict[str, Any]:
        """返回会话列表，供页面填充会话下拉框。"""

        try:
            store = self._webui_store()
            sessions = []
            for row in store.session_overview():
                session_id = str(row.get("session_id") or "")
                label = str(row.get("group_name") or "") or str(row.get("user_nickname") or "")
                sessions.append(
                    {
                        "session_id": session_id,
                        "platform": str(row.get("platform") or ""),
                        "chat_type": str(row.get("chat_type") or ""),
                        "group_id": str(row.get("group_id") or ""),
                        "group_name": str(row.get("group_name") or ""),
                        "user_nickname": str(row.get("user_nickname") or ""),
                        "label": label or session_id,
                        "last_seen_ts": float(row.get("last_seen_ts") or 0.0),
                        "pending_count": int(row.get("pending_count") or 0),
                        "active_count": int(row.get("active_count") or 0),
                        "resolved_count": int(row.get("resolved_count") or 0),
                        "expired_count": int(row.get("expired_count") or 0),
                        "forgotten_count": int(row.get("forgotten_count") or 0),
                        "pinned_count": int(row.get("pinned_count") or 0),
                        "total_count": int(row.get("total_count") or 0),
                    }
                )
            return {"ok": True, "sessions": sessions}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取会话失败：{exc}"}

    @API("mem_items", description="按条件分页查询记忆条目", version="1")
    async def mem_items(
        self,
        session_id: str = "",
        status: str = "",
        category: str = "",
        search: str = "",
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询条目。

        Args:
            session_id: 会话 id；空串表示不限会话。
            status: 状态过滤；空串表示全部。
            category: 类别过滤；空串表示全部。
            search: 内容子串；空串表示不筛。
            limit: 单页条数，1..200。
            offset: 偏移量。

        Returns:
            ``{ok, items, total, limit, offset, has_more}``。
        """

        try:
            store = self._webui_store()
            size = _clamp_page_size(limit)
            start = _clamp_offset(offset)
            rows, total = store.list_items(
                session_id=str(session_id or ""),
                status=str(status or ""),
                category=str(category or ""),
                search=str(search or ""),
                limit=size,
                offset=start,
            )
            return {
                "ok": True,
                "items": [_item_payload(row) for row in rows],
                "total": int(total),
                "limit": size,
                "offset": start,
                "has_more": start + len(rows) < total,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"查询失败：{exc}"}

    # ── 修改 ──

    @API("mem_update", description="修改一条记忆的内容/类别/显著度/状态", version="1")
    async def mem_update(
        self,
        item_id: int,
        content: str = "",
        category: str = "",
        salience: float = -1.0,
        status: str = "",
        pinned: bool | None = None,
        truth_score: float = -1.0,
    ) -> dict[str, Any]:
        """局部更新一条记忆；空串 / 负数表示「这项不改」。

        Args:
            item_id: 条目 id。
            content: 新内容；空串表示不改。
            category: 新类别；空串表示不改。
            salience: 新显著度；负数表示不改。
            status: 新状态；空串表示不改。
            pinned: 置顶开关；``None`` 表示不改。
            truth_score: 可信度；负数表示不改。

        Returns:
            ``{ok, item}`` 或 ``{ok: false, error}``。
        """

        try:
            store = self._webui_store()
            payload: dict[str, Any] = {}
            text = str(content or "").strip()
            if text:
                payload["content"] = text
            category_value = str(category or "").strip()
            if category_value:
                payload["category"] = category_value
            status_value = str(status or "").strip()
            if status_value:
                payload["status"] = status_value
            if salience is not None and float(salience) >= 0.0:
                payload["salience"] = float(salience)
            if truth_score is not None and float(truth_score) >= 0.0:
                payload["truth_score"] = float(truth_score)
            if pinned is not None:
                payload["pinned"] = bool(pinned)

            if not payload:
                return {"ok": False, "error": "没有要修改的字段"}

            row = store.update_item(int(item_id), **payload)
            if row is None:
                return {"ok": False, "error": f"条目 {item_id} 不存在"}
            return {"ok": True, "item": _item_payload(row)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"保存失败：{exc}"}

    @API("mem_pin", description="置顶或取消置顶一条记忆", version="1")
    async def mem_pin(self, item_id: int, pinned: bool = True) -> dict[str, Any]:
        """置顶 / 取消置顶。

        置顶的条目不会被衰减、不会被遗忘、也不会被条数上限挤掉 —— 用户
        第 32 轮明确要求「置顶的记忆不会被遗忘」。
        """

        try:
            store = self._webui_store()
            if not store.set_pinned(int(item_id), bool(pinned)):
                return {"ok": False, "error": f"条目 {item_id} 不存在"}
            row = store.get_item(int(item_id))
            return {"ok": True, "item": _item_payload(row) if row else {"id": int(item_id)}}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"置顶失败：{exc}"}

    @API("mem_recall", description="把一条遗忘的记忆重新想起来", version="1")
    async def mem_recall(self, item_id: int) -> dict[str, Any]:
        """手动「想起来」一条遗忘的记忆。

        自动唤起靠对方原话的相似度匹配（见 ``plugin._render_recalled``），
        但用户也可能想手动把它捞回来。
        """

        try:
            store = self._webui_store()
            if not store.recall_item(int(item_id)):
                return {"ok": False, "error": f"条目 {item_id} 不存在"}
            row = store.get_item(int(item_id))
            return {"ok": True, "item": _item_payload(row) if row else {"id": int(item_id)}}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"唤起失败：{exc}"}

    @API("mem_create", description="手工新增一条记忆", version="1")
    async def mem_create(
        self,
        session_id: str,
        category: str,
        content: str,
        salience: float = 0.6,
    ) -> dict[str, Any]:
        """新增一条记忆（不做相似度去重，人工操作要所见即所得）。"""

        try:
            store = self._webui_store()
            if not str(session_id or "").strip():
                return {"ok": False, "error": "必须先选择会话"}
            item_id = store.insert_item(
                str(session_id).strip(),
                str(category or "").strip(),
                str(content or ""),
                salience=float(salience),
            )
            row = store.get_item(item_id)
            return {"ok": True, "item": _item_payload(row) if row else {"id": item_id}}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"新增失败：{exc}"}

    @API("mem_delete", description="删除一条记忆", version="1")
    async def mem_delete(self, item_id: int) -> dict[str, Any]:
        """物理删除一条记忆。"""

        try:
            store = self._webui_store()
            deleted = store.delete_item(int(item_id))
            if not deleted:
                return {"ok": False, "error": f"条目 {item_id} 不存在"}
            return {"ok": True, "deleted": int(item_id)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"删除失败：{exc}"}

    @API("mem_purge_session", description="清空某个会话的全部记忆", version="1")
    async def mem_purge_session(self, session_id: str) -> dict[str, Any]:
        """删除某会话下的全部条目（保留会话本身）。"""

        try:
            store = self._webui_store()
            target = str(session_id or "").strip()
            if not target:
                return {"ok": False, "error": "必须指定会话"}
            removed = store.delete_session_items(target)
            return {"ok": True, "removed": int(removed)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"清空失败：{exc}"}

    # ── 情绪 ──

    def _webui_mood(self):
        """取情绪引擎；未初始化时抛异常。"""

        mood = getattr(self, "_mood", None)
        if mood is None:
            raise RuntimeError("情绪引擎尚未初始化，插件可能还没加载完成")
        return mood

    def _mood_payload(self, state: Any) -> dict[str, Any]:
        """把 ``MoodState`` 转成页面要的形状。"""

        from .cg_mood import (
            EMOTION_LABELS,
            EMOTIONS,
            desire_for,
            effort_for,
            guidance_for,
            label_for,
            length_for,
            primitives_of,
            render_block,
            tone_for,
        )

        primitives = primitives_of(state)
        baselines = self._mood_baselines()
        valence = float(getattr(state, "valence", 0.0))
        arousal = float(getattr(state, "arousal", 0.0))
        energy = float(getattr(state, "energy", 0.0))
        exhausted = bool(getattr(state, "exhausted", False))
        label = (
            str(getattr(state, "label", "") or "")
            or label_for(primitives, energy, exhausted=exhausted, baselines=baselines)
        )
        return {
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "energy": round(energy, 3),
            "exhausted": exhausted,
            "label": label,
            "reason": str(getattr(state, "reason", "") or ""),
            "updated_at": float(getattr(state, "updated_at", 0.0) or 0.0),
            "guidance": guidance_for(
                primitives,
                energy,
                exhausted=exhausted,
                baselines=baselines,
                arousal=arousal,
            ),
            # 页面要分块显示，所以单独给一份「只有语气、不含精力」的版本。
            # 否则面板把 guidance 和 effort 并排显示时，精力那两句会出现两遍。
            # exhausted 的判断要和 guidance_for 一致：energy 归零也算耗尽。
            "tone": tone_for(
                primitives,
                exhausted=exhausted or energy <= 0.0,
                baselines=baselines,
                arousal=arousal,
            ),
            "effort": effort_for(energy, exhausted=exhausted),
            # 页面把「精力」拆成两块显示：想不想聊 + 说多少，比一句合成的好懂。
            "desire": desire_for(energy, exhausted=exhausted),
            "length": length_for(energy, exhausted=exhausted),
            "block": render_block(state, baselines=baselines),
            # 八个情绪基元：页面把它们画成八条进度条。
            "primitives": {name: round(float(primitives.get(name, 0.0)), 3) for name in EMOTIONS},
            "primitive_labels": {name: EMOTION_LABELS[name] for name in EMOTIONS},
            "baselines": {name: round(float(baselines.get(name, 0.0)), 3) for name in EMOTIONS},
        }

    def _mood_baselines(self) -> dict[str, float]:
        """取当前配置下的八个基元基线（页面要拿它画参考线）。"""

        mood = getattr(self, "_mood", None)
        if mood is not None and hasattr(mood, "baselines"):
            try:
                return dict(mood.baselines())
            except Exception:  # noqa: BLE001
                pass
        from .cg_mood import DEFAULT_PRIMITIVES

        return dict(DEFAULT_PRIMITIVES)

    @API("mem_mood", description="读取 Bot 当前的情绪状态", version="1")
    async def mem_mood(self) -> dict[str, Any]:
        """返回当前心境、对应的行为指令，以及会注入的原始文本。"""

        try:
            mood = self._webui_mood()
            state = mood.snapshot()
            payload = self._mood_payload(state)
            payload["ok"] = True
            payload["enabled"] = bool(mood.enabled)
            return payload
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取情绪失败：{exc}"}

    @API("mem_mood_history", description="读取情绪变化历史", version="1")
    async def mem_mood_history(self, limit: int = 60) -> dict[str, Any]:
        """按时间倒序返回情绪历史，供页面画曲线。"""

        from .cg_mood import EMOTIONS

        try:
            store = self._webui_store()
            size = max(1, min(500, int(limit)))
            rows = store.mood_history(limit=size)
            points = []
            for row in rows:
                point: dict[str, Any] = {
                    "ts": float(row.get("ts") or 0.0),
                    "valence": round(float(row.get("valence") or 0.0), 3),
                    "arousal": round(float(row.get("arousal") or 0.0), 3),
                    "energy": round(float(row.get("energy") or 0.0), 3),
                    "label": str(row.get("label") or ""),
                    "source": str(row.get("source") or ""),
                    "reason": str(row.get("reason") or ""),
                }
                point["primitives"] = {
                    name: round(float(row.get(f"emotion_{name}") or 0.0), 3) for name in EMOTIONS
                }
                points.append(point)
            return {"ok": True, "points": points, "total": len(points)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取情绪历史失败：{exc}"}

    @API("mem_mood_set", description="手动调整 Bot 的情绪", version="1")
    async def mem_mood_set(
        self,
        valence: float = -999.0,
        arousal: float = -999.0,
        energy: float = -999.0,
        label: str = "",
        reason: str = "",
        joy: float = -999.0,
        anger: float = -999.0,
        anxiety: float = -999.0,
        sadness: float = -999.0,
        interest: float = -999.0,
        calm: float = -999.0,
        disgust: float = -999.0,
        surprise: float = -999.0,
    ) -> dict[str, Any]:
        """人工覆盖情绪。

        负数是「不改这一维」的哨兵值：合法的基元与 arousal/energy 最小都是 0，
        所以用 -999 表示「未提供」不会和真实值冲突。
        ``valence`` / ``arousal`` 是派生量，现在只作为兼容入口保留：
        传了也会被忽略（改它们没有意义，基元才是真值）。
        """

        from .cg_mood import EMOTIONS

        try:
            mood = self._webui_mood()
            store = self._webui_store()
            raw = {
                "joy": joy,
                "anger": anger,
                "anxiety": anxiety,
                "sadness": sadness,
                "interest": interest,
                "calm": calm,
                "disgust": disgust,
                "surprise": surprise,
            }
            primitives: dict[str, float] = {}
            for name in EMOTIONS:
                value = float(raw.get(name, -999.0))
                if value > -900:
                    primitives[name] = value
            kwargs: dict[str, Any] = {"label": label, "reason": reason}
            if primitives:
                kwargs["primitives"] = primitives
            if float(energy) > -900:
                kwargs["energy"] = float(energy)
            if not primitives and "energy" not in kwargs:
                if not str(label or "").strip() and not str(reason or "").strip():
                    return {"ok": False, "error": "没有要修改的字段"}

            state = mood.set_manual(**kwargs)
            try:
                store.record_mood(
                    valence=state.valence,
                    arousal=state.arousal,
                    energy=state.energy,
                    exhausted=bool(getattr(state, "exhausted", False)),
                    label=state.label,
                    source="manual",
                    reason=state.reason,
                    **{name: getattr(state, name) for name in EMOTIONS},
                )
                mood.persist(store)
            except Exception as exc:  # noqa: BLE001 - 落盘失败也返回新状态
                payload = self._mood_payload(state)
                payload["ok"] = True
                payload["warning"] = f"已生效但未写入数据库：{exc}"
                return payload

            payload = self._mood_payload(state)
            payload["ok"] = True
            return payload
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"调整情绪失败：{exc}"}

    @API("mem_mood_reset", description="把情绪恢复到基线", version="1")
    async def mem_mood_reset(self) -> dict[str, Any]:
        """清空情绪，回到配置里的基线值。"""

        from .cg_mood import EMOTIONS

        try:
            mood = self._webui_mood()
            store = self._webui_store()
            state = mood.reset()
            try:
                store.record_mood(
                    valence=state.valence,
                    arousal=state.arousal,
                    energy=state.energy,
                    exhausted=bool(getattr(state, "exhausted", False)),
                    label=state.label,
                    source="reset",
                    reason="手动重置",
                    **{name: getattr(state, name) for name in EMOTIONS},
                )
                mood.persist(store)
            except Exception as exc:  # noqa: BLE001
                payload = self._mood_payload(state)
                payload["ok"] = True
                payload["warning"] = f"已生效但未写入数据库：{exc}"
                return payload
            payload = self._mood_payload(state)
            payload["ok"] = True
            return payload
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"重置情绪失败：{exc}"}

    # ── 人物认知 ──

    def _person_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        """把一行人物记录转成页面能直接用的形状。"""

        from .cg_person import (
            AXIS_DEFAULTS,
            AXIS_KEYS,
            AXIS_LABELS,
            affection_from_axes,
            axes_of,
            contrast_lines,
            describe_axes,
            dominant_axes,
            has_observed_axes,
            intimacy_for,
            role_label,
        )

        roles = self._webui_roles()
        levels = self._webui_levels()
        role = str(row.get("relation_role") or "stranger")
        axes = axes_of(row)
        # 好感度是六维算出来的摘要（用户第 34 轮）。这里**按六维重算**而不是
        # 直接用库里那一列：库里的值可能是老版本留下的、或者被手动设过的，
        # 和当前六维不一致时页面显示会自相矛盾。
        affection = affection_from_axes(axes)
        intimacy, intimacy_label = intimacy_for(affection, levels)
        return {
            "person_key": str(row.get("person_key") or ""),
            "platform": str(row.get("platform") or ""),
            "user_id": str(row.get("user_id") or ""),
            "display_name": str(row.get("display_name") or ""),
            "relation_role": role,
            "relation_role_label": role_label(role, roles),
            "role_confidence": round(float(row.get("role_confidence") or 0.0), 3),
            "role_source": str(row.get("role_source") or "auto"),
            "is_manual": str(row.get("role_source") or "") == "manual",
            "affection": round(affection, 1),
            "intimacy": intimacy,
            "intimacy_label": intimacy_label,
            # 六维：页面要画六个滑杆，所以连默认值和标签一起下发，
            # 前端不用自己维护一份常量表（改了 cg_person 就自动同步）。
            "axes": {key: round(float(axes.get(key, AXIS_DEFAULTS[key])), 3) for key in AXIS_KEYS},
            "axes_text": describe_axes(axes),
            # 六维是自动跟着聊天走，还是被用户手动锁住了。页面据此显示
            # 「跟随聊天」开关 —— 不暴露这个，用户会以为手动调完又被程序
            # 悄悄改回来（第 43 轮报的就是这个体验）。
            "axes_source": str(row.get("axes_source") or "auto"),
            "axes_manual": str(row.get("axes_source") or "") == "manual",
            # 矛盾组合描述：这是页面上最有信息量的一块 —— 「你们混得很熟，
            # 但有些话你还是不跟他说」比六个百分比好读得多。
            # 但只有**真实观测到**的六维才谈得上矛盾：老库升级后六维是从一个
            # 好感度标量反推出来的，所谓「冲突」全是反推公式的副产品（认识最久
            # 的人被说成「他刚跟你搭上话」）。注入侧 render_person_block 已经挡了
            # 这一层，页面这边必须同样挡住，否则用户在面板上会看到一句假话。
            "contrasts": contrast_lines(axes, limit=3) if has_observed_axes(row) else [],
            "dominant": [
                {"key": key, "delta": round(delta, 3), "higher": higher}
                for key, delta, higher in dominant_axes(axes, limit=3)
            ],
            "emotional_impact": round(float(row.get("emotional_impact") or 0.0), 3),
            "tags": [str(item) for item in (row.get("tags") or [])],
            "last_seen_ts": float(row.get("last_seen_ts") or 0.0),
            "updated_at": float(row.get("updated_at") or 0.0),
        }

    @API("mem_people", description="列出插件记住的人物与关系", version="1")
    async def mem_people(self, limit: int = 200) -> dict[str, Any]:
        """返回人物列表，供页面渲染。

        ``roles`` / ``levels`` 一起下发：页面的身份下拉和「好感度到多少算哪一档」
        必须跟着配置页走，不能写死 —— 用户第 33 轮明确要求身份可以自由配置、
        阈值也可以调。
        """

        try:
            store = self._webui_store()
            size = max(1, min(500, int(limit)))
            rows = store.list_people(limit=size)
            return {
                "ok": True,
                "people": [self._person_payload(row) for row in rows],
                "total": len(rows),
                "roles": [
                    {"value": item["key"], "label": item["label"]}
                    for item in self._webui_roles()
                ],
                "levels": [
                    {
                        "value": item["key"],
                        "label": item["label"],
                        "min_affection": round(float(item["min_affection"]), 1),
                    }
                    for item in self._webui_levels()
                ],
                # 六维的元数据也下发：页面要画六个滑杆，标签和说明文字
                # 应该跟 cg_person 里的定义保持同一份真相，不要前端再抄一遍。
                "axes": [
                    {
                        "key": item["key"],
                        "label": item["label"],
                        "hint": item["hint"],
                        "default": round(float(item["default"]), 3),
                    }
                    for item in self._webui_axes()
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取人物失败：{exc}"}

    @API("mem_person_update", description="修改一个人的关系/好感度/六维/标签", version="1")
    async def mem_person_update(
        self,
        person_key: str,
        relation_role: str = "",
        affection: float = -999.0,
        tags: list[str] | None = None,
        display_name: str = "",
        axes: dict[str, float] | None = None,
        follow_chat: bool = False,
    ) -> dict[str, Any]:
        """改人物。

        ``relation_role`` 非空时会把来源标成 ``manual`` —— 之后自动判断改不动它，
        这正是用户要的「手动配置优先」。``affection`` 用 -999 当「不改」哨兵。

        ``axes`` 是用户第 34 轮加的：六维才是真相，好感度只是摘要。传了这个
        就按六维重算好感度，**并且忽略同一次请求里的 ``affection``** —— 两者
        一起给必然自相矛盾，六维更具体，以它为准。

        手动传 ``axes`` 或 ``affection`` 会把 ``axes_source`` 标成 ``manual``：
        用户手调过的六维不再被自动抽取和衰减覆盖（第 43 轮修的「我调的值自己
        变回去了」）。``follow_chat=True`` 是反向开关，把六维交还自动流程 ——
        没有这一步的话手动锁是单向的，用户只能改新值而没法解除锁定。

        ``tags`` 是**整份替换**而不是追加：用户第 32 轮明确说过「无法修改或
        删除已有备注，只能新加」，所以页面把编辑后的完整列表发过来，这里直接
        覆盖。``None`` 表示这次不动标签（与「清空成 []」区分开）。
        """

        from .cg_person import (
            AXIS_KEYS,
            affection_from_axes,
            axes_from_affection,
            axes_of,
            clamp_affection,
            clamp_axis,
            normalize_role,
            normalize_tags,
        )

        try:
            store = self._webui_store()
            key = str(person_key or "").strip()
            if not key:
                return {"ok": False, "error": "缺少 person_key"}
            row = store.get_person(key)
            if row is None:
                return {"ok": False, "error": f"没找到 {key}"}

            changed = False
            if display_name.strip():
                row["display_name"] = display_name.strip()[:16]
                changed = True

            role = ""
            if str(relation_role or "").strip():
                role = normalize_role(relation_role, self._webui_roles())
                # 传了身份却认不出来，必须明确拒绝。原来写成 ``if role:`` 直接跳过，
                # 用户选了配置页里已经删掉的旧身份时会得到「没有要改的字段」，
                # 以为保存成功了其实什么都没变。
                if not role:
                    known = " / ".join(
                        f"{item['key']}（{item['label']}）" for item in self._webui_roles()
                    )
                    return {
                        "ok": False,
                        "error": f"不认识的身份证 {relation_role!r}。当前可选：{known}",
                    }
            if role:
                row["relation_role"] = role
                row["role_confidence"] = 1.0
                row["role_source"] = "manual"
                changed = True

            # 六维优先：给了 axes 就按它重算好感度，不再看 affection。
            # 注意这里**不能**用 ``normalize_axes_delta`` —— 那个是给抽取结果
            # 用的「变化量」归一化，会把 familiarity 直接丢掉；页面上的滑杆是
            # 绝对值，六维都该能手动改（包括熟悉度：用户可能想手动标记「其实
            # 认识很久了」）。
            pending_axes = None
            if axes is not None:
                cleaned: dict[str, float] = {}
                for axis_key, axis_value in dict(axes).items():
                    name = str(axis_key or "").strip().lower()
                    if name not in AXIS_KEYS:
                        continue
                    try:
                        number = float(axis_value)
                    except (TypeError, ValueError):
                        continue
                    if math.isnan(number) or math.isinf(number):
                        continue
                    cleaned[name] = clamp_axis(number, name)
                if cleaned:
                    pending_axes = cleaned

            if pending_axes is not None:
                current = axes_of(row)
                for axis_key, axis_value in pending_axes.items():
                    current[axis_key] = clamp_axis(axis_value, axis_key)
                row["axes"] = current
                row["axes_source"] = "manual"
                row["affection"] = affection_from_axes(current)
                changed = True
            elif abs(float(affection) - (-999.0)) > 1e-9:
                # 只给了好感度：反摊到六维，否则下一次抽取算六维会把它覆盖。
                value = clamp_affection(affection)
                row["axes"] = axes_from_affection(value)
                row["axes_source"] = "manual"
                row["affection"] = value
                changed = True
            elif follow_chat:
                # 反向开关：把六维交还自动流程。下一次抽取起会重新跟着聊天走。
                if str(row.get("axes_source") or "") != "auto":
                    row["axes_source"] = "auto"
                    changed = True

            if tags is not None:
                cleaned = normalize_tags(tags)
                if cleaned != [str(item) for item in (row.get("tags") or [])]:
                    row["tags"] = cleaned
                    changed = True

            if not changed:
                return {"ok": False, "error": "没有要改的字段"}

            store.upsert_person(row)
            fresh = store.get_person(key) or row
            return {"ok": True, "person": self._person_payload(fresh)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"修改人物失败：{exc}"}

    @API("mem_person_create", description="手动新增一个认识的人", version="1")
    async def mem_person_create(
        self,
        user_id: str,
        display_name: str = "",
        platform: str = "",
        relation_role: str = "",
        affection: float = 0.0,
        tags: list[str] | None = None,
        axes: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """手动新增人物。

        用户明确要求「只需要给『认识的人』增加一个『新增』功能，让用户自行写入
        即可」，不要读好感度插件。所以这里只要 ``user_id``，其余都可省：
        平台留空时按现有会话里出现过的平台猜（只有一个就用它，多个就不猜），
        角色留空就是「让插件自己判断」，好感度默认 0。

        给了 ``axes`` 就以六维为准（与 ``mem_person_update`` 一致）：好感度
        由六维算出，``affection`` 参数被忽略。
        """

        from .cg_person import (
            AXIS_KEYS,
            affection_from_axes,
            axes_from_affection,
            clamp_affection,
            clamp_axis,
            normalize_role,
            normalize_tags,
            person_key,
        )

        try:
            store = self._webui_store()
            uid = " ".join(str(user_id or "").split())
            if not uid:
                return {"ok": False, "error": "请填写对方的 user_id"}

            platform_text = " ".join(str(platform or "").split())
            if not platform_text:
                # 从已有会话里找唯一平台；找不准就不猜，留给用户填。
                seen = {
                    str(row.get("platform") or "").strip()
                    for row in (store.list_states() or [])
                    if str(row.get("platform") or "").strip()
                }
                if len(seen) == 1:
                    platform_text = seen.pop()

            key = person_key(platform_text, uid)
            if not key:
                return {"ok": False, "error": "user_id 不合法"}
            existing = store.get_person(key)
            if existing is not None:
                return {
                    "ok": False,
                    "error": f"已经认识 {key} 了，直接编辑那一条即可",
                }

            role = ""
            if str(relation_role or "").strip():
                role = normalize_role(relation_role, self._webui_roles())
                if not role:
                    known = " / ".join(
                        f"{item['key']}（{item['label']}）" for item in self._webui_roles()
                    )
                    return {
                        "ok": False,
                        "error": f"不认识的身份证 {relation_role!r}。当前可选：{known}",
                    }
            now = time.time()
            # 六维优先：给了就用它算好感度，否则按 affection 反摊六维。
            start_axes: dict[str, float] | None = None
            if axes is not None:
                cleaned: dict[str, float] = {}
                for axis_key, axis_value in dict(axes).items():
                    name = str(axis_key or "").strip().lower()
                    if name not in AXIS_KEYS:
                        continue
                    try:
                        number = float(axis_value)
                    except (TypeError, ValueError):
                        continue
                    if math.isnan(number) or math.isinf(number):
                        continue
                    cleaned[name] = clamp_axis(number, name)
                if cleaned:
                    start_axes = cleaned
            if start_axes is not None:
                from .cg_person import axes_of

                merged = axes_of({"affection": 0.0})
                merged.update(start_axes)
                start_axes = merged
            else:
                start_axes = axes_from_affection(clamp_affection(affection))

            row: dict[str, Any] = {
                "person_key": key,
                "platform": platform_text,
                "user_id": uid,
                "display_name": " ".join(str(display_name or "").split())[:16] or uid,
                "relation_role": role or "stranger",
                "role_confidence": 1.0 if role else 0.0,
                # 用户亲手加的人：角色来源一律标 manual，自动判断不许改。
                "role_source": "manual",
                "affection": affection_from_axes(start_axes),
                "axes": start_axes,
                "emotional_impact": 0.0,
                "tags": normalize_tags(tags),
                "first_seen_ts": now,
                "last_seen_ts": now,
                "updated_at": now,
            }
            store.upsert_person(row, now=now)
            fresh = store.get_person(key) or row
            return {"ok": True, "person": self._person_payload(fresh)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"新增人物失败：{exc}"}

    @API("mem_person_delete", description="忘掉一个人", version="1")
    async def mem_person_delete(self, person_key: str) -> dict[str, Any]:
        try:
            store = self._webui_store()
            key = str(person_key or "").strip()
            if not key:
                return {"ok": False, "error": "缺少 person_key"}
            removed = store.delete_person(key)
            if not removed:
                return {"ok": False, "error": f"没找到 {key}"}
            return {"ok": True, "deleted": key}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"删除人物失败：{exc}"}

    # ── 喜好与厌恶 ──────────────────────────────────────────────────
    #
    # 和人物面板并列：人物是「对别人」的，这里是「关于她自己」的。
    # 页面上这一块放在「认识的人」和「记忆」中间（用户第 32 轮指定的位置）。

    def _pref_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        kind = str(row.get("kind") or "like")
        topic = str(row.get("topic") or "")
        return {
            "pref_key": str(row.get("pref_key") or ""),
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "topic": topic,
            "strength": round(float(row.get("strength") or 0.0), 3),
            "source": str(row.get("source") or ""),
            "source_label": SOURCE_LABELS.get(str(row.get("source") or ""), "未知"),
            "is_manual": str(row.get("source") or "") == SOURCE_MANUAL,
            "created_at": float(row.get("created_at") or 0.0),
            "updated_at": float(row.get("updated_at") or 0.0),
        }

    @API("mem_prefs", description="列出 Bot 自己的喜好与厌恶", version="1")
    async def mem_prefs(self, limit: int = 200) -> dict[str, Any]:
        try:
            store = self._webui_store()
            size = max(1, min(500, int(limit)))
            rows = store.list_prefs(limit=size)
            return {
                "ok": True,
                "prefs": [self._pref_payload(row) for row in rows],
                "total": len(rows),
                "kinds": [
                    {"value": kind, "label": KIND_LABELS.get(kind, kind)} for kind in KINDS
                ],
                "sources": [
                    {"value": key, "label": label} for key, label in SOURCE_LABELS.items()
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取喜好失败：{exc}"}

    @API("mem_pref_create", description="手动新增一条喜好或厌恶", version="1")
    async def mem_pref_create(
        self,
        kind: str,
        topic: str,
        strength: float = 0.6,
    ) -> dict[str, Any]:
        """新增一条口味。

        手动加的 ``source`` 恒为 ``manual``：用户亲手写的东西不该被聊天
        里的自动观察改掉（和人物标签是同一套规矩）。
        """

        try:
            store = self._webui_store()
            normalized_kind = normalize_kind(kind)
            if not normalized_kind:
                return {"ok": False, "error": "kind 只能是 like 或 dislike"}
            text = normalize_topic(topic)
            if not text:
                return {"ok": False, "error": "请填写喜欢/讨厌的话题"}
            key = prefs_key(normalized_kind, text)
            if store.get_pref(key) is not None:
                return {"ok": False, "error": f"已经有「{text}」这一条了，直接编辑它"}
            now = time.time()
            row = {
                "pref_key": key,
                "kind": normalized_kind,
                "topic": text,
                "strength": clamp_strength(strength),
                "source": SOURCE_MANUAL,
                "created_at": now,
                "updated_at": now,
            }
            store.upsert_pref(row, now=now)
            fresh = store.get_pref(key) or row
            return {"ok": True, "pref": self._pref_payload(fresh)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"新增喜好失败：{exc}"}

    @API("mem_pref_update", description="修改一条喜好的强度或话题", version="1")
    async def mem_pref_update(
        self,
        pref_key: str,
        strength: float = -1.0,
        topic: str = "",
    ) -> dict[str, Any]:
        """修改一条口味。

        ``strength`` 用 **-1 当「不改」哨兵**：合法强度是 0..1，
        不会和 -1 撞车（和 ``mem_person_update`` 的 ``-999`` 是同一个思路）。
        改了话题等于换了一条，所以主键要跟着变——旧行删掉、新行写入。
        """

        try:
            store = self._webui_store()
            key = str(pref_key or "").strip()
            if not key:
                return {"ok": False, "error": "缺少 pref_key"}
            row = store.get_pref(key)
            if row is None:
                return {"ok": False, "error": f"没找到 {key}"}

            changed = False
            if strength >= 0.0:
                new_strength = clamp_strength(strength)
                if abs(new_strength - float(row.get("strength") or 0.0)) > 1e-6:
                    row["strength"] = new_strength
                    changed = True
            new_topic = normalize_topic(topic)
            if new_topic and new_topic != str(row.get("topic") or ""):
                new_key = prefs_key(str(row.get("kind") or "like"), new_topic)
                if store.get_pref(new_key) is not None:
                    return {"ok": False, "error": f"已经有「{new_topic}」这一条了"}
                # 话题变了就是另一条口味：删旧写新，别留下两行同义不同名的。
                store.delete_pref(key)
                row["pref_key"] = new_key
                row["topic"] = new_topic
                changed = True

            if not changed:
                return {"ok": False, "error": "没有要改的字段"}

            now = time.time()
            row["updated_at"] = now
            store.upsert_pref(row, now=now)
            fresh = store.get_pref(str(row.get("pref_key") or key)) or row
            return {"ok": True, "pref": self._pref_payload(fresh)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"修改喜好失败：{exc}"}

    @API("mem_pref_delete", description="删掉一条喜好", version="1")
    async def mem_pref_delete(self, pref_key: str) -> dict[str, Any]:
        try:
            store = self._webui_store()
            key = str(pref_key or "").strip()
            if not key:
                return {"ok": False, "error": "缺少 pref_key"}
            if not store.delete_pref(key):
                return {"ok": False, "error": f"没找到 {key}"}
            return {"ok": True, "deleted": key}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"删除喜好失败：{exc}"}

    # ── 念头 / 留念 / 主动 ────────────────────────────────────────
    #
    # 这三块都是**跨会话**的。记忆、人物、喜好按会话切开还能用，但「她现在
    # 心里惦记着什么」按会话切就得一个个点过去才拼得全——那不是用户想看的。
    #
    # 页面上的位置（用户第 57 轮指定）：念头放记忆下面，默认折叠。留念和
    # 主动都是「她想出来的东西」，跟着念头一起排在记忆之后。
    #
    # ⚠️ 这一整块**不进任何注入路径**。它是给人看的，不是给模型看的——给模型
    # 看的东西在 cg_thought / cg_keepsake / cg_proactive 里，各有各的入口。

    def _webui_session_label(self, store, session_ids) -> dict[str, str]:
        """会话 id → 人话名字（「某某群」/ 对方昵称 / 短码）。"""

        return store.session_labels([s for s in session_ids if s])

    def _thought_payload(self, row: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
        status = str(row.get("status") or STATUS_ACTIVE)
        return {
            "id": int(row.get("id") or 0),
            "session_id": str(row.get("session_id") or ""),
            "session_label": labels.get(str(row.get("session_id") or ""), ""),
            "kind": str(row.get("kind") or KIND_DISSENT),
            "content": str(row.get("content") or ""),
            "source_kind": str(row.get("source_kind") or ""),
            "source_text": str(row.get("source_text") or ""),
            "salience": round(float(row.get("salience") or 0.0), 3),
            "status": status,
            "status_label": "还惦记着" if status == STATUS_ACTIVE else (
                "说出口了" if status == STATUS_VOICED else "忘了"
            ),
            "awaken_count": int(row.get("awaken_count") or 0),
            "last_awaken_ts": float(row.get("last_awaken_ts") or 0.0),
            "voiced_at": float(row.get("voiced_at") or 0.0),
            "expires_at": float(row.get("expires_at") or 0.0),
            "created_at": float(row.get("created_at") or 0.0),
        }

    @API("mem_thoughts", description="列出 Bot 心里惦记着的事（跨会话）", version="1")
    async def mem_thoughts(self, limit: int = 60, status: str = "") -> dict[str, Any]:
        """念头列表。

        ``status=""`` 只看还活着的（``active`` 且没过期）——这是默认视角，
        因为「忘了的」和「说出口的」都只是历史，翻它们要看专门的状态筛选。
        """

        try:
            store = self._webui_store()
            size = max(1, min(MAX_PAGE_SIZE, int(limit)))
            wanted = str(status or "").strip()
            if wanted and wanted not in ("active", "voiced", "expired"):
                return {"ok": False, "error": f"未知状态：{wanted}"}
            rows = store.list_all_thoughts(limit=size, status=wanted)
            labels = self._webui_session_label(store, [r.get("session_id") for r in rows])
            return {
                "ok": True,
                "thoughts": [self._thought_payload(row, labels) for row in rows],
                "total": len(rows),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取念头失败：{exc}"}

    @API("mem_thought_delete", description="删掉一条念头", version="1")
    async def mem_thought_delete(self, thought_id: int) -> dict[str, Any]:
        """删掉。

        注意这里和「说出口」不一样：说出口只是记一笔（``voiced``），「我把它
        从她心里拿走」才是删。两者都可能被用户点到，所以按钮文案要分开。
        """

        try:
            store = self._webui_store()
            tid = int(thought_id or 0)
            if tid <= 0:
                return {"ok": False, "error": "缺少 thought_id"}
            cursor = store.conn.execute("DELETE FROM gap_thoughts WHERE id = ?", (tid,))
            store.conn.commit()
            if not int(cursor.rowcount):
                return {"ok": False, "error": f"没找到念头 {tid}"}
            return {"ok": True, "deleted": tid}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"删除念头失败：{exc}"}

    def _keepsake_payload(self, row: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
        kind = str(row.get("kind") or "line")
        status = str(row.get("status") or STATUS_KEPT)
        return {
            "id": int(row.get("id") or 0),
            "session_id": str(row.get("session_id") or ""),
            "session_label": labels.get(str(row.get("session_id") or ""), ""),
            "kind": kind,
            "kind_label": KEEPSAKE_KIND_LABELS.get(kind, kind),
            "content": str(row.get("content") or ""),
            "source_text": str(row.get("source_text") or ""),
            "status": status,
            "status_label": "还留着" if status == STATUS_KEPT else "放掉了",
            "look_count": int(row.get("look_count") or 0),
            "last_looked_ts": float(row.get("last_looked_ts") or 0.0),
            "released_at": float(row.get("released_at") or 0.0),
            "created_at": float(row.get("created_at") or 0.0),
        }

    @API("mem_keepsakes", description="列出 Bot 留着的没用的东西（跨会话）", version="1")
    async def mem_keepsakes(self, limit: int = 200, status: str = "kept") -> dict[str, Any]:
        """留念列表。默认只看还留着的。

        页面上的措辞要克制：这是「她没舍得扔的东西」，不是「收藏夹」。所以
        不放数量进度条、不显示「已用 3/12」这种暗示要清理的东西。
        """

        try:
            store = self._webui_store()
            size = max(1, min(MAX_PAGE_SIZE, int(limit)))
            wanted = str(status or "").strip()
            if wanted and wanted not in (STATUS_KEPT, STATUS_RELEASED):
                return {"ok": False, "error": f"未知状态：{wanted}"}
            rows = store.list_all_keepsakes(limit=size, status=wanted)
            labels = self._webui_session_label(store, [r.get("session_id") for r in rows])
            totals = store.keepsake_totals()
            return {
                "ok": True,
                "keepsakes": [self._keepsake_payload(row, labels) for row in rows],
                "total": len(rows),
                "kept": totals["kept"],
                "released": totals["released"],
                "cap": KEEPSAKE_MAX_ITEMS,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取留念失败：{exc}"}

    @API("mem_keepsake_release", description="放掉一条留念（记录，不删除）", version="1")
    async def mem_keepsake_release(self, keepsake_id: int) -> dict[str, Any]:
        """「放掉」而不是「删除」。

        放掉之后它还留在列表里（``released``），因为「扔掉」和「从没存在过」
        是两件事。真要抹掉得用 ``mem_keepsake_delete``。
        """

        try:
            store = self._webui_store()
            kid = int(keepsake_id or 0)
            if kid <= 0:
                return {"ok": False, "error": "缺少 keepsake_id"}
            if not store.release_keepsake(kid):
                return {"ok": False, "error": "这条已经放掉了"}
            return {"ok": True, "released": kid}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"放掉留念失败：{exc}"}

    @API("mem_keepsake_delete", description="彻底删掉一条留念", version="1")
    async def mem_keepsake_delete(self, keepsake_id: int) -> dict[str, Any]:
        try:
            store = self._webui_store()
            kid = int(keepsake_id or 0)
            if kid <= 0:
                return {"ok": False, "error": "缺少 keepsake_id"}
            if not store.delete_keepsake(kid):
                return {"ok": False, "error": f"没找到留念 {kid}"}
            return {"ok": True, "deleted": kid}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"删除留念失败：{exc}"}

    @API("mem_sparks", description="列出 Bot 主动开口的记账（含没开口的原因）", version="1")
    async def mem_sparks(self, limit: int = 60) -> dict[str, Any]:
        """主动记账。

        **跳过的也列出来。** 这一块最大的用处不是「她说了什么」，而是「她
        为什么没说」——只显示成功的话，出问题时这一页永远是空的，看不出是
        没触发还是被挡住了。
        """

        try:
            store = self._webui_store()
            size = max(1, min(MAX_PAGE_SIZE, int(limit)))
            rows = store.list_all_sparks(limit=size)
            labels = self._webui_session_label(store, [r.get("session_id") for r in rows])
            totals = store.spark_totals()
            payload = []
            for row in rows:
                kind = str(row.get("kind") or "")
                status = str(row.get("status") or "skipped")
                payload.append(
                    {
                        "id": int(row.get("id") or 0),
                        "session_id": str(row.get("session_id") or ""),
                        "session_label": labels.get(str(row.get("session_id") or ""), ""),
                        "kind": kind,
                        "kind_label": SPARK_KIND_LABELS.get(kind, kind),
                        "content": str(row.get("content") or ""),
                        "status": status,
                        "status_label": "开口了" if status == "triggered" else "没开口",
                        "reason": str(row.get("reason") or ""),
                        "triggered_at": float(row.get("triggered_at") or 0.0),
                        "created_at": float(row.get("created_at") or 0.0),
                    }
                )
            return {
                "ok": True,
                "sparks": payload,
                "total": len(payload),
                "triggered": totals["triggered"],
                "skipped": totals["skipped"],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取主动记录失败：{exc}"}
