"""我，deepseek —— 主动（好奇心）。

## 这是什么

用户第 58 轮的原话：「在没有任何预设目标或指令的情况下，仅仅因为『好奇』或
『觉得有趣』，就去主动去做一件完全没有人告诉过她或让她去做的事情，又或者
自行去探索一个完全未知的领域。」

一条「主动」= 她自己想去看点什么，看完了再决定要不要说。

## 最重要的一条限制：她不能上网

宿主**没有** ``web.search`` / ``web.fetch``（``src`` 树里 0 命中，78 项能力里
一个 ``web.*`` 都没有）。所以「好奇 → 自己去查」在当前版本只能是编的，而编
出来的东西会立刻违反我们自己在提示词里立的那条规矩——「没实际查过不能声称
查过」。一个从头到尾在假装见过世面的角色，比一个不出门的角色更假。

**能真做的替代：回读自己的过去。** 她有几百条原文（``gap_history``）、有没聊完
的话题（``open_loop``）、有留着没用的东西（``gap_keepsake``）。「刚才那句话我
当时没听懂」→ 空闲时回去翻那一段 → 发现原来是这个意思 / 翻完还是不认同。
这是真的好奇，不需要假装见过任何东西。

## 出口是宿主的 proactive.trigger，而且 intent 只写状态

宿主 ``maisaka.proactive.trigger`` 会往聊天历史里塞一条
``<plugin_proactive_task>`` 并 arm 强制触发，绕过频率门控进 Planner。宿主自己
在提示里写的最后一句是「**请结合当前聊天关系、记忆和上下文，自行决定是否回复
以及如何表达**」——也就是说「说不说」是她的，不是我们的。

所以我们传的 ``intent`` 必须是一个**状态**（我在翻什么、看到了什么、当时怎么
想的），不能是一条**命令**（去跟他说……）。写成命令就等于绕过她直接替她开口，
那和「没有任何预设目标」正好相反。

## 三条硬闸（LLM 看不到，也不参与评分）

参考市场调研里 ``life-frequency`` 的写法：评分只负责排序，数量由一组正交硬闸
决定。这里的顺序是——

1. 开关（默认**关**：这是第一版原型，改的是「她会不会主动开口」）
2. 冷却（两次之间至少 N 小时）与每日上限
3. 精力（``cg_mood.can_initiate()``：精力耗尽时她连话都说不动，不可能好奇）
4. 时间（深夜不打扰，而且她那时候通常也在睡）
5. 边界感（正在静默窗里就不主动）

**每一次跳过都记原因。** 主动功能最难排查的从来不是「她为什么说了」，而是
「她为什么又没说话」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: 多久没说话才叫「清静下来了」。太短会把正常对话中间的停顿当成空闲。
DEFAULT_IDLE_MINUTES = 90.0

#: 两次主动之间至少隔多久。
DEFAULT_COOLDOWN_HOURS = 8.0

#: 每天最多主动几次。第一版原型的建议值是 1。
DEFAULT_DAILY_LIMIT = 1

#: 深夜不主动。值是小时，左闭右开区间（跨零点由 :func:`in_quiet_hours` 处理）。
DEFAULT_QUIET_START = 0
DEFAULT_QUIET_END = 8

#: 主动的三种由头。
#:
#: - ``revisit``：回去翻一段她当时不在的聊天（真好奇，且完全不需要外部信息）
#: - ``keepsake``：想起一件自己留着没用的东西（「浪费」的出口）
#: - ``unfinished``：一个没聊完的话题又浮上来（最接近人「忽然想起来」的感觉）
KIND_REVISIT = "revisit"
KIND_KEEPSAKE = "keepsake"
KIND_UNFINISHED = "unfinished"
KINDS = (KIND_REVISIT, KIND_KEEPSAKE, KIND_UNFINISHED)

KIND_LABELS = {
    KIND_REVISIT: "回去翻一段",
    KIND_KEEPSAKE: "想起留着的东西",
    KIND_UNFINISHED: "没聊完的事又浮上来",
}

#: 默认偏好顺序。**「没聊完的事」排第一**：它最接近人真的会忽然想起来的东西
#: ——不是翻旧账，是有件事一直悬在那儿没落地。翻旧记录排最后，因为那最像
#: 「系统在读日志」，而不是她想起了什么。
DEFAULT_PREFER = (KIND_UNFINISHED, KIND_KEEPSAKE, KIND_REVISIT)

#: 跳过的原因。**每一条都要能被人读懂**，这是排查主动功能唯一的抓手。
REASON_DISABLED = "主动开关关着"
REASON_NO_MATERIAL = "没有可以翻的东西"
REASON_COOLDOWN = "离上次主动还不够久"
REASON_DAILY_LIMIT = "今天的额度用完了"
REASON_EXHAUSTED = "精力耗尽"
REASON_QUIET = "现在是深夜"
REASON_NOT_IDLE = "聊天还在进行，不算空闲"
REASON_BOUNDARY = "正在静默窗里"
REASON_OK = "可以开口"


@dataclass(slots=True)
class Spark:
    """一个「想去看点什么」的由头。"""

    kind: str = KIND_REVISIT
    #: 给她看的那句话。写「状态」，不写「命令」。
    content: str = ""
    #: 这个由头来自哪里（会话 id / 留念 id / 话题 id），只用于日志和去重。
    source: str = ""
    #: 上一次因为什么没去成（可选，仅用于日志）。
    note: str = ""


@dataclass(slots=True)
class Plan:
    """一次「要不要主动」的判断结果。"""

    allow: bool = False
    reason: str = REASON_DISABLED
    spark: Spark | None = None
    parts: dict[str, float] = field(default_factory=dict)


def in_quiet_hours(now_ts: float, *, start: int = DEFAULT_QUIET_START,
                   end: int = DEFAULT_QUIET_END) -> bool:
    """现在是不是免打扰时段。

    支持跨零点（``start=23, end=7``）。``start == end`` 视为没有免打扰时段。
    """

    if start == end:
        return False
    import time as _time

    hour = _time.localtime(now_ts).tm_hour
    start = int(start) % 24
    end = int(end) % 24
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def idle_seconds(last_seen_ts: float, *, now: float | None = None) -> float:
    import time as _time

    current = float(now if now is not None else _time.time())
    last = float(last_seen_ts or 0.0)
    if last <= 0.0 or last >= current:
        return 0.0
    return current - last


def decide(
    *,
    enabled: bool = False,
    has_material: bool = True,
    idle: float = 0.0,
    idle_min_seconds: float = DEFAULT_IDLE_MINUTES * 60.0,
    last_spark_ts: float = 0.0,
    cooldown_seconds: float = DEFAULT_COOLDOWN_HOURS * 3600.0,
    today_count: int = 0,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
    exhausted: bool = False,
    quiet: bool = False,
    boundary: bool = False,
    now: float | None = None,
) -> Plan:
    """按固定顺序过一遍硬闸。

    顺序是有讲究的：开关在最前（关着就完全不该有副作用），素材次之（没有素材
    的话后面几条都白算），然后才是时间类条件。**任何一条不满足都要给出原因。**
    """

    import time as _time

    current = float(now if now is not None else _time.time())
    if not enabled:
        return Plan(allow=False, reason=REASON_DISABLED)
    if not has_material:
        return Plan(allow=False, reason=REASON_NO_MATERIAL)
    if exhausted:
        return Plan(allow=False, reason=REASON_EXHAUSTED)
    if quiet:
        return Plan(allow=False, reason=REASON_QUIET)
    if boundary:
        return Plan(allow=False, reason=REASON_BOUNDARY)
    if idle < max(0.0, float(idle_min_seconds)):
        return Plan(allow=False, reason=REASON_NOT_IDLE)
    gap = current - float(last_spark_ts or 0.0)
    if last_spark_ts and gap < max(0.0, float(cooldown_seconds)):
        return Plan(allow=False, reason=REASON_COOLDOWN)
    if int(today_count) >= max(0, int(daily_limit)):
        return Plan(allow=False, reason=REASON_DAILY_LIMIT)
    return Plan(allow=True, reason=REASON_OK)


#: 传进 ``proactive.trigger`` 的 intent 前缀。
#:
#: 注意每一句都是「我看到了什么 / 我当时怎么想」，没有一句是「你该去跟他说」。
INTENT_LEAD = "你自己想去看点东西，现在看完了。"

INTENT_TEMPLATES = {
    KIND_REVISIT: (
        "{lead}\n"
        "你翻回了一段你当时不在的聊天（{span}）。里面有一句：{content}\n"
        "你当时没听明白，现在好像有点懂了——也可能并没有。想到就说，想不到就算了。"
    ),
    KIND_KEEPSAKE: (
        "{lead}\n"
        "你想起自己留着的这么一件没什么用的东西：{content}\n"
        "不用为它找个理由，也不用让它变得有用。"
    ),
    KIND_UNFINISHED: (
        "{lead}\n"
        "有件没聊完的事又浮上来了：{content}\n"
        "要不要接着提，看现在合不合适。"
    ),
}


def render_intent(spark: Spark, *, span: str = "") -> str:
    """把由头写成 ``proactive.trigger`` 的 intent。

    **必须是状态不是命令。** 宿主会在后面自己补一句「请自行决定是否回复以及
    如何表达」，所以这里写「去对他说……」等于替她做了决定。

    空内容直接返回空串——没有素材就不要 arm 一轮，那是纯粹的打扰。
    """

    content = " ".join(str(spark.content or "").split())
    if not content:
        return ""
    kind = spark.kind if spark.kind in INTENT_TEMPLATES else KIND_REVISIT
    span_text = str(span or "").strip() or "前面某一段"
    return INTENT_TEMPLATES[kind].format(
        lead=INTENT_LEAD,
        span=span_text,
        content=content,
    )


def pick_spark(candidates: Sequence[Spark], *, prefer: Sequence[str] = ()) -> Spark | None:
    """从候选里挑一个。

    ``prefer`` 是有序偏好，默认走 ``DEFAULT_PREFER``（「没聊完的事」优先）。
    同一类型内部按传入顺序，**不做随机**——随机会让同一份数据每次跑出不同结果，
    排查时完全没法复现。
    """

    pool = [item for item in candidates if item and item.content.strip()]
    if not pool:
        return None
    order = [kind for kind in prefer if kind in KINDS] or list(DEFAULT_PREFER)
    for kind in order:
        for item in pool:
            if item.kind == kind:
                return item
    return pool[0]


def describe_plan(plan: Plan, *, span: str = "") -> str:
    """一行日志。**永远带原因**，包括「没去成」的那些。"""

    if not plan.allow:
        return f"主动：没开口（{plan.reason}）"
    spark = plan.spark
    if spark is None:
        return f"主动：放行了但没有素材（{plan.reason}）"
    label = KIND_LABELS.get(spark.kind, spark.kind)
    extra = f"，{span}" if span else ""
    return f"主动：{label}{extra}：{spark.content[:40]}"


def spark_from_history(
    rows: Sequence[Mapping[str, Any]],
    *,
    span: str = "",
    limit: int = 1,
) -> list[Spark]:
    """从 ``gap_history`` 的原始行里造由头。

    只取有正文、有时间戳的；按传入顺序（调用方已经排好序）。``limit`` 限制
    条数——第一版一次只翻一条，翻太多会变成「读完整个群的聊天记录」，那正是
    之前费了很大劲才避开的监控感。
    """

    out: list[Spark] = []
    for row in rows:
        text = " ".join(str(row.get("text") or "").split())
        if not text:
            continue
        out.append(
            Spark(
                kind=KIND_REVISIT,
                content=text[:80],
                source=str(row.get("message_id") or row.get("id") or ""),
                note=span,
            )
        )
        if len(out) >= max(1, int(limit)):
            break
    return out
