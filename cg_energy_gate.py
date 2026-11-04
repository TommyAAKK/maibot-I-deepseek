"""我，deepseek —— 精力对**发言频率**的门控。

## 为什么需要这一层

宿主没有「插件可以否决一轮发言」的 hook：是否进入 Planner 完全由
``src/maisaka/turn_trigger/`` 内部决定，外部拿不到决策点。但宿主提供了
``frequency.*`` 能力，这正是官方留出来的「调整本会话发言频率」的入口：

    ctx.frequency.set_adjust(chat_id, value)
      → heartflow_manager.adjust_talk_frequency(session_id, value)
      → MaisakaHeartFlowChatting.adjust_talk_frequency(value)
      → self._talk_frequency_adjust = value

而 ``_get_effective_reply_frequency() = talk_value * _talk_frequency_adjust``
参与两处判定：

- ``_get_message_trigger_threshold() = ceil(1 / frequency)``
  —— 频率越低，攒够一轮所需的条数越多；
- ``DynamicReplyTurnGate.evaluate(probability, frequency)``
  —— 频率越低，同样的回复概率越不容易越过门限。

所以调低它 = 真的少说话，不是靠提示词求模型配合。

## 分级而不是一刀切

精力是连续量，压频率也连续：``voice`` 倍率 1.0 表示不干预，0.5 表示压到
一半，0 表示压到下限。精力在 0.5 以上时 ``voice`` 恒为 1.0 —— 轻微疲劳
不该改变说话节奏，用户明确要求过这一点。

## 为什么**不能**调到 0

``turn_trigger/scheduler.py`` 的判定顺序是：

    if runtime._is_reply_frequency_silent():   # 频率 <= 0
        ...静默消费，直接 return
    if runtime._has_forced_turn_trigger():     # @ / 被点名
        ...进入 Planner

静默分支在强制触发**之前**，频率为 0 时连 @ 都不会回。用户明确要求
「被点名或被 @ 还是会回复」，所以下限必须是一个大于 0 的小值：
低到日常闲聊几乎攒不够条数，又不至于让 @ 失效。

## ⚠️ focus 模式下这一层完全不生效

``runtime.py`` 的 ``_get_effective_reply_frequency()`` 在 focus 模式下直接
``return 1.0``，而且 focus 组根本不经过 ``turn_trigger/scheduler.py``
（``register_message()`` 见 ``_focus_group`` 就 return，永不调度）。所以
focus 模式下无论写什么频率都没用 —— 那种场景靠 ``maisaka.planner`` 阶段
注入指令、引导模型自己调内置工具 ``wait`` / ``stop`` 来控制节奏。这一层
只在非 focus 的普通群聊里起作用。

## 为什么要记住原值

``_talk_frequency_adjust`` 是宿主会话级状态，别家插件（例如好感度类）
也可能在改。所以我们：

1. 第一次要改动时，先 ``get_adjust`` 把原值记下来（内存 + ``gap_meta``
   落盘，防止插件崩溃后没人恢复）；
2. 恢复时写回记下的原值，而不是写死 1.0，免得覆盖别人的调整；
3. 卸载时一定尝试恢复。
"""

from __future__ import annotations

import time
from typing import Any

from . import cg_log

#: 精力耗尽时的频率下限。必须 > 0，否则宿主会进入静默模式、连 @ 都不回。
DEFAULT_GATE_FLOOR = 0.05

#: 两次 RPC 之间的最小间隔（秒）。热路径上不能每条消息都发一次 RPC。
SYNC_INTERVAL_SECONDS = 30.0

#: 落盘的原值键名。
_META_KEY = "host_freq_original"

#: 精力刚耗尽时，让 Bot 先说一句「我去忙别的了」再安静下来。
#: 不这样做的话会出问题：频率一压低它就不说话了，那句交代永远说不出口。
DEFAULT_FAREWELL_INTENT = (
    "你现在精力见底了，马上要安静一段时间。请用一句话自然地说明你要去做什么"
    "（比如去休息、去忙别的、晚点回来），语气按你当前的心情来，不要解释这是设定，"
    "也不要在这条消息里继续聊别的话题。"
)


