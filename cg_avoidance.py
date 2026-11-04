"""我，deepseek —— 宿主配置避让层。

用户要求「不改动 MaiBot 本体配置」，所以插件必须**自适应**宿主当前状态：
A_Memorix 开着的时候只补它没有的东西，关掉的时候才放开手脚。

能读到的都是只读快照：``ctx.config.get`` 走宿主 ``global_config`` 的
点号取值（``_get_nested_config_value``），异常时返回默认值。读不到就
按「A_Memorix 开着」处理——宁可少注入，也不要和宿主重复刷屏。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: A_Memorix 相关配置键。
#:
#: 只保留**真正驱动避让行为**的那些。`a_memorix.integration.person_fact_writeback_enabled`
#: 曾经也在这里，但它是宿主「把人物事实写回画像库」的**写入**开关，与本插件
#: 只做注入的职责没有交集，避让无从谈起，已删。
KEY_A_MEMORIX_ENABLED = "a_memorix.plugin.enabled"
KEY_PERSON_PROFILE_INJECTION = "a_memorix.integration.enable_person_profile_injection"
KEY_CHAT_SUMMARY_WRITEBACK = "a_memorix.integration.chat_summary_writeback_enabled"
KEY_HEURISTIC_RECALL = "a_memorix.integration.heuristic_memory_recall_enabled"

#: 探测结果缓存时间（秒）。宿主配置热重载不会通知第三方插件，
#: 所以靠定期重探，而不是靠回调。
PROBE_TTL_SECONDS = 180.0


@dataclass(slots=True)
class AvoidanceProfile:
    """一次探测的结论。

    ## 五个字段里哪些真的影响行为

    这个类最初是照「宿主 A_Memorix 的几个开关」清单逐个抄下来的，但抄下来
    不等于接上了线。现在的实情：

    ========================  ==========================================
    字段                      实际作用
    ========================  ==========================================
    ``a_memorix_enabled``     总开关：关掉就完全放开，不收紧预算
    ``person_profile_injection``  开 → :meth:`item_budget` 收到 2
    ``chat_summary_writeback``    只进 :meth:`describe` 的日志，**有意**不动
                                  行为（它管写入，不占注入预算）
    ``heuristic_recall``          只进 :meth:`describe` 的日志，不动行为
    ========================  ==========================================

    ``heuristic_recall`` 原本还有一条 ``top_k_scale()``（启发式召回开启时把
    召回量打到 0.6）。那个方法从来没有被生产代码调用过——``plugin.py`` 只
    用了 ``item_budget()``——而 README 却一直宣称「注入预算再打 6 折」会生效。
    已连同 ``top_k_scale()`` 一起删掉：``compose_block`` 已有 ``max_chars``
    硬约束，再叠一层缩放属于重复设限，还会削弱注入轮换与相关性加权。
    这个字段保留仅为让 :meth:`describe` 的启动日志能反映宿主真实状态。
    """

    a_memorix_enabled: bool = True
    person_profile_injection: bool = True
    chat_summary_writeback: bool = True
    heuristic_recall: bool = False
    degraded: bool = False
    notes: list[str] = field(default_factory=list)

    def skip_categories(self) -> tuple[str, ...]:
        """当前应当跳过的类别。

        插件负责的五类（open_loop / emotion / relationship / self / creative）
        A_Memorix 一个都没有，所以即使它全开也**不跳过任何类别**。
        这里保留扩展点：将来若发现某类真的重复，可以在这里返回。
        """

        return ()

    def item_budget(self, configured: int) -> int:
        """每类条目上限的避让调整。"""

        budget = max(1, int(configured))
        if not self.a_memorix_enabled:
            return budget
        # A_Memorix 开了人物画像注入时，关系类线索的「人物档案」部分会重复，
        # 收紧一点，避免两段文本讲同一件事。
        if self.person_profile_injection:
            budget = min(budget, 2)
        return max(1, budget)

    # 这里曾经有个 ``top_k_scale()``：启发式召回开启时返回 0.6，本意是把
    # 召回量整体打六折「让一步」。它从来没被 ``plugin.py`` 调用过——生产路径
    # 只用了上面的 ``item_budget()``——只有 ``tests.py`` 在测它，而 README
    # 第 6 节一直宣称这件事生效。
    #
    # 没有接上它，而不是补上，理由是现在这套已经不需要它了：
    # ``compose_block`` 有 ``max_chars``（默认 600）硬约束总长度，真机实测
    # 注入 12~13 条 / 550~590 字本来就贴着上限；再乘 0.6 会直接砍掉两三成
    # 内容，还会削弱注入轮换与话题相关性加权这两层刚做好的机制。

    def describe(self) -> str:
        flags = [
            f"a_memorix={'on' if self.a_memorix_enabled else 'off'}",
            f"profile_inject={'on' if self.person_profile_injection else 'off'}",
            f"summary_writeback={'on' if self.chat_summary_writeback else 'off'}",
            f"heuristic_recall={'on' if self.heuristic_recall else 'off'}",
        ]
        if self.degraded:
            flags.append("degraded")
        return " ".join(flags)


class AvoidanceProbe:
    """带 TTL 的配置探测器。"""

    def __init__(self, ctx: Any, *, ttl: float = PROBE_TTL_SECONDS) -> None:
        self._ctx = ctx
        self._ttl = max(0.0, float(ttl))
        self._cached: AvoidanceProfile | None = None
        self._cached_at = 0.0

    @property
    def cached(self) -> AvoidanceProfile:
        """最近一次探测结果；从未探测过时返回保守默认值。"""

        return self._cached or AvoidanceProfile()

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    async def probe(self, *, force: bool = False) -> AvoidanceProfile:
        """读取宿主配置并缓存结论。"""

        now = time.monotonic()
        if not force and self._cached is not None and now - self._cached_at < self._ttl:
            return self._cached

        profile = AvoidanceProfile()
        errors: list[str] = []

        enabled_raw = await self._read(KEY_A_MEMORIX_ENABLED, errors)
        # 宿主侧语义：plugin 键缺失时视为启用（host_service._is_enabled_config）。
        profile.a_memorix_enabled = True if enabled_raw is None else _as_bool(enabled_raw, True)

        if profile.a_memorix_enabled:
            raw = await self._read(KEY_PERSON_PROFILE_INJECTION, errors)
            profile.person_profile_injection = _as_bool(raw, True)

            raw = await self._read(KEY_CHAT_SUMMARY_WRITEBACK, errors)
            profile.chat_summary_writeback = _as_bool(raw, True)

            raw = await self._read(KEY_HEURISTIC_RECALL, errors)
            profile.heuristic_recall = _as_bool(raw, False)
        else:
            profile.person_profile_injection = False
            profile.chat_summary_writeback = False
            profile.heuristic_recall = False

        if errors:
            profile.degraded = True
            profile.notes.extend(errors)

        self._cached = profile
        self._cached_at = now
        return profile

    async def _read(self, key: str, errors: list[str]) -> Any:
        try:
            return await self._ctx.config.get(key, None)
        except Exception as exc:  # noqa: BLE001 - 读不到配置不该让插件挂掉
            errors.append(f"{key}: {exc!r}")
            return None


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default
