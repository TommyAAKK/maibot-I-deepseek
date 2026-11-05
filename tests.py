"""我，deepseek —— 本地自测。

Runner 之外没有 ``maibot_sdk``，所以这里用最小的替身模块把它顶掉，
然后用真实的 pydantic 跑配置模型、用真实的 sqlite3 跑存储层。

覆盖的东西：

1. 存储层：写入 / 去重合并 / 过期 / 淘汰 / 解决
2. 组装层：预算裁剪、类别跳过、``extra_prompt`` 追加语义
3. 抽取层：JSON 容错解析、类别白名单、长度裁剪
4. 避让层：宿主配置读取与预算收紧
5. 插件整体：on_load → 捕获消息 → 后台抽取 → 注入 → 管理命令 → on_unload

跑法：``python tests.py``（在插件目录下）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types
from types import SimpleNamespace
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ── 先造出 maibot_sdk 替身，再 import 插件 ──


def _install_sdk_stub() -> None:
    from pydantic import BaseModel, ConfigDict, Field

    sdk = types.ModuleType("maibot_sdk")

    class PluginConfigBase(BaseModel):
        model_config = ConfigDict(validate_assignment=True, extra="ignore")
        __ui_label__: str = ""
        __ui_icon__: str = ""
        __ui_order__: int = 0

    class MaiBotPlugin:
        config_model = None

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._ctx = None
            self._config = None

        @property
        def ctx(self) -> Any:
            if self._ctx is None:
                raise RuntimeError("ctx 未注入")
            return self._ctx

        @property
        def config(self) -> Any:
            if self._config is None:
                raise RuntimeError("config 未注入")
            return self._config

        async def on_load(self) -> None:
            raise NotImplementedError

        async def on_unload(self) -> None:
            raise NotImplementedError

    def HookHandler(*args: Any, **kwargs: Any):
        def decorator(func):
            func._hook_args = args
            func._hook_kwargs = kwargs
            return func

        return decorator

    def Command(*args: Any, **kwargs: Any):
        def decorator(func):
            func._command_args = args
            func._command_kwargs = kwargs
            return func

        return decorator

    sdk.PluginConfigBase = PluginConfigBase
    sdk.MaiBotPlugin = MaiBotPlugin
    sdk.HookHandler = HookHandler
    sdk.Command = Command
    sdk.Field = Field

    types_mod = types.ModuleType("maibot_sdk.types")

    class _Enum:
        def __init__(self, name: str) -> None:
            self.name = name

        def __repr__(self) -> str:
            return f"<{self.name}>"

    class HookMode:
        BLOCKING = _Enum("BLOCKING")
        OBSERVE = _Enum("OBSERVE")

    class HookOrder:
        EARLY = _Enum("EARLY")
        NORMAL = _Enum("NORMAL")
        LATE = _Enum("LATE")

    class ErrorPolicy:
        SKIP = _Enum("SKIP")
        ABORT = _Enum("ABORT")
        LOG = _Enum("LOG")

    types_mod.HookMode = HookMode
    types_mod.HookOrder = HookOrder
    types_mod.ErrorPolicy = ErrorPolicy

    sdk.types = types_mod
    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = types_mod


_install_sdk_stub()

# 宿主 Runner 是用 spec_from_file_location(..., submodule_search_locations=[插件目录])
# 把 plugin.py 当成「包」来加载的，所以插件内部一律走相对导入。
# 这里必须用同样的方式加载，否则测不出宿主的真实导入行为。
import importlib  # noqa: E402
import importlib.util  # noqa: E402

_PKG_NAME = "_cgap_under_test"
_pkg_spec = importlib.util.spec_from_file_location(
    _PKG_NAME,
    os.path.join(_HERE, "plugin.py"),
    submodule_search_locations=[_HERE],
)
if _pkg_spec is None or _pkg_spec.loader is None:
    raise RuntimeError("无法为 plugin.py 构造模块 spec")
_pkg_module = importlib.util.module_from_spec(_pkg_spec)
sys.modules[_PKG_NAME] = _pkg_module
_pkg_spec.loader.exec_module(_pkg_module)

plugin_mod = _pkg_module
compose_mod = importlib.import_module(f"{_PKG_NAME}.cg_compose")
extract_mod = importlib.import_module(f"{_PKG_NAME}.cg_extract")
store_mod = importlib.import_module(f"{_PKG_NAME}.cg_store")
avoidance_mod = importlib.import_module(f"{_PKG_NAME}.cg_avoidance")
config_mod = importlib.import_module(f"{_PKG_NAME}.cg_config_model")
webui_mod = importlib.import_module(f"{_PKG_NAME}.cg_webui")
mood_mod = importlib.import_module(f"{_PKG_NAME}.cg_mood")
person_mod = importlib.import_module(f"{_PKG_NAME}.cg_person")
log_mod = importlib.import_module(f"{_PKG_NAME}.cg_log")
tracker_mod = importlib.import_module(f"{_PKG_NAME}.cg_tracker")
AvoidanceProfile = avoidance_mod.AvoidanceProfile
CompanionGapConfig = config_mod.CompanionGapConfig

PASSED = 0

#: 断言失败时也要能关掉，否则 Windows 上临时目录删不掉。
_OPEN_STORES: list[Any] = []

try:  # Windows 控制台默认 GBK，中文断言信息会乱码
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass


def check(condition: bool, label: str) -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"断言失败: {label}")
    PASSED += 1
    print(f"  ok  {label}")


# ── 测试用替身 ──


class FakeLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _record(self, level: str, message: str) -> None:
        self.lines.append((level, message))

    def info(self, message: str) -> None:
        self._record("info", message)

    def debug(self, message: str) -> None:
        self._record("debug", message)

    def warning(self, message: str) -> None:
        self._record("warning", message)


class FakePaths:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.runtime_dir = data_dir


class FakeConfigCapability:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    async def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


class FakeLlm:
    def __init__(self, response_text: str, *, success: bool = True) -> None:
        self.response_text = response_text
        self.success = success
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.success:
            return {"success": False, "error": "模型炸了"}
        return {"success": True, "response": self.response_text, "model": "fake-model"}


class FakeCtx:
    def __init__(
        self,
        data_dir: str,
        *,
        config_values: dict[str, Any] | None = None,
        llm: Any = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.paths = FakePaths(data_dir)
        self.logger = FakeLogger()
        self.config = FakeConfigCapability(config_values or {})
        self.llm = llm or FakeLlm('{"items":[],"resolved":[]}')
        self._messages = messages or []

    async def call_capability(self, capability: str, **kwargs: Any) -> Any:
        if capability == "message.get_recent":
            return list(self._messages)
        raise AssertionError(f"未预期的能力调用: {capability}")


def _msg(text: str, *, speaker: str = "阿岚", session: str = "s1", group: bool = True) -> dict:
    return {
        "session_id": session,
        "platform": "qq",
        "timestamp": str(time.time()),
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": "u1", "user_nickname": speaker, "user_cardname": speaker},
            "group_info": {"group_id": "g1", "group_name": "测试群"} if group else None,
        },
        "raw_message": [{"type": "text", "data": text}],
    }


# ── 1. 存储层 ──


def test_store(tmp: str) -> None:
    print("[1] 存储层")
    db = os.path.join(tmp, "store_test.db")
    store = store_mod.GapStore(db)
    store.open()
    _OPEN_STORES.append(store)

    now = time.time()
    item_id, created = store.upsert_item("s1", "open_loop", "说好周末一起看那部电影", salience=0.6, now=now)
    check(created is True, "首次写入 created=True")
    check(item_id > 0, "返回自增 id")

    same_id, created2 = store.upsert_item("s1", "open_loop", "说好周末一起看那部电影", salience=0.9, now=now + 10)
    check(created2 is False, "相似内容合并而非新增")
    check(same_id == item_id, "合并命中同一条")
    rows = store.list_by_category("s1", "open_loop")
    check(len(rows) == 1, "合并后仍只有 1 条")
    check(abs(float(rows[0]["salience"]) - 0.9) < 1e-6, "显著度取较大值")
    check(int(rows[0]["source_count"]) == 2, "source_count 累加")

    store.upsert_item("s1", "emotion", "最近工作压力有点大", salience=0.5, now=now)
    store.upsert_item("s1", "self", "答应过不用敬语称呼对方", salience=0.4, now=now)
    active = store.list_active("s1", now=now)
    check(len(active) == 3, "三类共 3 条活跃")
    check(active[0]["salience"] >= active[-1]["salience"], "按显著度降序")

    only_emotion = store.list_active("s1", categories=["emotion"], now=now)
    check(len(only_emotion) == 1, "按类别过滤")

    filtered = store.list_active("s1", now=now, min_salience=0.55)
    check(len(filtered) == 1, "按显著度过滤")

    # 过期
    store.upsert_item("s1", "emotion", "明天有点紧张", salience=0.5, expires_at=now - 1, now=now - 100)
    expired = store.expire_stale(now=now)
    check(expired == 1, "过期条目被清理")

    # 解决
    resolved = store.resolve_items([item_id], now=now)
    check(resolved == 1, "条目被标记 resolved")
    check(len(store.list_active("s1", now=now)) == 2, "resolved 不再出现在活跃列表")

    # 淘汰（用彼此不相似的句子，避免触发去重合并）
    distinct = [
        "约好下次一起去爬城郊那座小山",
        "她说想学吉他但一直没开始",
        "我们给那只流浪猫起名叫豆豆",
        "她讨厌别人在她忙的时候连发消息",
        "计划明年春天去看樱花",
        "她喜欢在深夜聊吃的",
        "约定不用敬语称呼彼此",
        "一起写过一段关于灯塔的故事",
        "她提过想换一份不那么累的工作",
        "共同约定每周留一个晚上不看手机",
    ]
    for text in distinct:
        store.upsert_item("s1", "creative", text, salience=0.3, now=now)
    check(len(store.list_active("s1", now=now)) > 5, "淘汰前条目数超过上限")
    removed = store.trim_session("s1", 5, now=now)
    check(removed > 0, "超出上限触发淘汰")
    check(len(store.list_active("s1", now=now)) == 5, "淘汰后正好保留上限条数")

    stats = store.session_stats("s1")
    check("by_category" in stats, "会话统计含类别分布")
    store.upsert_state("s1", platform="qq", chat_type="group", group_id="g1", pending_count=3)
    check(store.get_state("s1")["pending_count"] == 3, "会话状态写入并读回")
    check(len(store.list_states()) == 1, "会话状态列表")
    glob = store.global_stats()
    check(glob["sessions"] == 1, "全局统计会话数")

    store.delete_session_items("s1")
    check(len(store.list_active("s1", now=now)) == 0, "清空会话")

    # ── 跨流查询 ──
    # 写入时按会话分开（不同群的上下文不能混），读取时可以流通。
    store.upsert_state("g1", platform="qq", chat_type="group", group_name="方舟", pending_count=0)
    store.upsert_state("p1", platform="qq", chat_type="private", user_nickname="小明", pending_count=0)
    store.upsert_item("g1", "open_loop", "群里约好周末联机", salience=0.9, now=now)
    store.upsert_item("p1", "relationship", "私聊说过不喜欢被叫全名", salience=0.85, now=now)
    store.upsert_item("p1", "emotion", "私聊里情绪很低落", salience=0.2, now=now)

    other = store.list_other_sessions("cur", now=now, min_salience=0.5)
    check(len(other) == 2, "跨流查询排除低显著度条目")
    check(all(str(row["session_id"]) != "cur" for row in other), "跨流查询不含当前会话")
    check(
        all("source_chat_type" in row for row in other),
        "跨流查询带出 source_chat_type（用来区分群聊/私聊）",
    )
    check(
        any(str(row.get("source_group_name")) == "方舟" for row in other),
        "跨流查询带出群名",
    )

    groups_only = store.list_other_sessions("cur", chat_types=["group"], now=now, min_salience=0.5)
    check(len(groups_only) == 1, "按 chat_types 过滤只取群聊")
    privates_only = store.list_other_sessions("cur", chat_types=["private"], now=now, min_salience=0.5)
    check(len(privates_only) == 1, "按 chat_types 过滤只取私聊")

    capped = store.list_other_sessions("cur", now=now, min_salience=0.0, limit=1)
    check(len(capped) == 1, "跨流查询尊重 limit")

    labels = store.session_labels(["g1", "p1", "nope"])
    check(labels.get("g1") == "方舟", "群会话取群名作标签")
    check(labels.get("p1") == "小明", "私聊取对方昵称作标签")
    check(labels.get("nope") == "nope"[:8], "查不到的会话退化成短前缀")
    check(store.session_labels([]) == {}, "空输入返回空字典")

    # ── 记忆生命周期（第 32 轮）：衰减 / 遗忘 / 想起 / 置顶 / 可信度 ──
    life_sid = "qq:group:lifecycle"
    store.upsert_state(life_sid, chat_type="group", group_name="生命周期群", platform="qq")
    # 时间戳不能给 0：decay_salience 把 last_seen_ts <= 0 当成「没有时间信息」跳过，
    # 用 0 当基准会让衰减一条都处理不到，断言会莫名其妙失败。
    t0 = 1_700_000_000.0
    stale_id, _ = store.upsert_item(life_sid, "open_loop", "很久以前说过的一件小事", salience=0.9, now=t0)
    keep_id, _ = store.upsert_item(life_sid, "open_loop", "钉住不许忘的事", salience=0.9, now=t0)
    check(store.set_pinned(keep_id, True), "置顶成功")
    pinned_row = store.get_item(keep_id) or {}
    check(bool(pinned_row.get("pinned")), "置顶状态写进库")
    check(bool((store.get_item(stale_id) or {}).get("pinned")) is False, "没置顶的仍是 0")

    # 衰减：把 now 推到半衰期之外，未置顶的应该掉下来，置顶的不动
    decayed, forgotten = store.decay_salience(
        now=t0 + 90 * 86400.0,
        half_life_days=45.0,
        forget_below=0.12,
        forget_after_days=1000.0,
    )
    check(decayed >= 1, "衰减至少处理了一条")
    stale_row = store.get_item(stale_id) or {}
    keep_row = store.get_item(keep_id) or {}
    check(
        float(stale_row.get("salience") or 0) < 0.9,
        "闲置超过半衰期的条目显著度下降",
    )
    check(
        abs(float(keep_row.get("salience") or 0) - 0.9) < 1e-6,
        "置顶的条目不衰减",
    )
    check(int(keep_row.get("pinned") or 0) == 1, "衰减不会把置顶标记清掉")

    # 遗忘：阈值压到很高，让未置顶那条直接转遗忘
    store.update_item(stale_id, salience=0.05, now=t0 + 100 * 86400.0)
    _decayed2, forgotten2 = store.decay_salience(
        now=t0 + 101 * 86400.0,
        half_life_days=45.0,
        forget_below=0.12,
        forget_after_days=1000.0,
    )
    check(forgotten2 >= 1, "显著度低于阈值的条目被标为遗忘")
    forgotten_row = store.get_item(stale_id) or {}
    check(str(forgotten_row.get("status")) == "forgotten", "遗忘是状态而不是删除")
    check(float(forgotten_row.get("forgotten_at") or 0) > 0, "遗忘时间被记下来")
    check(
        store.get_item(keep_id) is not None and str((store.get_item(keep_id) or {}).get("status")) == "active",
        "置顶的条目不会被遗忘",
    )

    # 遗忘的条目不在主动读取的范围里
    active_ids = [row["id"] for row in store.list_active(life_sid)]
    check(stale_id not in active_ids, "遗忘的条目不会被主动读出来")
    check(keep_id in active_ids, "活跃条目照常读出来")
    check(
        any(row["id"] == stale_id for row in store.list_forgotten(life_sid)),
        "遗忘的条目单独能查到",
    )

    # ── 衰减复利 bug 的回归（真机毁过 297 条记忆）──
    #
    # 原来的 decay_salience 每轮都拿 last_seen_ts 现算整段闲置时长，乘完
    # 系数却不把基准往前推。而 cg_tracker.tick 每 refresh_tick_seconds
    # （默认 30 秒）调一次 —— 同一段闲置被反复计息，一小时就能把一条
    # 0.7 的记忆压到 0.118 并标成遗忘。
    #
    # 下面三条断言分别锁住：①重复调用不放大 ②只与时间总跨度有关
    # ③物理量级正确（45 天半衰期一天只该掉 1.5%）。
    comp_sid = "77777777c0ffee0077777777c0ffee00"
    store.upsert_state(comp_sid, platform="qq", chat_type="group")
    comp_t0 = 1_800_000_000.0

    def _decay_many(db_path: str, ticks: int, step: float, *, salience: float = 0.7,
                    idle_before: float = 25 * 3600.0) -> float:
        """在独立库里造一条记忆，按固定步长跑若干次衰减，返回最终显著度。"""

        probe = store_mod.GapStore(db_path)
        probe.open()
        try:
            ident = probe.insert_item("s1", "open_loop", "复利用例", salience=salience,
                                      now=comp_t0 - idle_before)
            for step_index in range(1, ticks + 1):
                probe.decay_salience(now=comp_t0 + step_index * step, half_life_days=45.0)
            return float((probe.get_item(ident) or {}).get("salience") or 0.0)
        finally:
            probe.close()

    import tempfile as _tempfile

    _tmpdir = _tempfile.mkdtemp()
    # ① 每 30 秒一次，跑 2 小时 = 240 次。修复前会掉到 ~0.118（被遗忘），
    #    修复后应几乎不动（25 小时闲置对 45 天半衰期而言只掉 0.03%）。
    repeated = _decay_many(os.path.join(_tmpdir, "a.db"), 240, 30.0)
    check(repeated > 0.65, f"重复衰减不会把记忆压垮（实测 {repeated:.4f}，修复前约 0.118）")

    # ② 同样 2 小时，拆成 240 次与 24 次必须给出同一个答案（数学上等价）
    coarse = _decay_many(os.path.join(_tmpdir, "b.db"), 24, 300.0)
    check(
        abs(repeated - coarse) < 5e-4,
        f"衰减结果只取决于时间总跨度，与调用次数无关（{repeated:.5f} vs {coarse:.5f}）",
    )

    # ③ 物理量级：闲置 25 小时 + 再跑 2 小时，应仍接近 0.7
    analytic = 0.7 * 0.5 ** ((25 * 3600.0 + 2 * 3600.0) / (45 * 86400.0))
    check(
        abs(repeated - analytic) < 5e-4,
        f"衰减量符合半衰期解析解（实现 {repeated:.5f} / 解析 {analytic:.5f}）",
    )

    # ④ 长跑不漂移：连跑 3 天（8640 次）误差仍有界，证明没有系统性累积
    long_run = _decay_many(os.path.join(_tmpdir, "c.db"), 3 * 2880, 30.0, salience=0.9,
                           idle_before=86400.0)
    long_expect = 0.9 * 0.5 ** ((86400.0 + 3 * 86400.0) / (45 * 86400.0))
    check(
        abs(long_run - long_expect) < 1e-3,
        f"长时间反复衰减不累积误差（实现 {long_run:.5f} / 解析 {long_expect:.5f}）",
    )

    # ⑤ 基准语义：衰减过的条目被重新提到后，旧闲置不再计息
    #    单独开一个库，避免往共享 store 里插数据影响后面「重开库会话数」的断言。
    reborn_store = store_mod.GapStore(os.path.join(_tmpdir, "reborn.db"))
    reborn_store.open()
    try:
        reborn = reborn_store.insert_item("s1", "open_loop", "被冷落很久又提起的事",
                                          salience=0.6, now=comp_t0 - 30 * 86400.0)
        reborn_store.decay_salience(now=comp_t0, half_life_days=45.0, forget_below=0.0,
                                    forget_after_days=99999.0)
        after_decay = reborn_store.get_item(reborn) or {}
        check(float(after_decay.get("salience_decayed_at") or 0) > 0,
              "衰减之后记下了基准时刻")
        reborn_store.upsert_item("s1", "open_loop", "被冷落很久又提起的事", salience=0.6, now=comp_t0)
        refreshed = reborn_store.get_item(reborn) or {}
        check(float(refreshed.get("salience_decayed_at") or 0) == 0.0,
              "重新被提到后衰减基准清零（旧闲置不再计息）")
    finally:
        reborn_store.close()

    # ── 注入记账（用户第 45 轮「同一批记忆连续多轮重复」的存储侧）──
    book_sid = "88888888deadbeef88888888deadbeef"
    store.upsert_state(book_sid, platform="qq", chat_type="group")
    book_a, _ = store.upsert_item(book_sid, "open_loop", "记账用甲：他答应周末一起去钓鱼", salience=0.80, now=t0)
    book_b, _ = store.upsert_item(book_sid, "open_loop", "记账用乙：上次那把刀还没修好", salience=0.70, now=t0)
    check(book_a != book_b, "两条测试记忆没有被去重合并（内容要够不同）")
    before_a = store.get_item(book_a) or {}
    check(int(before_a.get("inject_streak") or 0) == 0, "新条目连击从 0 开始")
    check(int(before_a.get("inject_count") or 0) == 0, "新条目累计注入次数从 0 开始")
    check(float(before_a.get("last_injected_ts") or 0) == 0.0, "新条目没有注入时间")

    marked = store.mark_injected([book_a], now=t0 + 10.0, decay=0.92)
    check(marked == 1, "记账报告实际写了几条")
    after_a = store.get_item(book_a) or {}
    check(int(after_a.get("inject_streak") or 0) == 1, "记账后连击 +1")
    check(int(after_a.get("inject_count") or 0) == 1, "记账后累计次数 +1")
    check(abs(float(after_a.get("last_injected_ts") or 0) - (t0 + 10.0)) < 1e-6, "记账记下注入时刻")
    check(
        abs(float(after_a.get("salience") or 0) - round(0.80 * 0.92, 4)) < 1e-6,
        "每注入一轮显著度按系数微降",
    )
    check(
        abs(float((store.get_item(book_b) or {}).get("salience") or 0) - 0.70) < 1e-6,
        "没被注入的条目显著度不动",
    )

    # 只记账真正注入的条目：被预算丢掉的候选不能进冷却，否则轮换会卡死
    store.mark_injected([book_a], now=t0 + 20.0, decay=0.92)
    twice_a = store.get_item(book_a) or {}
    check(int(twice_a.get("inject_streak") or 0) == 2, "再次记账连击继续累加")
    check(int((store.get_item(book_b) or {}).get("inject_count") or 0) == 0,
          "没传进来的条目不会被记账")

    # decay=1.0 表示不衰减
    store.mark_injected([book_b], now=t0 + 30.0, decay=1.0)
    check(
        abs(float((store.get_item(book_b) or {}).get("salience") or 0) - 0.70) < 1e-6,
        "损耗系数 1.0 时显著度不降",
    )

    # 空列表不报错也不写库
    check(store.mark_injected([]) == 0, "空列表记账返回 0")

    # 连击要能自己断掉：安静够久就清零，否则会被永久打入冷宫
    reset = store.reset_inject_streaks(now=t0 + 30.0 + 7 * 3600.0, gap_seconds=6 * 3600.0)
    check(reset >= 2, "安静够久的条目连击被清零")
    check(int((store.get_item(book_a) or {}).get("inject_streak") or 0) == 0, "清零后连击回到 0")
    check(
        int((store.get_item(book_a) or {}).get("inject_count") or 0) == 2,
        "清零只清连击，累计注入次数是历史事实要保留",
    )
    # 刚注入过的不会被清零
    store.mark_injected([book_b], now=t0 + 40.0, decay=1.0)
    store.reset_inject_streaks(now=t0 + 50.0, gap_seconds=6 * 3600.0)
    check(int((store.get_item(book_b) or {}).get("inject_streak") or 0) == 1,
          "刚注入过的条目连击不被清零")

    # 想起：回到活跃并抬高显著度
    check(store.recall_item(stale_id, now=t0 + 102 * 86400.0), "想起一条遗忘的记忆")
    recalled_row = store.get_item(stale_id) or {}
    check(str(recalled_row.get("status")) == "active", "想起后回到活跃状态")
    check(float(recalled_row.get("forgotten_at") or 0) == 0.0, "想起后清空遗忘时间")
    check(float(recalled_row.get("last_recalled_ts") or 0) > 0, "想起时间被记下来")
    check(
        float(recalled_row.get("salience") or 0) > 0.05,
        "想起会抬高显著度（否则下一轮又被遗忘）",
    )

    # 置顶时如果条目已经遗忘，应该顺带救回来
    store.update_item(stale_id, status="forgotten", now=t0 + 103 * 86400.0)
    check(str((store.get_item(stale_id) or {}).get("status")) == "forgotten", "手动置为遗忘")
    store.set_pinned(stale_id, True, now=t0 + 104 * 86400.0)
    revived = store.get_item(stale_id) or {}
    check(str(revived.get("status")) == "active", "置顶一条遗忘的条目会把它救回来")
    check(float(revived.get("forgotten_at") or 0) == 0.0, "救回来时清空遗忘时间")

    # ── 两条置顶路径必须同语义（原来只有 set_pinned 正确）──
    #
    # `update_item(pinned=True)` 早期只清 `forgotten_at` 而不动 `status`，
    # 于是条目变成「pinned=1 但还是 forgotten」：`list_active()` 读不到它
    # （条件是 status='active'），`decay_salience` 也跳过它（条件是
    # status='active' AND pinned=0）—— 卡在既不会被读、也不会被衰减的状态，
    # 用户看到的就是「点了置顶但没反应」。下面三条锁住两条路径行为一致。
    dual_sid = "66666666deadbeef66666666deadbeef"
    store.upsert_state(dual_sid, platform="qq", chat_type="group")
    via_update, _ = store.upsert_item(dual_sid, "open_loop", "经由 update_item 置顶的遗忘条目",
                                      salience=0.6, now=t0)
    store.update_item(via_update, status="forgotten", now=t0 + 1.0)
    store.update_item(via_update, pinned=True, now=t0 + 2.0)
    upd_row = store.get_item(via_update) or {}
    check(str(upd_row.get("status")) == "active",
          "update_item(pinned=True) 也会把遗忘条目救回 active")
    check(float(upd_row.get("forgotten_at") or 0) == 0.0,
          "update_item(pinned=True) 同样清空遗忘时间")
    check(
        any(row["id"] == via_update for row in store.list_active(dual_sid)),
        "经 update_item 置顶后条目真的能被注入层读到",
    )
    # 取消置顶不应该把状态改回去
    store.update_item(via_update, pinned=False, now=t0 + 3.0)
    check(
        str((store.get_item(via_update) or {}).get("status")) == "active",
        "取消置顶不会把状态改回遗忘",
    )
    # 显式指定 status 时以调用方为准，不被 pinned 推导覆盖
    store.update_item(via_update, status="forgotten", now=t0 + 4.0)
    store.update_item(via_update, pinned=True, status="expired", now=t0 + 5.0)
    explicit = store.get_item(via_update) or {}
    check(str(explicit.get("status")) == "expired",
          "同时传 pinned 与 status 时，以显式 status 为准")
    check(bool(explicit.get("pinned")), "同时传 pinned 与 status 时，置顶仍然生效")

    # 可信度：合并时取两次的平均，被重新提到的遗忘条目回到活跃
    truth_id, _ = store.upsert_item(life_sid, "self", "听说他养了只猫", salience=0.6, truth_score=0.4, now=t0)
    check(abs(float((store.get_item(truth_id) or {}).get("truth_score") or 0) - 0.4) < 1e-6, "首次写入的可信度被保存")
    store.upsert_item(life_sid, "self", "听说他养了只猫", salience=0.6, truth_score=0.8, now=t0 + 1.0)
    merged_truth = float((store.get_item(truth_id) or {}).get("truth_score") or 0)
    check(abs(merged_truth - 0.6) < 1e-6, f"重复提到时可信度取平均（实际 {merged_truth}）")

    store.update_item(truth_id, status="forgotten", now=t0 + 2.0)
    store.upsert_item(life_sid, "self", "听说他养了只猫", salience=0.6, truth_score=0.8, now=t0 + 3.0)
    check(
        str((store.get_item(truth_id) or {}).get("status")) == "active",
        "遗忘的条目被重新提到时自动回到活跃",
    )

    # 过期与淘汰都要跳过置顶条目
    store.conn.execute("UPDATE gap_items SET expires_at = ? WHERE id = ?", (t0 + 10.0, keep_id))
    store.conn.commit()
    store.expire_stale(now=t0 + 300 * 86400.0)
    check(
        str((store.get_item(keep_id) or {}).get("status")) == "active",
        "置顶的条目不会被过期规则清掉",
    )
    store.trim_session(life_sid, 1, now=t0 + 301 * 86400.0)
    check(store.get_item(keep_id) is not None, "置顶的条目不会被条数上限挤掉")

    store.delete_session_items(life_sid)
    store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", (life_sid,))
    store.conn.commit()

    # ── 唤起打分：去重用的 similarity 不能拿来当「想起」的判据 ──
    mem = "说好下个月一起去看海"
    explicit = store_mod.recall_score("之前是不是说过要一起去看海来着？", mem)
    partial = store_mod.recall_score("去看海吧", mem)
    unrelated = store_mod.recall_score("今天天气不错啊", mem)
    check(explicit >= 0.40, f"明确提到核心内容时分数过门槛（实际 {explicit:.2f}）")
    check(partial < explicit, "只沾一个词的分数低于完整提到")
    check(unrelated < 0.10, f"完全无关的话分数接近 0（实际 {unrelated:.2f}）")
    # 这条是回归：老实现用 Jaccard，长句对短记忆只有 0.2，永远触发不了想起。
    legacy = store_mod.similarity("之前是不是说过要一起去看海来着？", mem)
    check(legacy < 0.40, f"去重相似度在长句上确实偏低（{legacy:.2f}），所以唤起必须另用一把尺子")
    check(store_mod.recall_score("", mem) == 0.0, "空消息返回 0")
    check(store_mod.recall_score("随便说点什么", "") == 0.0, "空记忆返回 0")

    store.delete_session_items("g1")
    store.delete_session_items("p1")
    store.delete_session_items(book_sid)
    store.delete_session_items(comp_sid)
    store.delete_session_items(dual_sid)
    # 会话状态行是我刚插的，重开幂等断言只认 s1 那一条
    for extra in ("g1", "p1", book_sid, comp_sid, dual_sid):
        store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", (extra,))
    store.conn.commit()

    # 重新打开验证 schema 幂等
    store.close()
    _OPEN_STORES.remove(store)
    store2 = store_mod.GapStore(db)
    store2.open()
    _OPEN_STORES.append(store2)
    check(store2.global_stats()["sessions"] == 1, "重开数据库 schema 幂等")
    store2.close()
    _OPEN_STORES.remove(store2)

    # ── notes → tags 只搬一次（用户第 43 轮报的「删掉的标签又出现」）──
    # 老库里 notes 是手写备注，改名成 tags 时要搬过来。但 notes 列 SQLite 删不掉，
    # 而 _migrate 每次 open() 都跑；早期实现无条件搬运，于是用户把 tags 清空后
    # 下次启动又被 notes 填回去 —— 用户看到的就是「标签又出现了」。
    legacy_db = os.path.join(tmp, "notes_once.db")
    seed = store_mod.GapStore(legacy_db)
    seed.open()
    seed.conn.execute("ALTER TABLE gap_person ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
    seed.conn.execute("DELETE FROM gap_meta WHERE key = ?", (store_mod.NOTES_MIGRATED_KEY,))
    seed.conn.execute(
        "INSERT INTO gap_person (person_key, platform, user_id, display_name,"
        " relation_role, role_confidence, role_source, affection, emotional_impact,"
        " notes, first_seen_ts, last_seen_ts, updated_at, tags)"
        " VALUES ('qq:old', 'qq', 'old', '老熟客', 'friend', 1.0, 'manual', 40.0,"
        " 0.2, ?, 1000.0, 1000.0, 1000.0, '[]')",
        (json.dumps(["老备注一"], ensure_ascii=False),),
    )
    seed.conn.commit()
    seed.close()

    migrated = store_mod.GapStore(legacy_db)
    migrated.open()
    migrated_row = migrated.get_person("qq:old") or {}
    check(migrated_row.get("tags") == ["老备注一"], "老库的 notes 首次启动会搬进 tags")
    check(
        migrated.meta_prefix(store_mod.NOTES_MIGRATED_KEY).get(store_mod.NOTES_MIGRATED_KEY)
        is not None,
        "搬运完成后写了 NOTES_MIGRATED_KEY 标记",
    )
    # 用户把标签删掉
    user_cleared = dict(migrated_row)
    user_cleared["tags"] = []
    migrated.upsert_person(user_cleared)
    check((migrated.get_person("qq:old") or {}).get("tags") == [], "用户清空标签成功")
    migrated.close()

    # 关键回归：再开一次不能再搬回来
    again = store_mod.GapStore(legacy_db)
    again.open()
    check(
        (again.get_person("qq:old") or {}).get("tags") == [],
        "重启后清空的标签不会又被 notes 填回来（这是用户第 43 轮报的 bug）",
    )
    again.close()


# ── 2. 组装层 ──


def test_compose() -> None:
    print("[2] 组装层")
    now = time.time()
    items = [
        {"category": "open_loop", "content": "说好周末一起看电影", "salience": 0.8, "last_seen_ts": now},
        {"category": "open_loop", "content": "对方提过想学吉他", "salience": 0.5, "last_seen_ts": now - 5},
        {"category": "emotion", "content": "最近工作压力大", "salience": 0.7, "last_seen_ts": now},
        {"category": "relationship", "content": "习惯用绰号互相称呼", "salience": 0.6, "last_seen_ts": now},
    ]
    text, used = compose_mod.compose_block(items, max_chars=400, min_salience=0.35)
    check(bool(text), "能生成注入文本")
    check(text.startswith("<companion-state>"), "带起始标记")
    check(text.endswith("</companion-state>"), "带结束标记")
    check("还没接上的话题" in text, "包含未闭合话题分节")
    check("对方最近的状态" in text, "包含情绪分节")
    check(len(used) == 4, "4 条都被采用")

    tight, tight_used = compose_mod.compose_block(items, max_chars=120, min_salience=0.35)
    check(len(tight) <= 120, "严格尊重 max_chars 预算")
    check(len(tight_used) < 4, "预算紧张时裁剪条目")

    skipped, skipped_used = compose_mod.compose_block(items, max_chars=400, skip_categories=("open_loop",))
    check("还没接上的话题" not in skipped, "跳过指定类别")
    check(all(row["category"] != "open_loop" for row in skipped_used), "被跳过的类别不进 used")

    empty, empty_used = compose_mod.compose_block([], max_chars=400)
    check(empty == "" and empty_used == [], "空输入返回空串")

    low, _ = compose_mod.compose_block(items, max_chars=400, min_salience=0.95)
    check(low == "", "显著度全不达标时不注入")

    # 回归：预算不能被重复计入。
    # 早期实现先把行文本累加进 consumed，又拿 consumed + len(整节) 比一次预算，
    # 导致行文本被算两遍、后续本该放得下的分节被整节丢弃
    # （实测 max_chars=600 时只用了 54%，self/creative 长期缺席）。
    many = []
    for index, category in enumerate(("open_loop", "emotion", "relationship", "self", "creative")):
        for slot in range(2):
            many.append(
                {
                    "category": category,
                    "content": f"{category} 第 {slot} 条测试内容，用于验证预算分配是否会被重复计入。",
                    "salience": 0.9 - index * 0.05,
                    "last_seen_ts": now - index * 10 - slot,
                }
            )
    for budget_chars in (300, 400, 500, 600, 800, 1000):
        packed, packed_used = compose_mod.compose_block(
            many, max_chars=budget_chars, max_items_per_category=2, min_salience=0.35
        )
        check(len(packed) <= budget_chars, f"预算 {budget_chars} 内不超长（实际 {len(packed)}）")
        body_lines = [line for line in packed.split("\n") if line.startswith("- ")]
        check(
            len(body_lines) == len(packed_used),
            f"预算 {budget_chars} 时 used 与正文条目数一致（正文 {len(body_lines)} / used {len(packed_used)}）",
        )
        # 五个类别都有条目，预算足够时应当全部分节都能出现
        if budget_chars >= 600:
            sections = packed.count("【")
            check(sections == 5, f"预算 {budget_chars} 时五个分节都保留（实际 {sections}）")

    # 生产配置下的实际利用率，防止将来又退化成一半
    prod, _ = compose_mod.compose_block(many, max_chars=600, max_items_per_category=2, min_salience=0.35)
    check(len(prod) >= 480, f"max_chars=600 时利用率不低于 80%（实际 {len(prod)} 字）")

    # 极小预算不能崩，也不能超长
    for tiny in (1, 10, 60, 100):
        tiny_text, tiny_used = compose_mod.compose_block(many, max_chars=tiny, max_items_per_category=2)
        check(len(tiny_text) <= tiny, f"预算 {tiny} 时不超长")
        check(tiny_used == [], f"预算 {tiny} 时放不下任何条目")

    merged = compose_mod.merge_extra_prompt("原有要求", "<companion-state>x</companion-state>")
    check(merged.startswith("原有要求"), "追加语义保留原有内容")
    check(merged.endswith("</companion-state>"), "新内容在后")
    check(compose_mod.merge_extra_prompt("", "abc") == "abc", "原值为空时直接返回新块")
    check(compose_mod.merge_extra_prompt("abc", "") == "abc", "新块为空时原样返回")

    # ── 跨流块 ──
    # 用户要求：记忆写入时按会话分开，但读取时可以流通，避免「刚在私聊聊过、
    # 换个群就完全不记得」。这里验的是「拼出来的文本能不能让模型分清出处」。
    cross_rows = [
        {
            "category": "open_loop",
            "content": "答应帮对方查一下那个游戏的发售日",
            "salience": 0.9,
            "last_seen_ts": now,
            "session_id": "qq:group:111",
            "source_chat_type": "group",
            "source_group_name": "方舟",
        },
        {
            "category": "relationship",
            "content": "说过不喜欢被叫全名",
            "salience": 0.8,
            "last_seen_ts": now,
            "session_id": "qq:private:222",
            "source_chat_type": "private",
            "source_group_name": "",
        },
    ]
    cross, cross_used = compose_mod.compose_cross_session_block(
        cross_rows, labels={"qq:private:222": "小明"}, max_items=5
    )
    check(bool(cross), "跨流块能生成")
    check(cross.startswith("<companion-cross-session>"), "跨流块有独立起始标记")
    check(cross.endswith("</companion-cross-session>"), "跨流块有独立结束标记")
    check("在「方舟」里说过" in cross, "群聊来源标出群名")
    check("私聊「小明」里提过" in cross, "私聊来源标出对方名字")
    check(len(cross_used) == 2, "两条跨流记忆都被采用")
    # 独立成块的意义：不能和 companion-state 混在一起，否则模型分不清
    # 哪句是这个群里说的。
    check("<companion-state>" not in cross, "跨流块不混进当前会话的记忆块")
    check(compose_mod.PRIVATE_TO_GROUP_NOTE not in cross, "默认不追加私密提醒")
    cross_note, _ = compose_mod.compose_cross_session_block(
        cross_rows, labels={}, max_items=5, private_note=True
    )
    check("是否在群里提起由你判断" in cross_note, "开启后把「要不要提」的决定权交给模型")

    cross_tight, _ = compose_mod.compose_cross_session_block(
        cross_rows, max_items=5, max_chars=90
    )
    check(len(cross_tight) <= 90, "跨流块也严格尊重字数上限")
    cross_one, one_used = compose_mod.compose_cross_session_block(
        cross_rows, max_items=1
    )
    check(len(one_used) == 1, "max_items 限制跨流条数")
    cross_empty, empty_used2 = compose_mod.compose_cross_session_block([])
    check(cross_empty == "" and empty_used2 == [], "没有跨流记忆时返回空串")
    # 来源名全缺时要退化到 session_id 短前缀，不能显示裸 32 位 ID
    cross_noname, _ = compose_mod.compose_cross_session_block(
        [dict(cross_rows[0], source_group_name="", session_id="abcdef0123456789")]
    )
    check("abcdef01" in cross_noname and "abcdef0123456789" not in cross_noname,
          "来源缺名字时退化到短前缀")

    # ── 存疑标记（第 32 轮「辨识真假」）──
    doubted_rows = [
        {"category": "self", "content": "听说他养了只猫", "salience": 0.8,
         "last_seen_ts": now, "truth_score": 0.3},
        {"category": "self", "content": "他确实说过自己住城南", "salience": 0.7,
         "last_seen_ts": now, "truth_score": 0.95},
    ]
    doubted_block, _ = compose_mod.compose_block(doubted_rows)
    check(compose_mod.DOUBTED_SUFFIX in doubted_block, "可信度低的条目带上「不太确定」的提示")
    check(
        doubted_block.count(compose_mod.DOUBTED_SUFFIX) == 1,
        "只有存疑的那一条被标注，可信的那条不加",
    )
    # 缺 truth_score 的老数据不能被当成存疑
    legacy_block, _ = compose_mod.compose_block(
        [{"category": "self", "content": "老数据没有 truth_score", "salience": 0.8, "last_seen_ts": now}]
    )
    check(compose_mod.DOUBTED_SUFFIX not in legacy_block, "没有可信度字段的老数据不标存疑")

    # ── 想起块（第 32 轮「遗忘后被他人的话唤醒」）──
    recalled_rows = [
        {"category": "open_loop", "content": "很久以前提过想去看海", "salience": 0.3, "last_seen_ts": now},
    ]
    recall_block, recall_used = compose_mod.compose_recalled_block(recalled_rows)
    check(bool(recall_block), "想起块能生成")
    check(recall_block.startswith("<companion-recalled>"), "想起块有独立起始标记")
    check(recall_block.endswith("</companion-recalled>"), "想起块有独立结束标记")
    check("很久以前提过想去看海" in recall_block, "想起块带上内容")
    check(len(recall_used) == 1, "想起块报告用了哪几条")
    # 和记忆块必须分开：一个是「我一直记得」，一个是「我刚才才想起来」
    check("<companion-state>" not in recall_block, "想起块不混进记忆块")
    check("<companion-recalled>" not in recall_block.replace("<companion-recalled>", "", 1),
          "想起块的起始标记只出现一次")
    check("本来已经想不起来了" in recall_block, "想起块说明这些是刚想起来的")
    recall_empty, recall_empty_used = compose_mod.compose_recalled_block([])
    check(recall_empty == "" and recall_empty_used == [], "没有想起来的条目时返回空串")
    recall_tight, _ = compose_mod.compose_recalled_block(recalled_rows, max_chars=80)
    check(len(recall_tight) <= 80, "想起块也严格尊重字数上限")
    recall_two, two_used = compose_mod.compose_recalled_block(
        recalled_rows + [dict(recalled_rows[0], content="另一件忘了的事")], max_items=1
    )
    check(len(two_used) == 1, "max_items 限制想起条数")

    # ── 注入轮换（用户第 45 轮：「同一批记忆连续多轮重复」）──
    #
    # 场景就是要复现旧行为：三条同类别、显著度相差不大，其中最高分那条
    # 每轮都被取走。没有轮换时它会连刷到最后；有轮换时应当轮到别人。
    rot_now = 1_800_000_000.0
    rot_rows = [
        {
            "id": 1,
            "category": "open_loop",
            "content": "一直在说的那件事",
            "salience": 0.90,
            "last_seen_ts": rot_now,
            "last_injected_ts": 0.0,
            "inject_streak": 0,
            "inject_count": 0,
        },
        {
            "id": 2,
            "category": "open_loop",
            "content": "second",
            "salience": 0.80,
            "last_seen_ts": rot_now,
            "last_injected_ts": 0.0,
            "inject_streak": 0,
            "inject_count": 0,
        },
        {
            "id": 3,
            "category": "open_loop",
            "content": "third",
            "salience": 0.70,
            "last_seen_ts": rot_now,
            "last_injected_ts": 0.0,
            "inject_streak": 0,
            "inject_count": 0,
        },
    ]
    # 关掉轮换时，最高分永远排第一（这就是旧行为，要能复现才算测到了）
    no_rot, no_rot_used = compose_mod.compose_block(
        rot_rows, max_items_per_category=1, now=rot_now, rotation=False
    )
    check(no_rot_used[0]["id"] == 1, "关掉轮换时一律取显著度最高的那条（旧行为）")

    # 打开轮换但都还没被注入过，顺序与关掉时一致：轮换不能凭空改变初始顺序
    fresh, fresh_used = compose_mod.compose_block(
        rot_rows, max_items_per_category=1, now=rot_now, rotation=True
    )
    check(fresh_used[0]["id"] == 1, "轮换在首轮不打乱显著度顺序")

    # 让第 1 条刚被注入过、第 2 条从没被注入过 → 应当轮到第 2 条
    rotated_rows = [dict(row) for row in rot_rows]
    rotated_rows[0]["last_injected_ts"] = rot_now
    rotated_rows[0]["inject_streak"] = 1
    rotated_rows[0]["inject_count"] = 1
    swapped, swapped_used = compose_mod.compose_block(
        rotated_rows, max_items_per_category=1, now=rot_now, rotation=True
    )
    check(swapped_used[0]["id"] == 2, "刚说过的那条让位给没说过的那条")

    # 冷却期过了（超过 cooldown_hours）冷却折价应当消失。
    # 注意这里只考察「冷却」这一个信号，所以把 streak 留在 0 —— 连击是另一个
    # 独立信号，它要等 reset_inject_streaks 才清零，不该拿来测冷却到期。
    cool_row = {"salience": 0.9, "inject_streak": 0, "last_injected_ts": rot_now, "pinned": 0}
    cooling = compose_mod.rotation_score(cool_row, now=rot_now + 60.0)
    expired = compose_mod.rotation_score(cool_row, now=rot_now + 3 * 3600.0)
    check(cooling < 0.9, "冷却期内竞争分被打折")
    check(abs(expired - 0.9) < 1e-9, "冷却期过了之后不再打折")
    check(
        compose_mod.rotation_score(cool_row, now=rot_now + 3600.0)
        < compose_mod.rotation_score(cool_row, now=rot_now + 2 * 3600.0),
        "冷却折价随时间线性回升",
    )

    # 置顶不参与轮换：置顶的语义就是「我就是要它一直在」。
    # 注意置顶只免除折价，不改变它自身的显著度——所以这里比的是同一条目
    # 置顶/不置顶的竞争分，而不是「置顶就一定排第一」。
    pinned_row = {"salience": 0.7, "inject_streak": 9, "last_injected_ts": rot_now, "pinned": 1}
    unpinned_row = dict(pinned_row, pinned=0)
    check(
        abs(compose_mod.rotation_score(pinned_row, now=rot_now) - 0.7) < 1e-9,
        "置顶条目按原始显著度算分，不做任何折价",
    )
    check(
        compose_mod.rotation_score(unpinned_row, now=rot_now)
        < compose_mod.rotation_score(pinned_row, now=rot_now),
        "同样的条目不置顶就会被折价压下去",
    )
    # 置顶还能在排序里压过同分的非置顶条目
    pinned_rows = [dict(row) for row in rot_rows]
    pinned_rows[2]["pinned"] = 1
    _, pinned_used = compose_mod.compose_block(
        pinned_rows, max_items_per_category=1, now=rot_now, rotation=True
    )
    check(pinned_used[0]["id"] == 1, "置顶只免折价，不越过显著度更高且没被说过的条目")

    # 连击越久折价越重；权重为 0 时连击不生效
    heavy = {"salience": 0.9, "inject_streak": 5, "last_injected_ts": 0.0, "pinned": 0}
    light = {"salience": 0.9, "inject_streak": 1, "last_injected_ts": 0.0, "pinned": 0}
    clean = {"salience": 0.9, "inject_streak": 0, "last_injected_ts": 0.0, "pinned": 0}
    s_heavy = compose_mod.rotation_score(heavy, now=rot_now)
    s_light = compose_mod.rotation_score(light, now=rot_now)
    s_clean = compose_mod.rotation_score(clean, now=rot_now)
    check(s_heavy < s_light < s_clean, "连击越多竞争分越低")
    check(
        abs(compose_mod.rotation_score(heavy, now=rot_now, streak_weight=0.0) - 0.9) < 1e-9,
        "权重为 0 时连击完全不折价",
    )
    # 未来时间戳（时钟回拨）要被当成「刚注入过」，不能变成永久霸榜
    future = {"salience": 0.9, "inject_streak": 0, "last_injected_ts": rot_now + 86400.0, "pinned": 0}
    check(
        compose_mod.rotation_score(future, now=rot_now) < compose_mod.rotation_score(clean, now=rot_now),
        "时间戳在未来时按刚注入过处理",
    )
    # 冷却为 0 时 last_injected_ts 不影响分数
    check(
        abs(
            compose_mod.rotation_score(
                {"salience": 0.9, "inject_streak": 0, "last_injected_ts": rot_now, "pinned": 0},
                now=rot_now,
                cooldown_hours=0.0,
            )
            - 0.9
        )
        < 1e-9,
        "冷却时长为 0 时不做冷却折价",
    )

    # ── 话题相关加权（用户第 11 轮：「还可以再根据聊天的内容筛选一遍」）──
    #
    # 关键设计：相关性**只加分、不淘汰**。所以这里的断言分两部分——
    # 一部分证明相关条目确实被拉上来，另一部分证明不相关条目**没有**被踢掉。
    rel_rows = [
        {
            "id": 11,
            "category": "open_loop",
            "content": "约好周末去海边看日出",
            "salience": 0.70,
            "last_seen_ts": rot_now,
            "last_injected_ts": 0.0,
            "inject_streak": 0,
            "inject_count": 0,
            "pinned": 0,
        },
        {
            "id": 12,
            "category": "open_loop",
            "content": "那把断掉的雨伞还没修",
            "salience": 0.80,
            "last_seen_ts": rot_now,
            "last_injected_ts": 0.0,
            "inject_streak": 0,
            "inject_count": 0,
            "pinned": 0,
        },
    ]
    # 不相关时低分的仍然排在后面（相关性不能凭空改变顺序）
    _, rel_quiet = compose_mod.compose_block(
        rel_rows, max_items_per_category=1, now=rot_now, rotation=True, message="今天天气不错"
    )
    check(rel_quiet[0]["id"] == 12, "话题无关时仍按显著度排序")

    # 明确聊到海边那条 → 低分那条靠相关性反超
    _, rel_hot = compose_mod.compose_block(
        rel_rows,
        max_items_per_category=1,
        now=rot_now,
        rotation=True,
        message="之前是不是说过要一起去海边看日出？这周末有空吗",
    )
    check(rel_hot[0]["id"] == 11, "话题正好聊到那条记忆时它会排前面")

    # 相关性只加分不淘汰：即使只取 2 条，不相关的那条也还在（没被过滤掉）
    _, rel_both = compose_mod.compose_block(
        rel_rows,
        max_items_per_category=2,
        now=rot_now,
        rotation=True,
        message="之前是不是说过要一起去海边看日出？",
    )
    check(
        {row["id"] for row in rel_both} == {11, 12},
        "相关性不淘汰任何条目：不相关的仍然在候选里",
    )

    # relevance_score 自身的边界
    hot_row = {"content": "约好周末去海边看日出"}
    check(
        compose_mod.relevance_bonus(hot_row, message="") == 1.0,
        "拿不到当前消息时不加分",
    )
    check(
        compose_mod.relevance_bonus(hot_row, message="约好周末去海边看日出") > 1.0,
        "明确提到时加分大于 1",
    )
    check(
        compose_mod.relevance_bonus(hot_row, message="约好周末去海边看日出", weight=0.0) == 1.0,
        "权重为 0 时完全不看话题",
    )
    check(
        compose_mod.relevance_bonus({"content": ""}, message="随便说点什么") == 1.0,
        "空内容不加分",
    )
    bonus_max = compose_mod.relevance_bonus(hot_row, message="约好周末去海边看日出", weight=0.6)
    check(bonus_max <= 1.6000001, "加分上限是 1 + 权重")

    # 相关性不能把正在冷却的条目永久救回来：冷却系数仍然生效，
    # 只是被加分抵消一部分。这里断言「冷却 + 相关」仍低于「不冷却 + 不相关」，
    # 保证相关性是补丁而不是把轮换废掉。
    cooled = dict(hot_row, salience=0.9, last_injected_ts=rot_now, inject_streak=0, pinned=0)
    check(
        compose_mod.rotation_score(
            cooled, now=rot_now, message="约好周末去海边看日出"
        )
        < compose_mod.rotation_score(
            {"content": "无关内容", "salience": 0.9, "last_injected_ts": 0.0, "inject_streak": 0, "pinned": 0},
            now=rot_now,
            message="约好周末去海边看日出",
        ),
        "相关性是补丁不是豁免：正在冷却的条目相关也只能追回一部分",
    )


# ── 3. 抽取层 ──


def test_extract() -> None:
    print("[3] 抽取层")
    ok = extract_mod.parse_response(
        '```json\n{"items":[{"category":"open_loop","content":"说好周末看电影","salience":0.8},'
        '{"category":"乱写","content":"应被丢弃","salience":0.9},'
        '{"category":"emotion","content":"  最近   压力大  ","salience":"0.6"}],'
        '"resolved":["旧话题"]}\n```',
        max_items=10,
        max_content_chars=20,
    )
    check(ok.ok is True, "带代码围栏的 JSON 能解析")
    check(len(ok.items) == 2, "非法类别被丢弃")
    check(ok.items[0].category == "open_loop", "合法类别保留")
    check(ok.items[1].content == "最近 压力大", "正文空白归一化")
    check(abs(ok.items[1].salience - 0.6) < 1e-6, "字符串显著度被转换")
    check(ok.resolved == ["旧话题"], "resolved 解析")

    clipped = extract_mod.parse_response(
        '{"items":[{"category":"self","content":"%s","salience":2.0}]}' % ("长" * 100),
        max_items=5,
        max_content_chars=10,
    )
    check(len(clipped.items[0].content) == 10, "正文按上限裁剪")
    check(clipped.items[0].salience == 1.0, "显著度被夹到 1.0")

    # ── 可信度（第 32 轮「辨识真假」）──
    truth_parsed = extract_mod.parse_response(
        '{"items":[{"category":"self","content":"他很确定地说自己住城南","salience":0.7,"truth":0.9},'
        '{"category":"self","content":"有人开玩笑说他是外星人","salience":0.7,"truth":0.2},'
        '{"category":"self","content":"拿不准的事","salience":0.7},'
        '{"category":"self","content":"truth 乱写","salience":0.7,"truth":"不知道"}]}',
        max_items=10,
        max_content_chars=40,
    )
    check(len(truth_parsed.items) == 4, "给了 truth 的条目照常解析")
    check(abs(truth_parsed.items[0].truth - 0.9) < 1e-6, "可信度被读出来")
    check(abs(truth_parsed.items[1].truth - 0.2) < 1e-6, "低可信度也被保留（不丢弃可疑内容）")
    check(truth_parsed.items[2].truth == 1.0, "没给 truth 时默认 1.0（老提示词兼容）")
    check(truth_parsed.items[3].truth == 1.0, "truth 不是数字时退回 1.0 而不是报错")

    truth_clipped = extract_mod.parse_response(
        '{"items":[{"category":"self","content":"越界","salience":0.5,"truth":5}]}',
        max_items=5,
        max_content_chars=20,
    )
    check(truth_clipped.items[0].truth == 1.0, "可信度被夹到 1.0")
    truth_negative = extract_mod.parse_response(
        '{"items":[{"category":"self","content":"负数","salience":0.5,"truth":-3}]}',
        max_items=5,
        max_content_chars=20,
    )
    check(truth_negative.items[0].truth == 0.0, "可信度被夹到 0.0")

    bad = extract_mod.parse_response("模型今天不想输出 JSON", max_items=5, max_content_chars=20)
    check(bad.ok is False, "非 JSON 返回标记失败")
    check("JSON 解析失败" in bad.error, "错误信息说明原因")

    empty = extract_mod.parse_response("", max_items=5, max_content_chars=20)
    check(empty.ok is False and empty.error == "模型返回为空", "空返回标记失败")

    not_obj = extract_mod.parse_response("[1,2,3]", max_items=5, max_content_chars=20)
    check(not_obj.ok is False, "顶层不是对象时失败")

    messages = [_msg("第一条"), _msg("第二条"), _msg("第三条")]
    transcript = extract_mod.format_transcript(messages, max_chars=1000)
    check("第一条" in transcript and "第三条" in transcript, "聊天记录包含消息正文")
    # 用户第 33 轮：认人要用账号而不是昵称，所以聊天记录里必须同时出现
    # 昵称和 ID，模型被要求抄 user_id 时才有东西可抄。
    check("阿岚(u1):" in transcript, "聊天记录带说话人和账号")
    check("阿岚:" not in transcript, "说话人不能只写昵称（否则模型只能抄昵称）")

    # 没有 ID 的消息退化成只写昵称，不能渲染出「阿岚()」这种残缺标签。
    no_id = _msg("没有ID")
    no_id["message_info"]["user_info"].pop("user_id")
    no_id_transcript = extract_mod.format_transcript([no_id], max_chars=1000)
    check("阿岚: 没有ID" in no_id_transcript, "缺 user_id 时说话人只写昵称")
    check("()" not in no_id_transcript, "缺 user_id 时不会渲染出空括号")

    # 群名片优先于昵称（群名片是对方在这个群里希望被叫的名字）。
    card = _msg("名片")
    card["message_info"]["user_info"]["user_cardname"] = "群名片"
    card_transcript = extract_mod.format_transcript([card], max_chars=1000)
    check("群名片(u1):" in card_transcript, "显示名优先用群名片")

    prompt = extract_mod.build_prompt(
        transcript,
        categories=["open_loop", "emotion"],
        max_items=6,
        max_content_chars=30,
        existing_open_loops=["旧约定"],
    )
    check("open_loop" in prompt and "emotion" in prompt, "提示词包含类别")
    check("旧约定" in prompt, "提示词包含已有未闭合话题")
    check("只输出一个 JSON 对象" in prompt, "提示词要求纯 JSON 输出")


async def test_extract_async() -> None:
    print("[4] 抽取层（异步）")
    llm = FakeLlm('{"items":[{"category":"emotion","content":"最近压力大","salience":0.7}]}')
    result = await extract_mod.run_extraction(
        llm,
        transcript="阿岚: 最近有点累",
        categories=["emotion"],
        max_items=5,
        max_content_chars=30,
        task_name="utils",
    )
    check(result.ok is True and len(result.items) == 1, "正常抽取")
    check(result.model == "fake-model", "带回模型名")
    check(llm.calls[0]["task_name"] == "utils", "默认任务名 utils")
    check("temperature" in llm.calls[0], "透传 temperature")
    check("_timeout_ms" not in llm.calls[0], "未指定 rpc_timeout_ms 时不塞 _timeout_ms")

    sized = FakeLlm('{"items":[]}')
    await extract_mod.run_extraction(
        sized,
        transcript="x",
        categories=["emotion"],
        max_items=5,
        max_content_chars=30,
        rpc_timeout_ms=150000,
    )
    check(sized.calls[0].get("_timeout_ms") == 150000, "rpc_timeout_ms 以 _timeout_ms 透传给宿主")

    boom = FakeLlm("", success=False)
    failed = await extract_mod.run_extraction(
        boom,
        transcript="x",
        categories=["emotion"],
        max_items=5,
        max_content_chars=30,
    )
    check(failed.ok is False, "模型失败被收敛成 ok=False")
    check("模型炸了" in failed.error, "错误信息透传")

    class RaisingLlm:
        async def generate(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("网络断了")

    raised = await extract_mod.run_extraction(
        RaisingLlm(),
        transcript="x",
        categories=["emotion"],
        max_items=5,
        max_content_chars=30,
    )
    check(raised.ok is False and "网络断了" in raised.error, "抛异常也被收敛")


# ── 5. 避让层 ──


async def test_avoidance() -> None:
    print("[5] 避让层")
    on = AvoidanceProfile(a_memorix_enabled=True, person_profile_injection=True)
    check(on.item_budget(3) == 2, "A_Memorix 画像注入开启时收紧到 2")
    check(on.skip_categories() == (), "五类都不跳过")

    # 死代码清理的回归：``top_k_scale`` 从来没被 plugin.py 调用过，README 却
    # 一直声称「注入预算再打 6 折」。既然选择删掉而不是接上，就钉住它别被
    # 以后误加回来 —— 否则又会变成「文档说有、代码没接线」。
    check(
        not hasattr(on, "top_k_scale"),
        "top_k_scale 已被删除（它从未被生产代码调用）",
    )
    check(
        not hasattr(AvoidanceProfile(), "person_fact_writeback"),
        "person_fact_writeback 字段已被删除（写入开关与本插件注入职责无交集）",
    )
    # heuristic_recall 保留，但只用于启动日志
    heuristic = AvoidanceProfile(a_memorix_enabled=True, heuristic_recall=True)
    check(
        "heuristic_recall=on" in heuristic.describe(),
        "heuristic_recall 仍进启动日志（它现在只有这一个用途）",
    )

    off = AvoidanceProfile(a_memorix_enabled=False, person_profile_injection=False)
    check(off.item_budget(3) == 3, "A_Memorix 关闭时放开预算")
    check("a_memorix=off" in off.describe(), "描述含开关状态")

    ctx = FakeCtx(tempfile.mkdtemp(), config_values={"a_memorix.plugin.enabled": True})
    AvoidanceProbe = avoidance_mod.AvoidanceProbe

    probe = AvoidanceProbe(ctx)
    profile = await probe.probe()
    check(profile.a_memorix_enabled is True, "读到 A_Memorix 启用")
    check(profile.person_profile_injection is True, "缺省按开启处理")
    check(profile.heuristic_recall is False, "缺省按关闭处理")

    ctx2 = FakeCtx(tempfile.mkdtemp(), config_values={"a_memorix.plugin.enabled": False})
    probe2 = AvoidanceProbe(ctx2)
    profile2 = await probe2.probe()
    check(profile2.a_memorix_enabled is False, "读到 A_Memorix 关闭")
    check(profile2.item_budget(3) == 3, "关闭时预算不收紧")


# ── 5.5 「别把自己记成人」──
#
# 真机 bug：机器人自己的消息在 message.get_recent 里跟别人的一模一样
# （`小鱼(100000002): ...`），而 people 段落的提示词要求「判断聊天记录里
# 出现的人」。模型照做，把她自己当成陌生人写进了人物表 —— 真机上出现过
# `qq:100000002 / 小鱼`，好感度 5.3、六维全基线，等于她把自己当路人群友。
#
# 修法三道：①提示词明说「你」不算出现的人；②把聊天记录里自己的行标成「你」；
# ③落库前再按账号挡一次。下面用 ②③ 的实际行为做回归。


async def test_self_not_person() -> None:
    print("[5.5] 不把自己记成人")

    # ① frame_self_lines：自己说的话改标成「你」，别人的不受影响
    transcript = (
        "[11-04 20:31] 群友A(100000001): 今晚还上号吗\n"
        "[11-04 20:32] 小鱼(100000002): 上号！我抄好阵容了\n"
        "[11-04 20:33] 群友B(100000003): 等等我\n"
    )
    framed = extract_mod.frame_self_lines(transcript, {"100000002"})
    check("你: 上号！我抄好阵容了" in framed, "自己说的话被标成「你」")
    check("小鱼(100000002):" not in framed, "自己的昵称(ID) 标签不再出现")
    check("群友A(100000001): 今晚还上号吗" in framed, "别人的行原样保留")
    check("群友B(100000003): 等等我" in framed, "后续别人的行也保留")

    # 空集合 / 空文本都原样返回，不崩
    check(extract_mod.frame_self_lines(transcript, set()) == transcript, "不知道自己账号时原样返回")
    check(extract_mod.frame_self_lines("", {"100000002"}) == "", "空文本不崩")

    # 按昵称命中（配置里填昵称的兜底路径）
    by_nick = extract_mod.frame_self_lines(transcript, {"小鱼"})
    check("你: 上号！我抄好阵容了" in by_nick, "按昵称也能认出自己")

    # ①½ 差集学习：全量 vs filter_mai
    #
    # 第一版把方向写反了（用 mine-others，而 filtered 恒为全量子集，
    # 结果永远为空），加上数量判断也反了 —— 整套学习逻辑从未生效过一次。
    # 下面按真实形态锁住：全量 3 条（含自己），过滤后 2 条。
    def _msg(uid: str, nick: str = "") -> dict:
        return {"message_info": {"user_info": {"user_id": uid, "user_nickname": nick or uid}}}

    full = [_msg("100000002", "小鱼"), _msg("100000001", "群友A"), _msg("100000003", "群友B")]
    filtered = [_msg("100000001", "群友A"), _msg("100000003", "群友B")]

    learner = tracker_mod.SessionTracker(
        ctx=FakeCtx(tempfile.mkdtemp()),
        store=store_mod.GapStore(os.path.join(tempfile.mkdtemp(), "learn.db")),
        config_getter=lambda: _SelfIdConfig(),
        probe=None,
        logger=FakeCtx(tempfile.mkdtemp()).logger,
    )
    learner._store.open()
    try:
        learner._remember_self_accounts(full, filtered)
        check(learner._self_ids == {"100000002"},
              f"差集学出机器人账号（实际 {sorted(learner._self_ids)}）")

        # 过滤没起作用（两份一样多）时不能把人当自己
        learner2 = tracker_mod.SessionTracker(
            ctx=FakeCtx(tempfile.mkdtemp()),
            store=store_mod.GapStore(os.path.join(tempfile.mkdtemp(), "learn2.db")),
            config_getter=lambda: _SelfIdConfig(), probe=None,
            logger=FakeCtx(tempfile.mkdtemp()).logger,
        )
        learner2._store.open()
        learner2._remember_self_accounts(full, full)
        check(learner2._self_ids == set(), "过滤无效果时不乱认自己")

        # 换号：新号也能学进来（旧号保留）
        learner._remember_self_accounts(
            full + [_msg("9999999", "小鱼二号")],
            filtered,
        )
        check("9999999" in learner._self_ids, "换号后新账号也能学到")
        check("100000002" in learner._self_ids, "旧账号保留（历史消息里仍算自己）")
        learner2._store.close()
    finally:
        learner._store.close()

    # ② 落库前按账号挡一次
    SessionTracker = tracker_mod.SessionTracker

    store = store_mod.GapStore(os.path.join(tempfile.mkdtemp(), "self.db"))
    store.open()
    try:
        entry = _FakeEntry("qq", "100000002")  # 机器人自己的账号
        _cfg = _SelfIdConfig()
        tracker = SessionTracker(
            ctx=FakeCtx(tempfile.mkdtemp()),
            store=store,
            config_getter=lambda: _cfg,
            probe=None,
            logger=FakeCtx(tempfile.mkdtemp()).logger,
        )
        tracker._self_ids = {"100000002"}  # 模拟已经学到自己的账号

        observations = [
            {"user_id": "100000002", "display_name": "小鱼", "tags": [], "axes": {}},
            {"user_id": "100000001", "display_name": "群友A", "tags": [], "axes": {}},
        ]
        written = tracker._apply_people(entry, observations)
        check(written == 1, f"只写入真正的人（实际写了 {written} 条）")
        keys = {row["person_key"] for row in store.list_people(limit=50)}
        check("qq:100000002" not in keys, "机器人自己没有被写进人物表")
        check("qq:100000001" in keys, "别人照常写入")
    finally:
        store.close()



async def test_deference() -> None:
    """称呼闸门：只有 owner 能被叫「主人」。

    ## 这个闸门解决什么

    插件本身不注入称呼，但记忆里存着真实发生过的对话（「小鱼称呼 阿汤
    为主人」「坚持自己是主人群友A的小女仆」）。这些是群里真实的玩笑，
    不该删；可它们每轮被注入，等于每轮都在提醒模型「你有主人，叫主人」。
    真机后果：她对**所有人**都开始叫主人。

    用户选定的方案是「只允许对 owner 叫」。这一节锁住三件事：

    1. ``soften_deference`` 在 ``keep=False`` 时确实换掉了称呼
    2. ``keep=True`` 时一个字都不动（owner 该被叫主人）
    3. ``deference_allowed`` 只认 ``relation_role == "owner"``
    """

    print("[5.5] 称呼闸门")
    soften = compose_mod.soften_deference
    allowed = compose_mod.deference_allowed

    # ── soften_deference：非 owner 场景 ──
    check(
        soften("小鱼称呼 阿汤 为主人", keep=False)
        == "小鱼称呼 阿汤 为对方",
        "非 owner：称呼换成「对方」",
    )
    # 关键：不能用「你」。条目是第三人称记录，换成「你」等于直接告诉模型
    # 「她管你叫主人」—— 那不是消除暗示，是加强暗示。
    check(
        "为你" not in soften("小鱼称呼 阿汤 为主人", keep=False),
        "替换词不能是「你」（会反向加强暗示）",
    )
    check(
        "主人" not in soften("坚持自己是主人群友A的小女仆", keep=False),
        "非 owner：文本里不再出现「主人」",
    )
    # 长的先替换，不能留下「大人」这种残渣
    softened = soften("主人大人今天好", keep=False)
    check("主人" not in softened and "大人" not in softened, f"「主人大人」整体换掉（实际 {softened!r}）")
    # 多个称呼词同时出现
    multi = soften("主人和老公和达令都在", keep=False)
    check(
        not any(word in multi for word in compose_mod.DEFERENCE_WORDS),
        f"多个称呼词全部换掉（实际 {multi!r}）",
    )

    # ── soften_deference：owner 场景（原样保留）──
    original = "小鱼称呼 阿汤 为主人"
    check(soften(original, keep=True) == original, "owner：原文一字不动")

    # ── 边界：空串、无称呼词 ──
    check(soften("", keep=False) == "", "空串原样返回")
    plain = "今天天气不错"
    check(soften(plain, keep=False) == plain, "没有称呼词的文本不受影响")

    # ── deference_allowed：只认 owner ──
    check(allowed({"relation_role": "owner"}, enabled=True), "owner 可以叫主人")
    check(not allowed({"relation_role": "stranger"}, enabled=True), "stranger 不行")
    check(not allowed({"relation_role": "friend"}, enabled=True), "friend 不行")
    check(not allowed({"relation_role": "close_friend"}, enabled=True), "close_friend 不行")
    # 大小写/空格容错
    check(allowed({"relation_role": " Owner "}, enabled=True), "角色值前后空格与大小写都能识别")
    # 关掉闸门时一律放行（恢复旧行为）
    check(allowed({"relation_role": "stranger"}, enabled=False), "闸门关闭时退回旧行为")
    # 没有人物记录 → 不许叫
    check(not allowed(None, enabled=True), "查不到人物记录时不许叫")
    check(not allowed({}, enabled=True), "人物记录里没有 relation_role 时不许叫")

    # ── compose_block 真的把闸门接上了 ──
    items = [
        {
            "id": 1,
            "category": "relationship",
            "content": "小鱼称呼 阿汤 为主人",
            "salience": 0.9,
            "created_ts": 0.0,
            "last_seen_ts": 0.0,
            "pinned": 0,
            "hit_count": 0,
            "last_injected_ts": 0.0,
            "streak": 0,
        }
    ]
    block_denied, _ = compose_mod.compose_block(
        items,
        rotation=False,
        allow_deference=False,
    )
    check("主人" not in block_denied, f"compose_block(allow_deference=False) 里没有「主人」：{block_denied!r}")

    block_allowed, _ = compose_mod.compose_block(
        items,
        rotation=False,
        allow_deference=True,
    )
    check("主人" in block_allowed, "compose_block(allow_deference=True) 保留「主人」")

    # 默认值必须是「不许叫」——忘了传参数时应该安全，而不是放行
    block_default, _ = compose_mod.compose_block(items, rotation=False)
    check(
        "主人" not in block_default,
        f"compose_block 的 allow_deference 默认 False（安全默认）：{block_default!r}",
    )

    # ── 另外两条注入路径也必须过闸门 ──
    # 记忆有四个出口：当前流记忆块、跨流块、召回块、人物块。人物块不渲染条目
    # 正文，所以只剩前三个。漏掉任何一个，那一条路就会继续往模型耳边说「主人」。
    cross_items = [
        {
            "id": 2,
            "category": "relationship",
            "content": "小鱼称呼 阿汤 为主人",
            "salience": 0.9,
            "session_id": "qq:group:other",
            "source_chat_type": "group",
            "source_group_name": "别的群",
        }
    ]
    cross_denied, _ = compose_mod.compose_cross_session_block(cross_items)
    check(
        "主人" not in cross_denied,
        f"跨流块默认也过闸门：{cross_denied!r}",
    )
    cross_allowed, _ = compose_mod.compose_cross_session_block(
        cross_items, allow_deference=True
    )
    check("主人" in cross_allowed, "跨流块在 owner 场景保留「主人」")

    recalled_items = [
        {
            "id": 3,
            "category": "relationship",
            "content": "小鱼称呼 阿汤 为主人",
            "salience": 0.4,
        }
    ]
    recalled_denied, _ = compose_mod.compose_recalled_block(recalled_items)
    check(
        "主人" not in recalled_denied,
        f"召回块默认也过闸门：{recalled_denied!r}",
    )
    recalled_allowed, _ = compose_mod.compose_recalled_block(
        recalled_items, allow_deference=True
    )
    check("主人" in recalled_allowed, "召回块在 owner 场景保留「主人」")


class _FakeEntry:
    def __init__(self, platform: str, user_id: str) -> None:
        self.platform = platform
        self.user_id = user_id
        self.session_id = "s-self"
        self.chat_type = "group"
        self.group_id = "1"
        self.group_name = "g"
        self.nickname = "n"


class _SelfIdConfig:
    """只提供 _apply_people 需要的字段。"""

    class _People:
        auto_role_min_confidence = 0.7
        manual_override = False
        track_impact = True
        impact_decay = 0.95
        roles = ()
        self_identities = ()

    people = _People()


# ── 6. 插件整体 ──


async def test_plugin(tmp: str) -> None:
    print("[6] 插件整体")
    session = "qq:group:g1"
    llm = FakeLlm(
        json.dumps(
            {
                "items": [
                    {"category": "open_loop", "content": "说好周末一起看那部电影", "salience": 0.8},
                    {"category": "emotion", "content": "最近工作压力有点大", "salience": 0.7},
                    {"category": "relationship", "content": "习惯用绰号互相称呼", "salience": 0.6},
                ],
                "resolved": [],
            },
            ensure_ascii=False,
        )
    )
    messages = [
        _msg("周末一起看那部电影吧", session=session),
        _msg("最近工作压力有点大", session=session),
        _msg("好呀，老规矩叫我阿岚就行", session=session),
    ]
    ctx = FakeCtx(tmp, llm=llm, messages=messages)

    plugin = plugin_mod.create_plugin()
    plugin._ctx = ctx
    plugin._config = CompanionGapConfig()

    check(plugin._hook_args if hasattr(plugin, "_hook_args") else True, "插件可实例化")
    check(
        plugin.config.plugin.config_version == plugin_mod.CONFIG_VERSION,
        f"配置版本可读（{plugin_mod.CONFIG_VERSION}）",
    )
    # manifest 的 version 与配置模型的 CONFIG_VERSION 必须同步：宿主按前者识别版本，
    # 配置引擎按后者判断要不要重建配置文件，两边不一致会导致每次启动都当作「版本升级」。
    _manifest_version = json.loads(
        open(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "_manifest.json"),
            "r",
            encoding="utf-8",
        ).read()
    )["version"]
    check(
        _manifest_version == plugin_mod.CONFIG_VERSION,
        f"manifest 版本与 CONFIG_VERSION 一致（{_manifest_version}）",
    )
    check(plugin.config.extract.task_name == "utils", "默认任务名 utils")

    await plugin.on_load()
    check(plugin._store is not None, "on_load 建好存储")
    if plugin._store is not None:
        _OPEN_STORES.append(plugin._store)
    check(os.path.exists(os.path.join(tmp, plugin_mod.DB_FILENAME)), "数据库文件落盘")
    check(plugin._tracker is not None, "后台任务已启动")

    # 捕获消息
    for message in messages:
        result = await plugin.capture_message(message=message)
        check(result == {"action": "continue"}, "观察 hook 返回 continue")
    tracker = plugin._tracker
    assert tracker is not None
    entry = tracker._dirty.get(session)
    check(entry is not None and entry.pending == 3, "脏会话记录 3 条待抽取")
    check(entry.chat_type == "group", "识别为群聊")

    # 直接跑一次抽取（绕过 tick 的等待阈值）
    entry.first_pending_ts = time.time() - 10**9
    await tracker._extract_session(entry)
    check(len(llm.calls) == 1, "抽取调用了一次模型")
    items = plugin._store.list_active(session)  # type: ignore[union-attr]
    check(len(items) == 3, "三条记忆落库")
    categories = {row["category"] for row in items}
    check(categories == {"open_loop", "emotion", "relationship"}, "三类都在")

    # 注入
    injected = await plugin.inject_context(
        session_id=session,
        request_type="reply",
        task_name="replyer",
        extra_prompt="原有额外要求",
        attempt=1,
    )
    check(injected.get("action") == "continue", "注入 hook 返回 continue")
    modified = injected.get("modified_kwargs") or {}
    check("task_name" in modified and modified["task_name"] == "replyer", "回传完整 kwargs（含未改动字段）")
    check("session_id" in modified, "回传 session_id")
    prompt = str(modified.get("extra_prompt") or "")
    check(prompt.startswith("原有额外要求"), "原有 extra_prompt 被保留")
    check("<companion-state>" in prompt, "注入块已追加")
    check("说好周末一起看那部电影" in prompt, "注入内容含抽取结果")

    # 无记忆的会话：不注入 companion-state，但情绪是全局状态，仍然会带上
    other = await plugin.inject_context(session_id="qq:group:none", extra_prompt="")
    other_prompt = str((other.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-state>" not in other_prompt, "无记忆时不注入记忆块")
    check("<companion-mood>" in other_prompt, "无记忆的会话仍会注入情绪块（情绪是全局状态）")

    # ── 多轮注入轮换（用户第 45 轮：「同一批记忆连续多轮原样重复」）──
    # 这一节要端到端证明「下一轮真的换了人」，而不只是排序函数正确。
    rot_sid = "qq:group:rotation"
    store = plugin._store
    assert store is not None
    store.upsert_state(rot_sid, platform="qq", chat_type="group", group_name="轮换测试", pending_count=0)
    rot_topics = [
        "第一条：约好周末去海边看日出",
        "第二条：那把断掉的雨伞还没修",
        "第三条：上次说的那本小说借给谁了",
        "第四条：冰箱里的蛋糕是谁吃掉的",
    ]
    # 显著度递减，这样「没有轮换时」一定是第一条永远占位
    for offset, text in enumerate(rot_topics):
        store.upsert_item(rot_sid, "open_loop", text, salience=0.90 - offset * 0.05)
    # 预算压到只放得下一条，轮换效果才看得见
    def _set_inject(**patch: Any) -> None:
        plugin._config = plugin._config.model_copy(  # type: ignore[assignment]
            update={"inject": plugin._config.inject.model_copy(update=patch)}
        )

    def _set_absence(**patch: Any) -> None:
        plugin._config = plugin._config.model_copy(  # type: ignore[assignment]
            update={"absence": plugin._config.absence.model_copy(update=patch)}
        )

    def _set_boundary(**patch: Any) -> None:
        plugin._config = plugin._config.model_copy(  # type: ignore[assignment]
            update={"boundary": plugin._config.boundary.model_copy(update=patch)}
        )

    def _set_initiative(**patch: Any) -> None:
        plugin._config = plugin._config.model_copy(  # type: ignore[assignment]
            update={"initiative": plugin._config.initiative.model_copy(update=patch)}
        )

    _set_inject(max_items_per_category=1)

    seen_rounds: list[str] = []
    for round_index in range(len(rot_topics)):
        result = await plugin.inject_context(
            session_id=rot_sid,
            request_type="reply",
            task_name="replyer",
            extra_prompt="",
            attempt=1,
        )
        text = str((result.get("modified_kwargs") or {}).get("extra_prompt") or "")
        matched = [topic for topic in rot_topics if topic in text]
        check(len(matched) == 1, f"第 {round_index + 1} 轮只注入一条（预算只够一条）")
        seen_rounds.append(matched[0])
    check(
        len(set(seen_rounds)) >= 3,
        f"连续 {len(rot_topics)} 轮里至少换过 3 条不同记忆（实际 {seen_rounds}）",
    )
    check(seen_rounds[0] != seen_rounds[1], f"相邻两轮不重复同一条（实际 {seen_rounds}）")

    # 记账确实落库了：注入过的条目 streak 与 count 都涨了
    rot_rows = {row["content"]: row for row in store.list_active(rot_sid)}
    injected_rows = [rot_rows[text] for text in seen_rounds]
    check(
        all(int(row.get("inject_count") or 0) >= 1 for row in injected_rows),
        "被注入过的条目累计次数都记上了",
    )
    check(
        int((store.get_item(injected_rows[0]["id"]) or {}).get("inject_streak") or 0) >= 1,
        "第一条的连击也记上了",
    )
    # 显著度按系数微降（默认 0.92）
    check(
        float((store.get_item(injected_rows[0]["id"]) or {}).get("salience") or 1.0) < 0.90,
        "注入过的条目显著度按损耗系数下降",
    )

    # 关掉轮换后应当退回旧行为：永远取显著度最高的那条。
    # 这里要把「注入损耗」也关掉（decay=1.0）并重置显著度，否则显著度会被
    # 前面几轮记账磨低，排序变化的原因就不是「有没有轮换」了 —— 那样测出来的
    # 是两套机制混在一起，断言会骗人。
    _set_inject(rotation=False, inject_salience_decay=1.0)
    for offset, text in enumerate(rot_topics):
        row = next(item for item in store.list_active(rot_sid) if item["content"] == text)
        store.update_item(int(row["id"]), salience=0.90 - offset * 0.05)
    repeated: list[str] = []
    for _ in range(3):
        result = await plugin.inject_context(
            session_id=rot_sid,
            request_type="reply",
            task_name="replyer",
            extra_prompt="",
            attempt=1,
        )
        text = str((result.get("modified_kwargs") or {}).get("extra_prompt") or "")
        matched = [topic for topic in rot_topics if topic in text]
        repeated.append(matched[0] if matched else "")
    check(
        len(set(repeated)) == 1 and repeated[0] == rot_topics[0],
        f"关掉轮换后每轮都注入同一条最高分（这正是要修的旧行为，实际 {repeated}）",
    )
    _set_inject(rotation=True, max_items_per_category=3, inject_salience_decay=0.92)
    store.delete_session_items(rot_sid)
    store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", (rot_sid,))
    store.conn.commit()
    # 跨流断言用的是一个干净的 store 视图，把本轮插的测试数据清掉，
    # 否则后面「别的会话里记得的事」会被这些条目污染。
    store.delete_session_items(rot_sid)

    # ── 端到端：话题相关加权（用户第 11 轮：「还可以再根据聊天的内容筛选一遍」）──
    # 上面 test_compose 已经验过打分与排序，这里验的是**消息从哪来**：
    # replyer 的 before_request 载荷里没有原始消息，只能从 reply_reason /
    # reply_tool_args 里挖。挖不到的时候绝不能少注入。
    rel_sid = "qq:group:relevance"
    store.upsert_state(rel_sid, platform="qq", chat_type="group", group_name="相关测试", pending_count=0)
    store.upsert_item(rel_sid, "open_loop", "约好周末去海边看日出", salience=0.70)
    store.upsert_item(rel_sid, "open_loop", "那把断掉的雨伞还没修", salience=0.80)
    _set_inject(max_items_per_category=1, rotation=True, inject_salience_decay=1.0)

    # 拿不到消息（载荷里既没有 reply_reason 也没有 reply_tool_args）时
    # 仍然照常注入，并且按显著度取 —— 不能因为「不知道对方在说什么」就少注入。
    quiet = await plugin.inject_context(session_id=rel_sid, extra_prompt="", attempt=1)
    quiet_text = str((quiet.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("那把断掉的雨伞还没修" in quiet_text, "拿不到当前消息时仍按显著度注入")

    # reply_reason 里明确聊到海边 → 低分那条靠相关性反超
    hot = await plugin.inject_context(
        session_id=rel_sid,
        extra_prompt="",
        attempt=1,
        reply_reason="对方问之前是不是说过要一起去海边看日出，想问这周末有没有空",
    )
    hot_text = str((hot.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("约好周末去海边看日出" in hot_text, "reply_reason 聊到那条时会把它排到前面")

    # reply_reason 没有时退回 reply_tool_args.reply_reference
    via_args = await plugin.inject_context(
        session_id=rel_sid,
        extra_prompt="",
        attempt=1,
        reply_tool_args={"reply_reference": "之前是不是说过要一起去海边看日出"},
    )
    via_args_text = str((via_args.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("约好周末去海边看日出" in via_args_text, "reply_reason 缺失时退回 reply_tool_args")

    store.delete_session_items(rel_sid)
    store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", (rel_sid,))
    store.conn.commit()
    _set_inject(max_items_per_category=3, rotation=True, inject_salience_decay=0.92)

    # ── 端到端：缺席感（用户第 12 轮）──
    # 数据源是 gap_state.last_seen_ts，所以要真的往库里写一个「很久以前」的时间。
    abs_sid = "qq:private:absence"
    store.upsert_state(abs_sid, platform="qq", chat_type="private", user_id="20001",
                       user_nickname="小明", pending_count=0)
    store.upsert_item(abs_sid, "open_loop", "说好周末一起看电影", salience=0.8)
    store.upsert_person({
        "person_key": "qq:20001", "platform": "qq", "user_id": "20001",
        "display_name": "小明", "relation_role": "friend", "role_confidence": 1.0,
        "role_source": "manual", "affection": 80.0, "tags": [],
    })
    # 隔 5 天：熟人 → 应当到「介意」那一档
    store.upsert_state(abs_sid, last_seen_ts=time.time() - 5 * 86400)

    abs_res = await plugin.inject_context(session_id=abs_sid, extra_prompt="", attempt=1)
    abs_prompt = str((abs_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-absence>" in abs_prompt, "隔了 5 天会注入缺席提示")
    check("</companion-absence>" in abs_prompt, "缺席提示有结束标记")
    check("小明" in abs_prompt or "对方" in abs_prompt, "缺席提示带出对方")

    # 刚说过话 → 不注入
    store.upsert_state(abs_sid, last_seen_ts=time.time())
    fresh_res = await plugin.inject_context(session_id=abs_sid, extra_prompt="", attempt=1)
    fresh_prompt = str((fresh_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-absence>" not in fresh_prompt, "刚说过话时不注入缺席提示")

    # 群聊默认不提缺席
    abs_group = "qq:group:absence"
    store.upsert_state(abs_group, platform="qq", chat_type="group", group_name="冷清群",
                       pending_count=0)
    store.upsert_item(abs_group, "open_loop", "群里约的五黑", salience=0.8)
    store.upsert_state(abs_group, last_seen_ts=time.time() - 5 * 86400)
    grp_res = await plugin.inject_context(session_id=abs_group, extra_prompt="", attempt=1)
    grp_prompt = str((grp_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-absence>" not in grp_prompt, "群聊默认不提缺席（群冷清不是「你冷落我」）")

    # 打开群聊开关后才提，而且措辞是「这个群」不带人
    _set_absence(group_enabled=True)
    grp_on = await plugin.inject_context(session_id=abs_group, extra_prompt="", attempt=1)
    grp_on_prompt = str((grp_on.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-absence>" in grp_on_prompt, "打开群聊开关后会提缺席")
    check("这个群" in grp_on_prompt, "群聊措辞说的是「这个群」")
    _set_absence(group_enabled=False)

    # 关掉总开关 → 完全不注入
    _set_absence(enabled=False)
    off_res = await plugin.inject_context(session_id=abs_sid, extra_prompt="", attempt=1)
    store.upsert_state(abs_sid, last_seen_ts=time.time() - 5 * 86400)
    off_res = await plugin.inject_context(session_id=abs_sid, extra_prompt="", attempt=1)
    off_prompt = str((off_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-absence>" not in off_prompt, "关掉缺席感总开关后不注入")
    _set_absence(enabled=True)

    # ── 端到端：边界感（用户第 12 轮）──
    # 状态提示默认只进 replyer，不进 planner（note_to_planner 默认 False）。
    bd_sid = "qq:private:boundary"
    store.upsert_state(bd_sid, platform="qq", chat_type="private", user_id="20002",
                       user_nickname="老王", pending_count=0)
    store.upsert_item(bd_sid, "open_loop", "上次说的事还没定", salience=0.8)
    store.upsert_person({
        "person_key": "qq:20002", "platform": "qq", "user_id": "20002",
        "display_name": "老王", "relation_role": "friend", "role_confidence": 1.0,
        "role_source": "manual", "affection": 60.0, "tags": [],
        "axes": {"trust": 0.5, "closeness": 0.6, "respect": 0.5,
                 "tension": 0.8, "familiarity": 0.6, "perceived_warmth": 0.5},
    })
    store.upsert_pref({"pref_key": "dislike:被使唤来使唤去", "kind": "dislike",
                       "topic": "被使唤来使唤去", "strength": 0.8, "source": "manual"})

    bd_res = await plugin.inject_context(
        session_id=bd_sid, extra_prompt="", attempt=1,
        reply_reason="对方又说「你赶紧给我查一下，被使唤来使唤去」",
    )
    bd_prompt = str((bd_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("此刻的你" in bd_prompt, "边界状态提示注入到了 replyer")
    check("被使唤来使唤去" in bd_prompt, "边界状态提示带出踩中的厌恶口味")
    check("由你自己决定" in bd_prompt, "把判断权交给模型")
    check("干脆不发" not in bd_prompt, "silence_enabled 默认关时不给出沉默选项")

    # 关掉 note_enabled → 不注入
    _set_boundary(note_enabled=False)
    bd_off = await plugin.inject_context(session_id=bd_sid, extra_prompt="", attempt=1)
    bd_off_prompt = str((bd_off.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("此刻的你" not in bd_off_prompt, "关掉状态提示后不注入")
    _set_boundary(note_enabled=True)

    # planner 侧：默认不进 planner
    # 用一句必然不成立的低阈值来构造，确保闸门不干扰这条断言。
    plan_kwargs = {
        "session_id": bd_sid,
        "items": [
            {"item_type": "system", "meta": {"item_id": "s1", "logical_turn_id": None,
                                             "timestamp": "2026-01-01T00:00:00+08:00"},
             "parts": [{"type": "text", "text": "原有系统提示"}]},
        ],
        "item_schema_version": 1,
        "reply_reason": "被使唤来使唤去",
    }
    plan_res = await plugin.inject_planner_state(**plan_kwargs)
    plan_items = (plan_res.get("modified_kwargs") or {}).get("items") or []
    plan_text = json.dumps(plan_items, ensure_ascii=False)
    check("此刻的你" not in plan_text, "note_to_planner 默认关：planner 里没有状态提示")
    check("原有系统提示" in plan_text, "原有 items 被保留")

    # 打开后：状态提示进 planner，且写进已有的 system item
    _set_boundary(note_to_planner=True)
    plan_on = await plugin.inject_planner_state(**plan_kwargs)
    plan_on_items = (plan_on.get("modified_kwargs") or {}).get("items") or []
    plan_on_text = json.dumps(plan_on_items, ensure_ascii=False)
    check("此刻的你" in plan_on_text, "打开后状态提示进 planner")
    check(len(plan_on_items) == 1, "复用了已有的 system item，没有额外新建")
    check("原有系统提示" in plan_on_text, "原有系统提示仍在同一条 item 里")
    _set_boundary(note_to_planner=False)

    # silence_level 必须真的改变注入文本（端到端）。
    # 历史 bug：plugin 算出了 silence_mode 却只返回不消费，两条配置项渲染结果一样，
    # 「文档说有、代码没接线」的又一处。
    plugin._mood.set_manual(  # type: ignore[union-attr]
        primitives={"anger": 0.7, "joy": 0.02, "calm": 0.1, "anxiety": 0.5},
        energy=0.6,
        label="有点烦躁",
    )
    _set_boundary(silence_enabled=True, silence_level="choose")
    choose_res = await plugin.inject_context(
        session_id=bd_sid, extra_prompt="", attempt=1,
        reply_reason="被使唤来使唤去",
    )
    choose_prompt = str((choose_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("此刻你确实不太想接这个话" in choose_prompt, "沉默闸门通过时注入沉默提示")
    check("干脆不发" in choose_prompt, "choose 模式给出「干脆不发」这个选项")

    _set_boundary(silence_enabled=True, silence_level="taciturn")
    taciturn_res = await plugin.inject_context(
        session_id=bd_sid, extra_prompt="", attempt=1,
        reply_reason="被使唤来使唤去",
    )
    taciturn_prompt = str((taciturn_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("此刻你确实不太想接这个话" in taciturn_prompt, "taciturn 模式下沉默提示仍在")
    check(
        "干脆不发" not in taciturn_prompt,
        "taciturn 模式下注入文本里没有「干脆不发」（不允许真的不回）",
    )

    _set_boundary(silence_enabled=False, silence_level="choose")
    plugin._mood.reset()  # type: ignore[union-attr]
    # 关掉沉默后，配额记账会留下 history，清掉免得影响后续断言。
    store.delete_meta("boundary_silence_at:" + bd_sid)

    # 没有 items 时不能崩，也不能瞎造
    no_items = await plugin.inject_planner_state(session_id=bd_sid)
    check(no_items.get("action") == "continue", "planner hook 在没有 items 时安全返回")

    # ── 端到端：自主发言闸门 ──
    # 这是用户 m07262 明确要的：精力/心情不该去压「发言频率」，而该决定
    # 「这一轮到底开不开口」。宿主不给 abort 权限，所以手段是从工具清单里
    # 把 reply 收走 —— 模型不是被劝别说，是没有说的手段。
    _gate_items = [
        {"item_type": "system", "meta": {"item_id": "g1", "logical_turn_id": None,
                                         "timestamp": "2026-01-01T00:00:00+08:00"},
         "parts": [{"type": "text", "text": "原有系统提示"}]},
        {"item_type": "user", "meta": {"item_id": "g2", "logical_turn_id": None,
                                       "timestamp": "2026-01-01T00:00:01+08:00"},
         "parts": [{"type": "text", "text": "今天天气怎么样"}]},
    ]
    _gate_tools = [
        {"type": "function", "function": {"name": "reply", "description": "回复",
                                          "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "send_emoji", "description": "发表情",
                                          "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "query_memory", "description": "查记忆",
                                          "parameters": {"type": "object", "properties": {}}}},
    ]

    # 默认关：工具清单原样保留，一个字都不动
    _gate_off = await plugin.inject_planner_state(
        session_id=bd_sid, items=json.loads(json.dumps(_gate_items)),
        item_schema_version=1, tool_definitions=json.loads(json.dumps(_gate_tools)),
    )
    _gate_off_tools = (_gate_off.get("modified_kwargs") or {}).get("tool_definitions")
    check(
        [d["function"]["name"] for d in _gate_off_tools]
        == ["reply", "send_emoji", "query_memory"],
        "自主发言闸门默认关：工具清单一个字都不动",
    )

    # 打开闸门 + 低好感 + 心情差 + 没兴趣 → 收回 reply 和 send_emoji
    _set_initiative(enabled=True, threshold=0.45)
    store.upsert_person({
        "person_key": "qq:20002", "platform": "qq", "user_id": "20002",
        "display_name": "老王", "relation_role": "friend", "role_confidence": 1.0,
        "role_source": "manual", "affection": 8.0, "tags": [],
        "axes": {"trust": 0.3, "closeness": 0.2, "respect": 0.4,
                 "tension": 0.3, "familiarity": 0.3, "perceived_warmth": 0.3},
    })
    plugin._mood.set_manual(  # type: ignore[union-attr]
        primitives={"anger": 0.5, "joy": 0.05, "calm": 0.05, "anxiety": 0.5},
        energy=0.3,
        label="有点低落",
    )
    _gate_low = await plugin.inject_planner_state(
        session_id=bd_sid, items=json.loads(json.dumps(_gate_items)),
        item_schema_version=1, tool_definitions=json.loads(json.dumps(_gate_tools)),
    )
    _gate_low_tools = [
        d["function"]["name"]
        for d in ((_gate_low.get("modified_kwargs") or {}).get("tool_definitions") or [])
    ]
    check("reply" not in _gate_low_tools, "低分时收回 reply（没有说的手段）")
    check("send_emoji" not in _gate_low_tools, "连 send_emoji 一起收回（发表情包也是发言）")
    check("query_memory" in _gate_low_tools, "查询类工具不受影响（她还可以默默查资料）")

    # 被 @ → 无论分数多低都要能回
    plugin._tracker.mark_dirty({  # type: ignore[union-attr]
        "session_id": bd_sid, "platform": "qq", "message_info": {
            "group_info": {"group_id": "", "group_name": ""},
            "user_info": {"user_id": "20002", "user_nickname": "老王"},
        }, "is_at": True, "is_mentioned": False,
    })
    _gate_at = await plugin.inject_planner_state(
        session_id=bd_sid, items=json.loads(json.dumps(_gate_items)),
        item_schema_version=1, tool_definitions=json.loads(json.dumps(_gate_tools)),
    )
    _gate_at_tools = [
        d["function"]["name"]
        for d in ((_gate_at.get("modified_kwargs") or {}).get("tool_definitions") or [])
    ]
    check("reply" in _gate_at_tools, "被 @ 时即使分数很低也能开口（@ 必回）")

    # 关掉「@ 必回」后，@ 也要过软评分
    _set_initiative(at_always_reply=False)
    _gate_at_off = await plugin.inject_planner_state(
        session_id=bd_sid, items=json.loads(json.dumps(_gate_items)),
        item_schema_version=1, tool_definitions=json.loads(json.dumps(_gate_tools)),
    )
    _gate_at_off_tools = [
        d["function"]["name"]
        for d in ((_gate_at_off.get("modified_kwargs") or {}).get("tool_definitions") or [])
    ]
    check("reply" not in _gate_at_off_tools, "关掉 @ 必回后，@ 也要过软评分")
    _set_initiative(at_always_reply=True, enabled=False)

    # 关键回归：闸门不能被边界感的提前返回连坐跳过。
    # 历史 bug：整个方法包在一个大 try 里，note_to_planner=False（默认值）
    # 时就 return 了，闸门代码在 return 之后 —— 永远不跑。
    # 用一个没有 @ 记录的会话，免得上面那条 @ 时间窗干扰判定。
    _gate_sid2 = "qq:private:gateonly"
    _set_boundary(note_to_planner=False)
    _set_initiative(enabled=True)
    _gate_with_boundary_off = await plugin.inject_planner_state(
        session_id=_gate_sid2, items=json.loads(json.dumps(_gate_items)),
        item_schema_version=1, tool_definitions=json.loads(json.dumps(_gate_tools)),
    )
    _gate_wbo_tools = [
        d["function"]["name"]
        for d in ((_gate_with_boundary_off.get("modified_kwargs") or {}).get("tool_definitions") or [])
    ]
    check(
        "reply" not in _gate_wbo_tools,
        "note_to_planner 关着时闸门仍要生效（不跟边界感共用一个提前返回）",
    )
    _set_initiative(enabled=False)
    plugin._mood.reset()  # type: ignore[union-attr]

    store.delete_session_items(abs_sid)
    store.delete_session_items(abs_group)
    store.delete_session_items(bd_sid)
    for sid in (abs_sid, abs_group, bd_sid):
        store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", (sid,))
    store.delete_person("qq:20001")
    store.delete_person("qq:20002")
    store.delete_pref("dislike:被使唤来使唤去")
    store.conn.commit()


    # ── 跨流注入 ──
    # 用户的原话：不要让 Bot「刚在私聊或另一个群里聊过一件事，转头到另一个群
    # 就完全不记得」。所以别的会话里显著度高的记忆要能流通过来，并标出出处。
    store = plugin._store
    assert store is not None
    store.upsert_state("qq:group:fangzhou", chat_type="group", group_name="方舟", pending_count=0)
    store.upsert_state("qq:private:xiaoming", chat_type="private", user_nickname="小明", pending_count=0)
    store.upsert_item(
        "qq:group:fangzhou", "open_loop", "群里约好周末联机打那款新游戏", salience=0.9
    )
    store.upsert_item(
        "qq:private:xiaoming", "relationship", "私聊里说过不喜欢被叫全名", salience=0.9
    )

    cross = await plugin.inject_context(session_id="qq:group:none", extra_prompt="")
    cross_prompt = str((cross.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-cross-session>" in cross_prompt, "别的群里的记忆会跨流注入")
    check("在「方舟」里说过" in cross_prompt, "跨流注入标出记忆来自哪个群")
    check("群里约好周末联机打那款新游戏" in cross_prompt, "跨流注入带上了具体内容")
    # 私聊 → 群聊默认关闭（用户明确要求「先做一个总的开关」）
    check(
        "不喜欢被叫全名" not in cross_prompt,
        "私聊记忆默认不流入群聊",
    )

    # 打开总开关后，私聊记忆可以流入群聊，但由模型自己决定提不提
    plugin._config = plugin._config.model_copy(
        update={"inject": plugin._config.inject.model_copy(update={"private_to_group": True})}
    )
    cross_on = await plugin.inject_context(session_id="qq:group:none", extra_prompt="")
    cross_on_prompt = str((cross_on.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("不喜欢被叫全名" in cross_on_prompt, "打开开关后私聊记忆能流入群聊")
    check("私聊「小明」里提过" in cross_on_prompt, "流入群聊的私聊记忆标出「私聊」")
    check(
        "是否在群里提起由你判断" in cross_on_prompt,
        "开启后把「要不要提」的决定权交给模型",
    )

    # 群聊记忆 → 私聊：允许（不需要开关）
    private_view = await plugin.inject_context(session_id="qq:private:someone", extra_prompt="")
    private_prompt = str((private_view.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("在「方舟」里说过" in private_prompt, "群聊记忆可以流入私聊")
    check(
        "不喜欢被叫全名" not in private_prompt,
        "私聊记忆不会串到另一个私聊",
    )
    plugin._config = CompanionGapConfig()

    # 关掉跨流后什么都不带
    plugin._config = plugin._config.model_copy(
        update={"inject": plugin._config.inject.model_copy(update={"cross_session": False})}
    )
    no_cross = await plugin.inject_context(session_id="qq:group:none", extra_prompt="")
    no_cross_prompt = str((no_cross.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-cross-session>" not in no_cross_prompt, "关掉 cross_session 后不注入跨流块")
    plugin._config = CompanionGapConfig()
    for sid in ("qq:group:fangzhou", "qq:private:xiaoming"):
        store.delete_session_items(sid)
        store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", (sid,))
    store.conn.commit()

    # ── 人物注入 ──
    # 用户要求：告诉 Bot「正在聊天的 ta 是谁、你们关系如何」，但**不能注入称呼**，
    # 只注入对应的聊天方式；手动设定的关系优先于自动判断。
    store.upsert_state(
        "qq:group:renwu", chat_type="group", group_name="人物群",
        platform="qq", pending_count=0,
    )
    store.upsert_person(
        {
            "person_key": "qq:30001",
            "platform": "qq",
            "user_id": "30001",
            "display_name": "老王",
            "relation_role": "friend",
            "role_confidence": 1.0,
            "role_source": "manual",
            "affection": 60.0,
            "emotional_impact": 0.4,
            "tags": ["爱讲冷笑话的群友"],
        }
    )

    # 说话人从 reply_tool_args.message_info 里取
    speaker_kwargs = {
        "session_id": "qq:group:renwu",
        "reply_tool_args": {"message_info": {"user_info": {"user_id": "30001"}}},
    }
    person_res = await plugin.inject_context(**speaker_kwargs)
    person_prompt = str((person_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-people>" in person_prompt, "人物块注入到了 extra_prompt")
    check("老王" in person_prompt, "人物块带上了对方的名字")
    check("爱讲冷笑话的群友" in person_prompt, "人物块带上了标签")
    check(
        "你们算朋友" in person_prompt,
        "注入的是聊天方式（按密切度），不是称呼",
    )
    check(
        "（你认定他是朋友）" not in person_prompt,
        "默认不把关系称呼写进提示词",
    )
    check(
        "心情好一些" in person_prompt,
        "情绪影响为正时说明和他聊天会开心",
    )

    # 打开称呼开关后才会写「你认定他是…」
    plugin._config = plugin._config.model_copy(
        update={
            "people": plugin._config.people.model_copy(
                update={"inject_role_label": True}
            )
        }
    )
    labeled = await plugin.inject_context(**speaker_kwargs)
    labeled_prompt = str((labeled.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check(
        "（你认定他是朋友）" in labeled_prompt,
        "打开 inject_role_label 后才写关系称呼",
    )
    plugin._config = CompanionGapConfig()

    # 没记录过的人不注入
    stranger_kwargs = {
        "session_id": "qq:group:renwu",
        "reply_tool_args": {"message_info": {"user_info": {"user_id": "99999"}}},
    }
    stranger_res = await plugin.inject_context(**stranger_kwargs)
    stranger_prompt = str((stranger_res.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-people>" not in stranger_prompt, "没记住的人不注入人物块")

    # 关掉 people.inject 后什么都不带
    plugin._config = plugin._config.model_copy(
        update={"people": plugin._config.people.model_copy(update={"inject": False})}
    )
    no_people = await plugin.inject_context(**speaker_kwargs)
    no_people_prompt = str((no_people.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-people>" not in no_people_prompt, "关掉 people.inject 后不注入")
    plugin._config = CompanionGapConfig()

    # ── 命令层的六维手动锁定 / 解除（用户第 43 轮）──
    ok, reply, _ = await plugin.mcompgap(
        matched_groups={"action": "person", "arg": "axes 30001 trust 0.95"},
        stream_id="qq:group:renwu",
    )
    check(ok is True, "person axes 命令成功")
    check("不会再改" in reply, "person axes 明确告诉用户自动判断不会再改")
    locked_row = store.get_person("qq:30001") or {}
    check(locked_row.get("axes_source") == "manual", "person axes 把六维标成手动锁定")
    check(abs(float(locked_row["axes"]["trust"]) - 0.95) < 1e-6, "person axes 写入了指定维度")

    # 手动改过之后，自动观察改不动
    store.upsert_person(
        person_mod.merge_observation(
            locked_row,
            {"user_id": "30001", "relation_role": "", "role_confidence": 0.99,
             "axes": {"trust": -1.0}},
            now=time.time() + 60.0,
        )
    )
    check(
        abs(float((store.get_person("qq:30001") or {})["axes"]["trust"]) - 0.95) < 1e-6,
        "命令锁定后自动观察改不动六维",
    )

    ok, reply, _ = await plugin.mcompgap(
        matched_groups={"action": "person", "arg": "follow 30001"},
        stream_id="qq:group:renwu",
    )
    check(ok is True and "重新跟随" in reply, "person follow 解除锁定")
    check(
        (store.get_person("qq:30001") or {}).get("axes_source") == "auto",
        "person follow 把六维交还自动流程",
    )
    ok, reply, _ = await plugin.mcompgap(
        matched_groups={"action": "person", "arg": "follow 30001"},
        stream_id="qq:group:renwu",
    )
    check(ok is True and "本来就在" in reply, "重复 follow 是幂等的")
    ok, reply, _ = await plugin.mcompgap(
        matched_groups={"action": "person", "arg": "follow 99998"},
        stream_id="qq:group:renwu",
    )
    check(ok is True and "还没有这个人的记录" in reply, "follow 不存在的人给出提示")

    # ── 手动锁住的六维不参与衰减 ──
    manual_row = dict(store.get_person("qq:30001") or {})
    manual_row["axes_source"] = "manual"
    manual_row["axes_decayed_at"] = 1.0  # 很久以前衰减过
    manual_row["updated_at"] = 1.0
    store.upsert_person(manual_row)
    before_axes = dict(store.get_person("qq:30001") or {})["axes"]
    store.decay_person_axes(half_life_days=90.0, now=time.time() + 400 * 86400.0)
    after_axes = dict(store.get_person("qq:30001") or {})["axes"]
    check(
        all(abs(float(before_axes[k]) - float(after_axes[k])) < 1e-9 for k in before_axes),
        "手动锁定的六维不会随时间衰减（用户调的值停住）",
    )
    # 对照组：解锁后同样的衰减条件会推动它
    store.upsert_person({**manual_row, "axes_source": "auto"})
    store.decay_person_axes(half_life_days=90.0, now=time.time() + 400 * 86400.0)
    auto_axes = dict(store.get_person("qq:30001") or {})["axes"]
    check(
        any(abs(float(auto_axes[k]) - float(after_axes[k])) > 1e-6 for k in auto_axes),
        "没锁定时六维照常衰减",
    )

    # ── 遗忘与「想起」（第 32 轮第 4 点）──
    # 用户原话：「遗忘（标注为『遗忘』，并非删除，不主动读取，
    # 只有他人明确强调才会『想起』）」。
    forget_sid = "qq:group:wangji"
    store.upsert_state(forget_sid, chat_type="group", group_name="忘事群", platform="qq")
    old_id, _ = store.upsert_item(
        forget_sid, "open_loop", "说好下个月一起去看海", salience=0.8
    )
    store.update_item(old_id, status="forgotten")

    # 1) 遗忘的条目不进普通记忆块（不主动读取）
    quiet = await plugin.inject_context(session_id=forget_sid)
    quiet_prompt = str((quiet.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("说好下个月一起去看海" not in quiet_prompt, "遗忘的记忆不会被主动注入")
    check("<companion-recalled>" not in quiet_prompt, "没提到相关内容时不会触发想起")

    # 2) 对方明确提到类似内容 → 想起来，并真的改回 active
    hit = await plugin.inject_context(
        session_id=forget_sid, reply_reason="之前是不是说过要一起去看海来着？"
    )
    hit_prompt = str((hit.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-recalled>" in hit_prompt, "对方明确提到时触发「想起」")
    check("说好下个月一起去看海" in hit_prompt, "想起的内容被注入")
    revived_row = store.get_item(old_id) or {}
    check(
        str(revived_row.get("status")) == "active",
        "想起之后条目真的回到活跃状态（不是只做一次性的文本注入）",
    )

    # 3) 只是擦边提到，不该被唤醒
    store.update_item(old_id, status="forgotten")
    unrelated = await plugin.inject_context(
        session_id=forget_sid, reply_reason="今天天气不错啊"
    )
    unrelated_prompt = str((unrelated.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-recalled>" not in unrelated_prompt, "无关的话不会唤醒遗忘的记忆")
    check(
        str((store.get_item(old_id) or {}).get("status")) == "forgotten",
        "没被唤醒时状态保持遗忘",
    )

    # 4) 关掉 recall_enabled 后彻底想不起来
    plugin._config = plugin._config.model_copy(
        update={
            "retention": plugin._config.retention.model_copy(
                update={"recall_enabled": False}
            )
        }
    )
    disabled = await plugin.inject_context(
        session_id=forget_sid, reply_reason="之前是不是说过要一起去看海来着？"
    )
    disabled_prompt = str((disabled.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("<companion-recalled>" not in disabled_prompt, "关掉 recall_enabled 后不再想起")
    plugin._config = CompanionGapConfig()

    store.delete_session_items(forget_sid)
    store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", (forget_sid,))
    store.conn.commit()

    # ── 自动判断的可信度闸门（走 tracker._apply_people）──
    # 用户举的例子：群友 B 开玩笑说「我才是你的主人」，不能因此把 B 记成主人；
    # 而手动配置过的主人，自动判断永远不能覆盖。
    person_entry = tracker._dirty.get("qq:group:renwu")
    if person_entry is None:
        from types import SimpleNamespace as _NS

        person_entry = _NS(platform="qq", session_id="qq:group:renwu")
    person_entry.platform = "qq"

    # 1) 低置信的「我才是你的主人」不该改角色
    tracker._apply_people(
        person_entry,
        [{"user_id": "40001", "display_name": "捣蛋鬼", "relation_role": "owner",
          "role_confidence": 0.2, "note": "爱开玩笑"}],
    )
    low = store.get_person("qq:40001")
    check(low is not None, "低置信观察仍然建了人物记录")
    if low is not None:
        check(
            low["relation_role"] == "stranger",
            "低置信的「我是你主人」不会被记成主人",
        )
        # 用户第 32 轮第 1 点：标签只能由用户手写，自动抽取不写标签
        check(
            "爱开玩笑" not in (low["tags"] or []),
            "自动抽取不再写人物标签（只由用户手写）",
        )

    # 2) 高置信才允许改角色
    tracker._apply_people(
        person_entry,
        [{"user_id": "40001", "display_name": "捣蛋鬼", "relation_role": "friend",
          "role_confidence": 0.95}],
    )
    high = store.get_person("qq:40001")
    if high is not None:
        check(high["relation_role"] == "friend", "高置信判断可以改角色")
        check(high["role_source"] == "auto", "自动判断的来源标成 auto")

    # 3) 手动配置过的主人，自动判断改不动
    before = store.get_person("qq:30001") or {}
    tracker._apply_people(
        person_entry,
        [{"user_id": "30001", "display_name": "老王", "relation_role": "stranger",
          "role_confidence": 0.99}],
    )
    after = store.get_person("qq:30001") or {}
    check(
        after.get("relation_role") == before.get("relation_role"),
        "手动配置的关系不会被自动判断覆盖",
    )
    check(after.get("role_source") == "manual", "手动来源保持不变")

    # 4) 情绪影响会累积
    check(
        float(after.get("emotional_impact") or 0) != 0.0,
        "互动会累积这个人带来的情绪影响",
    )

    # 收尾清理，别把测试数据留给后面的断言
    for key in ("qq:30001", "qq:40001"):
        store.delete_person(key)
    store.delete_session_items("qq:group:renwu")
    store.conn.execute("DELETE FROM gap_state WHERE session_id = ?", ("qq:group:renwu",))
    store.conn.commit()

    # 回复 hook：OBSERVE 模式，只记录、不改写已发出的内容
    mood_before = plugin._mood.snapshot()  # type: ignore[union-attr]
    replied = await plugin.observe_reply(response="哈哈哈哈好耶！" * 6)
    check(replied == {"action": "continue"}, "回复 hook 返回 continue（不改写已发出的内容）")
    mood_after = plugin._mood.snapshot()  # type: ignore[union-attr]
    check(
        (mood_after.energy, mood_after.valence) != (mood_before.energy, mood_before.valence),
        "Bot 自己说过话后情绪发生变化（长回复会掉精力）",
    )
    # 空回复不应产生任何影响。
    # 注意：快照本身会按时间衰减，所以这里必须钉死同一个 now，
    # 否则「两次读快照之间过了几毫秒」也会让精力变一点点，断言随机失败。
    frozen_now = time.time()
    frozen = plugin._mood.snapshot(now=frozen_now)  # type: ignore[union-attr]
    await plugin.observe_reply(response="")
    after_empty = plugin._mood.snapshot(now=frozen_now)  # type: ignore[union-attr]
    check(
        (after_empty.energy, after_empty.valence) == (frozen.energy, frozen.valence),
        "空回复不改变情绪",
    )

    # 情绪块随状态改变：手动把愤怒拉满后，注入文本里必须出现对应指令
    plugin._mood.set_manual(  # type: ignore[union-attr]
        primitives={"anger": 0.85, "joy": 0.05, "calm": 0.15},
        energy=0.55,
        label="有点烦躁",
    )
    angry = await plugin.inject_context(session_id="qq:group:none", extra_prompt="")
    angry_prompt = str((angry.get("modified_kwargs") or {}).get("extra_prompt") or "")
    check("有点烦躁" in angry_prompt, "手动改情绪后，注入块立刻反映新标签")
    check("话更短" in angry_prompt, "愤怒状态下注入块给出「话更短」的行为指令")
    plugin._mood.reset()  # type: ignore[union-attr]

    # 管理命令
    ok, reply, intercept = await plugin.mcompgap(matched_groups={"action": "status"}, stream_id=session)
    check(ok is True and intercept is True, "status 命令成功且拦截")
    check("会话" in reply, "status 输出会话信息")

    ok, reply, _ = await plugin.mcompgap(matched_groups={"action": "list"}, stream_id=session)
    check(ok and "open_loop" in reply, "list 输出条目")

    ok, reply, _ = await plugin.mcompgap(matched_groups={"action": "global"}, stream_id=session)
    check(ok and "会话数" in reply, "global 输出统计")

    ok, reply, _ = await plugin.mcompgap(matched_groups={"action": "help"}, stream_id=session)
    check(ok and "/mcompgap status" in reply, "help 输出帮助")

    ok, reply, _ = await plugin.mcompgap(matched_groups={}, text="/mcompgap status", stream_id=session)
    check(ok and "会话" in reply, "无 matched_groups 时从 text 解析")

    ok, reply, _ = await plugin.mcompgap(matched_groups={"action": "clear"}, stream_id=session)
    check(ok and "已清空" in reply, "clear 清空")
    check(len(plugin._store.list_active(session)) == 0, "清空后无条目")  # type: ignore[union-attr]

    await plugin.on_unload()
    check(plugin._store is None, "on_unload 释放存储")


class _FakeAdminPlugin(webui_mod.MemoryAdminMixin):
    """只带 ``_store`` 的替身：WebUI 接口只依赖这一个属性。"""

    def __init__(self, store: Any) -> None:
        self._store = store


async def test_webui(tmp: str) -> None:
    print("[6] WebUI 管理接口")
    db = os.path.join(tmp, "webui_test.db")
    store = store_mod.GapStore(db)
    store.open()
    _OPEN_STORES.append(store)

    now = time.time()
    store.upsert_state(
        "s1", platform="qq", chat_type="group", group_id="g1", group_name="测试群", last_seen_ts=now
    )
    store.upsert_state(
        "s2", platform="qq", chat_type="private", user_id="u9", user_nickname="阿岚", last_seen_ts=now
    )
    store.upsert_item("s1", "open_loop", "说好周末一起看那部电影", salience=0.8, now=now)
    store.upsert_item("s1", "emotion", "最近工作压力有点大", salience=0.5, now=now)
    store.upsert_item("s2", "self", "答应过不用敬语称呼对方", salience=0.4, now=now)
    store.upsert_item("s1", "creative", "给那只流浪猫起名叫豆豆", salience=0.6, now=now)
    # 专门用来测 LIKE 转义：内容里带字面百分号
    store.upsert_item("s1", "open_loop", "折扣写着 100% 优惠但没细看", salience=0.3, now=now)

    admin = _FakeAdminPlugin(store)

    # ── mem_stats ──
    stats = await admin.mem_stats()
    check(stats["ok"] is True, "mem_stats 成功")
    check(stats["sessions"] == 2, "mem_stats 会话数 2")
    check(stats["items_by_status"].get("active") == 5, "mem_stats 活跃 5 条")
    check(stats["items_by_category"].get("open_loop") == 2, "mem_stats 按类别分组")
    check(len(stats["categories"]) == 5, "mem_stats 返回五个类别")
    check(len(stats["statuses"]) == 4, "mem_stats 返回四个状态（含遗忘）")
    check(
        [item["value"] for item in stats["statuses"]] == ["active", "resolved", "expired", "forgotten"],
        "状态顺序固定，遗忘排在最后",
    )
    check(
        any(item["value"] == "forgotten" and item["label"] == "遗忘" for item in stats["statuses"]),
        "遗忘状态有中文标签",
    )
    check("items_pinned" in stats, "mem_stats 带置顶数")
    check("items_doubted" in stats, "mem_stats 带存疑数")
    check(all(item["label"] for item in stats["categories"]), "类别都有中文标签")

    # ── mem_sessions ──
    sessions = await admin.mem_sessions()
    check(sessions["ok"] is True, "mem_sessions 成功")
    check(len(sessions["sessions"]) == 2, "mem_sessions 两个会话")
    by_id = {item["session_id"]: item for item in sessions["sessions"]}
    check(by_id["s1"]["label"] == "测试群", "群会话用群名作标签")
    check(by_id["s2"]["label"] == "阿岚", "私聊会话用昵称作标签")
    check(by_id["s1"]["total_count"] == 4, "会话条目总数正确")
    check(by_id["s1"]["active_count"] == 4, "会话活跃数正确")

    # ── mem_items：分页 ──
    page = await admin.mem_items()
    check(page["ok"] is True, "mem_items 成功")
    check(page["total"] == 5, "mem_items 总数 5")
    check(page["limit"] == 50, "默认每页 50")
    check(page["has_more"] is False, "一页装得下时 has_more=False")
    check(len(page["items"]) == 5, "返回 5 条")
    check(page["items"][0]["salience"] >= page["items"][-1]["salience"], "按显著度降序")

    first = await admin.mem_items(limit=2)
    check(len(first["items"]) == 2, "limit=2 生效")
    check(first["has_more"] is True, "还有更多时 has_more=True")
    second = await admin.mem_items(limit=2, offset=2)
    check(len(second["items"]) == 2, "offset=2 生效")
    overlap = {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    check(not overlap, "分页不重复")

    # 越界参数要夹住，不能让宿主 422 也不能让 SQL 拿到怪值
    check((await admin.mem_items(limit=0))["limit"] == 50, "limit=0 回退默认")
    check((await admin.mem_items(limit=9999))["limit"] == 200, "limit 上限 200")
    check((await admin.mem_items(offset=-5))["offset"] == 0, "offset 负数归零")

    # ── mem_items：过滤 ──
    only_s2 = await admin.mem_items(session_id="s2")
    check(only_s2["total"] == 1, "按会话过滤")
    only_emotion = await admin.mem_items(category="emotion")
    check(only_emotion["total"] == 1, "按类别过滤")
    only_open = await admin.mem_items(category="open_loop", session_id="s1")
    check(only_open["total"] == 2, "会话+类别组合过滤")

    # ── mem_items：搜索与 LIKE 转义 ──
    hit = await admin.mem_items(search="流浪猫")
    check(hit["total"] == 1, "搜索命中")
    check("豆豆" in hit["items"][0]["content"], "搜索结果内容正确")
    percent = await admin.mem_items(search="%")
    check(percent["total"] == 1, "百分号按字面匹配而不是通配符")
    check(not (await admin.mem_items(search="不存在的词"))["items"], "搜不到时返回空列表")
    # 下划线同样要转义，否则 'a_b' 会匹配 'axb'
    store.upsert_item("s1", "self", "a_b 是字面下划线", salience=0.2, now=now)
    store.upsert_item("s1", "self", "axb 不该被匹配", salience=0.2, now=now)
    underscore = await admin.mem_items(search="a_b")
    check(underscore["total"] == 1, "下划线按字面匹配")
    check("a_b" in underscore["items"][0]["content"], "下划线匹配到正确条目")
    # 清理这两条，避免影响后面的计数
    for item in (await admin.mem_items(search="a_b"))["items"] + (
        await admin.mem_items(search="axb")
    )["items"]:
        store.delete_item(item["id"])

    # ── mem_update ──
    target = (await admin.mem_items(search="那部电影"))["items"][0]
    updated = await admin.mem_update(item_id=target["id"], content="说好周日一起看那部电影")
    check(updated["ok"] is True, "mem_update 成功")
    check(updated["item"]["content"] == "说好周日一起看那部电影", "内容已更新")
    check(updated["item"]["salience"] == target["salience"], "未传的字段保持不变")

    changed = await admin.mem_update(
        item_id=target["id"], category="creative", salience=0.95, status="resolved"
    )
    check(changed["ok"] is True, "多字段一起改成功")
    check(changed["item"]["category"] == "creative", "类别已更新")
    check(abs(changed["item"]["salience"] - 0.95) < 1e-6, "显著度已更新")
    check(changed["item"]["status"] == "resolved", "状态已更新")
    check(changed["item"]["status_label"] == "已解决", "状态带回中文标签")

    no_field = await admin.mem_update(item_id=target["id"])
    check(no_field["ok"] is False and "没有要修改的字段" in no_field["error"], "空更新被拒绝")

    missing = await admin.mem_update(item_id=999999, content="x")
    check(missing["ok"] is False and "不存在" in missing["error"], "改不存在的条目报错")

    bad_category = await admin.mem_update(item_id=target["id"], category="不存在")
    check(bad_category["ok"] is False and "未知类别" in bad_category["error"], "非法类别被拒绝")

    bad_status = await admin.mem_update(item_id=target["id"], status="不存在")
    check(bad_status["ok"] is False and "未知状态" in bad_status["error"], "非法状态被拒绝")

    bad_salience = await admin.mem_update(item_id=target["id"], salience=1.5)
    check(bad_salience["ok"] is False and "显著度" in bad_salience["error"], "显著度越界被拒绝")

    blank = await admin.mem_update(item_id=target["id"], content="   ")
    check(blank["ok"] is False, "空白内容不算修改")

    # ── mem_create：人工写入不去重 ──
    created = await admin.mem_create(session_id="s1", category="open_loop", content="手工加的条目", salience=0.7)
    check(created["ok"] is True, "mem_create 成功")
    check(created["item"]["content"] == "手工加的条目", "新增内容正确")
    check(created["item"]["status"] == "active", "新增默认活跃")
    check(created["item"]["source_count"] == 1, "新增 source_count=1")
    again = await admin.mem_create(session_id="s1", category="open_loop", content="手工加的条目")
    check(again["ok"] is True and again["item"]["id"] != created["item"]["id"], "相同内容重复新增不被合并")

    no_session = await admin.mem_create(session_id="  ", category="open_loop", content="x")
    check(no_session["ok"] is False and "会话" in no_session["error"], "缺会话时拒绝新增")
    bad_create = await admin.mem_create(session_id="s1", category="乱写", content="x")
    check(bad_create["ok"] is False and "未知类别" in bad_create["error"], "新增非法类别被拒绝")

    # ── mem_delete ──
    doomed = again["item"]["id"]
    removed = await admin.mem_delete(item_id=doomed)
    check(removed["ok"] is True and removed["deleted"] == doomed, "mem_delete 成功")
    check(store.get_item(doomed) is None, "删除后查不到")
    gone = await admin.mem_delete(item_id=doomed)
    check(gone["ok"] is False and "不存在" in gone["error"], "重复删除报错")

    # ── mem_purge_session ──
    before = (await admin.mem_items(session_id="s1"))["total"]
    check(before > 0, "清空前该会话有条目")
    purged = await admin.mem_purge_session(session_id="s1")
    check(purged["ok"] is True and purged["removed"] == before, "清空返回删除条数")
    check((await admin.mem_items(session_id="s1"))["total"] == 0, "清空后该会话为空")
    check((await admin.mem_items(session_id="s2"))["total"] == 1, "其他会话不受影响")
    check(
        (await admin.mem_purge_session(session_id=" "))["ok"] is False,
        "缺会话时拒绝清空",
    )

    # ── 老库人物不能编造矛盾描述（页面侧）──
    # 老记录的六维是从一个好感度标量反推的，所谓「冲突」全是反推公式的副产品。
    # 注入侧 render_person_block 用 has_observed_axes 挡住了这一层，页面侧
    # 的 _person_payload 必须同样挡住，否则用户在面板上会看到一句假话
    # （认识最久的人被说成「他刚跟你搭上话」）。
    store.upsert_person(
        {
            "person_key": "qq:legacy",
            "platform": "qq",
            "user_id": "legacy",
            "display_name": "老朋友",
            "relation_role": "owner",
            "role_confidence": 1.0,
            "role_source": "manual",
            "affection": 100.0,
            "emotional_impact": 0.5,
            "tags": [],
            "first_seen_ts": now,
            "last_seen_ts": now,
        }
    )
    legacy_people = (await admin.mem_people(limit=50))["people"]
    legacy = next(item for item in legacy_people if item["person_key"] == "qq:legacy")
    check(legacy["axes"]["trust"] > 0.9, "老库好感 100 反推出高信任")
    check(legacy["contrasts"] == [], "老库记录不编造矛盾描述")
    check(all("刚跟你搭上话" not in line for line in legacy["contrasts"]), "老熟人不会被说成刚认识")
    check(isinstance(legacy["dominant"], list), "老库记录仍给出主要维度（不至于空着）")
    # 真观测过的六维才谈得上矛盾
    store.upsert_person(
        {
            "person_key": "qq:observed",
            "platform": "qq",
            "user_id": "observed",
            "display_name": "熟人",
            "relation_role": "friend",
            "role_confidence": 1.0,
            "role_source": "manual",
            "affection": 60.0,
            "emotional_impact": 0.3,
            "tags": [],
            "axes": {
                "trust": 0.2,
                "closeness": 0.9,
                "respect": 0.5,
                "tension": 0.1,
                "familiarity": 0.8,
                "perceived_warmth": 0.5,
            },
            "first_seen_ts": now,
            "last_seen_ts": now,
        }
    )
    observed_people = (await admin.mem_people(limit=50))["people"]
    observed = next(item for item in observed_people if item["person_key"] == "qq:observed")
    check(bool(observed["contrasts"]), "观测到的六维会给出矛盾描述")
    check(
        any("熟" in line or "信" in line for line in observed["contrasts"]),
        "矛盾描述说的是熟和信任之间的冲突",
    )
    check(observed["axes"]["closeness"] == 0.9, "观测值原样下发")
    for key in ("person_key", "axes", "axes_text", "contrasts", "dominant"):
        check(key in observed, f"人物载荷带 {key}")
    check(len(observed["axes"]) == 6, "人物载荷带六个维度")
    store.delete_person("qq:legacy")
    store.delete_person("qq:observed")

    # ── 手调六维会锁住（用户第 43 轮：「我调的好感度没有变回去」）──
    store.upsert_person(
        {
            "person_key": "qq:locked",
            "platform": "qq",
            "user_id": "locked",
            "display_name": "手调过的",
            "relation_role": "friend",
            "role_confidence": 1.0,
            "role_source": "auto",
            "affection": 30.0,
            "emotional_impact": 0.2,
            "tags": [],
            "first_seen_ts": now,
            "last_seen_ts": now,
        }
    )
    just_sliders = await admin.mem_person_update(
        person_key="qq:locked",
        axes={"trust": 0.95, "closeness": 0.9, "respect": 0.8,
              "tension": 0.05, "familiarity": 0.5, "perceived_warmth": 0.9},
    )
    check(just_sliders["ok"] is True, "手调六维成功")
    check(just_sliders["person"]["axes_source"] == "manual", "手调六维后标记成 manual")
    check(just_sliders["person"]["axes_manual"] is True, "载荷里的 axes_manual 同步为 True")
    locked_affection = just_sliders["person"]["affection"]
    # 自动观察再来一次，不能改动它
    store.upsert_person(
        person_mod.merge_observation(
            store.get_person("qq:locked"),
            {"user_id": "locked", "relation_role": "", "role_confidence": 0.99,
             "axes": {"trust": -1.0, "closeness": -1.0}},
            now=now + 600.0,
        )
    )
    still = store.get_person("qq:locked") or {}
    # 载荷里的好感度四舍五入到 1 位小数，所以比较时也要按同样的精度。
    check(
        round(float(still.get("affection") or 0), 1) == round(float(locked_affection), 1),
        "手调过的六维不会被后续自动观察改回（好感度保持不变）",
    )
    check(still.get("axes_source") == "manual", "自动观察不会把手调状态改回 auto")

    # 只给 affection 的老路径同样要锁
    by_scalar = await admin.mem_person_update(person_key="qq:locked", affection=77.0)
    check(by_scalar["ok"] is True, "只给好感度也能改")
    check(by_scalar["person"]["axes_source"] == "manual", "只给好感度时也标记 manual")
    check(abs(by_scalar["person"]["affection"] - 77.0) < 1.5, "好感度按给的值反摊后回算一致")

    # 反向开关：交给自动流程
    released = await admin.mem_person_update(person_key="qq:locked", follow_chat=True)
    check(released["ok"] is True, "follow_chat 解除锁定成功")
    check(released["person"]["axes_source"] == "auto", "解除后 axes_source 变回 auto")
    check(released["person"]["axes_manual"] is False, "载荷里的 axes_manual 同步为 False")
    # 解除后自动观察重新生效
    moved = person_mod.merge_observation(
        store.get_person("qq:locked"),
        {"user_id": "locked", "relation_role": "", "role_confidence": 0.99,
         "axes": {"trust": -1.0}},
        now=now + 1200.0,
    )
    check(moved["axes"]["trust"] < 0.95, "解除锁定后六维重新跟着聊天变化")

    # 已经在 auto 上再点「跟随」不该算一次改动
    noop = await admin.mem_person_update(person_key="qq:locked", follow_chat=True)
    check(noop["ok"] is False and "没有要改的字段" in noop["error"], "重复解除锁定不产生空写")
    store.delete_person("qq:locked")

    # ── 存储未初始化时的兜底 ──
    broken = _FakeAdminPlugin(None)
    broken_stats = await broken.mem_stats()
    check(broken_stats["ok"] is False and "尚未初始化" in broken_stats["error"], "存储未初始化时返回错误而非崩溃")

    # ── webui.json 与代码的一致性（不依赖宿主）──
    declaration_path = os.path.join(_HERE, "webui.json")
    with open(declaration_path, encoding="utf-8") as handle:
        declaration = json.load(handle)
    check(declaration["schema_version"] == 2, "webui.json schema_version 2")
    check("custom_pages" in declaration["required_capabilities"], "webui.json 声明 custom_pages")
    page = declaration["pages"][0]
    check(page["mode"] == "custom", "webui.json 页面是 custom")
    check(os.path.isfile(os.path.join(_HERE, page["entry"])), "entry 指向的文件存在")
    for kind in ("queries", "actions"):
        for name, binding in page[kind].items():
            method = getattr(webui_mod.MemoryAdminMixin, binding["api"], None)
            check(method is not None, f"{kind}.{name} 的 api {binding['api']} 有对应方法")
            if method is not None:
                params = set(method.__code__.co_varnames[: method.__code__.co_argcount])
                params.discard("self")
                declared = set(binding.get("parameters") or {})
                check(declared <= params, f"{binding['api']} 声明的参数都在方法签名里")
    # 页面文件必须能作为 body 片段使用（宿主会把它整个塞进 <body>）
    document = open(os.path.join(_HERE, page["entry"]), encoding="utf-8").read()
    check(len(document) > 1000, "页面 HTML 非空")
    check("window.maibot" in document, "页面用 window.maibot 通信")
    store.close()
    _OPEN_STORES.remove(store)


# ── 7. 配置项中文与一致性 ──


def test_config() -> None:
    """配置模型的每个字段都要有中文 label 与 hint，且与 config.example.toml 严格对齐。

    宿主的配置页取的是 ``json_schema_extra["label"]`` / ``["hint"]``
    （``maibot_sdk/config.py`` 的 ``_build_field_schema``），只写 ``description``
    页面上会退回显示英文裸键名。这条测试就是防止以后新增字段忘了写。

    ``config.toml`` 不进版本控制（市场规范不允许仓库根目录有它，而且它装的是
    使用者自己的真实开关），仓库里只有 ``config.example.toml``。两个都要能读：
    发布出去的副本只有 example，本机开发副本两个都有。
    """

    print("[7] 配置项中文与一致性")
    import tomllib

    config_path = os.path.join(_HERE, "config.example.toml")
    if not os.path.exists(config_path):
        config_path = os.path.join(_HERE, "config.toml")
    with open(config_path, "rb") as handle:
        toml_data = tomllib.load(handle)

    defaults = CompanionGapConfig().model_dump()
    sections = (
        "plugin",
        "capture",
        "extract",
        "inject",
        "retention",
        "mood",
        "people",
        "prefs",
        "absence",
        "boundary",
        "presence",
        "backfill",
        "initiative",
        "history",
        "debug",
    )

    # ── 节与键必须双向对齐，防止「代码加了配置项但 toml 没加」或反之 ──
    check(set(toml_data) == set(sections), f"config.toml 恰好 {len(sections)} 个节")
    for section in sections:
        model_keys = set(type(getattr(CompanionGapConfig(), section)).model_fields)
        toml_keys = set(toml_data.get(section) or {})
        check(model_keys == toml_keys, f"[{section}] 的键与配置模型完全一致")

    # ── 每个字段都要有中文 label 与 hint ──
    has_cjk = lambda text: any("\u4e00" <= ch <= "\u9fff" for ch in text)  # noqa: E731
    total = 0
    missing_label: list[str] = []
    missing_hint: list[str] = []
    for section in sections:
        model = type(getattr(CompanionGapConfig(), section))
        for name, field in model.model_fields.items():
            total += 1
            extra = field.json_schema_extra or {}
            label = str(extra.get("label") or "")
            hint = str(extra.get("hint") or "")
            if not label or label == name or not has_cjk(label):
                missing_label.append(f"[{section}].{name}")
            if not hint or not has_cjk(hint):
                missing_hint.append(f"[{section}].{name}")
    check(total == 168, f"{len(sections)} 个节共 168 个配置项（实际 {total}）")
    check(not missing_label, f"每个配置项都有中文 label（缺 {missing_label}）")
    check(not missing_hint, f"每个配置项都有中文 hint（缺 {missing_hint}）")

    # ── 每个字段都还要保留 description（给读代码的人看）──
    no_desc = [
        f"[{section}].{name}"
        for section in sections
        for name, field in type(getattr(CompanionGapConfig(), section)).model_fields.items()
        if not (field.description or "").strip()
    ]
    check(not no_desc, f"每个配置项都保留 description（缺 {no_desc}）")

    # ── max_tokens 默认值必须与 config.toml 一致，且 0 表示「交给宿主」──
    check(CompanionGapConfig().extract.max_tokens == 1200, "max_tokens 默认 1200")
    check(toml_data["extract"]["max_tokens"] == 1200, "config.toml 里 max_tokens 也是 1200")
    # empty_response_retries 之前只存在于代码、没写进 config.toml，现在两边都要有
    check("empty_response_retries" in toml_data["extract"], "config.toml 补上了 empty_response_retries")

    # ── 配置页能拿到的类别下拉选项要是中文名 ──
    check(len(config_mod.CATEGORY_LABELS) == 5, "五个类别都有中文名")
    check(
        set(config_mod.CATEGORY_LABELS) == set(config_mod.ALL_CATEGORIES),
        "中文名与类别集合一一对应",
    )

    # ── 八个情绪基元的基线/半衰期都必须是可配项（用户明确要求配置页可调）──
    mood_keys = set(type(CompanionGapConfig().mood).model_fields)
    for name in mood_mod.EMOTIONS:
        check(f"emotion_{name}_baseline" in mood_keys, f"愉悦等八个基元都有基线配置项（{name}）")
        check(
            f"emotion_{name}_half_life_minutes" in mood_keys,
            f"八个基元都有半衰期配置项（{name}）",
        )
    check("valence_baseline" not in mood_keys, "已弃用的 valence_baseline 不在配置里")
    check("valence_half_life_hours" not in mood_keys, "已弃用的 valence_half_life_hours 不在配置里")

    # ── 跨流记忆的四个开关 ──
    inject_keys = set(type(CompanionGapConfig().inject).model_fields)
    for key in ("cross_session", "cross_session_limit", "cross_session_min_salience", "private_to_group"):
        check(key in inject_keys, f"[inject] 有跨流配置项 {key}")
    inject_cfg = CompanionGapConfig().inject
    check(inject_cfg.cross_session is True, "群聊记忆默认跨流（用户要求群聊可以通入私聊）")
    check(
        inject_cfg.private_to_group is False,
        "私聊记忆默认不流入群聊（用户要求先做一个总开关，默认关）",
    )
    check(
        inject_cfg.cross_session_min_salience > inject_cfg.min_salience,
        "跨流条目要求更高的显著度（否则无关小事会到处冒出来）",
    )


def test_mood(tmp: str) -> None:
    """情绪引擎：八基元、派生量、衰减、混合、标签/指令、启发式、渲染、持久化。"""

    print("[8] 情绪引擎")
    MoodState = mood_mod.MoodState
    MoodEngine = mood_mod.MoodEngine
    EMOTIONS = mood_mod.EMOTIONS
    BASES = dict(mood_mod.DEFAULT_PRIMITIVES)

    # ── 八个基元必须齐全，且每个都有中文名与默认基线 ──
    check(len(EMOTIONS) == 8, "情绪基元一共八个")
    check(
        set(EMOTIONS)
        == {"joy", "anger", "anxiety", "sadness", "interest", "calm", "disgust", "surprise"},
        "八个基元就是用户点名的那八种（含厌恶与惊讶）",
    )
    check(set(mood_mod.EMOTION_LABELS) == set(EMOTIONS), "每个基元都有中文名")
    check(set(BASES) == set(EMOTIONS), "每个基元都有默认基线")
    check(set(mood_mod.DEFAULT_HALF_LIVES_MINUTES) == set(EMOTIONS), "每个基元都有默认半衰期")
    check(
        mood_mod.DEFAULT_HALF_LIVES_MINUTES["anger"]
        < mood_mod.DEFAULT_HALF_LIVES_MINUTES["sadness"],
        "愤怒比伤心退得快（符合常识）",
    )

    # ── clamp / clamp_primitive：越界与非法输入 ──
    check(mood_mod.clamp("valence", 5) == 1.0, "clamp 把 valence 上限夹到 1.0")
    check(mood_mod.clamp("valence", -5) == -1.0, "clamp 把 valence 下限夹到 -1.0")
    check(mood_mod.clamp("energy", -0.5) == 0.0, "clamp 把 energy 下限夹到 0.0")
    check(mood_mod.clamp("arousal", "坏值") == 0.5, "clamp 对非法输入回落到区间中位")
    check(mood_mod.clamp("arousal", float("nan")) == 0.5, "clamp 对 NaN 回落到区间中位")
    check(mood_mod.clamp_primitive("joy", 5) == 1.0, "clamp_primitive 把基元上限夹到 1.0")
    check(mood_mod.clamp_primitive("joy", -5) == 0.0, "clamp_primitive 把基元下限夹到 0.0")
    check(
        abs(mood_mod.clamp_primitive("anger", "坏值") - BASES["anger"]) < 1e-9,
        "clamp_primitive 对非法输入回落到该基元自己的基线",
    )

    # ── 派生量：用「相对基线的偏差」算，而不是绝对值 ──
    # 同样的 anger=0.30，对暴躁人格是常态、对温和人格是大事，所以必须减基线。
    flat = dict(BASES)
    check(abs(mood_mod.derive_valence(flat, BASES)) < 1e-9, "全部等于基线时愉悦度为 0")
    happy = dict(flat, joy=0.85)
    sad = dict(flat, sadness=0.85)
    check(mood_mod.derive_valence(happy, BASES) > 0.5, "愉悦基元拉高愉悦度")
    check(mood_mod.derive_valence(sad, BASES) < -0.5, "伤心基元压低愉悦度")
    check(
        abs(mood_mod.derive_arousal(flat, BASES) - 0.35) < 1e-9,
        "全部等于基线时情绪强度等于基线 0.35",
    )
    check(
        mood_mod.derive_arousal(dict(flat, anger=0.9), BASES)
        > mood_mod.derive_arousal(dict(flat, calm=0.9), BASES),
        "愤怒抬高情绪强度，平静压低情绪强度",
    )
    check(
        mood_mod.derive_valence({"joy": 1.0, "anger": 1.0}, BASES)
        == mood_mod.derive_valence({"anger": 1.0, "joy": 1.0}, BASES),
        "派生量与字典键顺序无关",
    )

    # ── dominant_emotions：只挑明显高于基线的 ──
    check(mood_mod.dominant_emotions(flat, baselines=BASES) == [], "全部等于基线时没有显性情绪")
    dom = mood_mod.dominant_emotions(dict(flat, anger=0.8, sadness=0.7), baselines=BASES)
    check(len(dom) == 2, "同时明显的两个基元都会被挑出来（复合情绪）")
    check(dom[0][0] == "anger", "偏离更大的排在前面")
    check(
        mood_mod.dominant_emotions(dict(flat, calm=0.60), baselines=BASES) == [],
        "只比基线高一点点不算显性情绪（避免「今天很平静」盖掉真情绪）",
    )

    # ── decay：这是曾经的真实 bug，必须盯住 ──
    # updated_at=0 是 falsy，早先写成 `state.updated_at or now` 会让 elapsed 恒为 0，
    # 情绪永远不衰减。这里用 0 起点验证「确实衰减了」。
    half = {"joy": 3600.0}
    zero = MoodState(joy=0.85, anger=0.85, energy=0.2, updated_at=0.0)
    decayed = mood_mod.decay(
        zero,
        now=3600.0,
        baselines=BASES,
        half_lives=half,
        energy_recover_per_hour=0.35,
    )
    check(abs(decayed.joy - 0.55) < 1e-6, "updated_at=0 时也会按半衰期衰减（joy 减半）")
    check(abs(decayed.anger - 0.85) < 1e-6, "半衰期为 0 的基元不衰减")
    check(decayed.joy < zero.joy, "updated_at=0 的衰减方向正确")

    # 八个基元各按自己的半衰期走：愤怒退得比伤心快
    mixed = MoodState(anger=0.85, sadness=0.85, updated_at=0.0)
    after_hour = mood_mod.decay(
        mixed,
        now=3600.0,
        baselines=BASES,
        half_lives={"anger": 1800.0, "sadness": 7200.0},
        energy_recover_per_hour=0.0,
        offline_reset_hours=0.0,
    )
    check(after_hour.anger < after_hour.sadness, "同样过一小时，愤怒退得比伤心多")

    # ── 精力：线性恢复，不走半衰期 ──
    one_hour = mood_mod.decay(
        MoodState(energy=0.2, updated_at=0.0),
        now=3600.0,
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.35,
    )
    # 0.2 + 0.35（基础恢复）+ 少量愉悦度补偿（全部等于基线 → 补偿为 0）
    check(abs(one_hour.energy - 0.55) < 1e-6, "精力线性恢复：0.2 一小时回 0.55")
    three_hours = mood_mod.decay(
        MoodState(energy=0.0, updated_at=0.0),
        now=3 * 3600.0,
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.35,
    )
    check(three_hours.energy >= 0.999, "约 3 小时精力回满（0.35/小时）")
    check(three_hours.energy <= 1.0, "精力不会超过 1.0")

    # 心情好会额外补精力（用户明确要求「愉悦度上升时也会回复一定的精力」）
    happy_recover = mood_mod.decay(
        MoodState(joy=0.95, energy=0.2, updated_at=0.0),
        now=3600.0,
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.35,
    )
    sad_recover = mood_mod.decay(
        MoodState(sadness=0.95, energy=0.2, updated_at=0.0),
        now=3600.0,
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.35,
    )
    check(happy_recover.energy > sad_recover.energy, "愉悦度越高，精力恢复越快")

    # ── 离线重置：用户要求「离线数小时后再上线时重置为最大值」 ──
    offline = mood_mod.decay(
        MoodState(joy=0.05, anger=0.9, sadness=0.9, energy=0.05, updated_at=0.0),
        now=5 * 3600.0,
        baselines=BASES,
        half_lives={name: 4 * 3600.0 for name in EMOTIONS},
        energy_recover_per_hour=0.35,
        offline_reset_hours=4.0,
    )
    check(offline.energy == 1.0, "离线超过重置时长后精力回满")
    for name in EMOTIONS:
        check(
            abs(getattr(offline, name) - BASES[name]) < 1e-6,
            f"离线重置后 {mood_mod.EMOTION_LABELS[name]} 回到基线",
        )
    check(abs(offline.valence) < 1e-6, "离线重置后愉悦度回到基线（0）")
    check(abs(offline.arousal - 0.35) < 1e-6, "离线重置后情绪强度回到基线")
    check(offline.exhausted is False, "离线重置后耗尽锁解除")
    check(offline.label == "", "离线重置后清掉旧标签")
    # 未到重置时长就不该满血
    not_yet = mood_mod.decay(
        MoodState(energy=0.05, updated_at=0.0),
        now=2 * 3600.0,
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.35,
        offline_reset_hours=4.0,
    )
    check(not_yet.energy < 1.0, "未到离线重置时长时按正常速度恢复")

    # ── 耗尽锁（滞回）──
    # 直接判断 energy<=0 会让 Bot 在 0 附近反复闭麦/诈尸，必须用锁。
    exhausted_now = mood_mod.decay(
        MoodState(energy=0.0, updated_at=0.0),
        now=1.0,
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.0,
        exhausted_exit=0.3,
    )
    check(exhausted_now.exhausted is True, "精力掉到 0 时置耗尽锁")
    still_locked = mood_mod.decay(
        MoodState(energy=0.0, exhausted=True, updated_at=0.0),
        now=1800.0,  # 半小时，回 0.175，仍在 0.3 以下
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.35,
        exhausted_exit=0.3,
    )
    check(still_locked.energy < 0.3, "半小时只回到 0.175")
    check(still_locked.exhausted is True, "回到 0.3 之前耗尽锁不解除（不会诈尸）")
    released = mood_mod.decay(
        MoodState(energy=0.0, exhausted=True, updated_at=0.0),
        now=3600.0,  # 一小时回 0.35，超过 0.3
        baselines=BASES,
        half_lives={},
        energy_recover_per_hour=0.35,
        exhausted_exit=0.3,
    )
    check(released.exhausted is False, "精力回到 0.3 以上后耗尽锁解除")

    # ── 消耗倍率：情绪高涨时省力，低落时费力 ──
    cheap = mood_mod.energy_cost_factor(valence=0.9, arousal=0.9)
    pricey = mood_mod.energy_cost_factor(valence=-0.9, arousal=0.5)
    check(cheap < 1.0 < pricey, "心情好时消耗倍率低于 1，心情差时高于 1")
    check(
        abs(mood_mod.energy_cost_factor(valence=1.0, arousal=1.0) - mood_mod.ENERGY_COST_FACTOR_MIN)
        < 1e-6,
        "最亢奋时倍率触到下限（接近不消耗）",
    )
    check(
        abs(mood_mod.energy_cost_factor(valence=-1.0, arousal=0.0) - mood_mod.ENERGY_COST_FACTOR_MAX)
        < 1e-6,
        "最低落时倍率触到上限",
    )
    short_cost = mood_mod.reply_energy_cost("嗯")
    long_cost = mood_mod.reply_energy_cost("这是一段很长的话" * 10)
    check(long_cost > short_cost, "说得越多越费精力")
    check(
        mood_mod.reply_energy_cost("嗯", valence=0.9, arousal=0.9)
        < mood_mod.reply_energy_cost("嗯", valence=-0.9, arousal=0.5),
        "同样一句话，心情好时更省力",
    )
    check(
        mood_mod.reply_energy_cost("嗯", primitives=dict(BASES, joy=0.95))
        < mood_mod.reply_energy_cost("嗯", primitives=dict(BASES, sadness=0.95)),
        "直接传基元时同样按情绪缩放消耗",
    )

    # ── effort_for：精力只管「回复欲望 + 本次回复字数」，不碰发言频率 ──
    # 用户明确要求：「精力影响的应该是对当前话题的回复欲望和本次回复字数的多少，
    # 而不是简单的影响发言频率（这样会很不自然）」。
    full_effort = mood_mod.effort_for(0.9)
    half_effort = mood_mod.effort_for(0.4)
    low_effort = mood_mod.effort_for(0.2)
    dead_effort = mood_mod.effort_for(0.0, exhausted=True)
    check("很有兴致接话" in full_effort, "精力充沛时提高回复欲望")
    check("可以多说几句" in full_effort, "精力充沛时放宽字数")
    check("不太想主动展开" in half_effort, "精力一般时压低回复欲望")
    check("两三句" in half_effort, "精力一般时限制句数")
    check("提不起劲" in low_effort, "精力偏低时进一步压低回复欲望")
    check("短一些" in low_effort, "精力偏低时要求更短")
    check("一点都不想接话" in dead_effort, "耗尽时没有回复欲望")
    check("先去忙点别的" in dead_effort, "耗尽时给出告别话术（避免突然闭麦）")
    check("@" in dead_effort, "耗尽时说明被 @ 仍然要回")
    check(mood_mod.effort_for(0.65) == "", "中间档精力不额外约束")
    check(dead_effort != low_effort, "耗尽与只是累了的指令不同")
    check(mood_mod.effort_for(0.5) == "", "精力正好 0.5 时不额外限制")
    # 关键回归：不能再出现「少说话 / 降低频率」这类按频率压的措辞
    all_effort = "".join(
        mood_mod.effort_for(value, exhausted=value <= 0.0)
        for value in (0.0, 0.2, 0.4, 0.5, 0.65, 0.9)
    )
    check("发言频率" not in all_effort and "少说话" not in all_effort,
          "精力指令里不再提发言频率（用户要求按欲望+字数）")
    # 两条独立的轴必须都能拿到：desire_for / length_for 各自可单独用
    check("提不起劲" in mood_mod.desire_for(0.2), "desire_for 单独可用")
    check("短一些" in mood_mod.length_for(0.2), "length_for 单独可用")
    check("短一些" not in mood_mod.desire_for(0.2), "desire_for 不含字数要求")
    check("提不起劲" not in mood_mod.length_for(0.2), "length_for 不含欲望要求")

    # ── tone_for：只给语气，不含精力那两句 ──
    # 面板把「语气」和「精力」分成两块显示，如果 tone_for 里也带精力文案，
    # 用户会在页面上看到同一段话出现两遍（这正是本次修掉的显示缺陷）。
    base_prims = dict(BASES)
    tone_only = mood_mod.tone_for(base_prims, baselines=BASES, arousal=0.35)
    check(tone_only != "", "tone_for 给出语气要求")
    for word in ("回复欲望", "回复长度", "可以多说几句", "提不起劲", "短一些"):
        check(word not in tone_only, f"tone_for 不含精力文案（{word}）")
    full_guide = mood_mod.guidance_for(base_prims, 0.9, baselines=BASES, arousal=0.35)
    check(full_guide.startswith(tone_only), "guidance_for = tone_for + effort_for")
    check("可以多说几句" in full_guide, "guidance_for 仍然带精力要求（注入需要完整一段）")
    # 耗尽时语气也必须转成「累得不想说话」，不能还在点名某个情绪
    dead_tone = mood_mod.tone_for(dict(BASES, joy=1.0), exhausted=True, baselines=BASES, arousal=0.9)
    check("累得不想说话" in dead_tone, "耗尽时 tone_for 只说累")
    check("愉悦" not in dead_tone and "开心" not in dead_tone,
          "耗尽时 tone_for 不再点名情绪（与 label 判定一致）")

    # ── voice_level：只有「照常 / 停下」两档，不再按精力连续压频率 ──
    # 按比例压会让 Bot 时灵时不灵，看着像掉线而不是像累了。

    def _mood_cfg(**overrides: Any):
        # 注意：引擎读配置走 getattr(cfg, "mood")，所以要传对象而不是 dict，
        # 传 dict 会静默退化成「配置缺失 → 全部默认值」，测出来的东西是假的。
        return SimpleNamespace(mood=SimpleNamespace(**overrides))

    voice_engine = MoodEngine(lambda: _mood_cfg(enabled=True))
    voice_engine.set_manual(energy=0.9)
    check(voice_engine.voice_level() == 1.0, "精力 0.9 时不压发言强度")
    voice_engine.set_manual(energy=0.5)
    check(voice_engine.voice_level() == 1.0, "精力正好 0.5 时仍不压")
    voice_engine.set_manual(energy=0.25)
    check(voice_engine.voice_level() == 1.0, "精力 0.25 时也不压（只有耗尽才压）")
    voice_engine.set_manual(energy=0.1)
    check(voice_engine.voice_level() == 1.0, "精力 0.1 时仍不压频率")
    voice_engine.set_manual(energy=0.0)
    check(voice_engine.voice_level() == 0.0, "精力归零时发言强度归零")
    off_voice = MoodEngine(lambda: _mood_cfg(enabled=False))
    off_voice.set_manual(energy=0.0)
    check(off_voice.voice_level() == 1.0, "关闭情绪模拟时不压发言强度")
    # 只有两档：任何非耗尽状态都必须是 1.0，不能出现 0 和 1 之间的值。
    # 注意 exhausted 是滞回锁，上面已经把它锁上了，所以每个值都用新引擎。
    for energy in (0.0, 0.05, 0.15, 0.3, 0.45, 0.49):
        fresh_voice = MoodEngine(lambda: _mood_cfg(enabled=True))
        fresh_voice.set_manual(energy=energy)
        expected = 0.0 if energy <= 0.0 else 1.0
        check(fresh_voice.voice_level() == expected,
              f"精力 {energy} 的发言强度只有 {expected}（无中间档）")

    # ── can_initiate：耗尽锁直接决定能不能主动开口 ──
    gate_engine = MoodEngine(lambda: _mood_cfg(enabled=True))
    gate_engine.set_manual(energy=0.0)
    check(gate_engine.can_initiate() is False, "精力归零时不允许主动发言")
    gate_engine.set_manual(energy=0.35)
    check(gate_engine.can_initiate() is True, "精力回到阈值以上后允许主动发言")
    off_engine = MoodEngine(lambda: _mood_cfg(enabled=False))
    off_engine.set_manual(energy=0.0)
    check(off_engine.can_initiate() is True, "关闭情绪模拟时不门控（不能悄悄闭麦）")

    # ── blend：朝目标混合，不越界，只动基元 ──
    start = MoodState(joy=0.25, anger=0.05, energy=0.5, updated_at=1.0)
    mixed = mood_mod.blend(
        start,
        target={"joy": 0.85, "anger": 0.65, "energy": 0.1},
        weight=0.5,
        now=2.0,
        baselines=BASES,
    )
    check(abs(mixed.joy - 0.55) < 1e-6, "blend 按权重朝目标值混合（joy 0.25→0.55）")
    check(abs(mixed.anger - 0.35) < 1e-6, "blend 同样作用于其他基元（anger 0.05→0.35）")
    check(abs(mixed.energy - 0.5) < 1e-6, "blend 不动精力（模型不许改机械量）")
    # 反复 blend 同一目标只应收敛，不应漂移
    repeat = start
    for _ in range(20):
        repeat = mood_mod.blend(repeat, target={"joy": 0.85}, weight=0.5, now=2.0, baselines=BASES)
    check(abs(repeat.joy - 0.85) < 1e-3, "反复 blend 收敛到目标值（不会漂移）")

    # ── label_for / guidance_for：同一套判定，复合情绪要体现出来 ──
    check(
        mood_mod.label_for(dict(BASES), 0.9) == "还算平稳",
        "没有显性情绪时标签是「还算平稳」",
    )
    check(
        mood_mod.label_for(dict(BASES, anger=0.9), 0.9) == "生气",
        "单一显性情绪用对应短语",
    )
    combo = mood_mod.label_for(dict(BASES, anger=0.9, sadness=0.8), 0.9)
    check("又" in combo and "又" in combo[1:], f"两个显性情绪拼成「又 X 又 Y」（{combo}）")
    check(len(combo) <= mood_mod.LABEL_MAX_CHARS, "复合标签不超过长度上限")
    check(
        mood_mod.label_for(dict(BASES), 0.0, exhausted=True) == "累到不想说话",
        "耗尽时标签优先显示累",
    )

    # 标签与语气必须指向**同一组**情绪：以前出现过「标签写烦躁、指令说累了」。
    # 做法是各自独立解析——从标签文本里读情绪短语，从指令文本里读语气句，
    # 再要求两边集合相等；直接拿 mood_branch() 比就没意义了（那是同一个函数）。
    phrase_to_emotion = {phrase: name for name, phrase in mood_mod._EMOTION_PHRASES.items()}
    tone_to_emotion = {tone: name for name, tone in mood_mod._EMOTION_TONES.items()}

    def _emotions_in_label(label: str) -> set[str]:
        found: set[str] = set()
        for phrase, name in phrase_to_emotion.items():
            if phrase in label:
                found.add(name)
        return found

    def _emotions_in_guide(guide: str) -> set[str]:
        found: set[str] = set()
        for tone, name in tone_to_emotion.items():
            if tone in guide:
                found.add(name)
        return found

    mismatches: list[str] = []
    grid_values = (0.0, 0.3, 0.5, 0.8, 1.0)
    grid_energy = (0.0, 0.2, 0.5, 0.9)
    for hot in EMOTIONS:
        for level in grid_values:
            primitives = dict(BASES)
            primitives[hot] = level
            for energy in grid_energy:
                label = mood_mod.label_for(primitives, energy, baselines=BASES)
                guide = mood_mod.guidance_for(primitives, energy, baselines=BASES)
                want = _emotions_in_label(label)
                got = _emotions_in_guide(guide)
                if energy <= 0.0:
                    # 耗尽时标签与指令都只讲「累」，不该再点名情绪
                    if want or got:
                        mismatches.append(f"{hot}={level} e={energy} 耗尽却仍点名情绪")
                    continue
                if want != got:
                    mismatches.append(
                        f"{hot}={level} e={energy} label={label!r} guide={guide[:18]!r}"
                    )
    check(
        not mismatches,
        f"标签与语气在整张网格上指向同一组情绪（不一致 {len(mismatches)} 处：{mismatches[:3]}）",
    )

    # 高唤醒会额外叠加一句「反应更激烈」
    hot_guide = mood_mod.guidance_for(dict(BASES, anger=0.95), 0.9, baselines=BASES, arousal=0.9)
    calm_guide = mood_mod.guidance_for(dict(BASES, anger=0.95), 0.9, baselines=BASES, arousal=0.05)
    check("激烈" in hot_guide, "情绪强度高时追加「反应更激烈」")
    check("平淡" in calm_guide, "情绪强度低时追加「反应更平淡」")
    # 耗尽时也必须带上「不要主动搭话」，否则模型仍会主动开口
    dead_guide = mood_mod.guidance_for(dict(BASES), 0.0, exhausted=True, baselines=BASES)
    check("不要主动搭话" in dead_guide, "耗尽时语气指令里也带「不要主动搭话」")
    check("@" in dead_guide, "耗尽时语气指令里说明被 @ 仍要回")

    # ── heuristic_delta：单次增量必须有上限 ──
    positive = mood_mod.heuristic_delta("哈哈哈哈笑死我了，太棒了谢谢！")
    check(positive.get("joy", 0) > 0, "正面词让愉悦上升")
    check(
        all(abs(value) <= mood_mod.HEURISTIC_CAP + 1e-9 for value in positive.values()),
        "启发式增量不超过单次上限",
    )
    negative = mood_mod.heuristic_delta("烦死了，别烦我，无语")
    check(negative.get("anger", 0) > 0, "负面词让愤怒上升")
    check(negative.get("joy", 0) < 0, "负面词压低愉悦")
    check(
        mood_mod.heuristic_delta("我好紧张，怎么办").get("anxiety", 0) > 0,
        "紧张类关键词命中焦虑基元",
    )
    check(
        mood_mod.heuristic_delta("恶心死了，太离谱").get("disgust", 0) > 0,
        "厌恶类关键词命中厌恶基元",
    )
    check(
        mood_mod.heuristic_delta("居然是这样，没想到").get("surprise", 0) > 0,
        "惊讶类关键词命中惊讶基元",
    )
    rest = mood_mod.heuristic_delta("晚安，我先睡了")
    check(rest.get("energy", 0) > 0, "收尾语回一点精力")
    check(rest.get("calm", 0) > 0, "收尾语也回一点平静")
    check(mood_mod.heuristic_delta("") == {}, "空消息不产生任何增量")
    bot = mood_mod.heuristic_delta("这是一段很长很长的回复" * 8, from_bot=True)
    check(bot.get("energy", 0) < 0, "Bot 自己说得多会掉精力")

    # ── render_block：头尾标记必须保住 ──
    block = mood_mod.render_block(
        MoodState(anger=0.8, energy=0.7, label="有点烦躁"),
        max_chars=300,
        baselines=BASES,
    )
    check(block.startswith("<companion-mood>"), "情绪块以 header 开头")
    check(block.rstrip().endswith("</companion-mood>"), "情绪块以 footer 结尾")
    check("有点烦躁" in block, "情绪块含心情标签")
    check("话更短" in block, "情绪块含行为指令（愤怒要求话更短）")
    tight = mood_mod.render_block(
        MoodState(anger=0.8, energy=0.7, label="标签"), max_chars=60, baselines=BASES
    )
    check(len(tight) <= 60, "情绪块遵守 max_chars")
    check(
        tight.startswith("<companion-mood>") and tight.rstrip().endswith("</companion-mood>"),
        "截断后仍保住头尾标记（否则模型会把标记当正文）",
    )
    check(mood_mod.render_block(MoodState(), max_chars=0) == "", "max_chars=0 返回空串")
    no_guide = mood_mod.render_block(
        MoodState(anger=0.8, energy=0.7, label="生气"),
        include_guidance=False,
        baselines=BASES,
    )
    check("你现在" not in no_guide, "include_guidance=False 时不输出行为指令")
    # 自定义基线要真的参与判定：把 anger 基线抬到 0.9 后，0.8 就不算显性情绪了
    high_base = dict(BASES, anger=0.9)
    check(
        mood_mod.label_for(dict(BASES, anger=0.8), 0.9, baselines=high_base) == "还算平稳",
        "基线抬高后同样的基元值不再算显性情绪（基线承担人格）",
    )

    # ── MoodEngine：全流程 ──
    engine = MoodEngine(lambda: config_mod.CompanionGapConfig())
    check(engine.enabled, "默认启用情绪")
    before = engine.snapshot(now=1000.0)
    check(abs(before.valence) < 1e-6, "初始愉悦度等于基线（0）")
    check(before.label == "还算平稳", "初始标签是「还算平稳」")

    engine.on_message("哈哈哈哈太棒了！", now=1001.0)
    after_pos = engine.snapshot(now=1001.0)
    check(after_pos.valence > before.valence, "正面消息让情绪变好")
    check(after_pos.joy > before.joy, "正面消息直接抬高愉悦基元")

    engine.on_message("烦死了无语", now=1002.0)
    engine.on_message("烦死了无语", now=1003.0)
    after_neg = engine.snapshot(now=1003.0)
    check(after_neg.valence < after_pos.valence, "负面消息让情绪变差")
    check(after_neg.anger > after_pos.anger, "负面消息直接抬高愤怒基元")

    engine.apply_llm(
        {"anger": 0.8, "sadness": 0.7, "joy": 0.1, "label": "又生气又难过", "reason": "对方连发抱怨"},
        now=1004.0,
    )
    applied = engine.snapshot(now=1004.0)
    check(applied.label == "又生气又难过", "LLM 给的标签被采用")
    check(applied.reason == "对方连发抱怨", "LLM 给的原因被记录")
    check(applied.valence < -0.2, "LLM 的权威评估把情绪拉向负值")
    check(applied.anger > 0.5 and applied.sadness > 0.4, "复合情绪的两个基元都被抬高（复合调节）")
    check(abs(applied.energy - 0.6) < 1e-6 or applied.energy > 0.5, "LLM 改不动精力")

    # 长间隔后应显著回落
    later = engine.snapshot(now=1004.0 + 24 * 3600)
    check(later.valence > applied.valence, "24 小时后情绪向基线回落")

    engine.reset(now=2000.0)
    reset_state = engine.snapshot(now=2000.0)
    check(abs(reset_state.valence) < 1e-6, "reset 回到基线愉悦度")
    for name in EMOTIONS:
        check(
            abs(getattr(reset_state, name) - BASES[name]) < 1e-6,
            f"reset 把 {mood_mod.EMOTION_LABELS[name]} 拉回基线",
        )

    engine.set_manual(
        primitives={"anger": 0.95, "joy": 0.02, "calm": 0.05},
        energy=0.1,
        label="很火大",
        reason="手动",
        now=3000.0,
    )
    manual = engine.snapshot(now=3000.0)
    check(abs(manual.anger - 0.95) < 1e-6, "手动设定的基元生效")
    check(manual.valence < -0.3, "手动改基元后派生量跟着重算")
    check(manual.label == "很火大", "手动设定的标签生效")
    check(abs(manual.energy - 0.1) < 1e-6, "手动设定的精力生效")
    check("愉悦" in engine.describe(), "describe() 输出可读摘要")
    check("情绪强度" in engine.describe(), "describe() 用「情绪强度」而不是「唤醒度」")
    check("愤怒" in engine.describe_primitives(), "describe_primitives() 列出八个基元")

    # 关闭开关后不再改变
    class _OffCfg:
        mood = type("M", (), {"enabled": False})()

    off_engine = MoodEngine(lambda: _OffCfg())
    off_before = off_engine.snapshot(now=1.0)
    off_engine.on_message("哈哈哈哈太棒了", now=2.0)
    off_after = off_engine.snapshot(now=2.0)
    check(abs(off_after.joy - off_before.joy) < 1e-9, "情绪开关关闭后消息不影响状态")

    # ── 持久化往返 ──
    store = store_mod.GapStore(os.path.join(tmp, "mood.db"))
    store.open()
    _OPEN_STORES.append(store)
    check(store.get_mood() is None, "新库还没有情绪记录")

    store.upsert_mood(
        valence=0.42,
        arousal=0.66,
        energy=0.31,
        label="测试",
        reason="单测",
        now=5000.0,
        joy=0.9,
        anger=0.4,
    )
    row = store.get_mood()
    check(row is not None, "upsert_mood 后能读回")
    check(abs(row["valence"] - 0.42) < 1e-9, "读回的 valence 一致")
    check(row["label"] == "测试", "读回的 label 一致")
    check(abs(row["emotion_joy"] - 0.9) < 1e-9, "读回的 joy 基元一致")
    check(abs(row["emotion_anger"] - 0.4) < 1e-9, "读回的 anger 基元一致")

    store.upsert_mood(valence=-0.1, arousal=0.2, energy=0.9, label="覆盖", now=5001.0)
    overwritten = store.get_mood()
    check(abs(overwritten["valence"] + 0.1) < 1e-9, "upsert_mood 是覆盖而不是新增")
    check(
        store.conn.execute("SELECT COUNT(*) AS n FROM gap_mood").fetchone()["n"] == 1,
        "gap_mood 始终只有一行（scope 主键）",
    )
    # 只传三维的老调用方式仍然可用，基元落回默认值而不是报错
    check(
        abs(overwritten["emotion_joy"] - BASES["joy"]) < 1e-9,
        "只传三维时基元列回落到默认基线（兼容老调用）",
    )

    for index in range(5):
        store.record_mood(
            valence=0.1 * index,
            arousal=0.5,
            energy=0.5,
            label=f"第{index}",
            source="llm",
            now=6000.0 + index,
            joy=0.5 + 0.05 * index,
        )
    history = store.mood_history(limit=10)
    check(len(history) == 5, "mood_history 返回全部记录")
    check(history[0]["label"] == "第4", "mood_history 按时间倒序（最新在前）")
    check(history[0]["source"] == "llm", "mood_history 记录了来源")
    check(abs(history[0]["emotion_joy"] - 0.7) < 1e-9, "mood_history 也记录八个基元")

    store.record_mood(valence=0.0, arousal=0.0, energy=0.0, label="x", source="manual", now=7000.0)
    removed = store.prune_mood_log(keep=3)
    check(removed >= 1, f"prune_mood_log 删掉了多余记录（{removed} 条）")
    check(len(store.mood_history(limit=100)) <= 3, "prune 后历史条数不超过 keep")

    # 引擎与存储的往返：persist 后 load 应拿到同一状态
    persist_engine = MoodEngine(lambda: config_mod.CompanionGapConfig())
    persist_engine.load(store)
    persist_engine.set_manual(
        primitives={"joy": 0.71, "anger": 0.33, "sadness": 0.42},
        energy=0.55,
        label="往返",
        now=8000.0,
    )
    persist_engine.persist(store, now=8000.0)
    reload_engine = MoodEngine(lambda: config_mod.CompanionGapConfig())
    reload_engine.load(store)
    restored = reload_engine.snapshot(now=8000.0)
    check(abs(restored.joy - 0.71) < 1e-6, "persist → load 往返保住 joy 基元")
    check(abs(restored.sadness - 0.42) < 1e-6, "persist → load 往返保住 sadness 基元")
    check(restored.label == "往返", "persist → load 往返保住 label")

    # 老库只有 valence/arousal 时要能反推基元，不能让情绪突然清零
    legacy_db = os.path.join(tmp, "mood_legacy.db")
    legacy = store_mod.GapStore(legacy_db)
    legacy.open()
    _OPEN_STORES.append(legacy)
    legacy.conn.execute(
        "INSERT INTO gap_mood (scope, valence, arousal, energy, label, reason, updated_at)"
        " VALUES ('global', -0.8, 0.7, 0.4, '', '', 1000.0)"
    )
    legacy.conn.commit()
    legacy_engine = MoodEngine(lambda: config_mod.CompanionGapConfig())
    legacy_engine.load(legacy)
    legacy_state = legacy_engine.snapshot(now=1000.0)
    check(legacy_state.sadness > BASES["sadness"], "老库负愉悦度能反推出伤心基元")
    check(legacy_state.anger > BASES["anger"], "老库负愉悦度能反推出愤怒基元")
    check(legacy_state.valence < 0, "老库迁移后愉悦度仍是负的（情绪没有清零）")

    # store 为 None 时所有持久化操作都必须安全
    safe = MoodEngine(lambda: config_mod.CompanionGapConfig())
    safe.load(None)
    safe.persist(None)
    check(True, "store 为 None 时 load/persist 不抛异常")

    # ── _parse_mood：非法输入不能污染状态 ──
    check(extract_mod._parse_mood(None) == {}, "_parse_mood 对 None 返回空 dict")
    check(extract_mod._parse_mood("字符串") == {}, "_parse_mood 对非 dict 返回空 dict")
    check(extract_mod._parse_mood({}) == {}, "_parse_mood 对空 dict 返回空 dict")
    parsed = extract_mod._parse_mood(
        {"joy": 5, "anger": -3, "sadness": 0.6, "label": "这" * 20, "reason": "r" * 100}
    )
    check(parsed["joy"] == 1.0, "_parse_mood 把越界基元夹到 1.0")
    check(parsed["anger"] == 0.0, "_parse_mood 把负基元夹到 0.0")
    check(abs(parsed["sadness"] - 0.6) < 1e-9, "_parse_mood 保留合法基元")
    check(len(parsed["label"]) <= mood_mod.LABEL_MAX_CHARS, "_parse_mood 截断过长的 label")
    check(len(parsed["reason"]) <= 60, "_parse_mood 截断过长的 reason")
    check(
        "joy" not in extract_mod._parse_mood({"joy": "坏值"}),
        "_parse_mood 丢弃非数值基元",
    )
    # 老提示词只返回 valence/arousal 时换算成基元，而不是丢弃
    legacy_parsed = extract_mod._parse_mood({"valence": 0.9, "arousal": 0.5})
    check(legacy_parsed.get("joy", 0) > BASES["joy"], "老格式 valence 能换算成愉悦基元")
    check(
        extract_mod._parse_mood({"valence": -0.9}).get("sadness", 0) > BASES["sadness"],
        "老格式负 valence 能换算成伤心基元",
    )

    # ── MoodConfig 默认值 ──
    defaults = config_mod.CompanionGapConfig().mood
    check(defaults.enabled is True, "MoodConfig.enabled 默认 True")
    check(abs(defaults.emotion_joy_baseline - 0.25) < 1e-9, "emotion_joy_baseline 默认 0.25")
    check(abs(defaults.emotion_anger_baseline - 0.05) < 1e-9, "emotion_anger_baseline 默认 0.05")
    check(abs(defaults.emotion_surprise_baseline - 0.10) < 1e-9, "emotion_surprise_baseline 默认 0.10")
    check(
        abs(defaults.emotion_anger_half_life_minutes - 30.0) < 1e-9,
        "emotion_anger_half_life_minutes 默认 30（生气退得快）",
    )
    check(
        abs(defaults.emotion_sadness_half_life_minutes - 180.0) < 1e-9,
        "emotion_sadness_half_life_minutes 默认 180（伤心退得慢）",
    )
    check(abs(defaults.arousal_baseline - 0.35) < 1e-9, "arousal_baseline 默认 0.35")
    check(abs(defaults.energy_baseline - 0.6) < 1e-9, "energy_baseline 默认 0.6")
    check(
        abs(defaults.llm_blend_weight - 0.7) < 1e-9,
        "llm_blend_weight 默认 0.7（低于这个值模型判断会被冲淡，实测 0.45 时永远落进「还算平稳」）",
    )
    check(defaults.inject is True, "inject 默认 True")
    check(defaults.include_guidance is True, "include_guidance 默认 True")
    check(abs(defaults.exhausted_exit - 0.3) < 1e-9, "exhausted_exit 默认 0.3")
    check(defaults.gate_enabled is True, "gate_enabled 默认 True")
    check(abs(defaults.gate_floor - 0.05) < 1e-9, "gate_floor 默认 0.05")
    check(
        not hasattr(defaults, "valence_baseline"),
        "已弃用的 valence_baseline 被彻底删掉（留着会让配置页出现一个不生效的项）",
    )
    check(
        not hasattr(defaults, "valence_half_life_hours"),
        "已弃用的 valence_half_life_hours 被彻底删掉",
    )


# ── 10. 人物认知（可信度闸门与手动优先，是用户最强调的两点）──


def test_person() -> None:
    """人物认知：辨可信度、手动优先、密切度只给聊天方式、不给称呼。"""

    print("[10] 人物认知")
    person = person_mod

    # ── 主键用 platform:user_id，昵称会改、ID 不会 ──
    check(person.person_key("qq", "10086") == "qq:10086", "人物主键是 platform:user_id")
    check(person.person_key("", "10086") == "unknown:10086", "平台为空时退化到 unknown")
    check(person.person_key("qq", "") == "", "没有 user_id 时主键为空")

    # ── 角色收敛 ──
    check(person.normalize_role("主人") == "owner", "中文「主人」收敛到 owner")
    check(person.normalize_role("FRIEND") == "friend", "大小写与英文别名都能收敛")
    check(person.normalize_role("路人") == "stranger", "「路人」收敛到 stranger")
    check(person.normalize_role("随便什么") == "", "无法识别的角色返回空而不是硬猜")

    # ── 密切度由好感度推导，且只给出聊天方式 ──
    check(person.intimacy_for(100)[0] == "close", "好感度 100 是知己")
    check(person.intimacy_for(60)[0] == "friend", "好感度 60 是朋友")
    check(person.intimacy_for(30)[0] == "acquaintance", "好感度 30 是普通群友")
    check(person.intimacy_for(-50)[0] == "stranger", "负好感度是陌生人")
    # 关键：密切度只能映射到「聊天方式」，绝不能出现称呼词。
    tone_words = " ".join(person.INTIMACY_TONES.values())
    check("主人" not in tone_words, "聊天方式里不出现「主人」这种称呼")
    check("知己" not in tone_words, "聊天方式里不出现「知己」这种称呼")
    for key, tone in person.INTIMACY_TONES.items():
        check(bool(tone.strip()), f"密切度 {key} 有对应的聊天方式")

    # ── 身份表与分级表都来自配置页（用户第 33 轮第 2 点） ──
    # 用户原话：「关系中的身份也应该可以自由配置（比如我想添加一个自定义的
    # 身份，并配置专门的提示词）好感度分级的阈值也应该可以调节」。
    custom_roles = person.roles_from_config(
        [
            {"key": "teacher", "label": "老师", "tone": "你把他当老师，说话会认真一些。"},
            {"key": "Owner", "label": "主人", "tone": "你愿意顺着他。"},
        ]
    )
    check([item["key"] for item in custom_roles][:2] == ["teacher", "owner"],
          "自定义身份被接受，标识统一转小写")
    check(any(item["key"] == "stranger" for item in custom_roles),
          "用户漏了 stranger 时自动补上兜底身份")
    check(custom_roles[-1]["key"] == "stranger", "兜底身份排在最后")
    check(person.roles_from_config([]) == list(person.DEFAULT_ROLES),
          "身份表为空时退回默认四档（误删全部身份不该让功能哑掉）")
    check(person.normalize_role("老师", custom_roles) == "teacher",
          "中文称呼能匹配到自定义身份")
    check(person.normalize_role("teacher", custom_roles) == "teacher",
          "自定义身份的标识也能直接匹配")
    only_teacher = person.roles_from_config(
        [{"key": "teacher", "label": "老师", "tone": "你把他当老师。"}]
    )
    check(person.normalize_role("主人", only_teacher) == "",
          "用户删掉 owner 之后，模型给的「主人」不会凭空造出这个身份")
    check(person.role_label("teacher", custom_roles) == "老师", "自定义身份有中文名")
    check(person.role_tone("teacher", custom_roles).startswith("你把他当老师"),
          "自定义身份的提示词能被取到")

    # 分级表：阈值可调，且必须按 min_affection 从高到低排序
    custom_levels = person.intimacy_levels_from_config(
        [
            {"key": "low", "label": "生人", "min_affection": 0.0, "tone": "客气一点。"},
            {"key": "best", "label": "死党", "min_affection": 60.0, "tone": "随便聊。"},
        ]
    )
    check([item["key"] for item in custom_levels][:2] == ["best", "low"],
          "分级表按好感度下限从高到低排序（顺序错会一直命中最后一档）")
    check(person.intimacy_for(70, custom_levels)[1] == "死党", "自定义阈值生效（70 算死党）")
    check(person.intimacy_for(30, custom_levels)[1] == "生人", "自定义阈值生效（30 算生人）")
    check(person.intimacy_for(-50, custom_levels)[1] == "陌生人",
          "最低阈值高于 -100 时自动补一档兜底，负好感度不会没有归属")
    check(person.intimacy_levels_from_config([]) == list(person.DEFAULT_INTIMACY_LEVELS),
          "分级表为空时退回默认四档")
    # 配置页给的是 PluginConfigBase 对象而不是 dict，必须也能吃进去。
    stub_level = type("Stub", (), {"model_dump": lambda self: {
        "key": "pal", "label": "伙伴", "min_affection": 10.0, "tone": "放松点。"}})()
    check(person.intimacy_levels_from_config([stub_level])[0]["label"] == "伙伴",
          "配置页传下来的对象行也能读（调 model_dump）")
    check(person.as_dicts(None) == [] and person.as_dicts("x") == [],
          "非法输入不会炸")

    # ── 可信度闸门：低置信判断不改写角色 ──
    # 用户的原话：群友 B 开玩笑说「我才是你的主人」，Bot 不该记下来。
    joke = {
        "user_id": "20001",
        "display_name": "群友B",
        "relation_role": "owner",
        "role_confidence": 0.2,
        "note": "",
    }
    fresh = person.merge_observation(None, joke, now=1000.0)
    check(fresh is not None and fresh["relation_role"] == "stranger",
          "低置信的「我才是你主人」不会被记成主人")

    serious = {
        "user_id": "10086",
        "display_name": "小明",
        "relation_role": "owner",
        "role_confidence": 0.9,
        "note": "说话很客气",
    }
    owner_row = person.merge_observation(None, serious, now=1000.0)
    check(owner_row is not None and owner_row["relation_role"] == "owner",
          "高置信的角色判断会被采信")
    check(owner_row is not None and owner_row["role_source"] == "auto",
          "自动判断的来源标成 auto")

    # ── 手动优先：手动配置过的行，自动结果改不动 ──
    manual_row = dict(owner_row or {})
    manual_row["role_source"] = "manual"
    manual_row["affection"] = 100.0
    # 第 34 轮起好感度是六维算出来的摘要，手动设好感度时必须同时把六维
    # 摊好（plugin 与 WebUI 都这么写），否则下一次抽取会把它覆盖掉。
    manual_row["axes"] = person.axes_from_affection(100.0)
    merged = person.merge_observation(
        manual_row,
        {
            "user_id": "20001",
            "display_name": "群友B",
            "relation_role": "owner",
            "role_confidence": 0.95,
            "note": "自称是主人",
        },
        now=2000.0,
    )
    check(merged is not None and merged["relation_role"] == "owner",
          "手动配置的角色不会被自动结果改写")
    check(merged is not None and merged["role_source"] == "manual", "来源仍标记为手动")
    # 好感度现在由六维反算，往返有 1 分以内的插值误差，所以断言用容差。
    check(merged is not None and abs(float(merged["affection"]) - 100.0) < 1.5,
          "手动配置的好感度不被自动结果改写（六维反算误差 1 分内）")
    check(
        merged is not None and "自称是主人" not in (merged.get("tags") or []),
        "手动行的标签也不会被自动结果塞进来",
    )

    # ── 自动观察不写标签，但也不能冲掉用户手写的标签 ──
    # 用户第 32 轮第 1 点：标签只能由用户自己写（「XX的XX」那种关系化说法），
    # 自动抽取只负责关系角色、好感度、情绪影响。
    row = person.merge_observation(None, serious, now=1000.0)
    check((row or {}).get("tags") == [], "新建人物时标签是空的")
    for index in range(6):
        row = person.merge_observation(
            row,
            {
                "user_id": "10086",
                "relation_role": "",
                "role_confidence": 0.9,
                "tags": [f"特征{index}"],
            },
            now=1000.0 + index,
        )
    check((row or {}).get("tags") == [], "自动观察反复跑也不会攒出标签")
    # 用户手写的标签必须原样保留
    user_row = dict(row or {})
    user_row["tags"] = ["爱讲冷笑话的朋友", "晚上会困的群友"]
    after_auto = person.merge_observation(
        user_row,
        {"user_id": "10086", "relation_role": "", "role_confidence": 0.9, "tags": ["爱发猫图的群友"]},
        now=3000.0,
    )
    tags = (after_auto or {}).get("tags") or []
    check(tags == ["爱讲冷笑话的朋友", "晚上会困的群友"], "自动观察不会改动用户手写的标签")
    check(len(tags) <= person.MAX_TAGS, "标签数量有上限")

    # ── 六维手动锁定：用户调过的值不能被自动观察改掉 ──
    # 用户第 43 轮原话：「我删除了人物标签后…标签又出现了，但我调的好感度
    # 没有变回去」。标签复活是 _migrate 的锅（见下面存储层的迁移断言），
    # 好感度被改回去是这里：六维默认跟着聊天走，手动调过一次就必须停住。
    locked = {
        "user_id": "10087",
        "relation_role": "friend",
        "role_source": "manual",
        "axes": {"trust": 0.9, "closeness": 0.85, "respect": 0.8,
                 "tension": 0.1, "familiarity": 0.5, "perceived_warmth": 0.9},
        "axes_source": "manual",
        "tags": [],
    }
    locked["affection"] = person.affection_from_axes(locked["axes"])
    kept = person.merge_observation(
        locked,
        {"user_id": "10087", "relation_role": "", "role_confidence": 0.9,
         "axes": {"trust": -1.0, "closeness": -1.0}},
        now=4000.0,
    )
    check(kept is not None, "手动锁定的人仍然能被合并（不返回 None）")
    check(
        abs(kept["axes"]["trust"] - 0.9) < 1e-6 and abs(kept["axes"]["closeness"] - 0.85) < 1e-6,
        "六维被手动锁住时，自动观察改不动它（用户调的值不会自己变回去）",
    )
    check(
        abs(float(kept["affection"]) - float(locked["affection"])) < 1e-6,
        "六维锁住时好感度摘要也不变",
    )
    check(kept.get("axes_source") == "manual", "锁住的状态不会被自动观察改成 auto")

    # 没锁的时候照旧跟着聊天走（不能因为加了闸门就永远不动）
    unlocked = dict(locked)
    unlocked["axes_source"] = "auto"
    moved = person.merge_observation(
        unlocked,
        {"user_id": "10087", "relation_role": "", "role_confidence": 0.9,
         "axes": {"trust": -1.0}},
        now=4001.0,
    )
    check(moved["axes"]["trust"] < 0.9, "没锁定时自动观察仍会推动六维")

    # 手动配身份（role_source=manual）但没锁六维的人，六维仍跟着聊天走 ——
    # 身份和感受是两路信号，不能互相牵连。
    role_only = {
        "user_id": "10088",
        "relation_role": "owner",
        "role_source": "manual",
        "axes": {"trust": 0.5, "closeness": 0.5, "respect": 0.5,
                 "tension": 0.5, "familiarity": 0.5, "perceived_warmth": 0.5},
        "axes_source": "auto",
        "tags": [],
    }
    role_kept = person.merge_observation(
        role_only,
        {"user_id": "10088", "relation_role": "", "role_confidence": 0.9,
         "axes": {"trust": -1.0}},
        now=4002.0,
    )
    check(role_kept["relation_role"] == "owner", "手动身份不会被自动观察改写")
    check(role_kept["axes"]["trust"] < 0.5, "只锁身份不锁六维时，六维仍跟着聊天变化")

    # ── 标签清洗：只做限长/去重/限条数，不强制句式 ──
    check(person.normalize_tag("  抽象的  群友 ") == "抽象的 群友", "标签会折叠多余空白")
    check(len(person.normalize_tag("x" * 40)) == person.TAG_MAX_CHARS, "标签超长会被截断")
    check(
        person.normalize_tags(["抽象的群友", "抽象的群友", "", "  "]) == ["抽象的群友"],
        "标签去重并丢掉空串",
    )
    check(
        len(person.normalize_tags([f"标签{i}" for i in range(9)])) == person.MAX_TAGS,
        "标签条数有上限",
    )
    check(person.normalize_tags("不是列表") == [], "非列表输入返回空标签")
    # 「XX的XX」两种写法都要能过 —— 硬套句式正则会把好标签挡掉
    for good in ("抽象的群友", "喜欢玩游戏的朋友", "爱熬夜的夜猫子"):
        check(person.normalize_tag(good) == good, f"「{good}」这种关系化写法被接受")

    # ── 情绪影响是衰减累加，不是纯累加 ──
    impact = person.accumulate_impact(0.0, 1.0)
    check(abs(impact - 1.0) < 1e-9, "第一次情绪影响直接生效")
    impact2 = person.accumulate_impact(impact, -1.0)
    check(impact2 < 0.0, "负面互动能把正面影响压下去（人会变）")
    check(person.accumulate_impact(1.0, 1.0) <= 1.0, "情绪影响被夹在 -1..1")

    # ── 渲染：不注入称呼、标注来源可信度 ──
    block, used = person.render_person_block(
        [{"display_name": "小明", "relation_role": "owner", "role_source": "manual",
          "affection": 100.0, "tags": ["说话很客气的群友"], "emotional_impact": 0.4}]
    )
    check("<companion-people>" in block, "人物块有独立标记")
    check("小明" in block, "人物块带显示名")
    check("主人" not in block, "默认不把「主人」这种称呼写进提示词")
    check("说话很客气的群友" in block, "人物块带标签")
    check("心情好一些" in block, "正面情绪影响会体现出来")
    check(len(used) == 1, "render_person_block 回传实际用到的行")

    # include_role=True 时才出现角色行为描述（仍然不是称呼）
    block_role, _ = person.render_person_block(
        [{"display_name": "小明", "relation_role": "owner", "role_source": "manual",
          "affection": 100.0}],
        include_role=True,
    )
    check("更在意他的看法" in block_role, "开启角色注入时给出行为描述")

    # 自动推断的角色要标明「不一定准」
    block_auto, _ = person.render_person_block(
        [{"display_name": "小红", "relation_role": "friend", "role_source": "auto",
          "affection": 60.0}],
        include_role=True,
    )
    check("不一定准" in block_auto, "自动推断的角色要标注可信度不足")

    # 字数上限与空输入
    long_block, _ = person.render_person_block(
        [{"display_name": "很长" * 20, "relation_role": "friend", "role_source": "manual",
          "affection": 60.0, "tags": ["标签" * 30]}],
        max_chars=60,
    )
    check(len(long_block) <= 60, "人物块遵守字数上限")
    check(person.render_person_block([]) == ("", []), "没有人物时返回空串")
    check(person.render_person_block([], max_chars=0) == ("", []), "字数上限为 0 时不渲染")

    # ── 解析模型输出 ──
    # 用户第 32 轮第 1 点：标签要「XX的XX」这种关系化说法，所以 parse_people
    # 收 tags 而不再收 note（画像式描述交给宿主 A_Memorix）。
    parsed = person.parse_people(
        [
            {"user_id": "10086", "display_name": "小明", "relation_role": "owner",
             "role_confidence": 0.9, "tags": ["抽象的群友"], "emotional_impact": 0.5,
             "affection": 80},
            {"user_id": "", "relation_role": "owner"},          # 没有 ID，丢弃
            {"user_id": "20001", "relation_role": "神仙", "note": "爱发猫图"},  # 只剩画像式备注，丢弃
            {"user_id": "20002"},                                # 什么信息都没有，丢弃
            "不是字典",                                           # 类型不对，丢弃
        ]
    )
    check(len(parsed) == 1, "解析时丢掉不合法条目")
    check(parsed[0]["relation_role"] == "owner", "合法角色保留")
    check(parsed[0]["role_confidence"] == 0.9, "置信度保留")
    check(parsed[0]["tags"] == ["抽象的群友"], "解析结果带上关系化标签")
    check("note" not in parsed[0], "解析结果不再带 note（与宿主画像分工）")
    # 非法角色但给了情绪影响 → 保留（情绪影响是本插件独有的信息）
    impact_only = person.parse_people(
        [{"user_id": "20003", "relation_role": "神仙", "emotional_impact": -0.6}]
    )
    check(len(impact_only) == 1 and impact_only[0]["relation_role"] == "",
          "非法角色被清空，但情绪影响让它仍被保留")
    # 只有标签也值得留：用户手写的标签本身就是有效信息
    tag_only = person.parse_people([{"user_id": "20004", "tags": ["爱玩梗的群友"]}])
    check(len(tag_only) == 1 and tag_only[0]["tags"] == ["爱玩梗的群友"],
          "只有标签的条目也会被保留")
    check(person.parse_people(None) == [], "people 不是数组时返回空")
    clamped = person.parse_people(
        [{"user_id": "1", "role_confidence": 5, "emotional_impact": -9,
          "axes": {"trust": 9, "closeness": -9, "respect": 0.5},
          "tags": ["x" * 100]}]
    )
    check(clamped[0]["role_confidence"] == 1.0, "置信度被夹到 1.0")
    check(clamped[0]["emotional_impact"] == -1.0, "情绪影响被夹到 -1.0")
    # 第 34 轮起模型给的是**变化量**，不再是 affection 绝对值：超出 -1..1 的
    # 值被夹回边界，familiarity 是程序累积的、模型给了也要丢掉。
    check(clamped[0]["axes"]["trust"] == 1.0, "六维变化量被夹到 1.0")
    check(clamped[0]["axes"]["closeness"] == -1.0, "六维变化量被夹到 -1.0")
    check("affection" not in clamped[0], "解析结果不再有 affection（改成六维了）")
    check(len(clamped[0]["tags"][0]) <= person.TAG_MAX_CHARS, "标签被截断")

    # ── describe_person 给命令输出用 ──
    desc = person.describe_person(
        {"display_name": "小明", "relation_role": "owner", "role_source": "manual",
         "affection": 100.0, "emotional_impact": 0.4, "tags": ["说话很客气的群友"]}
    )
    check("小明" in desc and "手动" in desc, "describe_person 标出手动来源")
    check("说话很客气的群友" in desc, "describe_person 带标签")
    # 用户第 44 轮：「QQ 号也应该显示出来」。昵称能改能重名，命令输出里
    # 只有昵称的话用户没法确认自己改的是不是那个人。
    desc_id = person.describe_person(
        {"display_name": "群友A", "user_id": "100000001",
         "relation_role": "owner", "role_source": "manual", "affection": 100.0}
    )
    check("100000001" in desc_id, "describe_person 带出账号")
    check("账号" in desc_id, "describe_person 标明那是账号")
    # 群名片等于账号是正常数据（真机 100000004 就是把群名片设成了自己的
    # QQ 号），不是异常。第一版在这种情况下跳过账号，结果那条路径反而看不到
    # 账号了 —— 现在要求任何情况都写全。
    desc_same = person.describe_person(
        {"display_name": "100000004", "user_id": "100000004",
         "relation_role": "stranger", "role_source": "auto", "affection": 10.0}
    )
    check(
        "账号 100000004" in desc_same,
        "群名片等于账号时仍然写出账号",
    )
    desc_noid = person.describe_person(
        {"display_name": "只有名字", "relation_role": "stranger",
         "role_source": "auto", "affection": 0.0}
    )
    check("账号" not in desc_noid, "完全没有账号时不硬编一段空的账号")

    # ── 六维关系模型（用户第 34 轮：关系不能只靠一个数表达）──
    # 用户原话：「衡量人与人之间的情感与关系，并不能简单地依靠一个"好感度"
    # 的数值进行表达。这种关系往往是复杂且多元的，还会受到其他人的影响。」
    check(len(person.AXIS_KEYS) == 6, "六个关系维度")
    check(
        set(person.AXIS_KEYS)
        == {"trust", "closeness", "respect", "tension", "familiarity", "perceived_warmth"},
        "六个维度的标识固定",
    )
    for key in person.AXIS_KEYS:
        check(key in person.AXIS_LABELS and bool(person.AXIS_LABELS[key]), f"{key} 有中文名")
        check(key in person.AXIS_HINTS and bool(person.AXIS_HINTS[key]), f"{key} 有说明")
        check(0.0 <= person.AXIS_DEFAULTS[key] <= 1.0, f"{key} 的基线在 0..1")

    # 好感度 → 六维 → 好感度 必须往返一致（否则老用户升级后好感度会跳变）
    for score in (-100.0, -50.0, -17.0, 0.0, 33.0, 60.0, 100.0):
        back = person.affection_from_axes(person.axes_from_affection(score))
        check(abs(back - score) < 1.0, f"好感度 {score:g} 反推六维后能算回原值（误差 1 分内）")

    # familiarity 刻意不参与好感度：认识久 ≠ 喜欢
    check(
        person.affection_from_axes({**person.AXIS_DEFAULTS, "familiarity": 1.0})
        == person.affection_from_axes(dict(person.AXIS_DEFAULTS)),
        "熟悉度不参与好感度计算（损友可以又熟又互相嫌弃）",
    )

    # 熟悉度反推不出来，必须按 first_seen_ts 估 —— 否则老熟人被说成刚认识的
    legacy = {"affection": 100.0, "first_seen_ts": 1000.0}
    old_axes = person.axes_of(legacy)
    check(old_axes["familiarity"] > 0.0, "老库按认识天数补出熟悉度（不恒为 0）")
    fresh_axes = person.axes_of({"affection": 100.0, "first_seen_ts": 0.0})
    check(fresh_axes["familiarity"] == 0.0, "没有时间戳时不瞎猜熟悉度")
    long_axes = person.axes_of(
        {"affection": 100.0, "first_seen_ts": person.time.time() - 86400 * 120}
    )
    check(long_axes["familiarity"] > 0.7, "认识 120 天的人熟悉度明显更高")

    # 反推出来的六维**不算观测值**：矛盾描述需要两个独立来源，
    # 从同一个标量编出来的「矛盾」是公式副产品（老熟人被说成刚搭上话）。
    check(person.has_observed_axes({"affection": 100.0}) is False,
          "只有好感度的老记录不算观测到六维")
    check(person.has_observed_axes({"axes": {"trust": 0.5}}) is True, "有 axes 就算观测过")
    check(person.has_observed_axes({"axes": ""}) is False, "axes 是空串不算观测过")
    legacy_block, _ = person.render_person_block(
        [{"display_name": "老熟人", "relation_role": "owner", "role_source": "manual",
          "affection": 100.0, "first_seen_ts": person.time.time() - 86400 * 200}]
    )
    check("刚跟你搭上话" not in legacy_block,
          "老库记录不会被说成「刚跟你搭上话」（反推数据不该编造矛盾）")

    # 观测到的矛盾必须说出来
    contradiction = person.render_person_block(
        [{"display_name": "熟人", "relation_role": "friend", "role_source": "manual",
          "affection": 60.0,
          "axes": {"trust": 0.2, "closeness": 0.8, "respect": 0.5,
                   "tension": 0.2, "familiarity": 0.7, "perceived_warmth": 0.5}}]
    )[0]
    check("熟" in contradiction and "不跟他说" in contradiction,
          "观测到的矛盾组合会写进提示词（很熟但不信任）")

    # 单次观察的推动有上限，负面推动更慢
    pushed = person.apply_axes(dict(person.AXIS_DEFAULTS), {"trust": 1.0})
    check(pushed["trust"] - person.AXIS_DEFAULTS["trust"] <= person.AXIS_GAIN + 1e-9,
          "单次观察的推动量有上限")
    up = person.apply_axes(dict(person.AXIS_DEFAULTS), {"respect": 0.5})["respect"]
    down = person.apply_axes(dict(person.AXIS_DEFAULTS), {"respect": -0.5})["respect"]
    check(
        person.AXIS_DEFAULTS["respect"] - down
        < up - person.AXIS_DEFAULTS["respect"],
        "尊重跌得比涨得慢（否定别人能力比不喜欢人难）",
    )
    # familiarity 由程序累积，模型给了也不算
    check(person.apply_axes(dict(person.AXIS_DEFAULTS), {"familiarity": 1.0})["familiarity"]
          == person.AXIS_DEFAULTS["familiarity"] + person.FAMILIARITY_GAIN,
          "熟悉度只按互动累积，不接受模型给的值")

    # 衰减：向基线回落，熟悉度不衰减
    decayed = person.decay_axes(
        {"trust": 1.0, "closeness": 1.0, "respect": 1.0, "tension": 1.0,
         "familiarity": 1.0, "perceived_warmth": 1.0},
        idle_days=person.AXIS_HALF_LIFE_DAYS,
    )
    check(decayed["trust"] < 1.0, "闲置后信任会回落")
    check(decayed["familiarity"] == 1.0, "熟悉度不会随时间衰减（认识过就是认识过）")

    # 第三方影响：只动信任与亲近，且越熟越不受影响
    links = [{"target_user_id": "20001", "attitude": -0.9}]
    stranger_axes = dict(person.AXIS_DEFAULTS)
    close_axes = {**person.AXIS_DEFAULTS, "familiarity": 0.95}
    stranger_bias = person.social_bias(stranger_axes, links, weight=0.5, cap=0.3)
    close_bias = person.social_bias(close_axes, links, weight=0.5, cap=0.3)
    check(stranger_bias["trust"] < 0.0, "别人说坏话会降低信任")
    check(abs(close_bias["trust"]) < abs(stranger_bias["trust"]),
          "越熟越不容易被别人影响（自己的判断更重要）")
    check("respect" not in stranger_bias, "第三方影响不动尊重")
    check(abs(stranger_bias["trust"]) <= 0.3 + 1e-9, "第三方影响有上限")

    # NaN / 非法值不能污染数据
    check(person.clamp_axis(float("nan")) == person.AXIS_DEFAULTS["trust"] + 0.0
          or 0.0 <= person.clamp_axis(float("nan")) <= 1.0, "NaN 被挡掉")
    check(0.0 <= person.clamp_axis(-99.0) <= 1.0, "超范围的值被夹回 0..1")
    check(person.normalize_axes_delta({"trust": float("inf"), "closeness": 0.5})
          == {"closeness": 0.5}, "无穷值被丢弃")

    # links 解析
    parsed_links = person.parse_links(
        [{"target_id": "20001", "attitude": -0.4, "evidence": "说他爱装"},
         {"target_id": "", "attitude": -0.4},              # 没有目标，丢弃
         {"target_id": "20002", "attitude": float("nan")}, # 非法强度，丢弃
         "不是字典"]
    )
    check(len(parsed_links) == 1, "links 解析丢掉不合法条目")
    check(parsed_links[0]["target_user_id"] == "20001", "links 用 target_user_id")
    check(abs(parsed_links[0]["attitude"] + 0.4) < 1e-9, "links 保留态度强度")

    # 老库升级后熟人不会变陌生人
    upgraded = person.axes_of({"affection": 90.0})
    check(upgraded["closeness"] > 0.8, "老库好感 90 的人升级后亲近度依然很高")


# ── 11. 日志分级（debug.log_level 必须对 tracker 也生效）──


def test_log() -> None:
    """日志阈值是全局共享的：把 log_level 调成 info 之后，tracker 的 debug 也不能再刷。

    这条回归的由来：``cg_tracker.py`` 曾经有一份自己的 ``_log()``，直接
    ``getattr(logger, level)`` 调用，绕过了 ``debug.log_level``。实测把级别
    调成 info 后仍每 30 秒刷一条 debug「记忆衰减」，1.5 小时 143 条。
    """

    logger = FakeLogger()

    # 认不出的值回落到 info，不能抛
    check(log_mod.set_min_level("不认识的级别") == 20, "认不出的日志级别回落到 info")
    check(log_mod.set_min_level(None) == 20, "None 回落到 info")
    check(log_mod.set_min_level("") == 20, "空串回落到 info")
    check(log_mod.set_min_level("INFO") == 20, "级别名大小写不敏感")
    check(log_mod.set_min_level("  warning  ") == 30, "级别名两边的空白被忽略")

    # info 阈值下：debug 被拦住，info/warning 放行
    log_mod.set_min_level("info")
    check(log_mod.is_enabled("debug") is False, "info 阈值下 debug 不启用")
    check(log_mod.is_enabled("info") is True, "info 阈值下 info 启用")
    logger.lines.clear()
    log_mod.log(logger, "debug", "这条不该出现")
    log_mod.log(logger, "info", "这条要出现")
    check(len(logger.lines) == 1, "info 阈值下只写出一条")
    check(logger.lines[0][0] == "info", "写出来的是 info 那条")
    check(logger.lines[0][1].startswith("[companion-gap] "), "日志带统一前缀")

    # debug 阈值下全都放行
    log_mod.set_min_level("debug")
    check(log_mod.is_enabled("debug") is True, "debug 阈值下 debug 启用")
    logger.lines.clear()
    log_mod.log(logger, "debug", "现在要出现了")
    check(len(logger.lines) == 1, "debug 阈值下 debug 写得出去")

    # 关键回归：tracker 的 _log 必须跟着走同一个闸门
    log_mod.set_min_level("info")
    logger.lines.clear()
    tracker_mod._log(logger, "debug", "记忆衰减：19 条降低显著度，0 条转入遗忘")
    check(logger.lines == [], "info 阈值下 tracker 的 debug 被拦住（曾经的 bug）")

    log_mod.set_min_level("warning")
    logger.lines.clear()
    tracker_mod._log(logger, "info", "这条不该出现")
    tracker_mod._log(logger, "warning", "这条要出现")
    check(len(logger.lines) == 1, "warning 阈值下 tracker 只写 warning")
    check(logger.lines[0][0] == "warning", "拦掉的是 info 那条")

    # plugin._set_log_level 与 cg_log 共用同一份状态
    plugin_mod._set_log_level("error")
    check(log_mod.get_min_level() == 40, "plugin._set_log_level 改的是同一个阈值")
    check(plugin_mod._MIN_LOG_LEVEL == 40, "plugin 自己的缓存值同步更新")
    logger.lines.clear()
    tracker_mod._log(logger, "warning", "error 阈值下 warning 也不该出现")
    check(logger.lines == [], "error 阈值下 tracker 的 warning 被拦住")

    # 精力门控也走同一个出口
    gate_cls = importlib.import_module(f"{_PKG_NAME}.cg_energy_gate").EnergyGate
    log_mod.set_min_level("warning")
    ctx_gate = FakeGateCtx()
    ctx_gate.logger.lines.clear()
    gate_cls(ctx_gate, logger=ctx_gate.logger)._log("少说几句")
    check(ctx_gate.logger.lines == [], "warning 阈值下精力门控的 info 被拦住")

    log_mod.set_min_level("info")
    ctx_gate2 = FakeGateCtx()
    ctx_gate2.logger.lines.clear()
    gate_cls(ctx_gate2, logger=ctx_gate2.logger)._log("少说几句")
    check(len(ctx_gate2.logger.lines) == 1, "info 阈值下精力门控的 info 写得出去")
    check(ctx_gate2.logger.lines[0][1].startswith("[companion-gap] "), "精力门控日志也带前缀")

    # logger 缺方法 / 抛异常都不能影响业务
    class OnlyInfoLogger:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def info(self, message: str) -> None:
            self.lines.append(message)

    log_mod.set_min_level("debug")
    only_info = OnlyInfoLogger()
    log_mod.log(only_info, "debug", "没有 debug 方法就退回 info")
    check(len(only_info.lines) == 1, "logger 缺 debug 方法时退回 info 不丢消息")

    class ExplodingLogger:
        def debug(self, message: str) -> None:
            raise RuntimeError("日志代理炸了")

    log_mod.log(ExplodingLogger(), "debug", "不允许把异常抛出去")
    check(True, "logger 抛异常时静默吞掉，不影响业务")

    # 收尾：恢复默认阈值，别影响后面跑的用例
    log_mod.set_min_level("info")


# ── 9. 精力门控（会真的改宿主发言频率、会往用户群里发消息，必须离线钉死）──


class FakeFrequencyCapability:
    """记录 set_adjust 的调用，并能模拟能力不可用 / 恢复失败 / 静默失败。"""

    def __init__(self, current: float = 1.0, *, fail_set: bool = False, silent: bool = False) -> None:
        self.current = current
        self.fail_set = fail_set
        #: 模拟宿主「找不到会话时只打 warning、set 静默不生效」的行为。
        self.silent = silent
        self.calls: list[tuple[str, str, float | None]] = []

    async def get_adjust(self, chat_id: str) -> dict[str, Any]:
        self.calls.append(("get", chat_id, None))
        return {"success": True, "value": self.current}

    async def set_adjust(self, chat_id: str, value: float) -> dict[str, Any]:
        self.calls.append(("set", chat_id, float(value)))
        if self.fail_set:
            raise RuntimeError("模拟能力故障")
        if not self.silent:
            self.current = float(value)
        return {"success": True, "value": float(value)}


class FakeProactiveCapability:
    def __init__(self) -> None:
        self.triggered: list[dict[str, Any]] = []

    async def trigger(self, stream_id: str, intent: str, **kwargs: Any) -> dict[str, Any]:
        self.triggered.append({"stream_id": stream_id, "intent": intent, **kwargs})
        return {"success": True}


class FakeGateCtx:
    def __init__(
        self,
        *,
        current: float = 1.0,
        fail_set: bool = False,
        with_maisaka: bool = True,
        silent: bool = False,
    ) -> None:
        self.frequency = FakeFrequencyCapability(current, fail_set=fail_set, silent=silent)
        if with_maisaka:
            self.maisaka = SimpleNamespace(proactive=FakeProactiveCapability())
        self.logger = FakeLogger()


async def test_energy_gate(tmp: str) -> None:
    """精力门控：下限不能为 0、原值要记住并还原、告别语只发一次、失败不炸插件。"""

    print("[9] 精力门控")
    gate_mod = importlib.import_module(f"{_PKG_NAME}.cg_energy_gate")
    EnergyGate = gate_mod.EnergyGate

    # ── 下限必须严格大于 0 ──
    # 宿主 scheduler.py 的静默分支在强制触发之前，频率为 0 时连 @ 都不回。
    check(gate_mod.DEFAULT_GATE_FLOOR > 0.0, "默认下限严格大于 0（否则 @ 也不回）")
    check(gate_mod.DEFAULT_GATE_FLOOR < 0.2, "默认下限足够低（日常闲聊攒不够条数）")

    # ── 进入耗尽：压频率 + 发一次告别语 ──
    ctx = FakeGateCtx(current=1.0)
    gate = EnergyGate(ctx, logger=ctx.logger)
    await gate.sync("s1", voice=0.0, exhausted=True, now=1000.0)
    check(abs(ctx.frequency.current - gate_mod.DEFAULT_GATE_FLOOR) < 1e-9,
          "耗尽时把频率压到下限")
    check(gate.tracked_sessions() == ["s1"], "记住被压过的会话")
    check(len(ctx.maisaka.proactive.triggered) == 1, "耗尽时触发一次告别语")
    farewell = ctx.maisaka.proactive.triggered[0]
    check(farewell["stream_id"] == "s1", "告别语发给正确的会话")
    check("精力见底" in farewell["intent"], "告别语说明了精力见底")
    check(farewell["priority"] == "low", "告别语用低优先级（不打断正在聊的话题）")

    # ── 再次同步不应重复压或重复告别 ──
    await gate.sync("s1", voice=0.0, exhausted=True, now=2000.0)
    check(len(ctx.maisaka.proactive.triggered) == 1, "重复同步不重复发告别语")
    sets = [c for c in ctx.frequency.calls if c[0] == "set"]
    check(len(sets) == 1, "重复同步不重复写宿主频率")

    # ── 节流：间隔内的调用直接返回 ──
    ctx2 = FakeGateCtx(current=1.0)
    gate2 = EnergyGate(ctx2, logger=ctx2.logger)
    await gate2.sync("s1", voice=0.0, exhausted=True, now=1000.0)
    before = len(ctx2.frequency.calls)
    await gate2.sync("s1", exhausted=False, now=1000.0 + gate_mod.SYNC_INTERVAL_SECONDS - 1)
    check(len(ctx2.frequency.calls) == before, "节流窗口内不发 RPC")

    # ── 恢复：写回原值，而不是写死 1.0 ──
    ctx3 = FakeGateCtx(current=0.4)  # 别的插件先把频率调成了 0.4
    gate3 = EnergyGate(ctx3, logger=ctx3.logger)
    await gate3.sync("s1", voice=0.0, exhausted=True, now=1000.0)
    check(abs(ctx3.frequency.current - gate_mod.DEFAULT_GATE_FLOOR) < 1e-9,
          "原值已低于下限时不再抬高")
    await gate3.sync("s1", exhausted=False, now=2000.0)
    check(abs(ctx3.frequency.current - 0.4) < 1e-9, "恢复时写回原值 0.4（不覆盖别人的调整）")
    check(gate3.tracked_sessions() == [], "恢复后不再跟踪该会话")

    # ── 恢复后可以再次告别（下一轮耗尽） ──
    await gate3.sync("s1", voice=0.0, exhausted=True, now=3000.0)
    check(len(ctx3.maisaka.proactive.triggered) == 2, "恢复精力后再次耗尽可以再告别一次")

    # ── 能力故障不能炸掉插件 ──
    ctx4 = FakeGateCtx(current=1.0, fail_set=True)
    gate4 = EnergyGate(ctx4, logger=ctx4.logger)
    await gate4.sync("s1", voice=0.0, exhausted=True, now=1000.0)
    check(gate4.tracked_sessions() == [], "写频率失败时不记录原值（避免错误恢复）")

    # ── 没有 maisaka 能力时只是少一句告别，门控照常 ──
    ctx5 = FakeGateCtx(current=1.0, with_maisaka=False)
    gate5 = EnergyGate(ctx5, logger=ctx5.logger)
    await gate5.sync("s1", voice=0.0, exhausted=True, now=1000.0)
    check(abs(ctx5.frequency.current - gate_mod.DEFAULT_GATE_FLOOR) < 1e-9,
          "没有 proactive 能力时仍然压频率")

    # ── 原值落盘与卸载恢复 ──
    db = os.path.join(tmp, "gate_test.db")
    store = store_mod.GapStore(db)
    store.open()
    _OPEN_STORES.append(store)

    ctx6 = FakeGateCtx(current=0.7)
    gate6 = EnergyGate(ctx6, logger=ctx6.logger)
    await gate6.sync("s2", voice=0.0, exhausted=True, now=1000.0, store=store)
    check(abs(ctx6.frequency.current - gate_mod.DEFAULT_GATE_FLOOR) < 1e-9, "压频率成功")
    persisted = store.meta_prefix("host_freq_original")
    check(len(persisted) == 1, "原值已落盘（插件崩溃后还能恢复）")

    # 模拟「插件被杀」：新实例从落盘值恢复
    ctx7 = FakeGateCtx(current=gate_mod.DEFAULT_GATE_FLOOR)
    gate7 = EnergyGate(ctx7, logger=ctx7.logger)
    await gate7.restore_all(store)
    check(abs(ctx7.frequency.current - 0.7) < 1e-9, "新实例能从落盘原值恢复频率")
    check(store.meta_prefix("host_freq_original") == {}, "恢复后清掉落盘记录")

    # restore_all 幂等
    await gate7.restore_all(store)
    check(True, "restore_all 可重复调用")

    # ── 宿主静默失败：set 返回 success 但实际没生效 ──
    # 宿主 adjust_talk_frequency 在会话还没建起来时只打 warning 就返回，
    # 能力层照样回 {"success": True}。不读回确认就会把「没改成」记成「改成了」。
    ctx_silent = FakeGateCtx(current=1.0, silent=True)
    gate_silent = EnergyGate(ctx_silent, logger=ctx_silent.logger)
    await gate_silent.sync("s1", voice=0.0, exhausted=True, now=1000.0)
    check(gate_silent.tracked_sessions() == [],
          "宿主静默失败时不记录原值（否则恢复时会写回错误值）")
    check(any("没能改成" in msg for _, msg in ctx_silent.logger.lines),
          "宿主静默失败时留下日志")

    # ── farewell=False 表示用户明确不要告别语（配置留空） ──
    ctx9 = FakeGateCtx(current=1.0)
    gate9 = EnergyGate(ctx9, logger=ctx9.logger)
    await gate9.sync("s1", voice=0.0, exhausted=True, now=1000.0, farewell=False)
    check(ctx9.maisaka.proactive.triggered == [], "farewell=False 时不发告别语")
    check(abs(ctx9.frequency.current - gate_mod.DEFAULT_GATE_FLOOR) < 1e-9,
          "farewell=False 仍然压频率")

    # ── 分级：voice 是倍率，不是开关（精力 <0.5 才压，越少压得越狠） ──
    ctx_half = FakeGateCtx(current=1.0)
    gate_half = EnergyGate(ctx_half, logger=ctx_half.logger)
    await gate_half.sync("s1", voice=0.5, now=1000.0)
    check(abs(ctx_half.frequency.current - 0.5) < 1e-9, "voice=0.5 时把频率压到原值的一半")
    check(ctx_half.maisaka.proactive.triggered == [],
          "没耗尽时只说少说几句，不说告别语")

    # 更累了，继续往下压
    await gate_half.sync("s1", voice=0.2, now=1000.0 + gate_mod.SYNC_INTERVAL_SECONDS + 1)
    check(abs(ctx_half.frequency.current - 0.2) < 1e-9, "voice 变小后频率继续下降")
    check(abs(gate_half._original["s1"] - 1.0) < 1e-9, "原值始终记的是最初那个 1.0")

    # 恢复：倍率回到 1.0 就还原
    await gate_half.sync("s1", voice=1.0, now=1000.0 + 3 * gate_mod.SYNC_INTERVAL_SECONDS)
    check(abs(ctx_half.frequency.current - 1.0) < 1e-9, "voice 回到 1.0 后频率还原")

    # 变化很小就不必再发一次 RPC
    ctx_tiny = FakeGateCtx(current=1.0)
    gate_tiny = EnergyGate(ctx_tiny, logger=ctx_tiny.logger)
    await gate_tiny.sync("s1", voice=0.5, now=1000.0)
    sets_before = len([c for c in ctx_tiny.frequency.calls if c[0] == "set"])
    await gate_tiny.sync("s1", voice=0.505, now=1000.0 + gate_mod.SYNC_INTERVAL_SECONDS + 1)
    sets_after = len([c for c in ctx_tiny.frequency.calls if c[0] == "set"])
    check(sets_after == sets_before, "倍率几乎没变时不重复写宿主频率")

    # ── 空 session_id 直接忽略 ──
    ctx8 = FakeGateCtx()
    gate8 = EnergyGate(ctx8, logger=ctx8.logger)
    await gate8.sync("", voice=0.0, exhausted=True, now=1000.0)
    check(ctx8.frequency.calls == [], "空 session_id 不发 RPC")

    # ── _pick_value 兼容两种返回形状 ──
    check(EnergyGate._pick_value({"success": True, "value": 0.6}) == 0.6, "认宿主包装格式")
    check(EnergyGate._pick_value(0.6) == 0.6, "认裸数字")
    check(EnergyGate._pick_value({"success": False, "value": 0.6}) is None, "success=False 视为失败")
    check(EnergyGate._pick_value("坏值") is None, "非法值返回 None")


# ── 12. 缺席感与边界感（用户第 12 轮要的两个功能）──


def test_absence() -> None:
    """缺席感：多久没见 + 关系多近 → 反应档位。

    这两件事必须一起看：陌生人没理由在意你多久没来，所以同样的时长，
    好感度低的人拿到的档位必须更低（甚至不提醒）。
    """

    print("[12] 缺席感")
    absence_mod = importlib.import_module(f"{_PKG_NAME}.cg_absence")
    now = 1_800_000_000.0

    # ── 时长换算 ──
    check(absence_mod.absence_seconds(now - 3600, now=now) == 3600.0, "缺席秒数按差值算")
    check(absence_mod.absence_seconds(0, now=now) == 0.0, "没有时间戳时返回 0（不产生缺席感）")
    check(absence_mod.absence_seconds(None, now=now) == 0.0, "None 时间戳返回 0")
    check(
        absence_mod.absence_seconds(now + 9999, now=now) == 0.0,
        "未来时间戳（时钟回拨）按刚说过处理，不能算出负数",
    )
    check(
        absence_mod.absence_seconds(float("nan"), now=now) == 0.0,
        "NaN 时间戳返回 0，不能因为脏数据突然说「很久没见」",
    )

    # ── 人话描述 ──
    check(absence_mod.describe_gap(0.5) == "30 分钟", "半小时说成人话")
    check(absence_mod.describe_gap(5) == "5 小时", "小时说成人话")
    check(absence_mod.describe_gap(36) == "一天多", "一天半说成「一天多」")
    check(absence_mod.describe_gap(24 * 3) == "3 天", "三天说成「3 天」")
    check("个月" in absence_mod.describe_gap(24 * 60), "两个月说成月")
    check("年" in absence_mod.describe_gap(24 * 800), "两年多说成年")

    # ── 档位：时长 + 好感度共同决定 ──
    # 低于最少间隔一律不提
    check(absence_mod.absence_level(1.0, affection=80.0, min_hours=6.0) == "none",
          "不到最少间隔不提")
    # 陌生人：即使很久，也只可能拿到 mild（而且大多数时候是 none）
    check(absence_mod.absence_level(10.0, affection=0.0, min_hours=6.0) == "none",
          "陌生人隔 10 小时不提（他为什么要在意）")
    check(absence_mod.absence_level(100.0, affection=0.0, min_hours=6.0) == "mild",
          "陌生人隔很久最多轻描淡写一句")
    # 熟人：可以真的介意
    check(absence_mod.absence_level(8.0, affection=80.0, min_hours=6.0) == "mild",
          "熟人隔 8 小时是轻提一句")
    check(absence_mod.absence_level(40.0, affection=80.0, min_hours=6.0) == "noted",
          "熟人隔 40 小时是注意到了")
    check(absence_mod.absence_level(100.0, affection=80.0, min_hours=6.0) == "missed",
          "熟人隔很久会真的介意")
    # 好感度分档
    check(absence_mod.absence_level(100.0, affection=30.0, min_hours=6.0) == "noted",
          "好感 30 的人隔很久是注意到，不会到「介意」")
    # 单调性：同样时长，好感越高档位不低于好感低的
    rank = {"none": 0, "mild": 1, "noted": 2, "missed": 3}
    seq = [
        rank[absence_mod.absence_level(50.0, affection=aff, min_hours=6.0)]
        for aff in (0.0, 30.0, 60.0, 90.0)
    ]
    check(seq == sorted(seq), f"好感度越高反应越重（实际 {seq}）")

    # ── 渲染 ──
    note = absence_mod.render_absence_note(
        hours=40.0, level="noted", chat_type="private", who="小明"
    )
    check("小明" in note, "私聊缺席提示带出对方称呼")
    check("40" in note or "一天多" in note, "私聊缺席提示说清隔了多久")
    check(note.startswith("（") and note.endswith("）"), "缺席提示用括号包起来")
    check("你要问他" not in note, "不下命令（不写「你要问他去哪了」）")
    group_note = absence_mod.render_absence_note(
        hours=40.0, level="noted", chat_type="group", who="小明"
    )
    check("小明" not in group_note, "群聊缺席提示不提具体的人")
    check("这个群" in group_note, "群聊缺席提示说的是「这个群」")
    check(
        absence_mod.render_absence_note(hours=40.0, level="none") == "",
        "档位为 none 时返回空串（调用方据此不注入）",
    )
    check(
        absence_mod.render_absence_note(hours=0, level="mild") == "",
        "时长为 0 时返回空串",
    )


def test_boundary() -> None:
    """边界感：三点同时成立才允许沉默 + 频率上限 + 只给状态不给命令。"""

    print("[13] 边界感")
    boundary_mod = importlib.import_module(f"{_PKG_NAME}.cg_boundary")
    now = 1_800_000_000.0

    # ── 厌恶口味匹配 ──
    prefs = [
        {"kind": "dislike", "topic": "被使唤来使唤去", "strength": 0.8},
        {"kind": "dislike", "topic": "沉重的话题", "strength": 0.4},  # 强度不够
        {"kind": "like", "topic": "颜文字", "strength": 0.9},  # 喜欢的不算
    ]
    hits = boundary_mod.matched_dislikes("你别老使唤我，被使唤来使唤去烦不烦", prefs)
    check(len(hits) == 1 and hits[0][0] == "被使唤来使唤去", "命中厌恶口味（按子串）")
    check(
        boundary_mod.matched_dislikes("沉重的话题", prefs) == [],
        "强度不够的厌恶口味不命中",
    )
    check(boundary_mod.matched_dislikes("颜文字", prefs) == [], "喜欢的口味不算踩雷")
    check(boundary_mod.matched_dislikes("", prefs) == [], "空消息不命中任何口味")

    # ── 三点闸门：任一不过就不许沉默 ──
    base = dict(
        valence=-0.6, anger=0.7, tension=0.8, dislikes=[("被使唤来使唤去", 0.8)],
        now=now, cooldown=[],
    )
    decision = boundary_mod.evaluate_silence(**base)
    check(decision["allowed"] is True, "三点都成立时允许沉默")
    check(decision["budget_left"] == 2, "初始配额是窗口上限")

    # 心情好 → 不许沉默（心情不好才可能）
    check(
        boundary_mod.evaluate_silence(**{**base, "valence": 0.5})["allowed"] is False,
        "心情好时不许沉默",
    )
    # 只有心情差、不够生气 → 不许
    check(
        boundary_mod.evaluate_silence(**{**base, "anger": 0.1})["allowed"] is False,
        "只有心情差不生气时不许沉默（愉悦度和愤怒要同时过）",
    )
    # 关系不绷 → 不许（陌生人张力天然低，靠这个避免误伤）
    check(
        boundary_mod.evaluate_silence(**{**base, "tension": 0.1})["allowed"] is False,
        "张力低时不许沉默（陌生人不会因为一句话就被冷落）",
    )
    # 没踩中厌恶 → 不许
    check(
        boundary_mod.evaluate_silence(**{**base, "dislikes": []})["allowed"] is False,
        "没踩中厌恶话题时不许沉默",
    )

    # ── 频率上限 ──
    full = [now - 60, now - 120]
    denied = boundary_mod.evaluate_silence(**{**base, "cooldown": full})
    check(denied["allowed"] is False, "配额用完后不许沉默")
    check(denied["budget_left"] == 0, "配额用完时剩余为 0")
    check(
        any("频率=不过" in r for r in denied["reasons"]),
        "拒绝原因里要说清是频率限制",
    )
    # 窗口外的历史不算
    old = [now - 4 * 3600, now - 5 * 3600]
    check(
        boundary_mod.evaluate_silence(
            **{**base, "cooldown": old, "window_hours": 3.0}
        )["allowed"]
        is True,
        "窗口之外的历史不占配额",
    )
    # 0 表示不限制
    check(
        boundary_mod.evaluate_silence(
            **{**base, "cooldown": full, "max_per_window": 0}
        )["allowed"]
        is True,
        "上限为 0 时不限制次数",
    )

    # ── 记账 ──
    history = boundary_mod.record_silence([], now=now)
    check(len(history) == 1 and abs(history[0] - now) < 1e-6, "记账记下当前时刻")
    again = boundary_mod.record_silence(history, now=now + 60)
    check(len(again) == 2, "记账会追加")
    trimmed = boundary_mod.record_silence([float(i) for i in range(100)], now=now, keep=8)
    check(len(trimmed) == 8, "记账列表有上限，不会无限增长")

    # ── 渲染：只给状态，不给命令 ──
    note = boundary_mod.render_boundary_note(
        valence=-0.5, anger=0.5, tension=0.7,
        dislikes=[("被使唤来使唤去", 0.8)], label="有点烦躁",
    )
    check("有点烦躁" in note, "状态提示带出情绪标签")
    check("被使唤来使唤去" in note, "状态提示带出她讨厌什么")
    check("绷着" in note or "别扭" in note, "张力高时描述相处感受")
    check("不要把它们说出来" in note, "提示别把状态直接讲出来")
    check(
        "由你自己决定" in note,
        "把「要不要回、怎么回」的判断权交给模型（用户第 12 轮要求）",
    )
    check(
        "如果被冒犯就" not in note,
        "不写「如果被冒犯就拒绝」这种命令式规则",
    )
    # 沉默提示按开关出现
    with_silence = boundary_mod.render_boundary_note(
        valence=-0.5, anger=0.5, tension=0.7, dislikes=[], silence_allowed=True
    )
    without_silence = boundary_mod.render_boundary_note(
        valence=-0.5, anger=0.5, tension=0.7, dislikes=[], silence_allowed=False
    )
    check("干脆不发" in with_silence, "允许沉默时给出「干脆不发」这个选项")
    check("冷淡地应一声" in with_silence, "允许沉默时给出「冷淡应一声」这个选项")
    check("干脆不发" not in without_silence, "没允许沉默时不给这两个选项")

    # silence_level 两种模式真的渲染出不同文本。
    # 这条曾经是假的：plugin 算出了 silence_mode 却没往下传，配置项等于摆设。
    taciturn = boundary_mod.render_boundary_note(
        valence=-0.5, anger=0.5, tension=0.7, dislikes=[],
        silence_allowed=True, silence_mode=boundary_mod.SILENCE_MODE_TACITURN,
    )
    check("干脆不发" not in taciturn, "taciturn 模式下不给「干脆不发」（不允许真的不回）")
    check("只回很短的一句" in taciturn, "taciturn 模式下只允许冷淡应一声")
    check(
        "两个选择" not in taciturn,
        "taciturn 模式下不再让模型二选一",
    )
    choose_mode = boundary_mod.render_boundary_note(
        valence=-0.5, anger=0.5, tension=0.7, dislikes=[],
        silence_allowed=True, silence_mode=boundary_mod.SILENCE_MODE_SILENT,
    )
    check(choose_mode == with_silence, "silent 模式是默认行为，与不传 silence_mode 一致")

    # 反向断言：affection 曾经是收了完全不用的死参数，删掉就别再加回来。
    import inspect as _inspect

    check(
        "affection" not in _inspect.signature(boundary_mod.render_boundary_note).parameters,
        "render_boundary_note 不再收 affection（曾经是收了不用的死参数）",
    )

    # 平静时的措辞不能露出「心情很好」这种会误导的话
    calm = boundary_mod.render_boundary_note(
        valence=0.0, anger=0.0, tension=0.0, dislikes=[]
    )
    check("心情一般" in calm, "没有明显情绪时说「心情一般」")


def test_presence() -> None:
    """在场感知：把「多久没说话」拆成「她不在」和「对方没来」。

    这个模块存在的唯一理由是一条真机事故：用户每天 00:00 关机、11:00 开机，
    某天上午开机后她看到「73 小时没说话」就说「你怎么这么久没来」——
    可其中 33.8 小时是她自己不在。所以核心断言全是围绕「别把关机算成冷落」。
    """

    print("[14] 在场感知")
    presence_mod = importlib.import_module(f"{_PKG_NAME}.cg_presence")
    from datetime import datetime, timedelta

    # base 必须**带时区**：模块内部统一用 aware datetime（宿主写的是本地时间的
    # datetime.now()，无时区字符串按本地时区补 tzinfo）。naive 和 aware 直接比
    # 恒为 False，会造出一堆看起来像「算错了」的假失败。
    base = datetime(2026, 11, 5, 12, 0, 0).astimezone()

    # ── 时间解析：宿主给的是无时区 ISO 字符串 ──
    # 解析结果一律带时区（宿主写的是本地时间的 datetime.now()），所以比对时
    # 要把 tzinfo 剥掉再比，否则 aware 和 naive 直接 == 恒为 False。
    parsed = presence_mod._to_datetime("2026-11-05T11:00:00")
    check(parsed is not None, "能解析宿主 _to_dict 产出的无时区 ISO 字符串")
    check(
        parsed.replace(tzinfo=None) == datetime(2026, 11, 5, 11, 0, 0),
        f"解析出来的时刻正确（实际 {parsed}）",
    )
    check(parsed.tzinfo is not None, "解析结果带时区（后续区间计算不能用 naive datetime）")
    check(presence_mod._to_datetime("") is None, "空字符串解析成 None")
    check(presence_mod._to_datetime("乱码") is None, "非法字符串解析成 None")
    check(
        presence_mod._to_datetime(1_800_000_000.0) is not None,
        "能解析 float 形式的时间戳",
    )
    check(
        presence_mod._to_datetime("1800000000") is not None,
        "能解析字符串形式的 unix 时间戳",
    )

    # ── 区间合并：相邻窗口要合并，否则离线时长会算爆 ──
    # 宿主每 5 分钟写一行、每次把 end 往后推 1 分钟，所以正常必然重叠。
    rows = [
        {"start_timestamp": base - timedelta(hours=6), "end_timestamp": base - timedelta(hours=5)},
        {"start_timestamp": base - timedelta(hours=5, minutes=-1), "end_timestamp": base - timedelta(hours=4)},
        {"start_timestamp": base - timedelta(hours=3), "end_timestamp": base - timedelta(hours=2)},
    ]
    merged = presence_mod.merge_online_windows(rows)
    check(len(merged) == 2, f"重叠窗口合并（3 段 → {len(merged)} 段）")
    check(
        merged[0][0] == base - timedelta(hours=6),
        "合并后的区间从最早那个起点开始",
    )
    check(
        merged[0][1] == base - timedelta(hours=4),
        "合并后的区间到最晚那个终点结束（中间那一分钟没被当成离线）",
    )
    # 间隔超过 slack 就不合并
    far = [
        {"start_timestamp": base - timedelta(hours=6), "end_timestamp": base - timedelta(hours=5)},
        {"start_timestamp": base - timedelta(hours=4), "end_timestamp": base - timedelta(hours=3)},
    ]
    check(
        len(presence_mod.merge_online_windows(far)) == 2,
        "间隔超过容差的两段不合并（那是真的离线）",
    )
    # end < start 的残行不能把时长算成负数
    broken = presence_mod.merge_online_windows(
        [{"start_timestamp": base, "end_timestamp": base - timedelta(hours=1)}]
    )
    check(
        len(broken) == 1 and broken[0][1] >= broken[0][0],
        "end < start 的脏行被拉平成零长度而不是负数",
    )

    # ── 覆盖计算 ──
    windows = [(base - timedelta(hours=10), base - timedelta(hours=8))]
    check(
        presence_mod.overlap_seconds(
            windows, base - timedelta(hours=12), base
        )
        == 2 * 3600.0,
        "覆盖时长只算真正落在窗口内的那两小时",
    )
    check(
        presence_mod.overlap_seconds(windows, base - timedelta(hours=6), base) == 0.0,
        "完全不相交时覆盖为 0",
    )

    # ── 四档判定 ──
    # 太短的间隔什么都不必提 —— 她离开两小时回来说「我刚才不在」是废话。
    # 这条是实跑真机副本时发现的：2.2 小时的间隔被老版本判成 away 并去注入。
    check(
        presence_mod.classify(gap_hours=2.2, awake_hours=0.1, offline_hours=2.1)
        == "none",
        "间隔不到门槛时什么都别提（「我刚离开两小时」不是缺席感）",
    )
    # away：她大半时间不在，且间隔确实够长
    check(
        presence_mod.classify(
            gap_hours=12.0, awake_hours=0.5, offline_hours=11.5
        )
        == "away",
        "间隔 12 小时但离线 11.5 小时 → 「我不在」而不是「被冷落」",
    )
    # none：刚聊过
    check(
        presence_mod.classify(gap_hours=0.2, awake_hours=0.2, offline_hours=0.0)
        == "none",
        "间隔很短 → 刚聊过",
    )
    # cold：真的很久没来
    check(
        presence_mod.classify(gap_hours=40.0, awake_hours=39.0, offline_hours=1.0)
        == "cold",
        "醒着等了 39 小时 → 这才该有缺席感",
    )
    # pending 优先级最高
    check(
        presence_mod.classify(
            gap_hours=40.0, awake_hours=39.0, offline_hours=1.0, pending=3
        )
        == "pending",
        "有待补消息时优先补洞（拿没读过的消息当感情依据很假）",
    )

    # ── analyse 把三件事串起来 ──
    presence = presence_mod.analyse(
        last_seen_ts=(base - timedelta(hours=12)).timestamp(),
        windows=windows,  # 只覆盖 10~8 小时前
        now=base,
    )
    check(abs(presence.gap_hours - 12.0) < 1e-3, "间隔算对了")
    check(abs(presence.offline_hours - 10.0) < 1e-3, "离线时长 = 间隔 − 在线覆盖")
    check(abs(presence.awake_hours - 2.0) < 1e-3, "醒着时长 = 间隔 − 离线时长")
    check(
        presence.gap_hours - presence.offline_hours - presence.awake_hours < 1e-6,
        "三段之和必须回到总间隔，不能凭空多出或少掉时间",
    )
    # 未来时间戳 / 没有时间戳
    check(
        presence_mod.analyse(last_seen_ts=base.timestamp() + 3600, windows=windows, now=base)
        .gap_seconds
        == 0.0,
        "未来时间戳（时钟回拨）按 0 处理",
    )
    check(
        presence_mod.analyse(last_seen_ts=0, windows=windows, now=base).gap_seconds == 0.0,
        "没有 last_seen 时返回全零",
    )

    # ── 措辞：只说事实，不带情绪，不怪对方 ──
    away_note = presence_mod.render_away_note(hours=11.5, how="11 小时")
    check("不在" in away_note, "「我不在」的提示要说清是她不在")
    check(
        "不是对方没来" in away_note,
        "必须明确排除「对方冷落我」这个误读",
    )
    check(
        "冷落" not in away_note and "怪" not in away_note,
        "不写任何指责性的词",
    )

    # 反向断言：这些常量一旦被改小，离线会被判成在线、结论全反。
    check(
        presence_mod.MERGE_SLACK.total_seconds() >= 60,
        "合并容差不能太小（太小会把连续在线切成几十段、离线算爆）",
    )
    check(
        presence_mod.ONLINE_SLACK.total_seconds() >= 60,
        "「此刻在线」的判定也要有容差",
    )


def test_initiative() -> None:
    """自主发言闸门：兴趣 / 好感度 / 心情 加权，硬过滤优先。

    这是用户明确要的东西（原话「精力门控即便不开 focus 模式，也不应该是简单地
    调整发言频率」）。核心语义是「决定不说」而不是「延迟」。
    """

    print("[15] 自主发言")
    init_mod = importlib.import_module(f"{_PKG_NAME}.cg_initiative")

    # ── 兴趣打分 ──
    likes = ["原神", "编程"]
    check(
        init_mod.interest_score("今天又去蹭原神的活动了", likes=likes) > 0.0,
        "命中喜欢的话题要有分",
    )
    check(
        init_mod.interest_score("今天天气不错", likes=likes) == 0.0,
        "没命中任何喜欢的话题就是 0 分",
    )
    check(
        init_mod.interest_score("原神和编程都比拼图好玩", likes=likes) >= 0.79,
        "命中两条 → 0.8（比只命中一条高，但还没到顶）",
    )
    check(
        init_mod.interest_score("原神、编程、还有拼图", likes=["原神", "编程", "拼图"]) >= 0.99,
        "命中三条封顶 1.0（再多也一样）",
    )
    check(
        init_mod.interest_score("", likes=likes) == 0.0,
        "空文本不产生兴趣分",
    )
    check(
        init_mod.interest_score("原神" * 200, likes=likes)
        == init_mod.interest_score("原神", likes=likes),
        "**长度不加权**：长和有兴趣是两件事",
    )

    # ── 好感度 / 心情 ──
    check(init_mod.affection_score(0.0) == 0.0, "好感度为 0 时分数为 0")
    check(
        init_mod.affection_score(init_mod.AFFECTION_CEILING) >= 0.99,
        "好感度到顶时分数到顶",
    )
    check(
        init_mod.affection_score(999.0) <= 1.0,
        "好感度再高也不超过 1.0",
    )
    check(init_mod.mood_score(valence=1.0, arousal=1.0) >= 0.99, "心情很好时分高")
    check(init_mod.mood_score(valence=-1.0, arousal=0.0) <= 0.01, "心情很差时分低")

    # ── 硬过滤优先 ──
    decision = init_mod.evaluate(
        at_pending=True, at_always_reply=True,
        exhausted=True, boundary_silent=True,
        text="完全无关的话题", likes=[], affection=0.0, valence=-1.0,
    )
    check(decision.allow, "被 @ 时优先于「能耗尽」「边界感」等所有硬过滤")
    check(decision.reason == init_mod.REASON_AT, f"原因是「被@」（实际 {decision.reason}）")

    decision = init_mod.evaluate(
        at_pending=True, at_always_reply=False,
        exhausted=True,
        text="", likes=[], affection=0.0, valence=0.0,
    )
    check(not decision.allow, "@ 必回开关关掉后，能耗尽仍然能拦住")

    decision = init_mod.evaluate(at_pending=False, mentioned_pending=True,
                                 mentioned_always_reply=True)
    check(decision.allow and decision.reason == init_mod.REASON_MENTIONED,
          "被提到 + 开关打开 → 必回")

    decision = init_mod.evaluate(at_pending=False, mentioned_pending=True,
                                 mentioned_always_reply=False,
                                 text="", likes=[], affection=0.0, valence=0.0,
                                 energy=0.0, threshold=0.99)
    check(not decision.allow,
          "被提到但开关关掉时走软评分（不会硬性必回）——@ 和「提到」是两个开关")

    decision = init_mod.evaluate(exhausted=True)
    check(
        not decision.allow and decision.hard and decision.reason == init_mod.REASON_EXHAUSTED,
        "精力耗尽 → 硬拦，且标记 hard（不参与软评分）",
    )

    decision = init_mod.evaluate(boundary_silent=True)
    check(
        not decision.allow and decision.reason == init_mod.REASON_BOUNDARY,
        "边界感判「不想接话」时主动必须一起压掉（两处共用一个闸门）",
    )

    # ── 软评分 ──
    good = init_mod.evaluate(
        text="今天原神新版本", likes=["原神"],
        affection=80.0, valence=0.8, arousal=0.7, energy=1.0,
    )
    check(good.allow, f"三样都好时应该开口（score={good.score:.2f}）")
    check(good.reason == init_mod.REASON_ALLOW, "放行的原因是「可以开口」")

    bad = init_mod.evaluate(
        text="今天天气不错", likes=["原神"],
        affection=0.0, valence=-0.5, arousal=0.2, energy=0.3,
    )
    check(not bad.allow, f"三样都差时不该开口（score={bad.score:.2f}）")
    check(bad.reason == init_mod.REASON_LOW_SCORE, "拦截的原因是「分数不够」")
    check(
        set(bad.parts) == {"interest", "affection", "mood", "energy"},
        "分数明细要能逐项看到（日志里靠它排查「为什么不说话」）",
    )

    # 没有文本时 interest 不参与，权重重新归一化 —— 否则「没话题」会被系统性压低分
    no_text = init_mod.evaluate(
        text="", likes=["原神"], affection=80.0, valence=0.8, arousal=0.7, energy=1.0
    )
    check(
        "interest" not in no_text.parts,
        "没有可读文本时兴趣项不参与打分",
    )
    check(
        no_text.allow,
        "没有文本时其余三项足够好就应该开口（权重要重新归一化）",
    )

    check(init_mod.evaluate(enabled=False).allow, "闸门总开关关闭时恒放行")
    check(
        init_mod.evaluate(enabled=False).reason == init_mod.REASON_DISABLED,
        "关闭状态要能被日志看出来",
    )

    # ── 工具过滤 ──
    tools = [
        {"type": "function", "function": {"name": "reply", "parameters": {}}},
        {"type": "function", "function": {"name": "send_emoji", "parameters": {}}},
        {"type": "function", "function": {"name": "query_memory", "parameters": {}}},
    ]
    kept, removed = init_mod.filter_tools(tools, allow_reply=False, drop=("reply", "send_emoji"))
    check(len(kept) == 1, "不许开口时把 reply 和 send_emoji 都收回")
    check(kept[0]["function"]["name"] == "query_memory", "只留下不发消息的工具")
    check(set(removed) == {"reply", "send_emoji"}, "被收回的工具名要报出来（日志用）")

    kept, removed = init_mod.filter_tools(tools, allow_reply=True, drop=("reply", "send_emoji"))
    check(len(kept) == 3 and removed == [], "允许开口时清单原样不动")

    kept, removed = init_mod.filter_tools(
        [{"name": "reply"}, {"name": "query_memory"}], allow_reply=False, drop=("reply",)
    )
    check(len(kept) == 1 and kept[0]["name"] == "query_memory", "兼容裸 name 形态")

    kept, removed = init_mod.filter_tools(None, allow_reply=False)
    check(kept is None, "工具清单形态不认识时返回 None（**看不懂就别改**）")
    check(removed == [], "看不懂时也不谎报收回了什么")

    # ── 描述 ──
    text = init_mod.describe(bad)
    check("分数不够" in text, "描述里带原因")
    check("score=" in text and "interest=" in text, "描述里带各项分数")


def test_backfill() -> None:
    """历史补抓：把「她没看见的那几天」从 QQ 端捞回来。

    三个坑都是真机撞出来的（NapCat 文档写错的地方）：锚点是 ``message_id``
    不是 ``message_seq``；``count`` 上限 200 且会被内部裁剪；**空数组不等于
    不可用**（用户原话「私聊应该不是拿不到，而是这几天并没有私聊」）。
    """

    print("[16] 历史补抓")
    backfill_mod = importlib.import_module(f"{_PKG_NAME}.cg_backfill")
    now = 1_800_000_000.0

    # ── 剥壳：适配器返回的是原始响应字典 ──
    rows, error = backfill_mod._unwrap_history(
        {"status": "ok", "retcode": 0, "data": {"messages": [{"message_id": 1}]}}
    )
    check(len(rows) == 1 and error == "", "标准响应能取到 messages")
    check(
        backfill_mod._unwrap_history({"status": "failed", "retcode": 100,
                                      "wording": "failed to resolve UID"})[1] != "",
        "status 不是 ok 时返回错误（不能当「没有消息」）",
    )
    check(
        backfill_mod._unwrap_history({"status": "ok", "retcode": 0, "data": {"messages": []}})
        == ([], ""),
        "**空数组是正常结果**，不是错误",
    )
    check(
        backfill_mod._unwrap_history(None)[1] != "",
        "None 响应报错",
    )

    # ── OneBot → 插件消息转换 ──
    raw = {
        "message_id": 12345,
        "time": 1_800_000_000,
        "user_id": "100000001",
        "sender": {"user_id": "100000001", "nickname": "群友A", "card": "群友A"},
        "group_id": "999",
        "message": [{"type": "text", "data": {"text": "你好"}}],
        "raw_message": "你好",
    }
    converted = backfill_mod.to_plugin_message(
        raw, platform="qq", session_id="s1", group_id="999", group_name="测试群"
    )
    check(converted.get("message_id") == "12345", "message_id 转成字符串")
    check(
        converted["message_info"]["user_info"]["user_id"] == "100000001",
        "用户 id 落进 message_info.user_info",
    )
    check(
        converted["message_info"]["group_info"]["group_id"] == "999",
        "群 id 落进 group_info",
    )
    check(
        converted["raw_message"][0]["data"] == "你好",
        "消息段的 data 是字符串（插件内部认的是这套格式，不是 OneBot 的 dict）",
    )
    check(converted.get("_backfilled") is True, "打上补抓标记，便于区分来源")
    check(
        backfill_mod.to_plugin_message({}, platform="qq", session_id="s1") == {},
        "没有 message_id 的脏数据直接丢弃",
    )

    # 非文本段只留摘要，别把 base64 塞进提示词
    image_msg = backfill_mod.to_plugin_message(
        {
            "message_id": 1, "time": 1_800_000_000, "user_id": "1",
            "sender": {"user_id": "1", "nickname": "A"},
            "message": [{"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg",
                                                   "base64": "AAAA" * 1000}}],
        },
        platform="qq", session_id="s1",
    )
    image_text = image_msg["raw_message"][0]["data"]
    check("base64" not in image_text, "图片段不保留 base64")
    check(len(image_text) < 200, "图片段只留短摘要")

    # ── 合并与去重 ──
    host = [
        {"message_id": "1", "timestamp": "100", "processed_plain_text": "宿主版"},
        {"message_id": "2", "timestamp": "200", "processed_plain_text": "只有宿主有"},
    ]
    extra = [
        {"message_id": "1", "timestamp": "100", "processed_plain_text": "补抓版"},
        {"message_id": "3", "timestamp": "300", "processed_plain_text": "只有补抓有"},
    ]
    merged = backfill_mod.merge_messages(host, extra)
    check(len(merged) == 3, "按 message_id 去重后合并（2 + 2 → 3）")
    check(
        [m["message_id"] for m in merged] == ["1", "2", "3"],
        "合并结果按时间升序",
    )
    first = next(m for m in merged if m["message_id"] == "1")
    check(
        first["processed_plain_text"] == "宿主版",
        "重复的消息保留宿主版本（更可信、带 reply_to）",
    )

    # ── 节流与起点 ──
    check(
        backfill_mod.should_backfill(last_backfill_ts=0.0, now=now),
        "从没补过就应该补",
    )
    check(
        not backfill_mod.should_backfill(last_backfill_ts=now - 60, now=now,
                                         min_interval=1800.0),
        "刚补过不到最小间隔就不再补（补的是关机留下的洞，不是天天有新洞）",
    )
    check(
        backfill_mod.should_backfill(last_backfill_ts=now - 3600, now=now,
                                     min_interval=1800.0),
        "超过最小间隔允许再补",
    )
    check(
        abs(backfill_mod.default_since(last_extract_ts=now - 3600, now=now) - (now - 3600)) < 1.0,
        "有上次抽取时间就从那里开始补",
    )
    check(
        abs(
            backfill_mod.default_since(last_extract_ts=0, now=now, max_age_days=10.0)
            - (now - 10 * 86400.0)
        )
        < 1.0,
        "从没抽取过就退回「最多补多少天」",
    )

    # 描述
    desc = backfill_mod.describe_result(
        backfill_mod.BackfillResult(messages=[{}] * 5, pages=2, reached_start=True)
    )
    check("5" in desc, "描述里带条数")
    check("尽头" in desc or "到头" in desc, "翻到缓存尽头要说出来")

    # 反向断言：这些数字是实测出来的，改小会漏消息、改大会撑爆提示词
    check(backfill_mod.PAGE_SIZE <= 200, "每页条数不超过适配器硬上限 200")
    check(backfill_mod.MAX_AGE_DAYS <= 15, "最多补的天数不超过适配器缓存深度")


def test_history() -> None:
    """未读历史：她知道自己错过了什么，也能在被提起时翻回去看。

    用户第 41 轮的原话：「正常人虽然会翻看历史消息，但仅限于几十条的程度，
    对于 99+ 这种，一般不会从头看完……更远的就需要在别人明确提起时，bot 才会
    一边说『我翻翻看哦』，一边回看这些消息。」

    核心取舍只有一条：**L1 只给条数和时间，不给内容。** 给了内容，她就从
    「那会儿我不在」变成「其实我都看过」——那正是用户嫌弃的那种假装。
    """

    print("[17] 未读历史")
    history_mod = importlib.import_module(f"{_PKG_NAME}.cg_history")

    # ── 时间工具：宿主给字符串、适配器给数字 ──
    check(history_mod.as_float("1800000000.5") == 1_800_000_000.5, "能解析字符串时间戳")
    check(history_mod.as_float(1_800_000_000) == 1_800_000_000.0, "能解析数字时间戳")
    check(history_mod.as_float("乱码") == 0.0, "非法字符串退化成 0")
    check(history_mod.as_float(None) == 0.0, "None 退化成 0")
    check(history_mod.as_float(True) == 0.0, "bool 不当成数字（True 不是 1 秒）")

    now = time.time()
    yesterday = now - 86400.0
    check(history_mod.format_day(now) == "今天", "今天的消息说「今天」")
    check(history_mod.format_day(yesterday) == "昨天", "一天前说「昨天」")
    check(history_mod.format_day(now - 2 * 86400.0) == "前天", "两天前说「前天」")
    check("月" in history_mod.format_day(now - 20 * 86400.0), "更早的给具体日期")
    check(history_mod.format_day(0) == "?", "拿不到时间就是问号")

    # 不用「3 天前」这种相对量：说的是「昨晚 11 点」，写成「3 天前」就没法对着
    # 时间线讲了。
    span = history_mod.describe_span(yesterday, yesterday + 3600.0, now=now)
    check("昨天" in span, f"同一天的时间段带日期（实际 {span}）")
    check(history_mod.describe_duration(1800) == "30 分钟", "半小时说成「30 分钟」")
    check("小时" in history_mod.describe_duration(7200), "两小时说成小时")
    check("天" in history_mod.describe_duration(3 * 86400), "三天说成天")

    # ── L1 拆分：窗口之外才是洞 ──
    msgs = []
    for i in range(10):
        msgs.append(
            {
                "message_id": f"m{i}",
                "timestamp": str(1_800_000_000.0 + i * 60),
                "processed_plain_text": f"第{i}条",
                "message_info": {
                    "user_info": {"user_id": "u1", "user_nickname": "阿岚"},
                    "group_info": {"group_id": "g1", "group_name": "测试群"},
                },
                "raw_message": [],
            }
        )
    window, hole = history_mod.split_window(msgs, 3)
    check(len(window) == 3, "窗口只留最后 3 条")
    check(len(hole) == 7, "剩下 7 条进洞")
    check(window[0]["message_id"] == "m7", "窗口是**最近**的 3 条")
    check(hole[-1]["message_id"] == "m6", "洞紧接着窗口之前")

    # since_ts 之外的历史在上一轮已经处理过，不该每轮重复报同一段
    # rest 是 m0..m6（base+0..base+360），since_ts = base+240，所以只有 m5/m6 是新的洞
    _, hole2 = history_mod.split_window(msgs, 3, since_ts=1_800_000_000.0 + 4 * 60)
    check(len(hole2) == 2, "上次抽取之前的消息不算本次的洞")
    check(history_mod.split_window(msgs, 0)[0] == [], "max_read=0 时窗口为空")
    check(history_mod.split_window([], 3) == ([], []), "空输入返回两个空列表")

    # ── L1 摘要：只有条数和时间 ──
    summary = history_mod.summarize_hole(hole)
    check(summary is not None, "能从洞里算出摘要")
    check(summary["count"] == 7, "摘要带条数")
    check(summary["start_ts"] < summary["end_ts"], "摘要带时间范围")
    check(
        history_mod.summarize_hole([{"timestamp": "0"}]) is None,
        "拿不到时间戳就不记洞（说不出「昨晚」就别瞎猜）",
    )

    # 渲染出来的文本**绝对不能**包含消息正文。这是整个功能的立身之本：
    # 一旦出现内容，她就从「我不在」变成「我都看过」。
    note = history_mod.render_unread_note(
        [{"start_ts": yesterday, "end_ts": yesterday + 3600.0, "count": 199}],
        now=now,
    )
    check("199" in note, "未读提示带条数")
    check("昨天" in note, "未读提示带时间")
    check("第0条" not in note and "第6条" not in note, "**未读提示不含任何消息内容**")
    check("companion-unread" in note, "带起止标记")
    check("假装" in note, "明确禁止假装看过")
    check(history_mod.render_unread_note([]) == "", "没有洞时返回空串")
    check(
        history_mod.render_unread_note(
            [{"start_ts": yesterday, "end_ts": yesterday + 60, "count": 0}], now=now
        )
        == "",
        "条数为 0 的洞不渲染",
    )
    group_note = history_mod.render_unread_note(
        [{"start_ts": yesterday, "end_ts": yesterday + 3600.0, "count": 55}],
        chat_type="group",
        now=now,
    )
    check("这个群" in group_note, "群聊措辞说的是「这个群」")
    private_note = history_mod.render_unread_note(
        [{"start_ts": yesterday, "end_ts": yesterday + 3600.0, "count": 5}],
        chat_type="private",
        who="阿岚",
        now=now,
    )
    check("阿岚" in private_note, "私聊措辞带对方名字")

    # ── 原文行：入库存的是什么 ──
    rows = history_mod.to_history_rows(hole)
    check(len(rows) == 7, "洞里的 7 条都转成入库行")
    check(all(row["ts"] > 0 for row in rows), "每行都有时间戳")
    check(all(row["text"] for row in rows), "每行都有正文")
    check(rows[0]["ts"] < rows[-1]["ts"], "入库行按时间升序")
    clipped = history_mod.to_history_rows(hole, limit=3)
    check(len(clipped) == 3, "超出上限时裁剪")
    check(clipped[-1]["message_id"] == "m6", "裁掉的是**更早**的那些")

    # ── 线索词 ──
    check(history_mod.looks_like_lookup("昨天那个游戏后来怎么样"), "「昨天」触发翻记录")
    check(history_mod.looks_like_lookup("你上次说的那个"), "「上次」触发翻记录")
    check(history_mod.looks_like_lookup("还记得吗"), "「还记得吗」触发翻记录")
    check(not history_mod.looks_like_lookup("今天天气不错"), "没有指名过去就不翻")
    check(not history_mod.looks_like_lookup(""), "空串不翻")
    check(not history_mod.looks_like_lookup("好"), "太短的不翻")

    # ── L2 检索 ──
    history_rows = [
        {"id": 1, "ts": yesterday, "speaker": "阿岚", "text": "周末要不要一起去看那部新上映的科幻电影"},
        {"id": 2, "ts": yesterday, "speaker": "小满", "text": "好啊好啊我一直想看那个"},
        {"id": 3, "ts": yesterday, "speaker": "阿岚", "text": "今天食堂的菜好难吃"},
    ]
    hits = history_mod.pick_history_hits(
        "我们上次说的那部科幻电影还去吗", history_rows, threshold=0.2
    )
    check(len(hits) >= 1, "能翻到相关的记录")
    check(hits[0]["id"] == 1, "最相关的那条排第一")
    check(
        history_mod.pick_history_hits("完全无关的内容", history_rows, threshold=0.9) == [],
        "门槛过高时翻不到",
    )
    check(history_mod.pick_history_hits("", history_rows) == [], "空消息不检索")

    formatted = history_mod.format_hit(history_rows[0], now=now)
    check("阿岚" in formatted, "翻到的记录带发言人")
    check("科幻电影" in formatted, "翻到的记录带正文")
    check("昨天" in formatted, "翻到的记录带日期")

    # ── L2 / L3 渲染 ──
    found = history_mod.render_lookup_block(hits, now=now)
    check("companion-lookup" in found, "翻到时有标记")
    check("科幻电影" in found, "翻到时给出原文")
    empty = history_mod.render_lookup_block([], now=now)
    check("companion-lookup" in empty, "**翻不到时也要有提示**（L3，否则模型会编）")
    check("没有找到" in empty, "翻不到时明说没找到")
    check(history_mod.render_lookup_block(hits, max_chars=120, now=now) != "", "小预算也能渲染")

    # ── 反向断言：这些数字决定「翻多少」和「存多久」 ──
    check(history_mod.DEFAULT_RECALL_LIMIT <= 5, "一次翻出来不超过 5 条（再多就是通读）")
    check(history_mod.DEFAULT_RECALL_SIMILARITY < 0.40, "翻记录门槛低于记忆的 0.40（消息更长）")
    check(history_mod.MAX_HOLE_ROWS >= 100, "原文至少留得下 100 条")


# ── 主流程 ──

async def main() -> None:
    # 用 mkdtemp 而非 TemporaryDirectory：后者的 __exit__ 早于 finally 执行，
    # Windows 上 SQLite 文件还没关就删目录会 PermissionError。
    tmp = tempfile.mkdtemp(prefix="cgap_")
    plugin_tmp = tempfile.mkdtemp(prefix="cgap_plugin_")
    try:
        test_store(tmp)
        test_compose()
        test_extract()
        await test_extract_async()
        await test_avoidance()
        await test_self_not_person()
        await test_deference()
        await test_plugin(plugin_tmp)
        await test_webui(tmp)
        test_mood(tmp)
        test_config()
        test_person()
        test_log()
        test_absence()
        test_boundary()
        test_presence()
        test_initiative()
        test_backfill()
        test_history()
        await test_energy_gate(tmp)
    finally:
        for store in list(_OPEN_STORES):
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
        _OPEN_STORES.clear()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(plugin_tmp, ignore_errors=True)
    print(f"\n全部通过：{PASSED} 项断言")


if __name__ == "__main__":
    asyncio.run(main())
