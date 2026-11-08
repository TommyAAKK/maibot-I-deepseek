"""我，deepseek —— 会话跟踪与后台抽取调度。

分工：

- ``mark_dirty()`` 在 replyer hook 里被调用，**只做内存字典更新**，不碰
  磁盘、不发 RPC，耗时在微秒级。
- ``run_forever()`` 是 ``on_load`` 里起的后台任务，负责低频拉消息、调 LLM、
  写库。所有慢操作都在这里，不在回复热路径上。
- 抽取节奏由 ``min_new_messages`` / ``max_wait_seconds`` /
  ``min_interval_seconds`` 三个参数共同决定：消息攒够了立刻抽，攒不够但
  等太久了也抽一次，同时保证两次抽取之间至少隔 ``min_interval_seconds``。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import cg_log
from .cg_avoidance import AvoidanceProbe
from .cg_backfill import (
    default_since,
    describe_result,
    fetch_friend_history,
    fetch_group_history,
    merge_messages,
    should_backfill,
)
from .cg_extract import format_transcript, frame_self_lines, run_extraction
from .cg_guard import system_text_reason
from .cg_history import (
    describe_span,
    message_stamp,
    message_text,
    split_window,
    summarize_hole,
    to_history_rows,
)
from .cg_presence import offline_spans
from .cg_person import (
    accumulate_impact,
    affection_from_axes,
    apply_social_bias,
    as_dicts,
    axes_of,
    decay_axes,
    intimacy_levels_from_config,
    is_manual,
    merge_observation,
    normalize_role,
    person_key,
    roles_from_config,
)
from .cg_prefs import merge_pref, normalize_topic, prefs_key
from .cg_store import GapStore, similarity
from .cg_thought import ttl_seconds

#: 单次抽取失败后的退避时间（秒），避免模型报错时每 tick 都重试。
FAILURE_BACKOFF_SECONDS = 120.0

#: 每个会话在内存里保留多少条入站正文。当场信号只看最近 6~8 条，
#: 留 40 条是给「多挂几个不同窗口」留的余量，也防着将来有人把窗口调大。
_INCOMING_KEEP = 40

#: 入站正文的有效期（秒）。超过这么久没新消息就当成对话断了——
#: 昨天连问三遍和现在连问三遍是两件事。
_INCOMING_WINDOW_SECONDS = 900.0

#: 情绪状态落盘的最小间隔（秒）。情绪在内存里每次消息都在变，但没必要
#: 每条消息都写一次磁盘——崩了最多丢这么多秒的漂移，而衰减本来就是连续的。
MOOD_PERSIST_INTERVAL_SECONDS = 60.0

#: 「机器人自己是哪个账号」的重新学习间隔（秒）。
#:
#: 为什么要定期重学：机器人换号之后（换小号、重登另一个号），旧号还在
#: ``_self_ids`` 里，新号却不知道 —— 于是新号又会被当成陌生人记进人物表。
#: 会话级标记保证每个群都学一次，这个时间兜底保证换号最多延迟这么久跟上。
SELF_ID_REFRESH_SECONDS = 6 * 3600.0

#: 情绪历史保留条数上限。
MOOD_LOG_KEEP = 2000

#: 「最近被 @ / 被提到」的时间窗（秒）。
#:
#: 自主发言闸门用它判断「这一轮不许压」。取 180 秒是因为：宿主攒消息的
#: 节奏（``talk_value = 0.25`` 时约 4 条一轮）通常在半分钟内，加上退避和
#: 排队，一分钟左右；3 分钟足够覆盖「@ 之后这一轮」而不至于把很久以前的
#: @ 也算进来。
ATTENTION_WINDOW_SECONDS = 180.0


@dataclass(slots=True)
class DirtySession:
    """一个待抽取会话的内存状态。"""

    session_id: str
    platform: str = ""
    chat_type: str = ""
    group_id: str = ""
    group_name: str = ""
    user_id: str = ""
    user_nickname: str = ""
    pending: int = 0
    first_pending_ts: float = 0.0
    last_seen_ts: float = 0.0
    last_attempt_ts: float = 0.0
    #: 最近一条「@ 她」的消息到达时间。自主发言闸门用它判断「这一轮必须回」。
    #:
    #: 宿主把 @ 强制触发的状态藏在 ``runtime._forced_turn_*`` 里，没有任何
    #: 能力能把它读出来，所以只能自己记。用时间戳而不是布尔标记：布尔标记
    #: 一旦 Planner 那轮没跑起来（被退避挡了）就会永远挂着，导致之后某一轮
    #: 莫名其妙被当成「被 @ 了」。
    at_ts: float = 0.0
    #: 最近一条「提到她名字」的消息到达时间，同上。
    mentioned_ts: float = 0.0
    #: 最近的入站消息正文（按时间升序，滚动保留）。
    #:
    #: 当场信号要判「同一件事问了几遍」「连着几条在敷衍」「近几轮全是问句」，
    #: 这三件事都只看最近几分钟，没有必要往库里写——它们也**不该**进记忆。
    #: 数据只在内存里，进程重启就没了，那正是想要的。
    incoming: list[str] = field(default_factory=list)


class SessionTracker:
    """跟踪脏会话并调度抽取。"""

    def __init__(
        self,
        *,
        ctx: Any,
        store: GapStore,
        config_getter: Callable[[], Any],
        probe: AvoidanceProbe,
        logger: Any,
        log_extract_raw: bool = False,
        mood: Any = None,
        gate: Any = None,
        online_windows_getter: Callable[[], list[Any]] | None = None,
    ) -> None:
        self._ctx = ctx
        self._store = store
        self._config = config_getter
        self._probe = probe
        self._logger = logger
        self._log_extract_raw = log_extract_raw
        #: ``MoodEngine``（可为 None，方便单测只测记忆部分）。
        self._mood = mood
        #: ``EnergyGate``（可为 None）。精力门控跟着后台 tick 走，而不是跟着
        #: replyer hook —— 因为精力耗尽后 Bot 不再回复，那个 hook 根本不会触发，
        #: 门控就永远等不到恢复的时机。
        self._gate = gate
        #: 读「在线区间」的回调（由 ``plugin.py`` 提供，读的是它那份内存缓存）。
        #:
        #: 为什么要回调而不是自己查：在线时段是宿主 ``online_time`` 表的
        #: 8600 行聚合，插件侧已经有一个后台任务每 10 分钟刷一次，这里再查
        #: 一遍纯属重复；更要紧的是**两边必须用同一份数据**——否则「补抓算出的
        #: 离线区间」和「注入时算的缺席感」会对不上，同一段时间一边说「她那会儿
        #: 不在」一边说「她一直在等」。
        self._online_windows_getter = online_windows_getter
        #: 机器人自身的账号 ID 集合。从「全量消息 vs filter_mai 消息」的差集
        #: 里学习得到，再并上配置 ``[people] self_identities``。用途只有一个：
        #: **不把她自己当成聊天里的「别人」记进人物表**。
        self._self_ids: set[str] = set()
        #: 已经学过「机器人账号」的会话（会话级各学一次）。
        self._self_id_sessions: set[str] = set()
        #: 上次学习时刻，用于定期重学（换号场景）。
        self._self_id_learned_at: float = 0.0
        self._dirty: dict[str, DirtySession] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._extract_locks: dict[str, asyncio.Lock] = {}
        self._last_mood_persist_ts = 0.0
        #: 每个目标人物身上**已经施加过**的第三方偏差（``trust``/``closeness``）。
        #: 重算时要先减掉它再加新的，否则同一条旧关系边会在每次抽取时被重复
        #: 计入，越聊越偏。只在内存里，重启后从当前六维重新开始算——最多多算
        #: 一次，不会累积错误。
        self._social_applied: dict[str, dict[str, float]] = {}

    # ── 热路径：只改内存 ──

    def mark_dirty(self, message: Any) -> None:
        """记录一条新消息。由 hook 调用，必须快且不抛异常。"""

        if not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return

        config = self._config()
        capture = getattr(config, "capture", None)
        if capture is not None and not getattr(capture, "enabled", True):
            return

        info = message.get("message_info")
        group_info = info.get("group_info") if isinstance(info, dict) else None
        user_info = info.get("user_info") if isinstance(info, dict) else None
        is_group = isinstance(group_info, dict) and bool(group_info.get("group_id"))

        if is_group:
            if capture is not None and not getattr(capture, "capture_groups", True):
                return
        else:
            if capture is not None and not getattr(capture, "capture_private", True):
                return

        now = time.time()
        entry = self._dirty.get(session_id)
        if entry is None:
            entry = DirtySession(session_id=session_id)
            self._dirty[session_id] = entry
            entry.first_pending_ts = now

        entry.pending += 1
        entry.last_seen_ts = now
        entry.platform = str(message.get("platform") or entry.platform or "")
        entry.chat_type = "group" if is_group else "private"
        # 记「这一轮有没有被点名」。宿主的强制触发状态（``_forced_turn_*``）
        # 没有任何能力能读出来，自主发言闸门要知道「这轮不许压」只能自己记。
        if bool(message.get("is_at")):
            entry.at_ts = now
        if bool(message.get("is_mentioned")):
            entry.mentioned_ts = now
        if isinstance(group_info, dict):
            entry.group_id = str(group_info.get("group_id") or entry.group_id or "")
            entry.group_name = str(group_info.get("group_name") or entry.group_name or "")
        if isinstance(user_info, dict):
            entry.user_id = str(user_info.get("user_id") or entry.user_id or "")
            nickname = user_info.get("user_cardname") or user_info.get("user_nickname")
            entry.user_nickname = str(nickname or entry.user_nickname or "")

        # 入站正文留给当场信号用。**跳过她自己的消息**：判断「对方在敷衍她」
        # 用的必须是对方说的话，把她自己的回复混进窗口会立刻把连续敷衍
        # 打断成 0。她的账号从差集学出来之前学不到，那时候宁可多留几条。
        speaker = str((user_info or {}).get("user_id") or "") if isinstance(user_info, dict) else ""
        if speaker and speaker in self._self_ids:
            return
        text = message_text(message)
        if text:
            entry.incoming.append(text)
            if len(entry.incoming) > _INCOMING_KEEP:
                del entry.incoming[: -_INCOMING_KEEP]

    # ── 后台任务 ──

    def recent_attention(
        self,
        session_id: str,
        *,
        within: float = ATTENTION_WINDOW_SECONDS,
        now: float | None = None,
    ) -> tuple[bool, bool]:
        """这个会话最近``within``秒内有没有被 @ / 被提到。

        自主发言闸门用这个判断「这一轮不许压」。**用时间窗而不是一次性标记**：
        标记一旦被设置就得有人负责清掉，而 Planner 那轮可能根本没跑起来
        （被退避挡了、被 focus 槽位挡了），那样标记会一直挂着，后面某一轮
        就会莫名其妙地被当成「被 @ 了」。

        Returns:
            ``(被@过, 被提到过)``。
        """

        entry = self._dirty.get(str(session_id or "").strip())
        if entry is None:
            return False, False
        current = time.time() if now is None else float(now)
        window = max(1.0, float(within))
        at_recent = entry.at_ts > 0 and current - entry.at_ts <= window
        mentioned_recent = entry.mentioned_ts > 0 and current - entry.mentioned_ts <= window
        return at_recent, mentioned_recent

    def recent_incoming(
        self,
        session_id: str,
        *,
        limit: int = 12,
        now: float | None = None,
        within: float = _INCOMING_WINDOW_SECONDS,
    ) -> list[str]:
        """这个会话最近对方说的几句话（升序）。

        给当场信号用。超过 ``within`` 秒没有新消息就当成「对话断了」返回空：
        昨天连问三遍和现在连问三遍是两件事，把中间隔了一夜的记录也算进来，
        她会对着一个已经换了话题的人发火。

        Returns:
            正文列表，可能为空。
        """

        entry = self._dirty.get(str(session_id or "").strip())
        if entry is None:
            return []
        current = time.time() if now is None else float(now)
        if entry.last_seen_ts > 0 and current - entry.last_seen_ts > float(within):
            return []
        keep = max(1, int(limit))
        return list(entry.incoming[-keep:])

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="companion_gap_tracker")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭阶段不抛
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            config = self._config()
            tick = float(getattr(getattr(config, "capture", None), "refresh_tick_seconds", 30.0) or 30.0)
            tick = min(300.0, max(5.0, tick))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不能因为一次异常退出
                self._warn(f"tracker tick 失败: {exc!r}")

    async def tick(self) -> None:
        """跑一轮：探测避让配置、清理过期、按需抽取。"""

        config = self._config()
        capture = getattr(config, "capture", None)
        extract_cfg = getattr(config, "extract", None)
        retention = getattr(config, "retention", None)

        await self._probe.probe()

        try:
            expired = self._store.expire_stale()
            if expired:
                self._info(f"清理过期条目 {expired} 条")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"expire_stale 失败: {exc!r}")

        # 念头的过期与失效。和记忆分开走，因为它有两条独立的死法：
        # ① 到期（TTL 按显著度算出来的那个时间点）；
        # ② **来源没了**——念头引用的是记忆条目，那条记忆被遗忘或删掉之后，
        #    这个念头就不再有任何现实依据，留着只会变成凭空的臆测。
        #    这是「念头不能升级成事实」那条约束的兜底执行点。
        try:
            gone = self._store.expire_thoughts()
            orphan = self._store.prune_orphan_thoughts()
            if gone or orphan:
                self._debug(f"念头过期 {gone} 条，因来源失效作废 {orphan} 条")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"expire_thoughts / prune_orphan_thoughts 失败: {exc!r}")

        # 衰减与遗忘：闲置越久显著度越低，淡到阈值以下就转 forgotten。
        # 放在 expire_stale 之后，因为两者互不依赖，但「过期」比「遗忘」更
        # 确定（有明确到期时间），先处理它能让日志读起来更顺。
        await self._decay_items(retention)

        # 情绪落盘与历史裁剪跟抽取开关无关：即使关了抽取，情绪也该继续存。
        await self._persist_mood_if_due()

        # 关系六维的淡化同理：与抽取无关，纯时间驱动。
        self._decay_people_axes()

        # 精力门控同样与抽取无关：耗尽要立刻压住频率，恢复了要立刻放开。
        await self._sync_energy_gate()

        if capture is not None and not getattr(capture, "enabled", True):
            return
        if extract_cfg is not None and not getattr(extract_cfg, "enabled", True):
            return

        now = time.time()
        due = [entry for entry in list(self._dirty.values()) if self._is_due(entry, capture, now)]
        for entry in due:
            await self._extract_session(entry)

        # 同步内存状态到 gap_state，方便管理命令查看
        for entry in list(self._dirty.values()):
            try:
                self._store.upsert_state(
                    entry.session_id,
                    platform=entry.platform,
                    chat_type=entry.chat_type,
                    group_id=entry.group_id,
                    group_name=entry.group_name,
                    user_id=entry.user_id,
                    user_nickname=entry.user_nickname,
                    last_seen_ts=entry.last_seen_ts,
                    pending_count=entry.pending,
                )
            except Exception as exc:  # noqa: BLE001
                self._warn(f"upsert_state 失败: {exc!r}")
        _ = retention

    def _decay_people_axes(self) -> None:
        """让久未联系的人六维往中性回落。

        每次 tick 都跑一遍 SQL 全表扫，人物数量是几十到几百的量级，代价可以
        忽略；而如果只在启动时跑，长期开着的进程就永远不会淡化。
        """

        people_cfg = getattr(self._config(), "people", None)
        if people_cfg is None or not bool(getattr(people_cfg, "enabled", True)):
            return
        try:
            half_life = float(getattr(people_cfg, "axis_half_life_days", 90.0) or 90.0)
            changed = self._store.decay_person_axes(half_life_days=half_life)
            if changed:
                self._debug(f"关系淡化 {changed} 人")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"decay_person_axes 失败: {exc!r}")

    async def _decay_items(self, retention: Any) -> None:
        """按配置衰减显著度，把淡掉的条目标记为遗忘。

        为什么单独一个方法而不是塞进 ``tick``：这一段有五个配置项、
        两个计数、一堆异常路径，塞进 ``tick`` 会把那个函数撑得读不下去。
        """

        if retention is None or not bool(getattr(retention, "decay_enabled", True)):
            return
        half_life = float(getattr(retention, "decay_half_life_days", 45.0) or 0.0)
        if half_life <= 0:
            return
        try:
            decayed, forgotten = self._store.decay_salience(
                half_life_days=half_life,
                forget_below=float(getattr(retention, "forget_below", 0.12) or 0.0),
                forget_after_days=float(getattr(retention, "forget_after_days", 180.0) or 0.0),
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"记忆衰减失败: {exc!r}")
            return
        # 顺手断掉过期的连击：安静够久的条目再出现时该算「重新提起」，
        # 而不是接着之前的 streak 继续 +1（否则它会一路被打入冷宫）。
        try:
            reset = self._store.reset_inject_streaks()
        except Exception as exc:  # noqa: BLE001
            reset = 0
            self._warn(f"连击重置失败: {exc!r}")
        if decayed or forgotten or reset:
            self._debug(
                f"记忆衰减：{decayed} 条降低显著度，{forgotten} 条转入遗忘"
                + (f"，{reset} 条连击清零" if reset else "")
            )

    def _is_due(self, entry: DirtySession, capture: Any, now: float) -> bool:
        if entry.pending <= 0:
            return False
        min_new = int(getattr(capture, "min_new_messages", 8) or 8)
        max_wait = float(getattr(capture, "max_wait_seconds", 900.0) or 900.0)
        min_interval = float(getattr(capture, "min_interval_seconds", 180.0) or 180.0)

        if now - entry.last_attempt_ts < max(0.0, min_interval):
            return False
        if entry.pending >= max(1, min_new):
            return True
        waited = now - (entry.first_pending_ts or now)
        return waited >= max(0.0, max_wait)

    async def _persist_mood_if_due(self) -> None:
        """按间隔把情绪落盘，并顺手裁剪历史。

        情绪在内存里被每条消息微调，但落盘不必那么频繁：崩了最多丢掉
        这个间隔内的漂移，而衰减本身是连续的，恢复时按 ``updated_at``
        重新算一遍就能补回来。
        """

        mood = self._mood
        if mood is None:
            return
        now = time.time()
        if now - self._last_mood_persist_ts < MOOD_PERSIST_INTERVAL_SECONDS:
            return
        self._last_mood_persist_ts = now
        try:
            mood.persist(self._store, now=now)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"情绪落盘失败: {exc!r}")
            return
        try:
            self._store.prune_mood_log(keep=MOOD_LOG_KEEP)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"裁剪情绪历史失败: {exc!r}")

    async def _sync_energy_gate(self) -> None:
        """把精力耗尽状态同步到宿主的发言频率。

        为什么放在后台 tick 而不是 replyer hook：精力耗尽后 Bot 不再回复，
        replyer hook 根本不会触发，门控就永远等不到「恢复」的时机。后台
        tick 与「Bot 说不说话」无关，才能保证恢复时一定被看到。
        """

        gate = self._gate
        mood = self._mood
        if gate is None or mood is None:
            return
        cfg = self._cfg()
        mood_cfg = getattr(cfg, "mood", None)
        if mood_cfg is not None and not bool(getattr(mood_cfg, "gate_enabled", True)):
            return
        if not mood.enabled:
            return

        now = time.time()
        exhausted = not mood.can_initiate(now=now)
        # 精力只在**耗尽**这一档才动宿主的发言频率。用户明确要求精力影响的是
        # 「对当前话题的回复欲望和本次回复字数」，按比例压频率会让 Bot 时灵时不灵，
        # 看着像掉线而不是像累了。所以 voice 现在只有 1.0 / 0.0 两档。
        try:
            voice = float(mood.voice_level(now=now))
        except Exception:  # noqa: BLE001 - 老版本没有这个方法就退回一刀切
            voice = 0.0 if exhausted else 1.0
        floor = float(getattr(mood_cfg, "gate_floor", 0.05) or 0.05) if mood_cfg is not None else 0.05
        intent = str(getattr(mood_cfg, "farewell_intent", "") or "") if mood_cfg is not None else ""
        # 配置里 farewell_intent 留空 = 用户不要这句告别语（配置说明写的就是这个），
        # 不能用「空串回退默认文案」的写法，否则用户根本关不掉。
        farewell = bool(intent.strip())

        # 需要同步的会话 = 正在被压着的（要恢复）+ 当前活跃的（可能要压）。
        sessions = set(gate.tracked_sessions())
        sessions.update(self._dirty.keys())
        for session_id in sessions:
            try:
                await gate.sync(
                    session_id,
                    voice=voice,
                    exhausted=exhausted,
                    floor=floor,
                    store=self._store,
                    now=now,
                    intent=intent,
                    farewell=farewell,
                )
            except Exception as exc:  # noqa: BLE001 - 门控失败不影响抽取
                self._warn(f"同步发言频率失败（{session_id}）: {exc!r}")

    def _cfg(self) -> Any:
        try:
            return self._config()
        except Exception:  # noqa: BLE001
            return None

    async def _extract_session(self, entry: DirtySession) -> None:
        lock = self._extract_locks.setdefault(entry.session_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            entry.last_attempt_ts = time.time()
            try:
                await self._do_extract(entry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                entry.last_attempt_ts = time.time() + FAILURE_BACKOFF_SECONDS
                self._warn(f"抽取会话 {entry.session_id} 失败: {exc!r}")

    async def _do_extract(self, entry: DirtySession) -> None:
        config = self._config()
        capture = getattr(config, "capture", None)
        extract_cfg = getattr(config, "extract", None)
        if capture is None or extract_cfg is None:
            return

        recent_limit = max(1, int(getattr(capture, "recent_limit", 60) or 60))
        recent_hours = max(0.1, float(getattr(capture, "recent_hours", 12.0) or 12.0))
        messages = await self._fetch_recent(entry.session_id, recent_hours, recent_limit)

        # 补抓关机期间的历史。宿主只在收到消息那一刻写库，所以她那几天不在的
        # 聊天根本没进库（真机实测：8 段离线区间内消息数全为 0）。QQ 客户端
        # 本地缓存里有，实测能往前翻 9~10 天。
        #
        # 只在「宿主库里的消息不够」时补：补抓要走一次 WebSocket 往返，
        # 而且补回来的东西和已有的要按 message_id 去重合并——不是免费的。
        backfilled = await self._backfill(entry, host_messages=messages)
        if backfilled:
            messages = merge_messages(messages, backfilled)

        if not messages:
            entry.pending = 0
            entry.first_pending_ts = 0.0
            return

        max_msgs = max(1, int(getattr(capture, "max_messages_per_extract", 30) or 30))

        # 把「她要读的那 30 条」和「她错过的那些」拆开。
        #
        # 用户第 41 轮的原话：「正常人虽然会翻看历史消息，但仅限于几十条的程度，
        # 对于 99+ 这种，一般不会从头看完。而 bot 现在抓取会直接全都看完，就失去
        # 了离线的感觉，反而会产生一种『我一直看着，但我不说话』的监控感。」
        #
        # 事实是她从来没「全都看完」——`window` 一直是最后 30 条。真正的问题在
        # 补抓那条路上：补回来的 200 条里，前 170 条**被直接丢掉了**，于是她用完
        # 最后 30 条之后，像从头到尾都在场一样说话。既不是监控感，也不是如实缺席，
        # 而是最糟的第三种：假装没缺过席。
        #
        # 现在把剩下的部分记成「洞」（只存条数和时间范围，不存内容）+ 原文入库
        # （供她被人明确提起时翻看）。她因此知道自己错过了什么，也只能诚实地说
        # 「那会儿我不在」。
        _state = self._store.get_state(entry.session_id) or {}
        _last_extract = float(_state.get("last_extract_ts") or 0.0)
        offline = self._offline_spans(messages, since_ts=_last_extract)
        window, hole = split_window(
            messages,
            max_msgs,
            since_ts=_last_extract,
            offline_spans=offline,
        )
        if hole:
            await self._record_hole(entry, hole)

        max_chars = int(getattr(capture, "max_session_chars", 6000) or 6000)
        transcript = format_transcript(window, max_chars=max_chars)
        # 把「她自己说的那几行」标成「你」。
        #
        # 不这么做的话，机器人自己的消息在模型眼里跟别人一模一样
        # （`小鱼(100000002): ...`），而 people 段落要求「判断聊天记录里
        # 出现的人」—— 模型会把她自己当成一个陌生人，于是人物表里多出一条
        # `qq:100000002 / 小鱼`。真机上就是这么来的。
        #
        # 不直接丢掉这些行：`self` 类记忆（「你答应过少刷 B 站」）正是从
        # 她自己说过的话里抽的。
        transcript = frame_self_lines(transcript, self._self_account_ids(entry))
        if not transcript.strip():
            entry.pending = 0
            entry.first_pending_ts = 0.0
            return

        categories = tuple(getattr(extract_cfg, "categories", ()) or ())
        max_items = max(1, int(getattr(extract_cfg, "max_items_per_extract", 12) or 12))
        max_content = max(8, int(getattr(extract_cfg, "max_content_chars", 60) or 60))

        existing_open_loops = [
            str(row.get("content") or "")
            for row in self._store.list_by_category(entry.session_id, "open_loop", limit=20)
        ]

        # 情绪判断搭车在这次抽取里，不额外花钱调模型。
        mood_enabled = self._mood_enabled()
        mood_now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 人物观察同样搭车。已经认识的人按 ``user_id 昵称`` 列出来，
        # 让模型沿用同一套 ID，避免同一个群友在库里分裂成好几个人。
        people_enabled = self._people_enabled()
        known_people = self._known_people() if people_enabled else ()
        # 身份表来自配置页（用户第 33 轮要求身份可自由配置），要同时喂给
        # 提示词（让模型知道有哪些可选身份）和解析器（只认这些身份）。
        people_cfg = getattr(self._config(), "people", None)
        role_table = roles_from_config(getattr(people_cfg, "roles", None))
        role_hints = [(item["key"], item["label"]) for item in role_table]

        # 喜好也搭车：给模型看已有的口味，它只会补充或调整，不会重复。
        prefs_enabled = self._prefs_enabled()
        known_prefs = self._known_prefs() if prefs_enabled else ()

        # 留念也搭车：同一份聊天记录，多问一句「有没有什么没用的东西想留着」。
        keepsake_enabled = self._keepsake_enabled()

        result = await run_extraction(
            self._ctx.llm,
            transcript=transcript,
            categories=categories,
            max_items=max_items,
            max_content_chars=max_content,
            task_name=str(getattr(extract_cfg, "task_name", "utils") or "utils"),
            model_name=str(getattr(extract_cfg, "model_name", "") or ""),
            temperature=getattr(extract_cfg, "temperature", 0.2),
            max_tokens=getattr(extract_cfg, "max_tokens", 0),
            rpc_timeout_ms=int(getattr(extract_cfg, "rpc_timeout_ms", 150000) or 150000),
            existing_open_loops=existing_open_loops,
            mood_enabled=mood_enabled,
            mood_now=mood_now,
            people_enabled=people_enabled,
            known_people=known_people,
            prefs_enabled=prefs_enabled,
            known_prefs=known_prefs,
            role_hints=role_hints,
            roles=role_table,
            keepsake_enabled=keepsake_enabled,
        )

        # 空回复在小模型上是高频瞬时问题（宿主自己也会重试），这里补一次重试。
        attempts = 1 + max(0, int(getattr(extract_cfg, "empty_response_retries", 1) or 0))
        attempt_no = 1
        while attempt_no < attempts and _is_empty_response_failure(result):
            self._info(
                f"会话 {entry.session_id} 第 {attempt_no} 次抽取遇到空回复，重试一次"
            )
            attempt_no += 1
            result = await run_extraction(
                self._ctx.llm,
                transcript=transcript,
                categories=categories,
                max_items=max_items,
                max_content_chars=max_content,
                task_name=str(getattr(extract_cfg, "task_name", "utils") or "utils"),
                model_name=str(getattr(extract_cfg, "model_name", "") or ""),
                temperature=getattr(extract_cfg, "temperature", 0.2),
                max_tokens=getattr(extract_cfg, "max_tokens", 0),
                rpc_timeout_ms=int(getattr(extract_cfg, "rpc_timeout_ms", 150000) or 150000),
                existing_open_loops=existing_open_loops,
                mood_enabled=mood_enabled,
                mood_now=mood_now,
                people_enabled=people_enabled,
                known_people=known_people,
                prefs_enabled=prefs_enabled,
                known_prefs=known_prefs,
                role_hints=role_hints,
                roles=role_table,
                keepsake_enabled=keepsake_enabled,
            )

        written = 0
        resolved_count = 0
        people_written = 0
        prefs_written = 0
        thoughts_written = 0
        keepsakes_written = 0
        if result.ok:
            resolved_count = self._apply_resolved(entry.session_id, result.resolved)
            written = self._apply_items(entry.session_id, result.items)
            self._apply_mood(result.mood)
            if people_enabled:
                people_written = self._apply_people(entry, result.people)
            if prefs_enabled:
                prefs_written = self._apply_prefs(result.prefs)
            thoughts_written = self._apply_thoughts(entry.session_id, result.thoughts)
            keepsakes_written = self._apply_keepsakes(entry.session_id, result.keepsakes)

        try:
            self._store.record_extraction(
                entry.session_id,
                model=result.model,
                ok=result.ok,
                item_count=written,
                resolved_count=resolved_count,
                prompt_chars=result.prompt_chars,
                response_chars=result.response_chars,
                error=result.error,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"record_extraction 失败: {exc!r}")

        if not result.ok:
            self._warn(f"会话 {entry.session_id} 抽取未成功: {result.error}")
        else:
            people_note = f", 人物 {people_written} 条" if people_written else ""
            thought_note = f", 念头 {thoughts_written} 条" if thoughts_written else ""
            keep_note = f", 留念 {keepsakes_written} 件" if keepsakes_written else ""
            self._info(
                f"会话 {entry.session_id} 抽取完成: 新增/合并 {written} 条, "
                f"解决 {resolved_count} 条{people_note}{thought_note}{keep_note}, "
                f"模型={result.model or '?'}"
            )
        if self._log_extract_raw and result.raw_response:
            self._debug(f"会话 {entry.session_id} 模型原始返回:\n{result.raw_response[:2000]}")

        entry.pending = 0
        entry.first_pending_ts = 0.0
        entry.last_attempt_ts = time.time()
        try:
            self._store.upsert_state(
                entry.session_id,
                platform=entry.platform,
                chat_type=entry.chat_type,
                group_id=entry.group_id,
                group_name=entry.group_name,
                user_id=entry.user_id,
                user_nickname=entry.user_nickname,
                last_seen_ts=entry.last_seen_ts,
                last_extract_ts=entry.last_attempt_ts,
                pending_count=0,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"upsert_state 失败: {exc!r}")

    def _mood_enabled(self) -> bool:
        mood = self._mood
        if mood is None:
            return False
        try:
            return bool(mood.enabled)
        except Exception:  # noqa: BLE001 - 配置读失败不该拖垮抽取
            return False

    def _apply_mood(self, payload: dict[str, Any]) -> None:
        """把模型给出的情绪评估并入当前状态。"""

        mood = self._mood
        if mood is None or not payload:
            return
        try:
            state = mood.apply_llm(payload)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"应用模型情绪失败: {exc!r}")
            return
        try:
            from .cg_mood import EMOTIONS

            self._store.record_mood(
                valence=state.valence,
                arousal=state.arousal,
                energy=state.energy,
                exhausted=bool(getattr(state, "exhausted", False)),
                label=state.label,
                source="llm",
                reason=state.reason,
                **{name: getattr(state, name, 0.0) for name in EMOTIONS},
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"记录情绪历史失败: {exc!r}")
        self._debug(f"情绪更新（模型）: {mood.describe()}")

    def _people_enabled(self) -> bool:
        people = getattr(self._config(), "people", None)
        if people is None:
            return False
        return bool(getattr(people, "enabled", False))

    def _known_people(self) -> list[str]:
        """列出已经认识的人，让模型沿用同一套 user_id。"""

        people_cfg = getattr(self._config(), "people", None)
        limit = int(getattr(people_cfg, "list_limit", 50) or 50)
        try:
            rows = self._store.list_people(limit=max(1, min(limit, 40)))
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取人物列表失败: {exc!r}")
            return []
        lines: list[str] = []
        for row in rows:
            user_id = str(row.get("user_id") or "").strip()
            if not user_id:
                continue
            name = str(row.get("display_name") or "").strip() or "(未命名)"
            role = str(row.get("relation_role") or "").strip()
            source = str(row.get("role_source") or "")
            marker = "（你手动确认过）" if source == "manual" else ""
            role_text = f"，{role}" if role and role != "stranger" else ""
            lines.append(f"- {user_id} {name}{role_text}{marker}")
        return lines

    def _apply_people(self, entry: DirtySession, observations: list[dict[str, Any]]) -> int:
        """把模型观察到的人物线索并入人物表。

        可信度闸门在 ``cg_person.merge_observation`` 里：低置信判断不会改写
        角色，手动配置的行永远不会被自动结果覆盖（除非配置里显式打开）。

        关系六维（用户第 34 轮）在这里落地：模型给的是**变化量**，叠加到
        已有六维上，再用 ``affection_from_axes`` 重算好感度摘要。第三方影响
        （``people[i].links``）单独走 ``_apply_person_links``。
        """

        if not observations:
            return 0
        people_cfg = getattr(self._config(), "people", None)
        min_confidence = float(getattr(people_cfg, "auto_role_min_confidence", 0.7) or 0.7)
        manual_override = bool(getattr(people_cfg, "manual_override", False))
        track_impact = bool(getattr(people_cfg, "track_impact", True))
        impact_decay = float(getattr(people_cfg, "impact_decay", 0.95) or 0.95)
        # 身份表要传给合并逻辑：用户自定义的身份（比如「老师」）只存在于配置里，
        # 不传的话会被 normalize_role 当成非法值清空。
        role_table = roles_from_config(getattr(people_cfg, "roles", None))
        platform = str(entry.platform or "")
        now = time.time()

        # **不许把自己记成人**。提示词已经明说「『你』自己不算出现的人」，
        # 但模型偶尔还是会写进来（真机上出现过 qq:100000002 / 小鱼，
        # 好感度 5.3、六维全基线 —— 她把自己当成了路人群友）。
        # 这是第二道防线：无论提示词怎么写，落到库之前再挡一次。
        self_ids = self._self_account_ids(entry)
        observations = [
            item for item in observations
            if str(item.get("user_id") or "").strip() not in self_ids
        ]
        if not observations:
            return 0

        written = 0
        for observation in observations:
            try:
                confidence = float(observation.get("role_confidence") or 0.0)
                if confidence < min_confidence:
                    # 置信度不够就抹掉角色，只保留备注。
                    observation = dict(observation)
                    observation["relation_role"] = ""
                key = person_key(platform, observation.get("user_id") or "")
                if not key:
                    continue
                existing = self._store.get_person(key)
                if existing is not None and not manual_override and is_manual(existing):
                    # 手动配置优先：角色不动，只更新见过的时间和互动统计。
                    # 六维**例外** —— 手动配置的是「身份」这种事实，六维是
                    # 「感受」，感受本来就该跟着聊天走。
                    existing["last_seen_ts"] = now
                    existing["updated_at"] = now
                    if track_impact:
                        existing["emotional_impact"] = accumulate_impact(
                            existing.get("emotional_impact"),
                            observation.get("emotional_impact"),
                            decay=impact_decay,
                        )
                    merged = merge_observation(
                        existing, observation, now=now, roles=role_table
                    )
                    if merged is not None:
                        merged["person_key"] = key
                        merged["platform"] = platform
                        merged["user_id"] = str(observation.get("user_id") or "")
                        merged["last_seen_ts"] = now
                        merged["first_seen_ts"] = float(existing.get("first_seen_ts") or now)
                        self._store.upsert_person(merged, now=now)
                    else:
                        self._store.upsert_person(existing, now=now)
                    written += 1
                    continue

                merged = merge_observation(existing, observation, now=now, roles=role_table)
                if merged is None:
                    continue
                merged["person_key"] = key
                merged["platform"] = platform
                merged["user_id"] = str(observation.get("user_id") or "")
                merged["last_seen_ts"] = now
                if existing is not None:
                    merged["first_seen_ts"] = float(existing.get("first_seen_ts") or now)
                    if track_impact:
                        merged["emotional_impact"] = accumulate_impact(
                            existing.get("emotional_impact"),
                            observation.get("emotional_impact"),
                            decay=impact_decay,
                        )
                self._store.upsert_person(merged, now=now)
                written += 1
            except Exception as exc:  # noqa: BLE001 - 单条人物失败不该拖垮抽取
                self._warn(f"记录人物失败 ({observation.get('user_id')}): {exc!r}")
        if written:
            self._debug(f"人物观察更新 {written} 条")
        # 第三方影响放在最后：它要读刚写进去的人物六维（说话人的信任度），
        # 也要写目标人物的六维，先写后算才能拿到最新值。
        self._apply_person_links(entry, observations)
        return written

    def _apply_person_links(
        self, entry: DirtySession, observations: list[dict[str, Any]]
    ) -> int:
        """把「谁对第三个人表了什么态」写进关系网络，并让它在目标身上生效。

        用户第 34 轮说关系「还会受到其他人的影响」。这里是那条影响的两步：

        1. **落库**：``gap_person_links`` 记 (说话人 → 被说的人, 态度)。
        2. **传播**：重算目标人物的六维 —— 用 ``apply_social_bias`` 把关系边
           折算成对目标 ``trust`` / ``closeness`` 的推动。

        第 2 步是**重算**而不是累加，这是关键：如果每次抽取都把偏差加一遍，
        同一条旧关系边会被反复计入，越聊越偏。重算保证每条边只贡献一次，
        而且说话人态度变了（``attitude`` 走衰减平均）目标也会跟着变。
        """

        people_cfg = getattr(self._config(), "people", None)
        if not bool(getattr(people_cfg, "social_influence", True)):
            return 0
        weight = float(getattr(people_cfg, "social_weight", 0.25) or 0.0)
        cap = float(getattr(people_cfg, "social_cap", 0.20) or 0.0)
        if weight <= 0.0 or cap <= 0.0:
            return 0

        platform = str(entry.platform or "")
        now = time.time()
        written = 0
        for observation in observations:
            source_id = str(observation.get("user_id") or "").strip()
            links = observation.get("links") or []
            if not source_id or not links:
                continue
            source_key = person_key(platform, source_id)
            for link in links:
                try:
                    target_key = person_key(platform, link.get("target_user_id") or "")
                    if not target_key or target_key == source_key:
                        continue
                    self._store.upsert_person_link(
                        source_key,
                        target_key,
                        link.get("attitude"),
                        evidence=str(link.get("evidence") or ""),
                        now=now,
                    )
                    written += 1
                except Exception as exc:  # noqa: BLE001 - 单条关系边失败不该拖垮抽取
                    self._warn(f"记录关系边失败 ({link.get('target_user_id')}): {exc!r}")

        if written:
            self._recompute_social(platform, observations, weight=weight, cap=cap, now=now)
            self._debug(f"关系边更新 {written} 条")
        return written

    def _recompute_social(
        self,
        platform: str,
        observations: list[dict[str, Any]],
        *,
        weight: float,
        cap: float,
        now: float,
    ) -> None:
        """按关系边重算目标人物的六维（幂等：同一批边算几次结果一样）。"""

        targets: list[str] = []
        for observation in observations:
            for link in observation.get("links") or []:
                target_key = person_key(platform, link.get("target_user_id") or "")
                if target_key and target_key not in targets:
                    targets.append(target_key)

        for target_key in targets:
            try:
                person = self._store.get_person(target_key)
                if person is None:
                    # 目标还没被记过，没有可影响的六维，等它自己出现再说。
                    continue
                edges = self._store.list_person_links(target_key)
                if not edges:
                    continue
                # 每条边的「来源可信度」= 我对那个说话人的信任。陌生人说的话
                # 不该有多大分量 —— 这也是 social_bias 里 source_trust 的含义。
                enriched: list[dict[str, Any]] = []
                for edge in edges:
                    source_key = str(edge.get("source_key") or "")
                    source = self._store.get_person(source_key) if source_key else None
                    source_trust = axes_of(source).get("trust", 0.35) if source else 0.35
                    enriched.append(
                        {
                            "source_trust": source_trust,
                            "attitude": float(edge.get("attitude") or 0.0),
                        }
                    )
                axes = axes_of(person)
                # 先撤掉上一次的第三方影响再加新的：不然每轮都在旧偏差上叠，
                # 一条旧关系边会被反复计入，越聊越偏。
                base = dict(axes)
                previous = self._social_applied.get(target_key)
                if previous:
                    for key in ("trust", "closeness"):
                        base[key] = max(0.0, min(1.0, base[key] - previous.get(key, 0.0)))
                biased = apply_social_bias(base, enriched, weight=weight, cap=cap)
                self._social_applied[target_key] = {
                    key: biased[key] - base[key] for key in ("trust", "closeness")
                }
                person["axes"] = biased
                person["affection"] = affection_from_axes(biased)
                self._store.upsert_person(person, now=now)
            except Exception as exc:  # noqa: BLE001 - 传播失败不该拖垮抽取
                self._warn(f"第三方影响传播失败 ({target_key}): {exc!r}")

    def _prefs_enabled(self) -> bool:
        prefs = getattr(self._config(), "prefs", None)
        if prefs is None:
            return False
        return bool(getattr(prefs, "enabled", False))

    def _keepsake_enabled(self) -> bool:
        """抽取时要不要问一句「有没有什么没用的东西想留着」。

        它和 ``keepsake.enabled`` 不是同一件事：``enabled`` 管的是「能不能留」，
        这个 ``extract_hint`` 管的是「要不要主动去问她」。关掉提示仍然可以在
        WebUI 里手动加——但那样就永远不会自己收下任何东西了。
        """

        keep = getattr(self._config(), "keepsake", None)
        if keep is None:
            return False
        if not bool(getattr(keep, "enabled", True)):
            return False
        return bool(getattr(keep, "extract_hint", True))

    def _known_prefs(self) -> list[str]:
        """列出已有的口味，让模型只补充、不重复。"""

        prefs_cfg = getattr(self._config(), "prefs", None)
        limit = int(getattr(prefs_cfg, "list_limit", 20) or 20)
        try:
            rows = self._store.list_prefs(limit=max(1, min(limit, 20)))
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取喜好列表失败: {exc!r}")
            return []
        lines: list[str] = []
        for row in rows:
            kind = str(row.get("kind") or "")
            topic = str(row.get("topic") or "").strip()
            if not topic:
                continue
            label = "喜欢" if kind == "like" else "讨厌"
            lines.append(f"- {label} {topic}")
        return lines

    def _apply_prefs(self, observations: dict[str, list[str]]) -> int:
        """把模型观察到的口味并入喜好表。

        强度怎么定：模型只给话题不给强度（它没法可靠地量化「有多喜欢」），
        所以这里按**出现次数**累积——同一样东西在多次抽取里被反复提到，
        强度就会顺着 ``merge_pref`` 的加权平均慢慢爬上去。这比让模型
        瞎猜一个数字靠谱。
        """

        if not observations:
            return 0
        prefs_cfg = getattr(self._config(), "prefs", None)
        decay = float(getattr(prefs_cfg, "merge_decay", 0.85) or 0.85)
        max_prefs = int(getattr(prefs_cfg, "max_prefs", 20) or 20)
        now = time.time()

        written = 0
        for kind, topics in (("like", observations.get("likes") or []),
                             ("dislike", observations.get("dislikes") or [])):
            for raw_topic in topics:
                try:
                    topic = normalize_topic(raw_topic)
                    if not topic:
                        continue
                    key = prefs_key(kind, topic)
                    existing = self._store.get_pref(key)
                    merged = merge_pref(
                        existing,
                        {"kind": kind, "topic": topic, "strength": 0.6},
                        now=now,
                        decay=decay,
                    )
                    if not merged:
                        continue
                    self._store.upsert_pref(merged, now=now)
                    written += 1
                except Exception as exc:  # noqa: BLE001 - 单条口味失败不该拖垮抽取
                    self._warn(f"记录喜好失败 ({raw_topic}): {exc!r}")
        if written:
            try:
                self._store.trim_prefs(keep=max_prefs)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"裁剪喜好失败: {exc!r}")
            self._debug(f"喜好观察更新 {written} 条")
        return written

    def _apply_items(self, session_id: str, items: list[Any]) -> int:
        config = self._config()
        retention = getattr(config, "retention", None)
        now = time.time()
        written = 0
        for item in items:
            # 脏文本防御：别的插件会往消息的 processed_plain_text 后面追加
            # 说明文字（真机真出现过），抽取时会被当成聊天内容一起记下来。
            # 入库前就挡掉——写进去之后再靠注入端过滤，等于让脏数据占着
            # 显著度配额，还会被 WebUI 看到。
            dirty = system_text_reason(getattr(item, "content", ""))
            if dirty:
                self._warn(
                    f"丢弃疑似系统文本的条目（{dirty}，{item.category}）: "
                    f"{str(getattr(item, 'content', ''))[:120]}"
                )
                continue
            expires_at = _expiry_for(item.category, retention, now)
            try:
                self._store.upsert_item(
                    session_id,
                    item.category,
                    item.content,
                    salience=item.salience,
                    expires_at=expires_at,
                    truth_score=float(getattr(item, "truth", 1.0) or 1.0),
                    now=now,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                self._warn(f"upsert_item 失败 ({item.category}): {exc!r}")
        max_items = int(getattr(retention, "max_items_per_session", 120) or 120)
        try:
            self._store.trim_session(session_id, max_items, now=now)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"trim_session 失败: {exc!r}")
        return written

    def _apply_thoughts(self, session_id: str, thoughts: list[dict[str, Any]]) -> int:
        """把抽取出来的念头写进 ``gap_thoughts``。

        **只写念头表，绝不写进 ``gap_items``。** 这条是结构约束不是风格偏好：
        念头一旦能进记忆库，就会和「已知的事实」混在一起，然后按记忆的规则
        被注入、被引用、被当成依据——「我猜他不开心 → 我想关心他 → 我确实
        知道他不对劲」这个循环就是这么转起来的。它进不了记忆，就转不起来。

        TTL 按显著度映射（40 分钟 ~ 36 小时），所以「一时的联想」和「没聊完
        的事」自己会分层，不需要人手调。
        """

        config = self._config()
        thought_cfg = getattr(config, "thought", None)
        if thought_cfg is not None and not bool(getattr(thought_cfg, "enabled", True)):
            return 0
        if not thoughts:
            return 0

        now = time.time()
        written = 0
        for thought in thoughts:
            content = str(thought.get("content") or "").strip()
            source_text = str(thought.get("source") or "").strip()
            if not content or not source_text:
                # 没有来源的念头一律不要（cg_extract 那边已经丢过一遍，
                # 这里再挡一次——两道防线，因为这个约束太容易被后来的改动破坏）。
                continue
            dirty = system_text_reason(content)
            if dirty:
                self._warn(f"丢弃疑似系统文本的念头（{dirty}）: {content[:120]}")
                continue
            try:
                salience = float(thought.get("salience") or 0.5)
            except (TypeError, ValueError):
                salience = 0.5
            try:
                self._store.add_thought(
                    session_id,
                    kind=str(thought.get("kind") or "dissent"),
                    content=content,
                    source_kind="text",
                    source_id=0,
                    source_text=source_text,
                    salience=salience,
                    expires_at=now + ttl_seconds(salience),
                    now=now,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                self._warn(f"add_thought 失败: {exc!r}")

        try:
            self._store.trim_thoughts(session_id, keep=40)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"trim_thoughts 失败: {exc!r}")
        return written

    def _apply_keepsakes(self, session_id: str, keepsakes: list[dict[str, Any]]) -> int:
        """把抽取出来的留念写进 ``gap_keepsakes``。

        三条和别处不同的规则，都是刻意的：

        1. **不查来源。** 念头必须能指回一句原文，留念不需要——一样没用的东西
           不该被要求证明自己从哪来。
        2. **满了就不再收。** 不收也不删旧的：满了之后 ``is_full`` 为真，由
           ``_keepsake_overflow_note`` 让她自己想「要不要放掉一个」。系统永远
           不替她决定扔哪一条。
        3. **不参与衰减、不参与注入。** 这里只写表，没有任何地方会把它读进
           提示词（除非「主动」把它当成开口的由头）。
        """

        config = self._config()
        keep_cfg = getattr(config, "keepsake", None)
        if keep_cfg is not None and not bool(getattr(keep_cfg, "enabled", True)):
            return 0
        if not keepsakes:
            return 0

        cap = int(getattr(keep_cfg, "max_items", 12) or 12)
        now = time.time()
        written = 0
        for entry in keepsakes:
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            dirty = system_text_reason(content)
            if dirty:
                self._warn(f"丢弃疑似系统文本的留念（{dirty}）: {content[:120]}")
                continue
            try:
                if self._store.keep_count(session_id) >= max(1, cap):
                    self._debug(f"留念已满（{cap} 件），这一件先不收：{content[:60]}")
                    break
                self._store.add_keepsake(
                    session_id,
                    content=content,
                    kind=str(entry.get("kind") or "line"),
                    source_text="",
                    now=now,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                self._warn(f"add_keepsake 失败: {exc!r}")
        return written

    def _apply_resolved(self, session_id: str, resolved: list[str]) -> int:
        if not resolved:
            return 0
        rows = self._store.list_by_category(session_id, "open_loop", limit=50)
        if not rows:
            return 0
        matched: list[int] = []
        for text in resolved:
            best_id = None
            best_score = 0.0
            for row in rows:
                score = similarity(text, str(row.get("content") or ""))
                if score > best_score:
                    best_score = score
                    best_id = int(row["id"])
            if best_id is not None and best_score >= 0.45:
                matched.append(best_id)
        if not matched:
            return 0
        return self._store.resolve_items(matched)

    def _self_account_ids(self, entry: "DirtySession") -> set[str]:
        """机器人自己的账号 ID 集合（用于「别把自己记成人」）。

        来源有两条，取并集：

        1. **本进程观察到的**：``_fetch_recent`` 在拉消息时顺便比对
           「全量列表」与「``filter_mai=True`` 列表」的差集 —— 差集里的账号
           就是宿主持有、且被 filter 掉的机器人自己。这是唯一精确的来源，
           但它要求至少成功拉过一次消息。
        2. **配置里写的**：``[people] self_identities``，用户可以把机器人
           在那几个群的 QQ 号或昵称补进来兜底。

        为什么要两条：宿主没有把「机器人账号」暴露成插件能力
        （``capabilities/registry.py`` 里没有任何 account/self 相关项），
        而 ``is_bot_self()`` 属于宿主内部函数，第三方插件不能直接 import。
        所以只能靠差集观察 + 配置兜底。
        """

        ids: set[str] = set(self._self_ids)
        people_cfg = getattr(self._config(), "people", None)
        for raw in getattr(people_cfg, "self_identities", ()) or ():
            value = str(raw).strip()
            if value:
                ids.add(value)
        return ids

    def _remember_self_accounts(
        self, messages: list[dict[str, Any]], filtered: list[dict[str, Any]]
    ) -> None:
        """从「全量 vs filter_mai」的差集里学出机器人账号。

        宿主的 ``filter_mai=True`` **只返回非机器人消息**。所以：

        - ``messages``（``filter_mai=False``）= 全部，含她自己
        - ``filtered``（``filter_mai=True``）  = 只有别人

        真实发出、但不在 ``filtered`` 里的账号 = 机器人自己。

        ## 之前这里写反过，值得记下来

        第一版把两个参数的名字和差集方向都搞混了：用
        ``mine = filtered``、``others = messages``，算的又是 ``mine - others``。
        由于 ``filtered`` 恒为 ``messages`` 的子集（只是少了机器人那几条），
        ``filtered - messages`` **永远为空**，加上前置的
        ``len(filtered) >= len(messages)`` 判断也反了 —— 整套学习逻辑从未
        生效过一次。真机日志里一次「识别到机器人自身账号」都没有。

        这次修正后用显式参数名（``messages`` / ``filtered``）并在测试里
        覆盖「全量 3 条、过滤后 2 条，应学到第 3 条的账号」这个场景。

        Args:
            messages: ``filter_mai=False`` 的全量消息。
            filtered: ``filter_mai=True`` 的过滤后消息。

        Returns:
            None。学到的新账号会并进 ``self._self_ids``。
        """

        def _uids(items: list[dict[str, Any]]) -> set[str]:
            out: set[str] = set()
            for item in items:
                info = item.get("message_info")
                if isinstance(info, dict):
                    user = info.get("user_info")
                    if isinstance(user, dict):
                        uid = str(user.get("user_id") or "").strip()
                        if uid:
                            out.add(uid)
            return out

        everything = _uids(messages)
        others = _uids(filtered)
        # 过滤没起作用（数量相同）时学不到东西，但**不能**因此就认定
        # 「全量里所有人都是自己」—— 所以这个分支只跳过，不做推断。
        if len(filtered) >= len(messages):
            return
        learned = {uid for uid in everything if uid not in others}
        if learned:
            self._self_ids.update(learned)
            self._debug(f"识别到机器人自身账号: {sorted(learned)}")

    async def _fetch_recent(self, session_id: str, hours: float, limit: int) -> list[dict[str, Any]]:
        """拉取会话最近消息。

        直接走 ``call_capability`` 而不是 ``ctx.message.get_recent``，
        因为后者把 ``hours`` 写死成宿主默认的 24 小时，而我们需要按配置
        控制回看窗口。

        第一次拉某个会话时，会额外拉一份 ``filter_mai=True`` 做差集，
        用来学出「机器人自己是哪个账号」（见 ``_remember_self_accounts``）。
        学到之后就不再拉第二遍。
        """

        try:
            payload = await self._ctx.call_capability(
                "message.get_recent",
                chat_id=session_id,
                hours=hours,
                limit=limit,
                limit_mode="latest",
                filter_mai=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"拉取会话 {session_id} 消息失败: {exc!r}")
            return []
        messages: list[dict[str, Any]] = []
        if isinstance(payload, list):
            messages = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            raw = payload.get("messages")
            if isinstance(raw, list):
                messages = [item for item in raw if isinstance(item, dict)]

        # 学习「机器人自己是哪个账号」。
        #
        # **不能只在第一次学**。第一版的条件是 `not self._self_ids`，
        # 意思是「学到过就不再学」。这有个致命缺口：**换号之后就永远学不到
        # 新号了** —— 旧号还在集合里，新号永远进不来，于是新号又被当成
        # 陌生人记进人物表。机器人换号并不是罕见操作（换小号、换账号重登）。
        #
        # 现在的策略：
        # - 每个会话都有个「学过」标记，会话级各学一次；
        # - 外加一个时间兜底，``SELF_ID_REFRESH_SECONDS`` 之后重学一次，
        #   这样换号最多延迟一个周期就能跟上；
        # - 已学到的账号**只增不减**（旧号留着也无害：那个号本来就还是她自己
        #   的历史账号，历史消息里出现它仍应算作「你说的话」）。
        now = time.time()
        last = self._self_id_learned_at
        should_learn = (
            bool(messages)
            and (
                session_id not in self._self_id_sessions
                or now - last >= SELF_ID_REFRESH_SECONDS
            )
        )
        if should_learn:
            try:
                filtered = await self._ctx.call_capability(
                    "message.get_recent",
                    chat_id=session_id,
                    hours=hours,
                    limit=limit,
                    limit_mode="latest",
                    filter_mai=True,
                )
                others: list[dict[str, Any]] = []
                if isinstance(filtered, list):
                    others = [item for item in filtered if isinstance(item, dict)]
                elif isinstance(filtered, dict):
                    raw = filtered.get("messages")
                    if isinstance(raw, list):
                        others = [item for item in raw if isinstance(item, dict)]
                self._remember_self_accounts(messages, others)
                self._self_id_sessions.add(session_id)
                self._self_id_learned_at = now
            except Exception as exc:  # noqa: BLE001 - 学不到账号不影响抽取
                self._debug(f"学习机器人账号失败（不影响抽取）: {exc!r}")

        return messages

    def _offline_spans(
        self,
        messages: list[dict[str, Any]],
        *,
        since_ts: float,
    ) -> list[tuple[float, float]]:
        """算出「这批消息里，哪些时间她根本不在线」。

        这是补抓路径上「洞」判定的补丁。原来的判据只有一条：**超出读窗口的
        部分**。真机上那条判据从来没生效过——补抓回来 20 条，全塞进 30 条的
        窗口，洞是空的，``gap_unread`` 一直 0 行。于是她读完那 20 条就像从头
        到尾都在场，而其中一半是她关机期间别人说的话。

        现在改成看**时间戳**：落在她离线区间里的消息，不管在不在窗口内，都是
        她没看见的。读窗口也把这几条扣掉——告诉她「这 10 条你一条都没看到」
        又把它们塞进读窗口，等于自相矛盾。

        拿不到在线数据时返回空列表，行为与旧版完全一致。理由和
        :func:`cg_presence.offline_spans` 一样：**不知道 ≠ 不在**。
        """

        if not messages:
            return []
        getter = self._online_windows_getter
        if getter is None:
            return []
        try:
            windows = getter()
        except Exception as exc:  # noqa: BLE001 - 拿不到就退回旧判据
            self._debug(f"读取在线区间失败（洞判据退回旧算法）: {exc!r}")
            return []
        if not windows:
            return []

        stamps = [message_stamp(item) for item in messages]
        stamps = [value for value in stamps if value > 0]
        if not stamps:
            return []

        # 区间上界用现在：她可能到现在还没回来，那段也是「不在」。
        try:
            return offline_spans(
                windows,
                start_ts=min(stamps),
                end_ts=time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - 算不出来就退回旧判据
            self._debug(f"换算离线区间失败（洞判据退回旧算法）: {exc!r}")
            return []

    async def _backfill(
        self,
        entry: DirtySession,
        *,
        host_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """从 QQ 端补抓她没看见的那段历史；返回补到的消息（可能为空）。

        三个设计要点：

        - **只在「宿主库里的消息装不满"最近一段"」时才补。** 判据是
          「最早那条宿主消息是不是比上次抽取时间还靠后」——如果宿主库已经
          覆盖到上次抽取，中间没有洞，补了也是重复。
        - **空数组不是失败。** 用户原话：「私聊应该不是拿不到，而是这几天并没有
          私聊。」``get_friend_msg_history`` 返回 ``[]`` 就是「没有新消息」。
        - **按会话节流。** 补的是「关机期间的洞」，不会天天有新洞，
          每次抽取都补一遍纯属浪费一次 WebSocket 往返。
        """

        config = self._config()
        backfill_cfg = getattr(config, "backfill", None)
        if backfill_cfg is None or not bool(getattr(backfill_cfg, "enabled", False)):
            return []

        state = None
        try:
            state = self._store.get_state(entry.session_id)
        except Exception as exc:  # noqa: BLE001 - 读不到状态就不补
            self._debug(f"补抓读取会话状态失败: {exc!r}")
        last_extract = float((state or {}).get("last_extract_ts") or 0.0)

        # 节流：同一个会话隔一段时间才补一次。
        meta_key = f"backfill_at:{entry.session_id}"
        try:
            raw_last = self._store.get_meta(meta_key)
            last_backfill = float(raw_last) if raw_last else 0.0
        except Exception:  # noqa: BLE001
            last_backfill = 0.0
        interval = max(1, int(getattr(backfill_cfg, "min_interval_minutes", 30) or 30)) * 60.0
        if not should_backfill(last_backfill_ts=last_backfill, min_interval=interval):
            return []

        # 宿主库已经覆盖到上次抽取时间 → 中间没有洞，不用补。
        if host_messages and last_extract > 0:
            stamps = [_as_float(item.get("timestamp")) for item in host_messages]
            stamps = [value for value in stamps if value > 0]
            if stamps and min(stamps) <= last_extract:
                self._debug(f"会话 {entry.session_id} 宿主消息已覆盖上次抽取，跳过补抓")
                return []

        since_ts = default_since(
            last_extract_ts=last_extract,
            max_age_days=float(getattr(backfill_cfg, "max_age_days", 10.0) or 10.0),
        )
        max_pages = max(1, int(getattr(backfill_cfg, "max_pages", 40) or 40))
        max_messages = max(10, int(getattr(backfill_cfg, "max_messages", 200) or 200))
        chat_type = str(entry.chat_type or "").strip().lower()

        try:
            if chat_type == "group" and entry.group_id:
                result = await fetch_group_history(
                    self._ctx,
                    group_id=entry.group_id,
                    session_id=entry.session_id,
                    group_name=entry.group_name,
                    since_ts=since_ts,
                    platform=entry.platform or "qq",
                    max_pages=max_pages,
                    logger=self._logger,
                )
            elif chat_type == "private" and entry.user_id:
                result = await fetch_friend_history(
                    self._ctx,
                    user_id=entry.user_id,
                    session_id=entry.session_id,
                    since_ts=since_ts,
                    platform=entry.platform or "qq",
                    max_pages=max_pages,
                    logger=self._logger,
                )
            else:
                return []
        except Exception as exc:  # noqa: BLE001 - 补抓失败绝不能影响正常抽取
            self._warn(f"会话 {entry.session_id} 补抓历史失败（不影响抽取）: {exc!r}")
            return []

        # 无论成功失败都记时间：失败也代表「刚试过」，不该马上重试。
        try:
            self._store.set_meta(meta_key, str(time.time()))
        except Exception:  # noqa: BLE001
            pass

        self._info(f"会话 {entry.session_id} 补抓历史：{describe_result(result)}")
        if not result.ok or not result.messages:
            # 空列表 = 这几天没有新消息，不是错误。**不要据此关闭功能。**
            return []

        messages = result.messages
        if len(messages) > max_messages:
            # 保留最近的：补回来的是为了抽取，越近越有用。
            messages = messages[-max_messages:]
        return messages

    async def _record_hole(self, entry: DirtySession, hole: list[dict[str, Any]]) -> None:
        """把「她没读到的那一段」记成洞，原文另存一份备用。

        为什么要分成两张表：

        - 洞（``gap_unread``）只存**条数和时间范围**，不存内容。她凭它知道自己
          错过了什么，也只能诚实地说「那会儿我不在」。一旦把内容塞进去，她就
          变成「其实全都看过」，那正是用户嫌弃的那种假装。
        - 原文（``gap_history``）留着，只在她被人明确提起时才翻。``gap_items``
          不能拿来干这个：那边会衰减、遗忘、按相似度合并，而聊天记录是事实。

        全程 best-effort：记洞失败绝不能影响正常抽取。
        """

        summary = summarize_hole(hole)
        if summary is None:
            self._debug(f"会话 {entry.session_id} 有 {len(hole)} 条未读，但拿不到时间戳，跳过记洞")
            return

        try:
            self._store.record_unread(
                entry.session_id,
                start_ts=summary["start_ts"],
                end_ts=summary["end_ts"],
                count=summary["count"],
            )
            rows = to_history_rows(hole)
            added = self._store.add_history_rows(entry.session_id, rows) if rows else 0
            self._store.prune_unread(entry.session_id)
            self._store.trim_history(entry.session_id)
        except Exception as exc:  # noqa: BLE001 - 记洞失败不影响抽取
            self._warn(f"会话 {entry.session_id} 记录未读历史失败（不影响抽取）: {exc!r}")
            return

        self._debug(
            f"会话 {entry.session_id} 记下未读：{summary['count']} 条"
            f"（{describe_span(summary['start_ts'], summary['end_ts'])}），"
            f"原文入库 {added} 条"
        )

    # ── 日志 ──

    def _info(self, message: str) -> None:
        _log(self._logger, "info", message)

    def _debug(self, message: str) -> None:
        _log(self._logger, "debug", message)

    def _warn(self, message: str) -> None:
        _log(self._logger, "warning", message)


def _as_float(value: Any, default: float = 0.0) -> float:
    """把消息里的 ``timestamp``（宿主给字符串、适配器给数字）统一成 float。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _is_empty_response_failure(result: Any) -> bool:
    """判断一次抽取失败是否属于「模型空回复」这类可重试的瞬时问题。"""

    if getattr(result, "ok", False):
        return False
    error = str(getattr(result, "error", "") or "")
    markers = ("响应内容为空", "EmptyResponse", "空回复", "empty response")
    return any(marker in error for marker in markers)


def _expiry_for(category: str, retention: Any, now: float) -> float:
    if retention is None:
        return 0.0
    if category == "emotion":
        hours = float(getattr(retention, "emotion_ttl_hours", 72) or 0)
        return now + hours * 3600.0 if hours > 0 else 0.0
    key = {
        "open_loop": "open_loop_ttl_days",
        "relationship": "relationship_ttl_days",
        "self": "self_ttl_days",
        "creative": "creative_ttl_days",
    }.get(category, "open_loop_ttl_days")
    days = float(getattr(retention, key, 30) or 0)
    return now + days * 86400.0 if days > 0 else 0.0


def _log(logger: Any, level: str, message: str) -> None:
    """安全写日志——走 ``cg_log`` 的统一出口。

    这里原本有一份自己的实现，直接 ``getattr(logger, level)`` 调用，绕过了
    ``debug.log_level`` 的阈值：把日志级别调成 ``info`` 之后，tracker 每 30 秒
    仍照刷一条 debug。现在阈值由 ``cg_log`` 统一持有，``plugin.py`` 在
    ``on_load`` / ``on_config_update`` 里调 ``cg_log.set_min_level()``，
    这里就自然跟着生效了。
    """

    cg_log.log(logger, level, message)
