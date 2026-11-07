"""我，deepseek —— MaiBot 插件入口。

这个插件只做一件事：把 A_Memorix 结构上不覆盖的五类「情境连续性」线索
（未闭合话题、对方情绪、关系线索、自身连续性、共创线索）在 replyer 阶段
注入给模型，让回复能接得上上下文。

它**不**做这些事（全部交给 A_Memorix，避免重复）：

- 不写聊天摘要、不建人物画像、不做事实抽取
- 不建知识图谱、不做向量检索
- 不主动发消息（唯一的出站是管理命令的回复）

运行时形态：

- ``chat.receive.after_process`` 观察消息 → 内存里记「这个会话有新消息」
- 后台任务按阈值拉历史、调 LLM 抽取、写本地 SQLite
- ``maisaka.replyer.before_request`` 从本地 SQLite 读条目，拼成一小段文本
  追加到 ``extra_prompt``

热路径（replyer hook）里只有一次 SQLite 查询 + 字符串拼接，没有网络、
没有 LLM 调用。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime
from time import perf_counter
from typing import Any

try:  # 正常安装环境
    from maibot_sdk import Command, HookHandler, MaiBotPlugin
except Exception:  # pragma: no cover - 仅用于脱离 Runner 的本地导入
    MaiBotPlugin = object  # type: ignore[assignment,misc]

    def HookHandler(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
        def decorator(func):
            return func

        return decorator

    def Command(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
        def decorator(func):
            return func

        return decorator

try:
    from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

    _HOOK_ENUMS = True
except Exception:  # pragma: no cover
    _HOOK_ENUMS = False

from .cg_absence import absence_level, absence_seconds, describe_gap, render_absence_note
from .cg_avoidance import AvoidanceProbe
from . import cg_log
from .cg_dissent import (
    DISSENT_MAX_CHARS,
    analyse_signals,
    describe_coldness,
    judge_coldness,
)
from .cg_backfill import (
    BackfillResult,
    default_since,
    describe_result,
    fetch_friend_history,
    fetch_group_history,
    merge_messages,
    should_backfill,
)
from .cg_boundary import (
    SILENCE_MODE_SILENT,
    SILENCE_MODE_TACITURN,
    evaluate_silence,
    matched_dislikes,
    record_silence,
    render_boundary_note,
)
from .cg_compose import (
    DEFAULT_RECALL_FOOTER,
    DEFAULT_RECALL_HEADER,
    DEFAULT_RECALL_LEAD_IN,
    compose_block,
    compose_cross_session_block,
    compose_recalled_block,
    deference_allowed,
    merge_extra_prompt,
)
from .cg_config_model import CONFIG_VERSION, CompanionGapConfig
from .cg_energy_gate import EnergyGate
from .cg_history import (
    as_float as history_as_float,
    looks_like_lookup,
    message_stamp,
    pick_history_hits,
    render_lookup_block,
    render_unread_note,
)
from .cg_initiative import (
    Decision,
    REASON_MUTED,
    describe as describe_initiative,
    evaluate as evaluate_initiative,
    filter_tools,
)
from .cg_keepsake import (
    Keepsake,
    describe_keepsake,
    is_full as keepsake_is_full,
    kept as kept_keepsakes,
    render_mention_hint,
    render_overflow_note,
)
from .cg_proactive import (
    KIND_KEEPSAKE,
    KIND_UNFINISHED,
    Plan as SparkPlan,
    Spark,
    decide as decide_spark,
    describe_plan as describe_spark_plan,
    in_quiet_hours,
    pick_spark,
    render_intent,
    spark_from_history,
)
from .cg_mood import MoodEngine, render_block as render_mood_block
from .cg_person import (
    AXIS_KEYS,
    AXIS_LABELS,
    MAX_TAGS,
    TAG_MAX_CHARS,
    affection_from_axes,
    axes_from_affection,
    axes_of,
    clamp_affection,
    clamp_axis,
    describe_person,
    normalize_role,
    normalize_tags,
    person_key,
    render_person_block,
)
from .cg_prefs import (
    SOURCE_PERSONA,
    build_persona_prompt,
    merge_pref,
    parse_persona_response,
    prefs_key,
    render_prefs_block,
)
from .cg_presence import (
    Presence,
    analyse as analyse_presence,
    describe_presence,
    fetch_online_windows,
    render_away_note,
)
from .cg_store import GapStore, recall_score
from .cg_thought import (
    describe_echo,
    pick_echo,
    render_dissent_block,
    render_planner_note,
    to_thoughts,
    ttl_seconds,
)
from .cg_tracker import SessionTracker
from .cg_webui import MemoryAdminMixin

PLUGIN_ID = "github.TommyAAKK.maibot-I-deepseek"
DB_FILENAME = "companion_gap.db"

#: 注入 hook 的超时预算。宿主给这个 hook 的默认预算是 6000ms，
#: 这里收紧到 4000ms：既保留「宁可这次不注入，也不要拖慢回复」的本意，
#: 又给串行链留出余量。
#:
#: 为什么从 2500 提到 4000：本机 8 个插件都挂在 ``maisaka.planner.before_request``
#: 上，宿主是**串行**逐个 await 的，每个插件的超时预算各自独立但总耗时叠加。
#: 2500ms 是最紧的一环，一旦那台机器慢一点就会连续超时——连续 3 次之后宿主
#: 熔断 60~300 秒（``circuit_breaker._FAILURE_THRESHOLD = 3``），熔断期间整个
#: 插件在这个 hook 上静默跳过，表现和「闸门把她永久静音了」一模一样。
INJECT_TIMEOUT_MS = 4000

#: 单次闸门判定的耗时告警线。超过就记一条 warning —— 别等熔断了才发现慢。
_GATE_SLOW_MS = 800.0

#: 「这一轮她不打算顺着」这个标记的有效期（秒）。
#:
#: planner 和 replyer 是两次独立的 hook 调用，中间隔着一次模型生成，插件
#: 没有别的办法把结论传下去。窗口给 3 分钟：一轮 planner→replyer 正常几秒到
#: 几十秒，给太短会漏，给太长会把「这一轮」的话带到下一轮去。
_DISSENT_WINDOW_SECONDS = 180.0

#: 宿主 planner 快照里「系统消息」这个 item 的类型名。
#:
#: **不是 ``"system"``。** 宿主 ``serialize_context_item_snapshot()`` 用
#: ``item.__class__.__name__`` 当 ``item_type``，反序列化时按类名精确匹配
#: ``"SystemMessageItem"``，认不出就抛
#: ``不支持的 Context Item 快照类型: system``，然后整个 items 被丢弃、
#: 只留一行 warning。也就是说写 ``"system"`` 的那一版**从来没注入进去过**。
#: 历史上我们一直写 ``"system"``，直到翻宿主日志才看见
#: ``Hook maisaka.planner.before_request 返回的 items 无效，已忽略``。
_SYSTEM_ITEM_TYPES = ("SystemMessageItem", "system")

#: 观察 hook 的超时预算。mark_dirty 只改内存，1000ms 足够。
CAPTURE_TIMEOUT_MS = 1000

#: 在线时段缓存的存活时间。宿主 ``online_time`` 的粒度本来就是 5 分钟，
#: 每次注入都重新查一遍（30 天约 8600 行）纯属浪费。
_ONLINE_CACHE_SECONDS = 600.0

#: 「主动」循环的判定间隔（秒）。
#:
#: 比在线时段勤，因为它要抓的是「她闲下来了」这个时刻：90 分钟的空闲门槛，
#: 每 5 分钟看一次最多晚 5 分钟发现，够用；每 10 分钟会让这个判断偏粗。
_SPARK_TICK_SECONDS = 300.0

_CAPTURE_KW: dict[str, Any] = {
    "name": "capture_message",
    "timeout_ms": CAPTURE_TIMEOUT_MS,
    "description": "记录会话有新消息，供后台抽取调度使用",
}
_INJECT_KW: dict[str, Any] = {
    "name": "inject_context",
    "timeout_ms": INJECT_TIMEOUT_MS,
    "description": "把情境连续性线索追加到本轮 replyer 的 extra_prompt",
}
#: 观察 Bot 自己的回复。用 BLOCKING 而不是 OBSERVE：OBSERVE 模式下宿主会
#: 直接丢掉我们返回的 ``modified_kwargs``（``hook_dispatcher.py:695-696``），
#: 而「这一句没冷下来，重说一遍」只能靠回传 ``retry`` 实现。
#:
#: 为什么必须亲自动手：人设里写着「喜欢用颜文字」「语气自然轻快」，
#: 还有 ``reply_style`` 那一整段，模型经常一句就把话说软了。**光靠提示词
#: 约束不够**——实测这一层没有重试纠偏机制，一次生成成什么样就是什么样。
_REPLY_KW: dict[str, Any] = {
    "name": "observe_reply",
    "timeout_ms": CAPTURE_TIMEOUT_MS,
    "description": "记录 Bot 刚发出的回复，用于更新自身情绪；必要时要求重生成",
}
#: planner 侧的状态注入。宿主这个 hook 默认预算也是 6000ms；它只读内存与
#: 本地 SQLite，没有任何网络调用，所以和 inject 用同一个收紧值。
_PLANNER_KW: dict[str, Any] = {
    "name": "inject_planner_state",
    "timeout_ms": INJECT_TIMEOUT_MS,
    "description": "在 planner 决定要不要回之前，把此刻的心情与关系状态交给它",
}
if _HOOK_ENUMS:
    _CAPTURE_KW.update(
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    _INJECT_KW.update(
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    _REPLY_KW.update(
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    # planner 那条用 NORMAL 而不是 LATE：身份类插件（Alcpare）也在这条
    # 链上，大家往 items 里追加的是独立段落，先后顺序不影响语义。
    _PLANNER_KW.update(
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )

#: 命令正则。命名捕获组会原样进 ``matched_groups``。
COMMAND_PATTERN = r"(?i)^\s*[/!！]?mcompgap(?:\s+(?P<action>[a-z_]+))?(?:\s+(?P<arg>.*?))?\s*$"

_HELP_TEXT = (
    "我，deepseek 管理命令：\n"
    "/mcompgap status —— 看当前会话的记忆统计\n"
    "/mcompgap list [类别] —— 列出当前会话的记忆条目\n"
    "/mcompgap extract —— 立即对当前会话跑一次抽取\n"
    "/mcompgap clear —— 清空当前会话的记忆\n"
    "/mcompgap global —— 看全局统计与避让状态\n"
    "/mcompgap person —— 列出记住的人\n"
    "/mcompgap person set <user_id> <owner|friend|member|stranger> —— 手动设定关系（自动判断不再改它）\n"
    "/mcompgap person affection <user_id> <数值> —— 手动设定好感度（-100 到 100，六维会跟着重算）\n"
    "/mcompgap person axes <user_id> <维度> <0-1> —— 直接设定某一维"
    "（trust 信任 / closeness 亲近 / respect 尊重 / tension 张力 / "
    "familiarity 熟悉 / perceived_warmth 他待我）\n"
    "/mcompgap person tag <user_id> <标签> —— 加一条关系化标签（「XX的XX」那种）\n"
    "/mcompgap person untag <user_id> <标签> —— 删掉某一条标签\n"
    "/mcompgap person untagall <user_id> —— 清空这个人的标签\n"
    "/mcompgap person follow <user_id> —— 让六维重新跟着聊天变化"
    "（取消手调锁定）\n"
    "/mcompgap person del <user_id> —— 忘掉这个人\n"
    "/mcompgap help —— 显示本帮助"
)

_BASE_CONFIG_GETTER = getattr(getattr(MaiBotPlugin, "config", None), "fget", None)


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    """把配置页里的对象列表转成纯 dict 列表。

    ``people.roles`` / ``people.intimacy_levels`` 在配置模型里是
    ``list[PluginConfigBase]``（为了让它渲染成可增删的表格），
    ``cg_person`` 里的函数只认 Mapping，所以这里统一摊平。
    """

    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append(dict(item))
            continue
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            try:
                dumped = dump()
            except Exception:  # noqa: BLE001 - 配置形状异常时跳过这一行
                continue
            if isinstance(dumped, dict):
                out.append(dict(dumped))
    return out


class CompanionGapPlugin(MemoryAdminMixin, MaiBotPlugin):
    """只补 A_Memorix 缺口的轻量情境记忆插件。

    ``MemoryAdminMixin`` 提供 WebUI 管理页面的 ``@API`` 方法（见 cg_webui.py）；
    它没有 ``__init__``，所以 MRO 放在前面不会影响宿主的构造流程。
    """

    config_model = CompanionGapConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        try:
            super().__init__(*args, **kwargs)
        except Exception:  # noqa: BLE001 - 本地脱离 Runner 构造时基类会拒绝
            pass
        self._config_fallback = CompanionGapConfig()
        self._store: GapStore | None = None
        self._tracker: SessionTracker | None = None
        self._probe: AvoidanceProbe | None = None
        self._mood: MoodEngine | None = None
        self._gate: EnergyGate | None = None
        # 在场判据的在线时段缓存：合并后的区间列表，以及下次该重新读的时间。
        # 由后台任务 _online_loop 维护，注入路径只读不写（注入只有 2500ms 预算）。
        self._online_windows: list[Any] = []
        self._online_windows_at: float = 0.0
        self._online_stop = asyncio.Event()
        self._online_task: asyncio.Task[Any] | None = None
        # 自主发言闸门的进程级静音窗截止时刻（epoch 秒）。
        #
        # focus 模式下判了「这轮不说」，模型可以改用 switch_chat 切到别的会话，
        # 而切过去会开**新的一轮**、闸门在干净上下文里重新判——于是「不在这说」
        # 变成了「去别处说」。窗口是进程级的，不按 session 记：换到哪个会话都算。
        self._mute_until: float = 0.0
        # 闸门连续超时的次数。宿主熔断器的阈值是 3 次，我们没有能力直接读熔断
        # 状态（不在那 75 项能力清单里），所以自己数——数到这里就等于「下一枪
        # 打完这个 hook 会被静默跳过」。
        self._gate_slow_streak: int = 0
        # 「这一轮她不打算顺着」的会话标记：session_id → 失效时刻。
        #
        # planner 和 replyer 是**两次独立的 hook 调用**，中间隔着一次模型生成，
        # 插件没有办法把状态顺着调用栈传下去。所以判在 planner、用在 replyer，
        # 只能靠这个短期标记。窗口给 3 分钟：一轮 planner→replyer 正常几秒到
        # 几十秒，给太短会漏、给太长会把「这一轮」的话带到下一轮去。
        self._dissent_until: dict[str, float] = {}
        # 上一次真的不顺着是什么时候（进程级）。配合 thought.frequency_hours 限速。
        self._dissent_last_ts: float = 0.0
        # 当天已经用掉几次。跨重启会归零——这是可接受的：重启本来就是新的一天
        # 这个量级的误差，不值得为它加一张表。
        self._dissent_day: str = ""
        self._dissent_day_count: int = 0
        # 这一轮是否已经进入「不顺着」的流程，以及本轮已经重生成过几次。
        #
        # **按会话记，不是全局一个布尔。** 宿主可以同时跑多个会话（群聊和私聊
        # 各一轮），全局布尔会让 A 组打开的轮次把 B 组的正常回复也拉回去重
        # 生成——而且 B 组一旦重生成，又会把 A 组的计数搅乱。
        #
        # 用字典而不是从 ``_dissent_until`` 反推：那个标记是「给 replyer 的
        # 时限」，而这里要记的是「这一轮重说了几次」，两者寿命不一样。
        # 键存在 = 这一轮在流程里；值 = 已用掉的重生成次数。
        self._dissent_rounds: dict[str, int] = {}
        # 「主动」后台循环的开关与任务句柄。
        #
        # 和 ``_online_loop`` 分开是因为两者节奏完全不同：在线时段 10 分钟刷一次
        # 就够了，而「她现在闲不闲、要不要开口」得勤一点看。合成一个循环会让
        # 一次慢查询把另一边也拖住。
        self._spark_stop = asyncio.Event()
        self._spark_task: asyncio.Task[Any] | None = None
        # 每个会话上次「清静下来」的时刻。用来算空闲多久，只放内存——
        # 重启后从最后一条消息重新算，误差可接受。
        self._spark_idle_since: dict[str, float] = {}

    # ── 配置 ──

    @property
    def config(self) -> CompanionGapConfig:  # type: ignore[override]
        """配置读取；Runner 尚未注入配置时回退到默认值。"""

        if _BASE_CONFIG_GETTER is not None:
            try:
                value = _BASE_CONFIG_GETTER(self)
                if value is not None:
                    return value
            except Exception:  # noqa: BLE001
                pass
        return self._config_fallback

    # ── 生命周期 ──

    async def on_load(self) -> None:
        cfg = self.config
        _set_log_level(cfg.debug.log_level)
        data_dir = self._data_dir()
        db_path = os.path.join(data_dir, DB_FILENAME)

        store = GapStore(db_path)
        store.open()
        self._store = store

        self._probe = AvoidanceProbe(self.ctx)
        profile = await self._probe.probe()
        self._info(f"启动：数据目录={data_dir}；宿主避让状态={profile.describe()}")

        mood = MoodEngine(lambda: self.config)
        mood.load(store)
        self._mood = mood
        if mood.enabled:
            self._info(f"情绪引擎已加载：{mood.describe()}")

        self._gate = EnergyGate(self.ctx, logger=self.ctx.logger)

        self._tracker = SessionTracker(
            ctx=self.ctx,
            store=store,
            config_getter=lambda: self.config,
            probe=self._probe,
            logger=self.ctx.logger,
            log_extract_raw=cfg.debug.log_extract_raw,
            mood=mood,
            gate=self._gate,
        )
        await self._tracker.start()

        # 喜好从人设起步：只在库里一条口味都没有、而且从没尝试过的时候跑一次。
        # 放在后台等它自己跑完不行——on_load 是 async 的，直接 await 更简单，
        # 而且这里只调一次 LLM（几百毫秒），不值得为它引入一个任务。
        try:
            await self._seed_prefs_from_persona(store, cfg)
        except Exception as exc:  # noqa: BLE001 - 起步失败不影响主流程
            self._warn(f"从人设提炼喜好失败（已跳过）: {exc!r}")

        if not cfg.plugin.enabled:
            self._info("配置里 plugin.enabled=false，采集与注入都不会生效")

        # 在线时段缓存：注入路径绝对不能查库（只有 2500ms 预算），所以由
        # 后台任务每 10 分钟刷一次，注入时只读内存。启动时先同步跑一遍，
        # 否则第一次注入时缓存是空的，缺席判据会悄悄退回旧算法。
        try:
            await self._refresh_online_windows()
        except Exception as exc:  # noqa: BLE001 - 拿不到就退回旧算法
            self._warn(f"启动时读取在线时段失败（缺席判据退回旧算法）: {exc!r}")
        self._online_stop.clear()
        self._online_task = asyncio.create_task(self._online_loop(), name="companion_gap_online")

        # 主动（好奇心）后台循环。默认关，但任务照建——开关在循环里判，
        # 这样在 WebUI 里打开开关不需要重启插件。
        self._spark_stop.clear()
        self._spark_task = asyncio.create_task(self._spark_loop(), name="companion_gap_spark")

    async def on_unload(self) -> None:
        spark_task = self._spark_task
        self._spark_task = None
        self._spark_stop.set()
        if spark_task is not None and not spark_task.done():
            spark_task.cancel()
            try:
                await spark_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭阶段不抛
                pass
        online_task = self._online_task
        self._online_task = None
        self._online_stop.set()
        if online_task is not None and not online_task.done():
            online_task.cancel()
            try:
                await online_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭阶段不抛
                pass
        tracker = self._tracker
        self._tracker = None
        if tracker is not None:
            await tracker.stop()
        # 卸载前必须把发言频率还原：那是宿主会话级状态，留着会把别的插件
        # 和用户自己的设置一起锁死在低频率上。
        gate = self._gate
        self._gate = None
        if gate is not None:
            try:
                await gate.restore_all(self._store)
            except Exception as exc:  # noqa: BLE001 - 卸载阶段不抛
                self._warn(f"卸载时恢复发言频率失败: {exc!r}")
        # 卸载前把情绪落盘，否则重启后这段时间的漂移会丢。
        mood = self._mood
        store = self._store
        if mood is not None and store is not None:
            try:
                mood.persist(store)
            except Exception as exc:  # noqa: BLE001 - 卸载阶段不抛
                self._warn(f"卸载时保存情绪失败: {exc!r}")
        self._mood = None
        self._store = None
        if store is not None:
            store.close()
        self._info("已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新：让避让探测与后台任务立刻看到新值。"""

        _ = (config_data, version)
        self._info(f"配置已更新 (scope={scope}, version={CONFIG_VERSION})")
        cfg = self.config
        _set_log_level(cfg.debug.log_level)
        tracker = self._tracker
        if tracker is not None:
            tracker._log_extract_raw = bool(cfg.debug.log_extract_raw)
        if self._probe is not None:
            self._probe.invalidate()

    async def _seed_prefs_from_persona(self, store: GapStore, cfg: Any) -> None:
        """首次启动时从宿主人设提炼初始口味。

        为什么只跑一次：人设不会天天变，反复提炼既浪费配额，又会让口味
        飘来飘去（今天觉得她喜欢猫、明天觉得她喜欢狗，人设明明没改）。
        所以用 ``gap_meta`` 里的一个标记位记录「已经起步过了」——哪怕
        提炼失败也不重试，免得每次重载插件都白花一次调用；用户想重来
        可以用 ``/mcompgap prefs reset`` 清掉标记。

        为什么不用后台任务：这里只调一次 LLM，几百毫秒，而且 on_load 本身
        就是 async 的。为它引入一个任务反而要多管一套生命周期。
        """

        prefs_cfg = getattr(cfg, "prefs", None)
        if prefs_cfg is None or not bool(getattr(prefs_cfg, "enabled", False)):
            return
        if not bool(getattr(prefs_cfg, "persona_seed", False)):
            return

        seed_key = "prefs_persona_seeded"
        if store.meta_prefix(seed_key).get(seed_key):
            self._debug("人设提炼已经跑过了，跳过")
            return
        # 已经有口味（比如用户手动加过）就不覆盖，直接标记为已起步。
        try:
            existing = store.list_prefs(limit=1)
        except Exception:  # noqa: BLE001
            existing = []
        if existing:
            store.set_meta(seed_key, "skipped:already-has-prefs")
            return

        persona_path = str(getattr(prefs_cfg, "persona_key", "") or "").strip()
        if not persona_path:
            store.set_meta(seed_key, "skipped:no-key")
            return
        persona = ""
        try:
            value = await self.ctx.config.get(persona_path, None)
            if isinstance(value, str):
                persona = value.strip()
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取宿主 {persona_path} 失败: {exc!r}")
        if not persona:
            # 读不到就标记，免得每次重载都重试一次读配置。
            store.set_meta(seed_key, "skipped:empty-persona")
            self._info(f"宿主 {persona_path} 是空的，跳过从人设提炼喜好")
            return

        prompt = build_persona_prompt(persona, limit=6)
        payload = await self.ctx.llm.generate(
            prompt=prompt,
            task_name=str(getattr(prefs_cfg, "persona_task_name", "utils") or "utils"),
            temperature=0.4,
            max_tokens=600,
            _timeout_ms=int(getattr(prefs_cfg, "persona_timeout_ms", 60000) or 60000),
        )
        if not isinstance(payload, dict) or not payload.get("success", False):
            error = payload.get("error") if isinstance(payload, dict) else "返回非 dict"
            store.set_meta(seed_key, f"failed:{error}"[:200])
            self._warn(f"人设提炼失败: {error}")
            return

        parsed = parse_persona_response(str(payload.get("response") or ""), limit=6)
        now = time.time()
        written = 0
        for kind, rows in (("like", parsed.get("likes") or []), ("dislike", parsed.get("dislikes") or [])):
            for row in rows:
                topic = str(row.get("topic") or "").strip()
                if not topic:
                    continue
                merged = merge_pref(
                    None,
                    {
                        "kind": kind,
                        "topic": topic,
                        "strength": row.get("strength", 0.6),
                        "source": SOURCE_PERSONA,
                    },
                    now=now,
                )
                if not merged:
                    continue
                merged["pref_key"] = prefs_key(kind, topic)
                store.upsert_pref(merged, now=now)
                written += 1
        store.set_meta(seed_key, f"ok:{written}")
        self._info(f"从人设提炼出 {written} 条口味（喜欢 {len(parsed.get('likes') or [])} / 讨厌 {len(parsed.get('dislikes') or [])}）")

    # ── 组件：观察消息 ──

    @HookHandler("chat.receive.after_process", **_CAPTURE_KW)
    async def capture_message(self, **kwargs: Any) -> dict[str, Any]:
        """把新消息登记为「该会话有增量」，顺手微调情绪。

        这里只做内存计算，不碰磁盘、不发 RPC。``processed_plain_text`` 由宿主
        在预处理阶段填好，直接拿来判断语气即可。
        """

        try:
            tracker = self._tracker
            message = kwargs.get("message")
            if tracker is not None and isinstance(message, dict):
                tracker.mark_dirty(message)
                mood = self._mood
                if mood is not None:
                    text = str(message.get("processed_plain_text") or "")
                    if text:
                        mood.on_message(text)
        except Exception as exc:  # noqa: BLE001 - 观察型 hook 绝不能影响消息处理
            self._warn(f"capture_message 失败: {exc!r}")
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.after_response", **_REPLY_KW)
    async def observe_reply(self, **kwargs: Any) -> dict[str, Any]:
        """记录 Bot 自己刚说的话；如果这一轮该冷下来却没冷，要求重说。

        两件事共用这条 hook：

        - **情绪**（一直都有）：只影响下一轮，不改写已经发出去的内容。
        - **冷场兜底**（新增）：planner 那一轮判了「不顺着」，但生成出来的
          这句话被 ``cg_dissent.judge_coldness`` 判成「还是太软」，就带上
          ``retry_reason`` 让宿主重生成一次。

        为什么判据是规则而不是再调一次 LLM：这条 hook 的预算是 6000ms，
        判完还要再生成一次，再塞一次模型调用会直接把预算吃穿。而且规则
        可解释——日志里能写出「她这句带了缓和词（呀）」这种话，模型判不出。

        **返回必须裹在 ``modified_kwargs`` 里。** 宿主拿到 hook 结果后只读
        ``modified_kwargs`` 再整体覆盖 kwargs（``hook_dispatcher.py:576-578``），
        把 ``retry`` 放在顶层会被直接丢掉——写了等于没写。
        """

        result: dict[str, Any] = {"action": "continue"}
        try:
            mood = self._mood
            if mood is not None:
                text = str(kwargs.get("response") or "")
                if text:
                    mood.on_reply(text)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"observe_reply 更新情绪失败: {exc!r}")

        try:
            request = self._dissent_retry_request(kwargs)
        except Exception as exc:  # noqa: BLE001 - 兜底失败就放行，不能拖累发送
            self._warn(f"冷场兜底判定失败（按已冷处理）: {exc!r}")
            request = None
        if request is not None:
            # modified_kwargs 是整体替换语义，必须回传完整副本。
            out_kwargs = dict(kwargs)
            out_kwargs.update(request)
            result["modified_kwargs"] = out_kwargs
        return result

    def _dissent_retry_request(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """「该冷却没冷」时返回要给宿主的 retry 参数，否则 ``None``。

        只在 planner 那一轮真的判了「不顺着」时才生效——平时回复里多一个
        「哈哈」是再正常不过的事，绝不能因为全局检查把她所有回复都拉回去
        重说一遍。
        """

        cfg = self.config
        thought_cfg = getattr(cfg, "thought", None)
        if thought_cfg is None or not bool(getattr(thought_cfg, "enabled", True)):
            return None
        if not bool(getattr(thought_cfg, "retry_enabled", True)):
            return None

        session_id = str(kwargs.get("session_id") or "").strip()
        if not self._dissent_consumed_gate(session_id):
            return None

        text = str(kwargs.get("response") or "")
        if not text:
            self._dissent_end_round(session_id)
            return None

        max_chars = int(getattr(thought_cfg, "dissent_max_chars", DISSENT_MAX_CHARS) or DISSENT_MAX_CHARS)
        verdict = judge_coldness(text, max_chars=max_chars)
        if verdict.cold:
            self._debug(f"冷场兜底：这一句够冷了（{describe_coldness(verdict)}）")
            self._dissent_end_round(session_id)
            return None

        used = self._dissent_rounds.get(session_id, 0)
        limit = int(getattr(thought_cfg, "retry_max", 1) or 0)
        if used >= limit:
            self._info(
                f"冷场兜底：已经重生成 {used} 次仍然偏软，就用这一句"
                f"（{verdict.reason}）：{text[:60]}"
            )
            self._dissent_end_round(session_id)
            return None
        self._dissent_rounds[session_id] = used + 1
        self._info(f"冷场兜底：这一句太软（{verdict.reason}），要求重生成：{text[:60]}")
        return {
            "retry": True,
            "retry_reason": (
                "刚才那句话太软了。不要附和、不要补缓和的话、不要解释，"
                "也不要加语气词和表情——只说你自己真正想说的那一句，说完就完。"
            ),
        }

    def _dissent_consumed_gate(self, session_id: str) -> bool:
        """这个会话的这一轮是否被判过「不顺着」。

        **按会话查，不能用一个全局布尔。** 宿主能同时跑多个会话，全局布尔会
        让 A 组打开的轮次把 B 组正常的回复也拉回去重生成。

        轮次边界就是「这一轮的重生成全部结束」：够冷 / 到了重试上限时由
        ``_dissent_retry_request`` 把它清掉，所以下一轮不会继承。
        """

        return str(session_id or "").strip() in self._dissent_rounds

    # ── 组件：注入情境 ──

    @HookHandler("maisaka.planner.before_request", **_PLANNER_KW)
    async def inject_planner_state(self, **kwargs: Any) -> dict[str, Any]:
        """在 planner 决定「要不要回」之前，把她的状态告诉它。

        这是用户第 12 轮要的那条路：**不注入命令，只注入状态**。

        为什么不写「如果被冒犯就拒绝」而写「你现在有点烦、你讨厌被使唤」：
        判断「这句话是不是在冒犯她」需要完整对话语境和人物关系，那是 planner
        的活。插件按关键词判「你怎么这么笨」和「你怎么这么笨啊哈哈」分不清，
        误判一次就很难看。所以插件只提供它独有的那些信息（情绪基元、六维
        张力、踩中的厌恶口味），把判断权交出去。

        默认**关**（``boundary.note_to_planner = false``）：这条路径会改变
        「要不要回」的决策，影响面比 replyer 那一层大得多，而且每轮多几十个
        token。想开就自己去配置页打开。

        写入方式是往 ``items`` 里追加一条 SystemMessageItem ——和
        ``Alcpare_identity`` 用的是同一套写法（宿主允许改 items 的前提是
        ``item_schema_version`` 原样回传、item_id 唯一）。

        **三件事共用这一个 hook，但互不依赖**：边界感那段文字（默认可关）、
        自主发言闸门（默认关，收紧 ``tool_definitions``）、念头（默认开，
        决定这一轮要不要让她不顺着）。所以下面拆成三个 try 块——任何一边
        失败或提前返回，都不能连带把另外两边也跳过。
        """

        out = dict(kwargs)
        try:
            out = self._inject_planner_boundary(out, **kwargs)
        except Exception as exc:  # noqa: BLE001 - planner 注入失败绝不能拖累决策
            self._warn(f"planner 状态注入失败: {exc!r}")

        # ── 念头 ──────────────────────────────────────────────────────
        # 先判念头再判发言闸门：闸门决定「这一轮说不说」，念头决定「如果要说，
        # 是怎么说」。两者语义正交，顺序不影响结果，但日志读起来更顺。
        try:
            out = self._apply_thought_note(out, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 念头失败退回「正常聊」
            self._warn(f"念头注入失败（退回正常回复）: {exc!r}")

        # ── 自主发言闸门 ──────────────────────────────────────────────
        # 必须**无条件**跑：它改的是工具清单，和上面有没有注入哪段文字无关。
        try:
            out = self._apply_initiative_gate(out, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 闸门失败退回「放行」
            self._warn(f"自主发言闸门失败（退回放行）: {exc!r}")
        return {"action": "continue", "modified_kwargs": out}

    def _inject_planner_boundary(self, out: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """把边界感的状态提示追加进 planner 的 items（可关）。"""

        cfg = self.config
        store = self._store
        boundary_cfg = getattr(cfg, "boundary", None)
        if store is None or boundary_cfg is None:
            return out
        if not bool(getattr(boundary_cfg, "enabled", False)):
            return out
        if not bool(getattr(boundary_cfg, "note_enabled", False)):
            return out
        if not bool(getattr(boundary_cfg, "note_to_planner", False)):
            return out

        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return out

        # 复用 replyer 那条路的渲染逻辑：同一份状态，两处共用，
        # 免得 planner 和 replyer 对「她现在什么心情」给出两种说法。
        note, _mode = self._render_boundary(session_id, cfg, kwargs)
        if not note:
            return out

        self._append_system_note(out, note, tag="boundary")
        self._debug(f"planner 状态已注入（{len(note)} 字）")
        return out

    def _apply_thought_note(self, out: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """决定这一轮有没有一个「心里的念头」，并把它的存在交给模型。

        三个入口，按可信度从高到低：

        1. **当场信号**（``cg_dissent.analyse_signals``）：同一件事问到第三遍、
           对方连着敷衍、近几轮全是问句。这些是刚发生的、能从对话里指出来
           的事实，最硬。
        2. **被勾起的念头**（``cg_thought.pick_echo``）：当前话题撞上了她心里
           存着的那条念头的来源。这是「自然回响」，Kimi 说的主要去处。
        3. **什么都发生**：不注入、不记账、不惩罚。

        判完之后把「这一轮不顺着」这个结论写进 ``self._dissent_until``，
        由 ``inject_context``（replyer 侧）取用。**planner 和 replyer 是两次
        独立的 hook 调用**，中间隔着一次模型生成，没有别的办法把状态传过去。

        频率限制放在这里而不是 replyer 侧：这是唯一一次「还能选择不说」的
        机会，到 replyer 那步已经晚了。
        """

        out_kwargs = dict(out)
        cfg = self.config
        thought_cfg = getattr(cfg, "thought", None)
        store = self._store
        tracker = self._tracker
        if thought_cfg is None or store is None or tracker is None:
            return out_kwargs
        if not bool(getattr(thought_cfg, "enabled", True)):
            return out_kwargs

        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return out_kwargs

        now = time.time()
        cfg_dissent = bool(getattr(thought_cfg, "dissent_enabled", True))
        cfg_signals = bool(getattr(thought_cfg, "instant_signals", True))

        # ── 当场信号 ──────────────────────────────────────────────
        signal = None
        if cfg_signals:
            try:
                incoming = tracker.recent_incoming(session_id, limit=12)
                text = self._planner_user_text(kwargs)
                signal = analyse_signals(incoming, current=text)
            except Exception as exc:  # noqa: BLE001 - 算不出来就当没信号
                self._debug(f"当场信号计算失败（按无信号处理）: {exc!r}")
        reason = signal.reason if signal is not None and signal.fired else ""

        # ── 被勾起的念头 ─────────────────────────────────────────
        echo = None
        if bool(getattr(thought_cfg, "echo_enabled", True)):
            try:
                rows = store.list_thoughts(session_id, limit=6)
                current = self._current_message(kwargs)
                echo = pick_echo(to_thoughts(rows), current)
            except Exception as exc:  # noqa: BLE001
                self._debug(f"念头唤起失败（按没想起处理）: {exc!r}")
            if echo is not None:
                try:
                    store.touch_thought(echo.thought.id, now=now)
                except Exception as exc:  # noqa: BLE001
                    self._debug(f"touch_thought 失败: {exc!r}")
                if not reason:
                    reason = f"想起了「{echo.thought.content[:20]}」"

        # ── 这一轮要不要真的不顺着 ────────────────────────────────
        # 硬条件：开关 + 有理由 + 没被限速。三条都过了才写标记。
        allow_dissent = False
        block_reason = ""
        if not cfg_dissent:
            block_reason = "dissent_enabled 关着"
        elif not reason:
            block_reason = "没有触发理由"
        else:
            blocked, why = self._dissent_rate_limited(thought_cfg, now)
            if blocked:
                block_reason = why
            else:
                allow_dissent = True

        # 无论放不放行，先把念头本身告诉 planner ——「你心里有件事没放下」
        # 和「这一轮要不要冷下来」是两个决定。念头被记起来了却决定正常聊，
        # 是完全正常的一天。
        note = ""
        if echo is not None and bool(getattr(thought_cfg, "echo_enabled", True)):
            try:
                note = render_planner_note(echo.thought)
            except Exception as exc:  # noqa: BLE001
                self._debug(f"渲染念头失败: {exc!r}")

        if signal is not None and signal.fired:
            extra = f"（{signal.reason}）"
            note = (note + "\n" if note else "") + extra

        if note:
            self._append_system_note(out_kwargs, note)
            self._debug(f"planner 念头已注入：{note[:80]}")

        if allow_dissent:
            self._dissent_record(thought_cfg, session_id, now)
            if echo is not None:
                try:
                    store.voice_thought(echo.thought.id, now=now)
                except Exception as exc:  # noqa: BLE001
                    self._debug(f"voice_thought 失败: {exc!r}")
            self._info(
                f"念头：这一轮她不想顺着（{reason}）"
                f"{'；' + describe_echo(echo) if echo is not None else ''}"
            )
        elif reason:
            self._debug(f"念头：有理由但不触发（{reason}；{block_reason}）")
        return out_kwargs

    def _dissent_active(self, session_id: str, *, now: float | None = None) -> bool:
        """这个会话还有没有「这一轮不顺着」的标记。

        **不删标记。** 因为被要求重生成时 replyer 会被再调一次
        （``replyer.before_request`` → 生成 → ``after_response`` 要求重试 →
        再走一遍），那一次正是最需要这段指令的时候。标记的清除交给
        ``_dissent_end_round``：一轮结束（够冷 / 到了重试上限）或 3 分钟到期。
        """

        current = time.time() if now is None else float(now)
        key = str(session_id or "").strip()
        until = self._dissent_until.get(key, 0.0)
        if until <= 0.0:
            return False
        if until < current:
            self._debug("念头标记已过期（这一轮没走到 replyer）")
            self._dissent_until.pop(key, None)
            self._dissent_rounds.pop(key, None)
            return False
        # 进入「这一轮不顺着」的流程。次数只在**本轮第一次**进来时建立：
        # 重生成会把 replyer 再调一次，那一次不能覆盖已有计数，否则
        # ``retry_max`` 就永远撞不到上限。
        self._dissent_rounds.setdefault(key, 0)
        return True

    def _dissent_end_round(self, session_id: str) -> None:
        """这一轮不顺着结束（说出来了，或者到了重试上限就用这句）。"""

        key = str(session_id or "").strip()
        self._dissent_rounds.pop(key, None)
        self._dissent_until.pop(key, None)

    def _dissent_rate_limited(self, thought_cfg: Any, now: float) -> tuple[bool, str]:
        """限速判定。返回 ``(是否被拦, 原因)``。

        两道：最短间隔 + 每日上限。**两道都要**——只有间隔的话，一天里
        「每隔 6 小时冷一次」正好撞上用户的作息；只有上限的话，四次都挤在
        同一小时里，那读起来不像她有自己的节奏，像开关。
        """

        hours = float(getattr(thought_cfg, "frequency_hours", 6.0) or 0.0)
        if hours > 0.0 and self._dissent_last_ts > 0.0:
            gap = now - self._dissent_last_ts
            if gap < hours * 3600.0:
                left = hours * 3600.0 - gap
                return True, f"距上次不顺着才 {gap / 60.0:.0f} 分钟（还差 {left / 60.0:.0f} 分钟）"

        limit = int(getattr(thought_cfg, "daily_limit", 2) or 0)
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if today != self._dissent_day:
            self._dissent_day = today
            self._dissent_day_count = 0
        if limit > 0 and self._dissent_day_count >= limit:
            return True, f"今天已经不顺着 {self._dissent_day_count} 次（上限 {limit}）"
        return False, ""

    def _dissent_record(self, thought_cfg: Any, session_id: str, now: float) -> None:
        """记下「这一轮真的不顺着」，并给 replyer 侧留标记。"""

        self._dissent_last_ts = now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if today != self._dissent_day:
            self._dissent_day = today
            self._dissent_day_count = 0
        self._dissent_day_count += 1
        # 3 分钟：一轮 planner→replyer 正常几秒到几十秒。给太短会漏，
        # 给太长会把「这一轮」的话带到下一轮去。
        self._dissent_until[session_id] = now + _DISSENT_WINDOW_SECONDS
        # 顺手清掉过期的，别让这个字典无限长（群多了会积）。
        stale = [key for key, until in self._dissent_until.items() if until < now]
        for key in stale:
            self._dissent_until.pop(key, None)

    def _append_system_note(self, out: dict[str, Any], note: str, *, tag: str = "thought") -> None:
        """往 planner 的 items 里追加一段系统消息。

        抽出来是因为现在有两处要往里加文字（边界感、念头），而 item 的构造
        细节很容易改一处忘一处：``item_type`` 必须是宿主认识的类名、
        ``item_id`` 必须唯一、``meta.timestamp`` 必须是能 ``fromisoformat``
        的串。**不复制粘贴第二份。**

        Args:
            out: planner 的 kwargs 副本，原地修改。
            note: 要追加的文字。
            tag: 新建 item 时 item_id 里的前缀，方便在日志里认出来源。
        """

        items = out.get("items")
        if not isinstance(items, list):
            return
        for item in reversed(items):
            if isinstance(item, dict) and item.get("item_type") in _SYSTEM_ITEM_TYPES:
                parts = item.get("parts")
                if not isinstance(parts, list):
                    parts = []
                    item["parts"] = parts
                parts.append({"type": "text", "text": note})
                return
        items.insert(
            0,
            {
                # 必须是宿主认识的类名。写 "system" 会被
                # request_snapshot.deserialize 拒掉，然后**整批 items 被丢弃**。
                "item_type": "SystemMessageItem",
                "meta": {
                    "item_id": f"companion-gap-{tag}-{uuid.uuid4().hex}",
                    "logical_turn_id": None,
                    "timestamp": datetime.now().astimezone().isoformat(),
                },
                "parts": [{"type": "text", "text": note}],
            },
        )

    def _apply_initiative_gate(self, out: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """按「兴趣 / 好感度 / 心情」决定这一轮要不要开口。

        **唯一有效的硬手段是把 ``reply`` 从工具清单里删掉。** 宿主的发言频率
        只管「什么时候触发 Planner」，管不了「Planner 跑起来之后要不要说」；
        而本会话开着 focus 模式时频率被硬编码成 1.0、空闲退避也失效，于是
        看到啥都要回。插件没有能力 abort 一轮发言，只能把工具收走——模型
        不是「被劝别说」，是**没有说的手段**。

        判定顺序（硬过滤在前，软评分在后）：开关 → @/提及 → 静音窗 →
        能耗尽 → 边界感 → 软评分。每次判定都写日志，因为主动功能最难排查的
        就是「它为什么不说话」。

        **静音窗（``initiative.mute_seconds``）解决的是 focus 模式下的漏网。**
        ``switch_chat`` 切到别的会话时会 ``_queue_proactive_turn`` 开**新的一轮**，
        闸门在干净的上下文里重新判一次——于是一句「这轮不在这儿说」就变成了
        「去别处说」。静音窗是进程级的：判了 deny 之后的一小段时间里，换到
        哪个会话都不开口。@ 仍然穿透这个窗口。
        """

        started = perf_counter()
        out_kwargs = dict(out)
        cfg = self.config
        initiative_cfg = getattr(cfg, "initiative", None)
        tracker = self._tracker
        store = self._store
        if initiative_cfg is None or tracker is None or store is None:
            return out_kwargs
        if not bool(getattr(initiative_cfg, "enabled", False)):
            return out_kwargs

        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return out_kwargs

        # 被 @ / 被提到：时间窗内发生过就算（见 tracker.recent_attention）。
        at_pending, mentioned_pending = tracker.recent_attention(session_id)

        # 能耗尽：复用情绪引擎的两档（voice_level 的 0.0 档）。
        exhausted = False
        try:
            if self._mood is not None:
                exhausted = not self._mood.can_initiate()
        except Exception as exc:  # noqa: BLE001 - 读不到就当没耗尽
            self._debug(f"读取精力状态失败（按未耗尽处理）: {exc!r}")

        # 边界感的沉默判定：主动和边界感必须共用同一个闸门，
        # 不能一边注入「你现在不太想接话」一边又允许她主动开口。
        boundary_silent = False
        try:
            note, silence_mode = self._render_boundary(session_id, cfg, kwargs)
            boundary_silent = bool(silence_mode)
            _ = note
        except Exception as exc:  # noqa: BLE001 - 判不出来就不拦
            self._debug(f"边界感沉默判定失败（按未沉默处理）: {exc!r}")

        # 心情与关系：给软评分用。精力不再参与打分（见 cg_initiative 的说明），
        # 但仍然作为硬闸的输入由上面 exhausted 承担。
        valence = 0.0
        arousal = 0.5
        try:
            if self._mood is not None:
                snapshot = self._mood.snapshot()
                valence = float(getattr(snapshot, "valence", 0.0) or 0.0)
                arousal = float(getattr(snapshot, "arousal", 0.5) or 0.5)
        except Exception as exc:  # noqa: BLE001
            self._debug(f"读取情绪快照失败（用中性值）: {exc!r}")

        affection = 0.0
        who = "对方"
        try:
            state = store.get_state(session_id) or {}
            key = person_key(str(state.get("platform") or ""), str(state.get("user_id") or ""))
            if key:
                person = store.get_person(key)
                if person is not None:
                    affection = float(person.get("affection") or 0.0)
                    who = str(person.get("display_name") or "").strip() or who
        except Exception as exc:  # noqa: BLE001 - 拿不到关系按陌生处理
            self._debug(f"读取关系失败（按陌生处理）: {exc!r}")

        # 当前这轮要说的话：从 planner 的 items 里捞最近的用户文本。
        text = self._planner_user_text(kwargs)
        likes = self._liked_topics(store)

        now = time.time()
        muted = now < self._mute_until

        decision: Decision = evaluate_initiative(
            enabled=True,
            at_pending=at_pending,
            mentioned_pending=mentioned_pending,
            at_always_reply=bool(getattr(initiative_cfg, "at_always_reply", True)),
            mentioned_always_reply=bool(getattr(initiative_cfg, "mentioned_always_reply", False)),
            muted=muted,
            exhausted=exhausted,
            boundary_silent=boundary_silent,
            text=text,
            likes=likes,
            affection=affection,
            valence=valence,
            arousal=arousal,
            threshold=float(getattr(initiative_cfg, "threshold", 0.45) or 0.45),
        )

        # 判了不说 → 开静音窗。
        #
        # 这里必须用「最终是否允许开口」，而不是「是不是被软评分拒的」——
        # 能耗尽、边界感这些硬闸同样得让窗口生效，否则下一轮换个会话又会
        # 重新判一次，一样漏。
        #
        # 但**已经在窗内时不再续窗**：否则每一轮 planner 都会把截止时间往后推
        # 30 秒，只要她一直在被消息唤醒，窗口就永远关不上——从「这轮不说」
        # 变成「永久静音」，和原来那个 bug 是同一个形状。
        if not decision.allow and decision.reason != REASON_MUTED:
            seconds = max(0.0, float(getattr(initiative_cfg, "mute_seconds", 30.0) or 0.0))
            if seconds > 0.0:
                self._mute_until = max(self._mute_until, now + seconds)

        drop: tuple[str, ...] = ("reply",)
        if bool(getattr(initiative_cfg, "gate_send_emoji", True)):
            drop = ("reply", "send_emoji")
        # 静音窗内是否连「换台」也收回。默认关——拿掉 reply 就够拦住「说出来」，
        # 把 switch_chat 一起拿走会让她连「去看看别的群」都做不到，focus 就该废了。
        #
        # 这里用「窗口还没过期」而不是本轮进来时的 ``muted``：判完不说的这一轮
        # 刚刚把窗口打开，如果按 ``muted`` 判，这一轮就漏掉了——而恰恰是这一轮
        # 模型最可能顺手 switch_chat 换个地方说。
        if self._mute_until > now and bool(getattr(initiative_cfg, "gate_switch_chat", False)):
            drop = tuple(drop) + ("switch_chat",)

        kept, removed = filter_tools(
            kwargs.get("tool_definitions"),
            allow_reply=decision.allow,
            drop=drop,
        )
        if bool(getattr(initiative_cfg, "log_decisions", True)):
            tail = f"；已收回 {', '.join(removed)}" if removed else ""
            window = ""
            if self._mute_until > now:
                window = f"；静音窗剩 {self._mute_until - now:.0f}s"
            self._debug(f"自主发言：{describe_initiative(decision)}（{who}）{tail}{window}")
        if kept is None:
            # 工具清单形态不认识 —— **看不懂就别改**，这比猜测性地清空安全得多。
            return out_kwargs
        out_kwargs["tool_definitions"] = kept

        # 熔断保护：这个 hook 的超时预算是最紧的一环（宿主给 6000ms，我们自己的
        # _PLANNER_KW 只给 4000ms），连续超时 3 次会被宿主熔断 60~300 秒，熔断
        # 期间整个插件在这个 hook 上静默跳过——表现和「闸门把她永久静音了」
        # 一模一样，但根因完全不同。超过 800ms 就提前记一条 warning，别等熔断。
        #
        # 宿主没有把熔断状态暴露成能力（``get_plugin_circuit_statuses`` 挂在
        # integration 对象上，不在 75 项能力清单里），所以这里只能自己数：
        # 连续 3 次慢就是熔断器的 ``_FAILURE_THRESHOLD``，到点必须喊一声。
        cost_ms = (perf_counter() - started) * 1000.0
        if cost_ms > _GATE_SLOW_MS:
            self._gate_slow_streak += 1
            self._warn(
                f"自主发言闸门耗时 {cost_ms:.0f}ms（阈值 {_GATE_SLOW_MS:.0f}ms，"
                f"连续 {self._gate_slow_streak} 次），持续超时会被宿主熔断"
            )
            if self._gate_slow_streak >= 3:
                self._warn(
                    "闸门已连续 3 次超过 800ms，宿主熔断器阈值就是 3——"
                    "再超一次这个 hook 会被静默跳过 60~300 秒，"
                    "那段时间她完全不受闸门管（不是被静音，是闸门没跑）"
                )
        else:
            self._gate_slow_streak = 0
        return out_kwargs

    @staticmethod
    def _planner_user_text(kwargs: dict[str, Any]) -> str:
        """从 planner 的 ``items`` 里取出最近的用户文本，用于兴趣匹配。"""

        items = kwargs.get("items")
        if not isinstance(items, list):
            return ""
        chunks: list[str] = []
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            if str(item.get("item_type") or "") in _SYSTEM_ITEM_TYPES:
                continue
            parts = item.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if str(part.get("type") or "") != "text":
                    continue
                value = part.get("text")
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())
            if chunks:
                break
        return "\n".join(reversed(chunks))

    def _liked_topics(self, store: GapStore) -> list[str]:
        """她「喜欢」的口味词，用于兴趣匹配。

        ``list_prefs`` 返回时已按强度降序，这里自己按 ``min_strength`` 过滤——
        强度 0.1 的口味拿去算兴趣匹配只会把「喜欢」稀释成噪音。
        """

        try:
            rows = store.list_prefs(kind="like", limit=24)
        except Exception as exc:  # noqa: BLE001 - 拿不到就不做兴趣加权
            self._debug(f"读取口味失败（不做兴趣加权）: {exc!r}")
            return []
        return [
            str(row.get("topic") or "").strip()
            for row in rows
            if row.get("topic") and float(row.get("strength") or 0.0) >= 0.35
        ][:12]

    @HookHandler("maisaka.replyer.before_request", **_INJECT_KW)
    async def inject_context(self, **kwargs: Any) -> dict[str, Any]:
        """把本地记忆拼成一小段文本，追加到本轮 extra_prompt。

        注意：``modified_kwargs`` 是**整体替换**语义（宿主用
        ``dict(modified_kwargs)`` 覆盖原 kwargs），所以必须回传完整副本。
        """

        out = dict(kwargs)
        started = time.perf_counter()
        try:
            cfg = self.config
            store = self._store
            if not cfg.inject.enabled or store is None:
                return {"action": "continue", "modified_kwargs": out}

            session_id = str(kwargs.get("session_id") or "").strip()
            if not session_id:
                return {"action": "continue", "modified_kwargs": out}

            # 情绪块与记忆块是**两段独立文本**：情绪不占 companion-state 的
            # 字符预算，否则加了情绪就会挤掉记忆条目。
            mood_text = self._render_mood()

            items = store.list_active(
                session_id,
                categories=cfg.extract.categories,
                min_salience=cfg.inject.min_salience,
            )
            block = ""
            used: list[dict[str, Any]] = []
            if items:
                profile = self._probe.cached if self._probe is not None else None
                per_category = (
                    profile.item_budget(cfg.inject.max_items_per_category)
                    if profile is not None
                    else cfg.inject.max_items_per_category
                )
                block, used = compose_block(
                    items,
                    header=cfg.inject.header,
                    footer=cfg.inject.footer,
                    lead_in=cfg.inject.lead_in,
                    max_chars=cfg.inject.max_chars,
                    max_items_per_category=per_category,
                    min_salience=cfg.inject.min_salience,
                    skip_categories=profile.skip_categories() if profile is not None else (),
                    rotation=cfg.inject.rotation,
                    streak_weight=cfg.inject.rotation_streak_weight,
                    cooldown_hours=cfg.inject.rotation_cooldown_hours,
                    message=self._current_message(kwargs),
                    relevance_weight=cfg.inject.relevance_weight,
                    allow_deference=self._deference_allowed(session_id, cfg, kwargs),
                )

            merged = "\n\n".join(part for part in (mood_text, block) if part)
            cross_text = ""
            cross_used: list[dict[str, Any]] = []
            if cfg.inject.cross_session:
                try:
                    cross_text, cross_used = self._render_cross_session(session_id, cfg, kwargs)
                except Exception as exc:  # noqa: BLE001 - 跨流失败不影响本会话注入
                    self._warn(f"跨流注入失败（已跳过）: {exc!r}")

            # 人物块是第三段独立文本，同样不占记忆块预算。
            people_text = ""
            people_used: list[dict[str, Any]] = []
            try:
                people_text, people_used = self._render_people(session_id, cfg, kwargs)
            except Exception as exc:  # noqa: BLE001 - 人物失败不影响记忆注入
                self._warn(f"人物注入失败（已跳过）: {exc!r}")

            # 缺席感：距离上次说话多久。放在记忆块之后——「她注意到你几天没来」
            # 是背景情绪，不该挤掉具体记得的事。
            absence_text = ""
            try:
                absence_text = await self._render_absence(session_id, cfg)
            except Exception as exc:  # noqa: BLE001 - 缺席感失败不影响回复
                self._warn(f"缺席感渲染失败（已跳过）: {exc!r}")

            # 边界感：把「此刻的心情 / 跟他的关系 / 讨厌什么」交给模型自己判断。
            # 只给状态、不给命令 —— 判不判冒犯是模型的事。
            boundary_text = ""
            silence_mode = ""
            try:
                boundary_text, silence_mode = self._render_boundary(session_id, cfg, kwargs)
            except Exception as exc:  # noqa: BLE001 - 边界感失败不影响回复
                self._warn(f"边界感渲染失败（已跳过）: {exc!r}")

            # 第四段：被对方这句话勾起来的遗忘记忆（「想起来」）。
            # 这一段有个副作用——真的会把条目状态从 forgotten 改回 active，
            # 所以要放在最后，前面任何一步失败都不该触发它。
            recall_text = ""
            recall_used: list[dict[str, Any]] = []
            try:
                recall_text, recall_used = self._render_recalled(session_id, cfg, kwargs)
            except Exception as exc:  # noqa: BLE001 - 想不起来不影响正常回复
                self._warn(f"唤起失败（已跳过）: {exc!r}")

            # 第五段：她自己的口味。放在最后是因为它最不紧急 ——
            # 情绪和眼前的话题比「我喜不喜欢猫」重要得多。
            prefs_text = ""
            prefs_used: list[dict[str, Any]] = []
            try:
                prefs_text, prefs_used = self._render_prefs(cfg)
            except Exception as exc:  # noqa: BLE001 - 口味失败不影响记忆注入
                self._warn(f"喜好注入失败（已跳过）: {exc!r}")

            # 未读历史（L1 / L2-L3）。
            #
            # L1「你不在的时候这儿有 N 条」：只给条数和时间，让她知道自己错过
            # 了什么。这一段必须排在记忆之前——先知道「我不在」，后面的记忆才
            # 不会被当成「我一直都在看着」。
            #
            # L2/L3「翻记录」也是两段独立文本：翻到了给原文，翻不到明说没找到。
            unread_text = ""
            unread_used: list[dict[str, Any]] = []
            try:
                unread_text, unread_used = self._render_unread(session_id, cfg, kwargs)
            except Exception as exc:  # noqa: BLE001 - 未读失败不影响回复
                self._warn(f"未读历史渲染失败（已跳过）: {exc!r}")

            lookup_text = ""
            lookup_used: list[dict[str, Any]] = []
            try:
                lookup_text, lookup_used = self._render_lookup(session_id, cfg, kwargs)
            except Exception as exc:  # noqa: BLE001 - 翻记录失败不影响回复
                self._warn(f"翻聊天记录失败（已跳过）: {exc!r}")

            # 念头的「不顺着」指令。**放在最后一段**——它是关于「怎么说」的
            # 要求，不是背景信息，前面那些是「你现在是什么状态」。混在一起
            # 会让模型把「不必顺着」当成又一个状态描述。
            #
            # 这里判断的是 planner 那一轮留下的标记：planner 已经决定了这一轮
            # 她不想顺着，replyer 这一步的活只是别把它又劝回去。标记过期之后
            # 什么都不会发生，不需要有人负责清。
            dissent_text = ""
            if self._dissent_active(session_id):
                dissent_text = render_dissent_block()

            # 留念满了的提醒。**它只出现在「放不下」的时候**，平时一句都不说：
            # 一样没用的东西不该每轮都在提示词里占位置，那会让它变回「有用的
            # 信息」。措辞也刻意不给建议、不列清单（见 cg_keepsake 模块注释）。
            keepsake_text = ""
            try:
                keepsake_text = self._render_keepsake_note(session_id)
            except Exception as exc:  # noqa: BLE001 - 留念失败不影响回复
                self._warn(f"留念提醒渲染失败（已跳过）: {exc!r}")

            # 统一拼接。顺序即重要性：情绪 → 边界 → 缺席 → 人物 → 记忆
            # → 跨流 → 想起 → 口味 → 念头。原来这里是层层嵌套的三元式，
            # 每加一段就要重写一次，加到最后没人敢动；改成一次性列表拼接。
            segments = (
                mood_text,
                boundary_text,
                absence_text,
                unread_text,
                lookup_text,
                people_text,
                block,
                cross_text,
                recall_text,
                prefs_text,
                dissent_text,
                keepsake_text,
            )
            merged = "\n\n".join(part for part in segments if part)
            if not merged:
                return {"action": "continue", "modified_kwargs": out}

            original = kwargs.get("extra_prompt")
            out["extra_prompt"] = merge_extra_prompt(
                original if isinstance(original, str) else "",
                merged,
            )

            elapsed_ms = (time.perf_counter() - started) * 1000.0

            # 轮换记账：只记**真的写进提示词**的条目。
            # 这一步必须放在 merged 已经确定之后 —— compose_block 会因预算
            # 只收前几条，对被丢弃的候选记账会让它们永远卡在冷却里。
            if used:
                try:
                    store.mark_injected(
                        [int(row.get("id") or 0) for row in used if row.get("id")],
                        decay=cfg.inject.inject_salience_decay,
                    )
                except Exception as exc:  # noqa: BLE001 - 记账失败不影响回复
                    self._warn(f"注入记账失败: {exc!r}")

            if (block or cross_text) and cfg.inject.record_injections:
                try:
                    store.record_injection(
                        session_id,
                        chars=len(merged),
                        item_count=len(used) + len(cross_used) + len(recall_used),
                        categories=",".join(
                            sorted(
                                {
                                    str(row.get("category") or "")
                                    for row in (*used, *cross_used, *recall_used)
                                }
                            )
                        ),
                        elapsed_ms=elapsed_ms,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._warn(f"record_injection 失败: {exc!r}")

            if cfg.debug.log_injection_text:
                self._info(f"注入 {len(used)} 条 / {len(merged)} 字 / {elapsed_ms:.1f}ms：\n{merged}")
            else:
                self._debug(
                    f"注入 {len(used)} 条 + 跨流 {len(cross_used)} 条 / "
                    f"人物 {len(people_used)} 个 / 想起 {len(recall_used)} 条 / "
                    f"口味 {len(prefs_used)} 条 / 未读 {len(unread_used)} 段 / "
                    f"翻到 {len(lookup_used)} 条 / {len(merged)} 字 / {elapsed_ms:.1f}ms"
                )

            # 洞已经跟她讲过了，标一下：同一段时间不必每轮都重复提。
            # 放在最后 —— 只有真的写进 extra_prompt 才算「讲过了」。
            if unread_used and unread_text:
                try:
                    store.mark_unread_used(
                        [int(row.get("id") or 0) for row in unread_used if row.get("id")]
                    )
                except Exception as exc:  # noqa: BLE001 - 记账失败不影响回复
                    self._warn(f"标记未读已讲失败: {exc!r}")
        except Exception as exc:  # noqa: BLE001 - 注入失败就放行，不拖累回复
            self._warn(f"inject_context 失败: {exc!r}")
            return {"action": "continue", "modified_kwargs": dict(kwargs)}
        return {"action": "continue", "modified_kwargs": out}

    def _render_cross_session(
        self, session_id: str, cfg: Any, kwargs: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        """渲染「在别的聊天里记得的事」这一段。

        边界是用户定的，不是我想的：

        - **群聊记忆 → 私聊**：允许。群里聊过的事在一对一里提起来很正常。
        - **私聊记忆 → 群聊**：默认关。私聊内容更私密，得用户显式打开
          ``inject.private_to_group``；打开后也不由我决定说不说，而是在
          注入里明确告诉模型「是否在群里提起由你判断」。
        - **私聊 → 另一个私聊**：不允许。那是两个人的私事，串台就是事故。
        """

        store = self._store
        if store is None:
            return "", []

        limit = int(getattr(cfg.inject, "cross_session_limit", 3) or 0)
        if limit <= 0:
            return "", []

        state = store.get_state(session_id) or {}
        chat_type = str(state.get("chat_type") or "").strip()
        if not chat_type:
            # gap_state 还没写（新会话第一条消息），从 session_id 的形状猜。
            chat_type = "group" if "group" in session_id else "private"

        if chat_type == "private":
            allowed_types = ["group"]
            private_note = False
        else:
            allowed_types = ["group"]
            private_note = bool(getattr(cfg.inject, "private_to_group", False))
            if private_note:
                allowed_types = ["group", "private"]

        rows = store.list_other_sessions(
            session_id,
            chat_types=allowed_types,
            categories=cfg.extract.categories,
            min_salience=float(getattr(cfg.inject, "cross_session_min_salience", 0.55) or 0.0),
            limit=max(limit * 4, limit),
        )
        if not rows:
            return "", []

        labels = store.session_labels({str(row.get("session_id") or "") for row in rows})
        return compose_cross_session_block(
            rows,
            labels=labels,
            max_items=limit,
            # 跨流块自己也有字数上限，且不占 companion-state 的预算。
            max_chars=max(120, int(getattr(cfg.inject, "max_chars", 600) or 600) // 2),
            private_note=private_note and any(
                str(row.get("source_chat_type") or "") == "private" for row in rows
            ),
            allow_deference=self._deference_allowed(session_id, cfg, kwargs),
        )

    def _deference_allowed(self, session_id: str, cfg: Any, kwargs: dict[str, Any]) -> bool:
        """当前说话人能不能被叫「主人」。

        ## 这个闸门解决什么

        插件本身不注入称呼（``people.inject_role_label`` 默认关），但**记忆里
        会存真实发生过的对话**：「小鱼称呼 阿汤 为主人」「坚持自己是
        主人群友A的小女仆」。这些是群里真实的玩笑，不该删；可它们每轮
        被注入，就等于每轮都在提醒模型「你有主人，叫主人」。

        真机上的后果：她对**所有人**都开始叫主人，包括普通群友。

        ## 判定

        只有当前说话人的 ``relation_role`` 是 ``owner`` 才放行。身份可以是
        手动设的（``role_source == "manual"``，用户亲手标的），也可以是自动
        判断的 —— 只要是 owner 就算。

        配置 ``people.deference_owner_only`` 关掉时，退回旧行为（不泛化，
        谁都能叫）。默认开启。
        """

        people_cfg = getattr(cfg, "people", None)
        # 先看闸门开关本身：用户显式关掉时，不管人物功能开没开，都退回旧行为。
        if people_cfg is not None and not bool(
            getattr(people_cfg, "deference_owner_only", True)
        ):
            return True
        if people_cfg is None or not getattr(people_cfg, "enabled", False):
            # 人物功能关着 → 查不到 owner → 按「不许叫」处理（安全侧）。
            return False

        store = self._store
        if store is None:
            return False
        try:
            state = store.get_state(session_id) or {}
            platform = str(state.get("platform") or "")
            user_id = self._current_speaker_id(kwargs, state)
            key = person_key(platform, user_id) if user_id else ""
            person = store.get_person(key) if key else None
            return deference_allowed(person, enabled=True)
        except Exception as exc:  # noqa: BLE001 - 判不出来就按「不许叫」处理
            self._debug(f"判断称呼权限失败（按不允许处理）: {exc!r}")
            return False

    def _render_people(
        self, session_id: str, cfg: Any, kwargs: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        """渲染「此刻和你说话的人」这一段。

        这里刻意**不注入称呼**（用户要求）：只描述该怎么说话，不写「他是你的主人」
        这种称呼，否则模型会开口就叫。要开启称呼得显式打开
        ``people.inject_role_label``。

        **与宿主画像分工**（用户第 31 轮第 2 点）：宿主 A_Memorix 的
        「人物画像-内部参考」已经在注入这个人的性格、喜好、背景资料。所以
        自动抽取不再收集这类资料（见 ``cg_extract`` / ``cg_person``），这里
        只补宿主没有的那部分 —— 关系角色、密切度对应的说话方式、这个人给
        Bot 带来的情绪影响。

        用户**手写**的备注照旧注入：那是用户自己交代的话（「别叫他全名」这种
        跟两人关系有关的事），画像里不会有，压掉反而丢信息。画像开着时只是
        把整块字数收紧一点，省得两段提示词重复占预算。

        说话人从 hook 载荷里取：``replyer.before_request`` 的 kwargs 没有直接的
        用户 ID，但 ``reply_reason`` 之外的 ``reply_tool_args`` 里通常带
        ``message_info``；取不到就退回「本会话最近一次记录过的私聊对象」。
        """

        people_cfg = getattr(cfg, "people", None)
        if people_cfg is None or not getattr(people_cfg, "enabled", False):
            return "", []
        if not getattr(people_cfg, "inject", False):
            return "", []

        store = self._store
        if store is None:
            return "", []

        state = store.get_state(session_id) or {}
        platform = str(state.get("platform") or "")
        user_id = self._current_speaker_id(kwargs, state)
        if not user_id:
            return "", []

        key = person_key(platform, user_id)
        if not key:
            return "", []
        person = store.get_person(key)
        if person is None:
            return "", []

        # 标签只由用户自己写，所以这里的标签一定是用户亲手加的，照常注入。
        # 画像开着时只是把整块预算收紧，避免两段提示词争抢注意力。
        profile = self._probe.cached if self._probe is not None else None
        portrait_on = bool(getattr(profile, "person_profile_injection", False))
        max_chars = int(getattr(people_cfg, "max_chars", 240) or 240)
        include_tags = bool(getattr(people_cfg, "inject_notes", True))
        if portrait_on:
            max_chars = min(max_chars, 200)

        return render_person_block(
            [person],
            header=str(getattr(people_cfg, "header", "<companion-people>") or ""),
            footer=str(getattr(people_cfg, "footer", "</companion-people>") or ""),
            lead_in=str(getattr(people_cfg, "lead_in", "") or ""),
            max_chars=max_chars,
            max_people=int(getattr(people_cfg, "max_people", 2) or 2),
            include_role=bool(getattr(people_cfg, "inject_role_label", False)),
            include_label=bool(getattr(people_cfg, "inject_role_label", False)),
            include_tone=bool(getattr(people_cfg, "inject_tone", True)),
            include_tags=include_tags,
            include_impact=bool(getattr(people_cfg, "track_impact", True)),
            # 关系六维描述（用户第 34 轮）：把互相矛盾的维度组合说成一句人话，
            # 例如「你们混得很熟，但有些话你还是不跟他说」。这是「活人感」的
            # 主要来源 —— 单值好感度永远说不出这种话。
            include_axes=bool(getattr(people_cfg, "inject_axes", True)),
            # 身份表与分级表都来自配置页：用户第 33 轮要求身份可以自由配置
            # （自定义身份 + 专属提示词），好感度分级的阈值也要能调。
            roles=_as_dicts(getattr(people_cfg, "roles", None)),
            levels=_as_dicts(getattr(people_cfg, "intimacy_levels", None)),
        )

    def _render_recalled(
        self, session_id: str, cfg: Any, kwargs: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        """把「本来忘了、被对方刚才这句话勾起来」的记忆渲染成一段。

        用户第 32 轮的原话：「遗忘（标注为『遗忘』，并非删除，不主动读取，
        只有他人明确强调才会『想起』）」。所以触发条件是**对方的原话**，不是
        时间、不是随机数：只有当前这轮消息和某条遗忘记忆足够像，才算「明确
        强调」，才把它拉回 ``active``。

        为什么用纯字符串相似度而不是让模型判断：这个函数跑在 replyer 的
        ``before_request`` 里，整条链路只有 6000ms 预算，而且此刻还没开始生成
        回复——在这里再调一次 LLM 既超预算又会和主生成抢配额。字符二元组
        相似度对「同一件事换个说法」够用，误唤醒的代价也只是「多说一句本来
        忘了的事」，可以接受。

        Returns:
            ``(text, used_items)``；空字符串表示这轮没想起任何事。
        """

        recall_cfg = getattr(cfg, "retention", None)
        if recall_cfg is None or not bool(getattr(recall_cfg, "recall_enabled", True)):
            return "", []
        limit = int(getattr(recall_cfg, "recall_limit", 2) or 0)
        if limit <= 0:
            return "", []

        store = self._store
        if store is None:
            return "", []

        message = self._current_message(kwargs)
        if len(message) < 2:
            return "", []

        try:
            forgotten = store.list_forgotten(session_id, limit=200)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取遗忘条目失败: {exc!r}")
            return "", []
        if not forgotten:
            return "", []

        threshold = float(getattr(recall_cfg, "recall_similarity", 0.40) or 0.40)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in forgotten:
            # 用 recall_score 而不是 similarity：这里要判断的是「这句长话里
            # 有没有明确提到那条记忆」，两边长度差一个数量级，Jaccard 会把
            # 分数压得很低，永远过不了门槛。
            score = recall_score(message, str(row.get("content") or ""))
            if score >= threshold:
                scored.append((score, row))
        if not scored:
            return "", []
        scored.sort(key=lambda pair: -pair[0])

        revived: list[dict[str, Any]] = []
        for score, row in scored[:limit]:
            try:
                if store.recall_item(int(row["id"])):
                    revived.append(row)
                    self._debug(f"想起一条遗忘记忆（相似度 {score:.2f}）: {row.get('content')}")
            except Exception as exc:  # noqa: BLE001
                self._warn(f"唤起失败 (id={row.get('id')}): {exc!r}")
        if not revived:
            return "", []

        return compose_recalled_block(
            revived,
            header=str(getattr(recall_cfg, "recall_header", "") or "") or DEFAULT_RECALL_HEADER,
            footer=str(getattr(recall_cfg, "recall_footer", "") or "") or DEFAULT_RECALL_FOOTER,
            lead_in=DEFAULT_RECALL_LEAD_IN,
            max_chars=int(getattr(recall_cfg, "recall_max_chars", 240) or 240),
            max_items=limit,
            allow_deference=self._deference_allowed(session_id, cfg, kwargs),
        )

    def _render_unread(
        self, session_id: str, cfg: Any, kwargs: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        """L1：告诉她「你不在的时候这儿有 N 条」。

        **只给条数和时间范围，不给内容。** 这是整个功能的关键取舍：一旦给了
        内容，她就从「那会儿我不在」变成「其实我都看过」——那正是用户嫌弃的
        那种假装。反过来，只给一个数字，她就只能诚实地说「你们昨晚聊了两百条？
        我一条都没看到」。

        Returns:
            ``(text, used_gaps)``；空字符串表示这段时间没有洞。
        """

        history_cfg = getattr(cfg, "history", None)
        if history_cfg is None or not bool(getattr(history_cfg, "enabled", True)):
            return "", []
        if not bool(getattr(history_cfg, "inject_unread", True)):
            return "", []

        store = self._store
        if store is None:
            return "", []

        try:
            state = store.get_state(session_id) or {}
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取会话状态失败（未读历史）: {exc!r}")
            return "", []

        # 刚聊完就别提「你刚才不在」——那是噪音。
        gaps = []
        try:
            gaps = store.list_unread(session_id, limit=4)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取未读洞失败: {exc!r}")
            return "", []
        if not gaps:
            return "", []

        chat_type = str(state.get("chat_type") or "").strip().lower()
        who = self._person_label(state)
        text = render_unread_note(
            gaps,
            chat_type=chat_type,
            who=who,
            max_chars=int(getattr(history_cfg, "unread_max_chars", 180) or 180),
        )
        return text, gaps if text else []

    def _render_lookup(
        self, session_id: str, cfg: Any, kwargs: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        """L2 / L3：对方提到过去的事时，翻记录。

        触发条件是 ``looks_like_lookup``（「昨天」「上次」「说过」这类词），
        和「想起遗忘记忆」用的是同一个思路：**对方的原话**才是钩子，不是时间、
        不是随机数。

        L3 同样重要：翻了但没找到时也必须返回提示，否则模型会顺着上下文把
        「昨天那个」编下去。她得知道自己翻了、并且没翻到。

        为什么仍然不调 LLM：跑在 replyer 的 ``before_request`` 里，6000ms 预算。
        """

        history_cfg = getattr(cfg, "history", None)
        if history_cfg is None or not bool(getattr(history_cfg, "enabled", True)):
            return "", []
        if not bool(getattr(history_cfg, "lookup_enabled", True)):
            return "", []

        store = self._store
        if store is None:
            return "", []

        message = self._current_message(kwargs)
        if not looks_like_lookup(message):
            return "", []

        limit = int(getattr(history_cfg, "lookup_limit", 3) or 3)
        threshold = float(getattr(history_cfg, "lookup_similarity", 0.34) or 0.34)
        try:
            rows = store.search_history(session_id, limit=400)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取历史原文失败（翻记录已跳过）: {exc!r}")
            return "", []
        if not rows:
            # 从来没存过原文 → 不是「翻了没找到」，是「没得翻」，什么都不说
            return "", []

        hits = pick_history_hits(message, rows, limit=limit, threshold=threshold)
        if hits:
            try:
                store.mark_history_looked([int(hit["id"]) for hit in hits if hit.get("id")])
            except Exception as exc:  # noqa: BLE001
                self._debug(f"标记翻看时间失败: {exc!r}")
            self._debug(f"翻到 {len(hits)} 条历史记录（线索：{message[:24]}）")

        text = render_lookup_block(hits, max_chars=420)
        return text, hits

    def _person_label(self, state: dict[str, Any]) -> str:
        """取「对方」在这段会话里该怎么称呼——用于缺席感/未读提示里的第三人称。"""

        who = "对方"
        store = self._store
        if store is None:
            return who
        try:
            platform = str(state.get("platform") or "qq")
            user_id = str(state.get("user_id") or "")
            if not user_id:
                return who
            person = store.get_person(person_key(platform, user_id))
        except Exception:  # noqa: BLE001 - 取不到就用默认
            return who
        if person:
            name = str(person.get("display_name") or person.get("nickname") or "").strip()
            if name:
                return name
        return who

    def _render_prefs(self, cfg: Any) -> tuple[str, list[dict[str, Any]]]:
        """渲染「她自己的口味」这一块。

        这一块和记忆块、人物块的性质都不一样：它跟当前聊天对象无关，
        只跟「她是谁」有关。所以放在最后注入——情绪和眼前的话题永远
        比「我喜不喜欢猫」优先。

        为什么要单独一段而不是塞进记忆块：记忆是「发生过的事」，
        口味是「我是什么样的人」。混在一起模型会把口味当成某次聊天的
        记录，说出来就变成「你上次说过喜欢猫」而不是「我本来就喜欢猫」。
        """

        prefs_cfg = getattr(cfg, "prefs", None)
        if prefs_cfg is None or not bool(getattr(prefs_cfg, "enabled", False)):
            return "", []
        if not bool(getattr(prefs_cfg, "inject", False)):
            return "", []
        store = self._store
        if store is None:
            return "", []

        limit = int(getattr(prefs_cfg, "list_limit", 50) or 50)
        try:
            rows = store.list_prefs(limit=max(1, min(limit, 60)))
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取喜好失败: {exc!r}")
            return "", []
        if not rows:
            return "", []

        return render_prefs_block(
            rows,
            header=str(getattr(prefs_cfg, "header", "") or "") or "<companion-prefs>",
            footer=str(getattr(prefs_cfg, "footer", "") or "") or "</companion-prefs>",
            lead_in=str(getattr(prefs_cfg, "lead_in", "") or "")
            or "（这些是你自己的口味，不是别人告诉你的）",
            max_chars=int(getattr(prefs_cfg, "max_chars", 200) or 200),
            max_items=int(getattr(prefs_cfg, "max_items", 6) or 6),
            min_strength=float(getattr(prefs_cfg, "min_strength", 0.35) or 0.35),
        )

    async def _presence_of(self, state: dict[str, Any], cfg: Any) -> Presence | None:
        """算出这个会话的「在场解剖」；读不到在线数据时返回 ``None``。

        返回 ``None`` 的含义是「**不知道**她当时在不在」——调用方据此退回旧算法
        （一律按「对方没来」处理）。这是刻意的：读不到在线时段时如果把离线时长
        猜成 0，那等于假设她一直醒着；猜成「全都不在」，又会把所有缺席感抹平。
        都不对，所以退回原样。

        **只读缓存，不查库**：在线时段由 :meth:`_refresh_online_windows` 在
        后台任务里每 10 分钟刷一次。注入 hook 只有 2500ms 预算，在这里等一次
        8600 行的查询是拿回复速度换一个本来就不精确的判据，不值。
        """

        presence_cfg = getattr(cfg, "presence", None)
        if presence_cfg is None or not bool(getattr(presence_cfg, "enabled", False)):
            return None

        windows = self._online_windows
        if not windows:
            return None

        try:
            pending = int(state.get("pending_count") or 0)
        except (TypeError, ValueError):
            pending = 0

        presence = analyse_presence(
            last_seen_ts=state.get("last_seen_ts"),
            windows=windows,
            pending=pending,
            offline_dominant=float(getattr(presence_cfg, "offline_dominant", 0.5) or 0.5),
            min_hours=float(
                getattr(getattr(cfg, "absence", None), "min_hours", 6.0) or 6.0
            ),
        )
        self._debug(f"在场判据：{describe_presence(presence)}")
        return presence

    async def _refresh_online_windows(self) -> None:
        """后台刷新在线时段缓存（10 分钟一次，失败就保留上一次的结果）。"""

        cfg = self.config
        presence_cfg = getattr(cfg, "presence", None)
        if presence_cfg is None or not bool(getattr(presence_cfg, "enabled", False)):
            return
        try:
            windows = await fetch_online_windows(
                self.ctx,
                days=float(getattr(presence_cfg, "online_days", 30.0) or 30.0),
                logger=self.ctx.logger,
            )
        except Exception as exc:  # noqa: BLE001 - 读不到就维持上一次的结果
            self._debug(f"刷新在线时段失败（沿用上一次结果）: {exc!r}")
            return
        if not windows:
            # 空结果可能是「宿主这张表还没写过」，也可能是读失败。两种情况都
            # 不该把已有缓存清掉——清掉等于让缺席判据悄悄退回旧算法。
            self._debug("在线时段为空（保持上一次结果）")
            return
        self._online_windows = windows
        self._online_windows_at = time.time()
        self._debug(f"在线时段已刷新：{len(windows)} 段区间")

    async def _online_loop(self) -> None:
        """后台任务：周期性刷新在线时段，直到插件卸载。"""

        while not self._online_stop.is_set():
            try:
                await self._refresh_online_windows()
            except Exception as exc:  # noqa: BLE001 - 后台任务绝不能死
                self._debug(f"在线时段刷新循环异常（继续）: {exc!r}")
            try:
                await asyncio.wait_for(self._online_stop.wait(), timeout=_ONLINE_CACHE_SECONDS)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                return

    async def _spark_loop(self) -> None:
        """后台任务：定期看一次「她现在要不要主动开口」。

        **这个循环只做判断和记账，不直接说话。** 真正的开口走
        ``maisaka.proactive.trigger``，那条路径会 arm 强制触发、绕过频率门控，
        等于绕过了现有所有「别太吵」的机制——所以它必须由一堆硬闸守着，
        而不是随手调一次。

        开关关着的时候循环仍然在跑（每轮只判一次 ``enabled`` 就 continue），
        这样在 WebUI 里打开就能立刻生效，不需要重启插件。
        """

        while not self._spark_stop.is_set():
            try:
                await self._maybe_spark()
            except Exception as exc:  # noqa: BLE001 - 后台任务绝不能死
                self._debug(f"主动循环异常（继续）: {exc!r}")
            try:
                await asyncio.wait_for(self._spark_stop.wait(), timeout=_SPARK_TICK_SECONDS)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                return

    async def _maybe_spark(self) -> None:
        """对所有已知会话判一次要不要主动。"""

        cfg = self.config
        spark_cfg = getattr(cfg, "proactive", None)
        if spark_cfg is None or not bool(getattr(spark_cfg, "enabled", False)):
            return
        store = self._store
        tracker = self._tracker
        if store is None or tracker is None:
            return

        now = time.time()
        quiet = in_quiet_hours(
            now,
            start=int(getattr(spark_cfg, "quiet_start_hour", 0) or 0),
            end=int(getattr(spark_cfg, "quiet_end_hour", 8) or 8),
        )
        # 精力耗尽时她连话都说不动，不可能好奇。这一条和「不顺着」共用同一个
        # 判据，因为它问的是同一件事：现在有没有力气开口。
        mood = self._mood
        exhausted = bool(mood is not None and not mood.can_initiate())

        for state in self._spark_sessions(store):
            session_id = str(state.get("session_id") or "").strip()
            if not session_id:
                continue
            try:
                await self._spark_one(
                    session_id,
                    state,
                    spark_cfg,
                    now=now,
                    quiet=quiet,
                    exhausted=exhausted,
                )
            except Exception as exc:  # noqa: BLE001
                self._debug(f"主动判定失败（{session_id}）: {exc!r}")

    def _spark_sessions(self, store: Any) -> list[dict[str, Any]]:
        """列出可以主动的会话。

        只取「她自己也认识」的会话（``gap_state`` 里有记录、且最近有过互动），
        不去遍历宿主的所有群——一个从没说过话的群里"忽然想起点什么"是纯打扰。
        """

        try:
            states = store.list_states()
        except Exception as exc:  # noqa: BLE001
            self._debug(f"读取会话列表失败: {exc!r}")
            return []
        now = time.time()
        out: list[dict[str, Any]] = []
        for state in states:
            last = float(state.get("last_seen_ts") or 0.0)
            if last <= 0.0:
                continue
            # 超过 30 天没动静的会话不再主动：那里的人都快忘了她是谁了。
            if now - last > 30 * 86400.0:
                continue
            out.append(state)
        return out

    async def _spark_one(
        self,
        session_id: str,
        state: dict[str, Any],
        spark_cfg: Any,
        *,
        now: float,
        quiet: bool,
        exhausted: bool,
    ) -> None:
        """对一个会话判一次，够了就 trigger。"""

        store = self._store
        if store is None:
            return

        spark = self._pick_spark(session_id, spark_cfg, state)
        last_ts = 0.0
        today = 0
        try:
            last_ts = store.last_spark_ts(session_id)
            today = store.spark_count_today(session_id, now=now)
        except Exception as exc:  # noqa: BLE001
            self._debug(f"读取主动记账失败（{session_id}）: {exc!r}")

        # 空闲时长按「这条会话最后一条消息」算，不是按她的思考循环算。
        idle = max(0.0, now - float(state.get("last_seen_ts") or now))
        plan = decide_spark(
            enabled=True,
            has_material=spark is not None,
            idle=idle,
            idle_min_seconds=float(getattr(spark_cfg, "idle_minutes", 90.0) or 90.0) * 60.0,
            last_spark_ts=last_ts,
            cooldown_seconds=float(getattr(spark_cfg, "cooldown_hours", 8.0) or 8.0) * 3600.0,
            today_count=today,
            daily_limit=int(getattr(spark_cfg, "daily_limit", 1) or 1),
            exhausted=exhausted,
            quiet=quiet,
            boundary=False,
            now=now,
        )
        plan.spark = spark

        log_skips = bool(getattr(spark_cfg, "log_skips", True))
        if not plan.allow:
            if log_skips:
                self._debug(f"{describe_spark_plan(plan)}（{session_id}）")
                self._record_spark(session_id, spark, plan, status="skipped")
            return

        intent = render_intent(spark) if spark is not None else ""
        if not intent:
            self._debug(f"主动：素材渲染不出 intent，放弃（{session_id}）")
            return
        sent = await self._send_spark(session_id, intent, spark)
        self._record_spark(session_id, spark, plan, status="triggered" if sent else "failed")
        if sent:
            self._info(f"{describe_spark_plan(plan)}（{session_id}）")

    def _pick_spark(
        self,
        session_id: str,
        spark_cfg: Any,
        state: dict[str, Any],
    ) -> Spark | None:
        """挑一个由头。三条来源按「最像人忽然想起来」的顺序。"""

        store = self._store
        if store is None:
            return None
        candidates: list[Spark] = []

        # ① 没聊完的事。最接近人「忽然想起来」的感觉，而且不需要新数据。
        if bool(getattr(spark_cfg, "include_unfinished", True)):
            try:
                for row in store.list_by_category(session_id, "open_loop", limit=4):
                    content = str(row.get("content") or "").strip()
                    if content:
                        candidates.append(
                            Spark(
                                kind=KIND_UNFINISHED,
                                content=content[:80],
                                source=f"item:{row.get('id')}",
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                self._debug(f"读取未闭合话题失败: {exc!r}")

        # ② 留着的没用东西。「浪费」唯一的出口就在这儿。
        if bool(getattr(spark_cfg, "include_keepsake", True)):
            try:
                rows = store.list_keepsakes(session_id, limit=6, status="kept")
                for row in rows:
                    item = Keepsake.from_row(row)
                    content = str(item.content or "").strip()
                    if content:
                        candidates.append(
                            Spark(
                                kind=KIND_KEEPSAKE,
                                content=content[:80],
                                source=f"keepsake:{item.id}",
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                self._debug(f"读取留念失败: {exc!r}")

        # ③ 回去翻一段当时不在的聊天。真好奇，且完全不需要外部信息。
        try:
            holes = store.list_unread(session_id, limit=2)
            for hole in holes:
                rows = store.search_history(
                    session_id,
                    since_ts=float(hole.get("start_ts") or 0.0),
                    until_ts=float(hole.get("end_ts") or 0.0),
                    limit=40,
                )
                if not rows:
                    continue
                # 只挑一条：翻整段就变成「读完整个群的聊天记录」，
                # 那正是之前费很大劲才避开的监控感。
                picks = spark_from_history(rows, span="", limit=1)
                candidates.extend(picks)
        except Exception as exc:  # noqa: BLE001
            self._debug(f"读取未读历史失败: {exc!r}")

        return pick_spark(candidates)

    async def _send_spark(self, session_id: str, intent: str, spark: Spark | None) -> bool:
        """走宿主的能力把这个由头交给她。

        失败只记一行——主动开口本来就不该报错到用户面前。
        """

        maisaka = getattr(self.ctx, "maisaka", None)
        proactive = getattr(maisaka, "proactive", None)
        if proactive is None:
            self._debug("宿主没有 proactive 能力，主动开口不可用")
            return False
        try:
            await proactive.trigger(
                session_id,
                intent,
                reason=f"她自己想起来的（{spark.kind if spark else 'unknown'}）",
                priority="low",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._warn(f"触发主动开口失败（{session_id}）: {exc!r}")
            return False

    def _record_spark(
        self,
        session_id: str,
        spark: Spark | None,
        plan: SparkPlan,
        *,
        status: str,
    ) -> None:
        """记账。**没开口的也要记**，否则出问题时日志里一片空白。"""

        store = self._store
        if store is None:
            return
        try:
            store.record_spark(
                session_id,
                kind=spark.kind if spark is not None else "",
                content=spark.content if spark is not None else "",
                source=spark.source if spark is not None else "",
                status=status,
                reason=plan.reason,
            )
        except Exception as exc:  # noqa: BLE001
            self._debug(f"记录主动失败: {exc!r}")

    # ── 留念（浪费）────────────────────────────────────────────────

    def _render_keepsake_note(self, session_id: str) -> str:
        """满了之后提醒她一句「可以挑一件放掉」。

        **只在满的时候说，而且不列出具体是哪几件。** 一旦列出来，模型就会开始
        按「哪条最没用」排序，而「一样东西有没有用」正是这个功能要拒绝的问法。
        """

        cfg = self.config
        keep_cfg = getattr(cfg, "keepsake", None)
        if keep_cfg is None or not bool(getattr(keep_cfg, "enabled", True)):
            return ""
        store = self._store
        if store is None:
            return ""
        try:
            kept = store.list_keepsakes(session_id, limit=200, status="kept")
            cap = int(getattr(keep_cfg, "max_items", 12) or 12)
            if not keepsake_is_full(kept_keepsakes([Keepsake.from_row(r) for r in kept]), cap=cap):
                return ""
        except Exception as exc:  # noqa: BLE001
            self._debug(f"读取留念失败: {exc!r}")
            return ""
        return render_overflow_note(len(kept), cap=cap)

    def _render_presence_shift(self, presence: Presence, chat_type: str, cfg: Any) -> str:
        """渲染「那段时间我不在 / 有待补消息」的提示。

        这两种情况和「你冷落我」是**相反**的语义，所以不能走
        ``render_absence_note``——那套措辞全在暗示「对方没来」。
        """

        presence_cfg = getattr(cfg, "presence", None)
        if presence_cfg is None or not bool(getattr(presence_cfg, "away_enabled", True)):
            return ""

        if presence.level == "pending":
            # 有待补消息：那段时间她没看见的东西还没抽取。这时候提「好久没见」
            # 是拿没读过的消息当感情依据，很假。让后台先把洞补上。
            self._debug(f"在场判据：有待补消息，这轮不注入缺席感（{describe_presence(presence)}）")
            return ""

        if presence.level != "away":
            return ""

        absence_cfg = getattr(cfg, "absence", None)
        max_chars = int(getattr(absence_cfg, "max_chars", 60) or 60)
        header = str(getattr(absence_cfg, "inject_header", "") or "").strip() or "<companion-absence>"
        footer = str(getattr(absence_cfg, "inject_footer", "") or "").strip() or "</companion-absence>"

        how = describe_gap(presence.offline_hours)
        note = render_away_note(hours=presence.offline_hours, how=how, chat_type=chat_type or "private")
        if not note:
            return ""
        body = note[: max(1, max_chars - len(header) - len(footer) - 2)]
        return f"{header}\n{body}\n{footer}"

    async def _render_absence(self, session_id: str, cfg: Any) -> str:
        """渲染「多久没说话了」这一段。

        数据来自 ``gap_state.last_seen_ts``——它是**每次收到消息时**更新的
        （``mark_dirty`` 里写内存、tick 里落库），所以「现在 - last_seen_ts」
        正好就是「这个会话上一次有人说话到现在隔了多久」。

        两个刻意的边界：

        - **群聊默认不注入**。群冷清是群的事，不是「你冷落我」。只有在配置里
          明确打开 ``absence.group_enabled`` 时才提，而且措辞不带情绪。
        - **需要真人关系的档位**。陌生人档位恒为 ``mild`` 且大多数时候返回
          空串——他为什么要在意一个陌生人多久没来。

        在场判据（``presence.enabled``）会把总间隔拆成「她不在」和「对方没来」：
        前者渲染成「我那段时间没在」，后者才是真正的缺席感。详见
        :mod:`cg_presence` 的模块文档。

        Returns:
            渲染好的文本（含 ``<companion-absence>`` 标记）；空串表示不注入。
        """

        absence_cfg = getattr(cfg, "absence", None)
        if absence_cfg is None:
            return ""
        if not bool(getattr(absence_cfg, "enabled", False)):
            return ""
        if not bool(getattr(absence_cfg, "inject", False)):
            return ""
        store = self._store
        if store is None:
            return ""

        try:
            state = store.get_state(session_id) or {}
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取会话状态失败: {exc!r}")
            return ""

        chat_type = str(state.get("chat_type") or "").strip().lower()
        if chat_type == "group" and not bool(getattr(absence_cfg, "group_enabled", False)):
            return ""

        seconds = absence_seconds(state.get("last_seen_ts"))
        if seconds <= 0:
            return ""

        hours = seconds / 3600.0

        # ── 在场判据：把「多久没说话」拆成「她不在」和「对方没来」 ──────────
        # 旧算法只有 hours 一个数，等于假设她一直醒着。用户每天 00:00 关机、
        # 11:00 开机，于是上午开机后她看到「73 小时没说话」就说「你怎么这么久
        # 没来」——可其中 33.8 小时是她自己不在。
        presence = await self._presence_of(state, cfg)
        if presence is not None:
            if presence.level in {"away", "pending"}:
                return self._render_presence_shift(presence, chat_type, cfg)
            if presence.level == "none":
                return ""
            # level == "cold"：继续走原来的缺席感渲染，但用醒着的时长而不是总间隔。
            hours = presence.awake_hours
            if hours <= 0:
                return ""
        # 关系亲疏决定「提不提、提得多重」：好感度从人物表拿（查不到就按 0 算，
        # 也就是「不熟」，只会得到最轻的档位或直接不提）。
        affection = 0.0
        who = "对方"
        try:
            user_id = str(state.get("user_id") or "").strip()
            platform = str(state.get("platform") or "").strip()
            key = person_key(platform, user_id)
            if key:
                person = store.get_person(key)
                if person is not None:
                    affection = float(person.get("affection") or 0.0)
                    who = str(person.get("display_name") or "").strip() or who
            elif user_id:
                who = str(state.get("user_nickname") or "").strip() or who
        except Exception as exc:  # noqa: BLE001 - 拿不到关系就按最轻的处理
            self._debug(f"缺席感读取关系失败（按最轻档处理）: {exc!r}")

        level = absence_level(
            hours,
            affection=affection,
            min_hours=float(getattr(absence_cfg, "min_hours", 6.0) or 6.0),
            long_hours=float(getattr(absence_cfg, "long_hours", 72.0) or 72.0),
        )
        if level == "none":
            return ""

        note = render_absence_note(
            hours=hours,
            level=level,
            chat_type=chat_type or "private",
            who=who,
        )
        if not note:
            return ""

        header = str(getattr(absence_cfg, "inject_header", "") or "").strip() or "<companion-absence>"
        footer = str(getattr(absence_cfg, "inject_footer", "") or "").strip() or "</companion-absence>"
        max_chars = int(getattr(absence_cfg, "max_chars", 60) or 60)
        body = note[: max(1, max_chars - len(header) - len(footer) - 2)]
        self._debug(f"缺席感：{hours:.1f} 小时 / 好感 {affection:.0f} / 档位 {level}")
        return f"{header}\n{body}\n{footer}"

    def _render_boundary(
        self, session_id: str, cfg: Any, kwargs: dict[str, Any]
    ) -> tuple[str, str]:
        """渲染边界感那一段；返回 ``(文本, 沉默方式)``。

        **这不是「判定她有没有被冒犯」**——判定是模型的事，插件只提供三样
        状态：此刻心情、跟这个人的关系张力、当前这句话踩中了哪些她讨厌的东西。
        模型拿到这些自己决定「要不要回、回什么」。

        用户在配置里打开 ``boundary.silence_enabled`` 时，插件才额外做一次
        「该不该沉默」的闸门判定（三个维度同时成立 + 频率上限），判定通过就
        在文本里给出两个选项让模型自己挑。**闸门只决定「提示不提示」，
        不决定「说哪句」**。

        Returns:
            ``(渲染文本, 沉默方式)``；方式为空串表示没有触发沉默闸门。
        """

        boundary_cfg = getattr(cfg, "boundary", None)
        if boundary_cfg is None or not bool(getattr(boundary_cfg, "enabled", False)):
            return "", ""
        if not bool(getattr(boundary_cfg, "note_enabled", False)):
            return "", ""

        store = self._store
        mood = self._mood
        if store is None or mood is None:
            return "", ""

        message = self._current_message(kwargs)

        # 心情：直接读内存快照（不落盘、不衰减，快照本身就是最新的）。
        try:
            snapshot = mood.snapshot()
            valence = float(getattr(snapshot, "valence", 0.0) or 0.0)
            anger = float(getattr(snapshot, "anger", 0.0) or 0.0)
            label = str(getattr(snapshot, "label", "") or "")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取情绪快照失败（边界感跳过）: {exc!r}")
            return "", ""

        # 关系：张力从人物表拿。（好感度不参与这段文字——它只用来分级，
        # 写进提示词既不可执行也会诱导模型报数，所以这里根本不读。）
        tension = 0.0
        try:
            state = store.get_state(session_id) or {}
            user_id = str(state.get("user_id") or "").strip()
            platform = str(state.get("platform") or "").strip()
            key = person_key(platform, user_id)
            if key:
                person = store.get_person(key)
                if person is not None:
                    axes = axes_of(person)
                    tension = float(axes.get("tension") or 0.0)
        except Exception as exc:  # noqa: BLE001 - 读不到关系就只当「没张力」
            self._debug(f"边界感读取关系失败: {exc!r}")

        # 话题：当前这句话踩中了哪些她讨厌的口味。
        dislikes: list[tuple[str, float]] = []
        try:
            prefs_cfg = getattr(cfg, "prefs", None)
            if prefs_cfg is not None and bool(getattr(prefs_cfg, "enabled", False)):
                rows = store.list_prefs(limit=60)
                dislikes = matched_dislikes(
                    message,
                    rows,
                    min_strength=float(getattr(boundary_cfg, "silence_min_dislike", 0.55) or 0.55),
                )
        except Exception as exc:  # noqa: BLE001
            self._debug(f"边界感读取喜好失败: {exc!r}")

        # 沉默闸门：只有显式开启才判。
        silence_mode = ""
        silence_allowed = False
        if bool(getattr(boundary_cfg, "silence_enabled", False)):
            history_key = f"boundary_silence_at:{session_id}"
            try:
                raw = store.meta_prefix("boundary_silence_at").get(history_key, "")
                history = [float(item) for item in raw.split(",") if item.strip()]
            except Exception:  # noqa: BLE001
                history = []
            decision = evaluate_silence(
                valence=valence,
                anger=anger,
                tension=tension,
                dislikes=dislikes,
                cooldown=history,
                min_valence=float(getattr(boundary_cfg, "silence_min_valence", -0.35) or -0.35),
                min_anger=float(getattr(boundary_cfg, "silence_min_anger", 0.45) or 0.45),
                min_tension=float(getattr(boundary_cfg, "silence_min_tension", 0.55) or 0.55),
                max_per_window=int(getattr(boundary_cfg, "silence_max_per_window", 2) or 0),
                window_hours=float(getattr(boundary_cfg, "silence_window_hours", 3.0) or 3.0),
            )
            if decision.get("allowed"):
                mode_cfg = str(getattr(boundary_cfg, "silence_level", "choose") or "choose").strip().lower()
                silence_mode = SILENCE_MODE_TACITURN if mode_cfg == "taciturn" else SILENCE_MODE_SILENT
                silence_allowed = True
                # **只在实际沉默时记账**：挂在 kwargs 上，由 replyer 之后确认。
                # 这里先把「候选时刻」放进 out 供调用方落库。
                self._debug(
                    "边界感闸门通过：" + "；".join(decision.get("reasons") or [])
                )
                try:
                    updated = record_silence(history)
                    store.set_meta(history_key, ",".join(f"{t:.0f}" for t in updated))
                except Exception as exc:  # noqa: BLE001
                    self._warn(f"沉默记账失败: {exc!r}")

        note = render_boundary_note(
            valence=valence,
            anger=anger,
            tension=tension,
            dislikes=dislikes,
            label=label,
            silence_allowed=silence_allowed,
            # 把「沉默方式」真的交给渲染层：taciturn 不允许完全不发。
            # 早期这里算了 silence_mode 却没往下一层传，配置项等于摆设。
            silence_mode=silence_mode or SILENCE_MODE_SILENT,
        )
        return note, silence_mode

    @staticmethod
    def _current_message(kwargs: dict[str, Any]) -> str:
        """尽量找出「对方这一轮到底说了什么」。

        ``replyer.before_request`` 的载荷里没有原始消息文本，最接近的是
        ``reply_reason``——模型自己写的「为什么回这条」，通常就带着对方说的话。
        拿不到就退回 ``reply_tool_args`` 里的 ``reply_reference`` 原文。

        这个文本有两个用途，且**都只是加分项，不是硬条件**：

        - ``_render_recalled`` 拿它判断「对方是不是明确提到了某条遗忘记忆」；
        - ``compose_block`` 拿它给相关的活跃条目加权。

        取不到很正常（比如纯被 @ 的场景），调用方一律当作「没有相关性信号」
        继续跑，绝不能因为拿不到消息就少注入。
        """

        message = str(kwargs.get("reply_reason") or "")
        if not message:
            tool_args = kwargs.get("reply_tool_args")
            if isinstance(tool_args, dict):
                message = str(tool_args.get("reply_reference") or "")
        return " ".join(message.split())

    @staticmethod
    def _current_speaker_id(kwargs: dict[str, Any], state: dict[str, Any]) -> str:
        """尽量找出「这一轮在跟谁说话」。

        ``replyer.before_request`` 的载荷里没有直接的说话人字段，只有
        ``reply_reason`` / ``reply_tool_args``。这里按可靠度依次尝试：
        载荷里的显式字段 → ``reply_tool_args`` 里的 message_info → 会话状态。
        私聊会话的 ``user_id`` 就是对方，所以私聊可以直接用。
        """

        for field_name in ("user_id", "speaker_id", "sender_id"):
            value = str(kwargs.get(field_name) or "").strip()
            if value:
                return value

        tool_args = kwargs.get("reply_tool_args")
        if isinstance(tool_args, dict):
            info = tool_args.get("message_info")
            if isinstance(info, dict):
                user_info = info.get("user_info")
                if isinstance(user_info, dict):
                    value = str(user_info.get("user_id") or "").strip()
                    if value:
                        return value

        # 私聊会话的会话状态里 user_id 就是对方。
        if str(state.get("chat_type") or "") == "private":
            return str(state.get("user_id") or "").strip()
        return ""

    def _render_mood(self) -> str:
        """渲染情绪块。任何异常都当作「这轮不注入情绪」。"""

        mood = self._mood
        if mood is None:
            return ""
        try:
            cfg = self.config
            section = getattr(cfg, "mood", None)
            if section is None or not getattr(section, "enabled", True):
                return ""
            if not getattr(section, "inject", True):
                return ""
            state = mood.snapshot()
            # 基线要一起传：八个基元的标签与语气都是相对基线算出来的，
            # 不传就退回代码里的默认基线，页面和注入就会对不上。
            baselines = mood.baselines() if hasattr(mood, "baselines") else None
            return render_mood_block(
                state,
                header=str(getattr(section, "header", "") or "<companion-mood>"),
                footer=str(getattr(section, "footer", "") or "</companion-mood>"),
                max_chars=int(getattr(section, "max_chars", 180) or 180),
                include_guidance=bool(getattr(section, "include_guidance", True)),
                baselines=baselines,
            )
        except Exception as exc:  # noqa: BLE001 - 情绪渲染失败不该影响记忆注入
            self._warn(f"渲染情绪块失败: {exc!r}")
            return ""

    # ── 组件：管理命令 ──
    @Command(
        "mcompgap",
        description="我，deepseek：查看/管理当前会话的情境记忆",
        pattern=COMMAND_PATTERN,
        permission="operator",
    )
    async def mcompgap(self, **kwargs: Any) -> tuple[bool, str, bool]:
        """管理命令入口。返回 ``(成功, 回复文本, 是否拦截后续消息)``。"""

        groups = kwargs.get("matched_groups")
        action = ""
        arg = ""
        if isinstance(groups, dict):
            action = str(groups.get("action") or "").strip().lower()
            arg = str(groups.get("arg") or "").strip()
        if not action:
            text = str(kwargs.get("text") or "").strip()
            parts = text.split()
            if len(parts) >= 2:
                action = parts[1].lower()
            if len(parts) >= 3:
                arg = " ".join(parts[2:])

        stream_id = str(kwargs.get("stream_id") or "").strip()

        try:
            reply = await self._handle_command(action, arg, stream_id)
        except Exception as exc:  # noqa: BLE001 - 命令失败要如实告诉用户
            self._warn(f"mcompgap 命令失败: {exc!r}")
            return True, f"命令执行失败：{exc!r}", True
        return True, reply, True

    async def _handle_command(self, action: str, arg: str, stream_id: str) -> str:
        store = self._store
        if store is None:
            return "插件还没初始化完成，稍后再试。"

        if action in ("", "help"):
            return _HELP_TEXT

        if action == "status":
            if not stream_id:
                return "拿不到当前会话 ID，无法查看。"
            stats = store.session_stats(stream_id)
            lines = [f"会话：{stream_id}"]
            by_category = stats.get("by_category") or {}
            if not by_category:
                lines.append("还没有任何记忆条目。")
            else:
                for category, counts in sorted(by_category.items()):
                    detail = ", ".join(f"{status}×{n}" for status, n in sorted(counts.items()))
                    lines.append(f"  {category}: {detail}")
            last = stats.get("last_extraction")
            if isinstance(last, dict):
                ok = "成功" if last.get("ok") else "失败"
                lines.append(
                    f"上次抽取：{ok}，产出 {last.get('item_count')} 条"
                    + (f"，错误：{last.get('error')}" if last.get("error") else "")
                )
            else:
                lines.append("还没有抽取过。")
            return "\n".join(lines)

        if action == "list":
            if not stream_id:
                return "拿不到当前会话 ID，无法列出。"
            categories = [arg] if arg else None
            items = store.list_active(stream_id, categories=categories)
            if not items:
                return "当前会话没有活跃记忆条目。"
            lines = [f"当前会话活跃条目 {len(items)} 条："]
            for row in items[:40]:
                lines.append(
                    f"  [{row['category']}] (salience={float(row['salience']):.2f}) {row['content']}"
                )
            if len(items) > 40:
                lines.append(f"  …还有 {len(items) - 40} 条")
            return "\n".join(lines)

        if action == "extract":
            tracker = self._tracker
            if tracker is None:
                return "后台任务未启动。"
            if not stream_id:
                return "拿不到当前会话 ID，无法抽取。"
            entry = tracker._dirty.get(stream_id)  # noqa: SLF001 - 管理命令需要强制触发
            if entry is None:
                from .cg_tracker import DirtySession

                entry = DirtySession(session_id=stream_id)
                tracker._dirty[stream_id] = entry  # noqa: SLF001
            entry.pending = max(1, entry.pending)
            entry.first_pending_ts = time.time() - 10**9  # 立刻满足 max_wait
            entry.last_attempt_ts = 0.0
            await tracker._extract_session(entry)  # noqa: SLF001
            stats = store.session_stats(stream_id)
            last = stats.get("last_extraction")
            if isinstance(last, dict) and last.get("ok"):
                return f"抽取完成，产出 {last.get('item_count')} 条。"
            if isinstance(last, dict):
                return f"抽取未成功：{last.get('error') or '未知原因'}"
            return "抽取已触发，但没有记录到结果。"

        if action == "clear":
            if not stream_id:
                return "拿不到当前会话 ID，无法清空。"
            removed = store.delete_session_items(stream_id)
            return f"已清空当前会话 {removed} 条记忆条目。"

        if action == "global":
            stats = store.global_stats()
            profile = self._probe.cached if self._probe is not None else None
            lines = [
                f"数据库：{stats.get('db_path')}",
                f"会话数：{stats.get('sessions')}",
                f"条目分布：{stats.get('items_by_status') or '空'}",
                f"抽取次数：{stats.get('extractions')}（成功 {stats.get('extractions_ok')}）",
            ]
            if profile is not None:
                lines.append(f"宿主避让：{profile.describe()}")
                if profile.notes:
                    lines.append(f"探测告警：{'; '.join(profile.notes)}")
            return "\n".join(lines)

        if action == "person":
            return await self._handle_person_command(arg, stream_id)

        return f"未知子命令：{action}\n\n{_HELP_TEXT}"

    async def _handle_person_command(self, arg: str, stream_id: str) -> str:
        """``mcompgap person`` 子命令。

        支持：

        - ``person`` / ``person list``：列出记住的人
        - ``person set <user_id> <角色>``：手动设定角色（优先级最高）
        - ``person affection <user_id> <数值>``：手动设定好感度
        - ``person axes <user_id> <维度> <数值>``：手动设定某一维
        - ``person follow <user_id>``：六维交还自动流程
        - ``person tag <user_id> <标签>``：加一条关系化标签
        - ``person untag <user_id> <标签>``：删掉某一条标签
        - ``person untagall <user_id>``：清空这个人的标签
        - ``person del <user_id>``：删掉这个人

        标签用「XX的XX」这种关系化说法（「抽象的群友」「喜欢玩游戏的朋友」），
        不要写画像式的具体描述 —— 那部分交给宿主的 A_Memorix 画像。

        手动设定会把 ``role_source`` 标成 ``manual``，此后自动判断改不动它 ——
        这正是用户要的「手动配置优先」。
        """

        store = self._store
        if store is None:
            return "插件还没初始化完成，稍后再试。"

        parts = arg.split(None, 2)
        sub = (parts[0].lower() if parts else "list") or "list"

        # 身份表与分级表都来自配置页（用户第 33 轮）：自定义身份和自定义
        # 阈值必须体现在命令输出里，否则用户改完配置看不到任何变化。
        people_cfg = getattr(self.config, "people", None)
        role_table = _as_dicts(getattr(people_cfg, "roles", None))
        level_table = _as_dicts(getattr(people_cfg, "intimacy_levels", None))

        if sub in ("list", ""):
            limit = int(getattr(people_cfg, "list_limit", 50) or 50)
            rows = store.list_people(limit=limit)
            if not rows:
                return "还没有记住任何人。等抽取跑过几轮就会有了。"
            lines = [f"已记住 {len(rows)} 个人（按最近互动排序）："]
            for row in rows[:limit]:
                lines.append(
                    "  " + describe_person(row, roles=role_table, levels=level_table)
                )
            return "\n".join(lines)

        if sub in (
            "set",
            "affection",
            "axes",
            "follow",
            "tag",
            "untag",
            "untagall",
            "del",
            "delete",
        ):
            if len(parts) < 2:
                return f"用法：mcompgap person {sub} <user_id> ..."
            user_id = parts[1].strip()
            rest = parts[2].strip() if len(parts) > 2 else ""

            platform = ""
            if stream_id:
                state = store.get_state(stream_id) or {}
                platform = str(state.get("platform") or "")

            key = person_key(platform, user_id)
            if not key:
                return "缺少 user_id。"

            existing = store.get_person(key)

            if sub in ("del", "delete"):
                if existing is None:
                    return f"没找到 {user_id}。"
                store.delete_person(key)
                return f"已删除 {describe_person(existing, roles=role_table, levels=level_table)}。"

            if sub == "set":
                role = normalize_role(rest, _as_dicts(getattr(people_cfg, "roles", None)))
                if not role:
                    names = [
                        f"{item['key']}（{item['label']}）"
                        for item in _as_dicts(getattr(people_cfg, "roles", None))
                    ] or ["owner（主人）", "friend（朋友）", "member（普通群友）", "stranger（陌生人）"]
                    return "角色只能是 " + " / ".join(names) + "。要加新身份去配置页改「关系身份」。"
                row = dict(existing or {})
                row.update(
                    {
                        "person_key": key,
                        "platform": platform,
                        "user_id": user_id,
                        "display_name": str(row.get("display_name") or user_id),
                        "relation_role": role,
                        "role_confidence": 1.0,
                        "role_source": "manual",
                    }
                )
                store.upsert_person(row)
                return f"已手动设定：{describe_person(row, roles=role_table, levels=level_table)}（自动判断不会再改它）"

            if sub == "affection":
                try:
                    value = float(rest)
                except ValueError:
                    return "好感度需要是数字（-100 到 100）。"
                row = dict(existing or {})
                # 好感度现在**是六维算出来的摘要**，不再是独立的真相
                # （用户第 34 轮）。所以手动设好感度要反过来摊到六维上，
                # 否则下一次抽取算出六维就会把手设的值覆盖掉。
                # ``axes_source="manual"`` 才是真正挡住覆盖的那道闸门：
                # 只反摊不标记，下一次抽取照样会改（用户第 43 轮报的问题）。
                row.update(
                    {
                        "person_key": key,
                        "platform": platform,
                        "user_id": user_id,
                        "display_name": str(row.get("display_name") or user_id),
                        "affection": clamp_affection(value),
                        "axes": axes_from_affection(value),
                        "axes_source": "manual",
                        "role_source": str(row.get("role_source") or "manual"),
                    }
                )
                if not row.get("relation_role"):
                    row["relation_role"] = "stranger"
                store.upsert_person(row)
                return f"已设定好感度：{describe_person(row, roles=role_table, levels=level_table)}（六维已按这个数重算，自动判断不会再改）"

            if sub == "axes":
                # ``person axes <user_id> <维度> <数值>`` —— 直接设某一维。
                # 这是六维化之后真正该用的手动入口：好感度只是摘要，
                # 想精确表达「很熟但不信他」只能靠逐维设定。
                parts = rest.split()
                if len(parts) != 2:
                    names = " / ".join(f"{key}（{AXIS_LABELS[key]}）" for key in AXIS_KEYS)
                    return (
                        "用法：person axes <user_id> <维度> <0-1 的数值>。\n"
                        f"维度：{names}"
                    )
                axis_key = str(parts[0]).strip().lower()
                if axis_key not in AXIS_KEYS:
                    names = " / ".join(f"{key}（{AXIS_LABELS[key]}）" for key in AXIS_KEYS)
                    return f"没有这个维度。可选：{names}"
                try:
                    axis_value = float(parts[1])
                except ValueError:
                    return "维度数值需要是 0 到 1 之间的数字。"
                row = dict(existing or {})
                axes = axes_of(row)
                axes[axis_key] = clamp_axis(axis_value, axis_key)
                row.update(
                    {
                        "person_key": key,
                        "platform": platform,
                        "user_id": user_id,
                        "display_name": str(row.get("display_name") or user_id),
                        "axes": axes,
                        "axes_source": "manual",
                        "affection": affection_from_axes(axes),
                        "role_source": str(row.get("role_source") or "manual"),
                    }
                )
                if not row.get("relation_role"):
                    row["relation_role"] = "stranger"
                store.upsert_person(row)
                return f"已设定 {AXIS_LABELS[axis_key]}：{describe_person(row, roles=role_table, levels=level_table)}（自动判断不会再改这六维）"

            if sub == "follow":
                # 反向开关：把手调过的六维交还自动流程。没有这一步，手动
                # 锁定就是单向的 —— 用户只能改成新值，没法让关系重新跟着
                # 聊天变化（用户第 43 轮抱怨的另一半）。
                if existing is None:
                    return f"还没有这个人的记录：{user_id}"
                if str(existing.get("axes_source") or "auto") == "auto":
                    return f"{user_id} 的六维本来就在跟着聊天变化，不需要恢复。"
                row = dict(existing)
                row["axes_source"] = "auto"
                store.upsert_person(row)
                return (
                    f"已让 {user_id} 的六维重新跟随聊天变化"
                    f"：{describe_person(row, roles=role_table, levels=level_table)}"
                )

            if sub == "tag":
                if not rest:
                    return "标签不能为空。"
                row = dict(existing or {})
                tags = [str(item) for item in (row.get("tags") or [])]
                tag_text = rest[:TAG_MAX_CHARS]
                if tag_text in tags:
                    return f"这条标签已经有了：{tag_text}"
                tags.append(tag_text)
                row.update(
                    {
                        "person_key": key,
                        "platform": platform,
                        "user_id": user_id,
                        "display_name": str(row.get("display_name") or user_id),
                        "tags": normalize_tags(tags),
                        "role_source": str(row.get("role_source") or "manual"),
                    }
                )
                if not row.get("relation_role"):
                    row["relation_role"] = "stranger"
                store.upsert_person(row)
                return f"已加标签：{describe_person(row, roles=role_table, levels=level_table)}"

            if sub in ("untag", "untagall"):
                row = dict(existing or {})
                if sub == "untagall":
                    removed = [str(item) for item in (row.get("tags") or [])]
                    if not removed:
                        return "这个人还没有标签。"
                    row["tags"] = []
                else:
                    if not rest:
                        return "要说清楚删哪一条标签。"
                    target = rest[:TAG_MAX_CHARS]
                    tags = [str(item) for item in (row.get("tags") or [])]
                    if target not in tags:
                        return f"没有这条标签：{target}"
                    removed = [target]
                    row["tags"] = [item for item in tags if item != target]
                row.update(
                    {
                        "person_key": key,
                        "platform": platform,
                        "user_id": user_id,
                        "display_name": str(row.get("display_name") or user_id),
                    }
                )
                store.upsert_person(row)
                return f"已删除标签「{'、'.join(removed)}」：{describe_person(row, roles=role_table, levels=level_table)}"

        return f"未知子命令：person {sub}\n\n{_HELP_TEXT}"

    # ── 工具方法 ──

    def _data_dir(self) -> str:
        try:
            return str(self.ctx.paths.data_dir)
        except Exception:  # noqa: BLE001 - 脱离 Runner 时用插件目录兜底
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    def _info(self, message: str) -> None:
        _log(self.ctx, "info", message)

    def _debug(self, message: str) -> None:
        _log(self.ctx, "debug", message)

    def _warn(self, message: str) -> None:
        _log(self.ctx, "warning", message)


_LOG_LEVELS = cg_log.LOG_LEVELS
_MIN_LOG_LEVEL = cg_log.DEFAULT_MIN_LEVEL


def _set_log_level(name: Any) -> None:
    """按配置里的 debug.log_level 设置本插件自己的日志阈值。

    阈值存在 ``cg_log`` 里，``cg_tracker`` / ``cg_energy_gate`` 共用同一份，
    这样 ``debug.log_level`` 对**所有** [companion-gap] 日志都生效。
    """

    global _MIN_LOG_LEVEL
    _MIN_LOG_LEVEL = cg_log.set_min_level(name)


def _log(ctx: Any, level: str, message: str) -> None:
    """安全写日志：日志代理缺失或抛异常都不能影响业务。"""

    try:
        logger = ctx.logger
    except Exception:  # noqa: BLE001
        return
    cg_log.log(logger, level, message)


def create_plugin() -> CompanionGapPlugin:
    """Runner 约定的插件工厂。"""

    return CompanionGapPlugin()