class EnergyGate:
    """把精力状态翻译成宿主的发言频率。

    只在**状态发生跃迁**时动宿主（进入耗尽 / 解除耗尽），其余时候只做
    内存判断，零 RPC。
    """

    def __init__(self, ctx: Any, *, logger: Any = None) -> None:
        self._ctx = ctx
        self._logger = logger
        #: session_id → 我们改动前的原始 adjust 值。
        self._original: dict[str, float] = {}
        #: session_id → 我们上一次写下去的倍率，用于判断「要不要再写一次」。
        self._applied: dict[str, float] = {}
        #: session_id → 上次同步时间，用于节流。
        self._last_sync: dict[str, float] = {}
        #: 是否已经恢复过（避免重复 RPC）。
        self._restored = False
        #: 已经为哪些会话说过「先去做别的」的告别语，避免反复触发。
        self._farewell_done: set[str] = set()

    # ── 日志 ──

    def _log(self, message: str) -> None:
        """走 ``cg_log`` 统一出口，这样 ``debug.log_level`` 对它也生效。"""

        if self._logger is not None:
            cg_log.log(self._logger, "info", message)

    # ── 能力调用 ──

    async def _call(self, method: str, chat_id: str, value: float | None = None) -> Any:
        """调用 ``ctx.frequency`` 上的方法；任何失败都返回 None 而不是抛出。"""

        capability = getattr(self._ctx, "frequency", None)
        if capability is None:
            return None
        try:
            if value is None:
                return await capability.get_adjust(chat_id)
            return await capability.set_adjust(chat_id, float(value))
        except Exception as exc:  # noqa: BLE001 - 门控失败不能让插件报错
            self._log(f"调用 frequency.{method} 失败: {exc!r}")
            return None

    @staticmethod
    def _pick_value(payload: Any) -> float | None:
        """从能力返回值里取出数值。

        宿主返回的是 ``{"success": True, "value": 1.0}``；不同版本也可能
        直接返回数字，所以两种都认。
        """

        if isinstance(payload, dict):
            if not payload.get("success", True):
                return None
            payload = payload.get("value")
        try:
            return float(payload)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    # ── 主流程 ──

    def tracked_sessions(self) -> list[str]:
        """返回当前被我们压过频率的会话 ID（用于定时检查是否需要恢复）。"""

        return list(self._original)

    async def sync(
        self,
        session_id: str,
        *,
        voice: float = 1.0,
        exhausted: bool = False,
        floor: float = DEFAULT_GATE_FLOOR,
        store: Any = None,
        now: float | None = None,
        intent: str = "",
        farewell: bool = True,
    ) -> None:
        """按当前精力水平调整本会话的发言频率。

        精力是个连续量，压频率也应该连续：``voice`` 是「还能说多少」的
        倍率，1.0 表示不干预，0.5 表示压到一半，0 表示压到下限。精力在
        0.5 以上时 ``voice`` 恒为 1.0 —— 用户明确要求「精力降到 0.5 之前
        也可以不调整发言频率」，轻微疲劳不该影响说话节奏。

        Args:
            session_id: 会话 ID，同时也是宿主 ``frequency`` 能力的 chat_id。
            voice: 发言强度倍率，0..1。1.0 表示不压（会触发恢复）。
            exhausted: 精力是否处于耗尽锁定状态（只影响是否说告别语）。
            floor: 频率下限（必须 > 0）。
            store: 用于把原值落盘，插件崩溃后下次启动还能恢复。
            now: 注入时间，便于测试。
            intent: 耗尽瞬间让 Bot 交代去向时用的意图描述；留空用默认文案。
            farewell: 是否说那句告别语。配置里 ``farewell_intent`` 留空表示
                用户不想要这句话，此时传 False —— 不能用「空字符串」表达
                「不说」，因为空字符串同时是「没配，用默认」的写法。
        """

        session_id = str(session_id or "").strip()
        if not session_id:
            return

        current = time.time() if now is None else now
        last = self._last_sync.get(session_id, 0.0)
        if current - last < SYNC_INTERVAL_SECONDS:
            return
        self._last_sync[session_id] = current

        # 下限必须严格大于 0：0 会让宿主进入静默模式，连 @ 都不回。
        safe_floor = max(1e-3, min(1.0, float(floor or DEFAULT_GATE_FLOOR)))
        try:
            level = max(0.0, min(1.0, float(voice)))
        except (TypeError, ValueError):
            level = 1.0

        if level < 1.0:
            # 先把「我去忙别的了」这句话放出去，再压频率。
            # 顺序很重要：一旦频率被压到下限，Bot 就很难再被调度起来说话，
            # 那句交代就永远说不出口了。只在真正耗尽时说，别为 0.4 的
            # 轻微疲劳也告一次别。
            if exhausted and farewell and session_id not in self._farewell_done:
                self._farewell_done.add(session_id)
                await self._farewell(session_id, intent=intent)

            original = self._original.get(session_id)
            if original is None:
                original = self._pick_value(await self._call("get_adjust", session_id))
                if original is None:
                    original = 1.0
            target = original * level
            # 原值本来就比下限还低，说明别人（或用户）已经压得更狠，不要抬高。
            target = min(max(target, safe_floor), original) if original > 0 else safe_floor
            if target <= 0.0:
                target = safe_floor

            applied = self._applied.get(session_id)
            if applied is not None and abs(applied - target) < 0.02:
                return  # 和上次差不多，不必再发一次 RPC
            result = await self._call("set_adjust", session_id, target)
            if result is None:
                return
            # 宿主的 adjust_talk_frequency 在会话还没建起来时只打 warning 就返回，
            # 但能力层照样回 {"success": True}。所以必须读回来确认真的生效了，
            # 否则会把「没改成」记成「改成了」，之后恢复时又把错误值写回去。
            actual = self._pick_value(await self._call("get_adjust", session_id))
            if actual is None or abs(actual - target) > 1e-6:
                self._log(
                    f"会话 {session_id} 的频率没能改成 {target:.3f}"
                    f"（实际 {actual if actual is not None else '读取失败'}），"
                    "可能这个会话还没建立，下一轮再试"
                )
                return
            self._applied[session_id] = target
            if session_id not in self._original:
                self._original[session_id] = original
                self._persist_original(store, session_id, original)
            self._log(
                f"精力不足（发言强度 {level:.2f}）：把会话 {session_id} 的发言频率"
                f"从 {original:.3f} 降到 {target:.3f}（不设为 0，否则宿主连 @ 都不回）"
            )
            return

        # 发言强度已回到 1.0：如果之前压过，恢复原值。
        original = self._original.pop(session_id, None)
        self._applied.pop(session_id, None)
        if original is None:
            return
        result = await self._call("set_adjust", session_id, original)
        if result is None:
            # 恢复失败就把记录塞回去，下一轮再试。
            self._original[session_id] = original
            return
        # 同样要读回确认：会话可能已经销毁，宿主的 set 是静默失败的。
        actual = self._pick_value(await self._call("get_adjust", session_id))
        if actual is not None and abs(actual - original) > 1e-6:
            self._original[session_id] = original
            self._log(
                f"会话 {session_id} 的频率没能还原成 {original:.3f}"
                f"（实际 {actual:.3f}），下一轮再试"
            )
            return
        self._clear_original(store, session_id)
        # 恢复精力后允许下次耗尽时再说一次告别语。
        self._farewell_done.discard(session_id)
        self._log(f"精力恢复：会话 {session_id} 的发言频率已还原为 {original:.3f}")

    async def _farewell(self, session_id: str, *, intent: str) -> None:
        """让 Bot 说一句「我先去做别的了」，然后才真正安静下来。

        用 ``maisaka.proactive.trigger`` 而不是直接发消息：这条路径会 arm
        强制触发状态，能绕过频率门控进入 Planner，让模型自己决定措辞。
        失败只是少一句交代，不影响门控本身，所以异常全部吞掉。
        """

        maisaka = getattr(self._ctx, "maisaka", None)
        proactive = getattr(maisaka, "proactive", None)
        if proactive is None:
            return
        text = str(intent or "").strip() or DEFAULT_FAREWELL_INTENT
        try:
            await proactive.trigger(
                session_id,
                text,
                reason="精力耗尽，交代去向",
                priority="low",
            )
        except Exception as exc:  # noqa: BLE001 - 少一句告别不算故障
            self._log(f"触发告别语失败（{session_id}）: {exc!r}")

    async def restore_all(self, store: Any = None) -> None:
        """把记得的原值全部写回宿主。卸载时调用。"""

        if self._restored:
            return
        self._restored = True
        pending = dict(self._original)
        pending.update(self._load_persisted(store))
        for session_id, original in pending.items():
            result = await self._call("set_adjust", session_id, original)
            if result is not None:
                self._clear_original(store, session_id)
        if pending:
            self._log(f"卸载时恢复了 {len(pending)} 个会话的发言频率")
        self._original.clear()

    # ── 原值持久化 ──

    def _persist_original(self, store: Any, session_id: str, original: float) -> None:
        if store is None:
            return
        try:
            store.set_meta(f"{_META_KEY}:{session_id}", f"{original:.6f}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"保存原始发言频率失败: {exc!r}")

    def _clear_original(self, store: Any, session_id: str) -> None:
        if store is None:
            return
        try:
            store.delete_meta(f"{_META_KEY}:{session_id}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"清除原始发言频率失败: {exc!r}")

    def _load_persisted(self, store: Any) -> dict[str, float]:
        """读出上次没来得及恢复的原值（插件崩溃/被杀的情况）。"""

        if store is None:
            return {}
        try:
            rows = store.meta_prefix(_META_KEY)
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, float] = {}
        for key, value in rows.items():
            session_id = key[len(_META_KEY) + 1 :] if key.startswith(_META_KEY + ":") else ""
            if not session_id:
                continue
            try:
                out[session_id] = float(value)
            except (TypeError, ValueError):
                continue
        return out
