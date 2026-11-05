"""我，deepseek —— 自主发言闸门。

## 为什么需要这个

宿主的发言频率（``talk_value``）管的是**触发**：攒够几条消息才让 Planner 跑
一轮。它管不了「这一轮跑起来之后，要不要说话」——Planner 一旦跑起来，手里就
有 ``reply`` 工具，模型想发就发。

所以压频率只能让发言**变晚**，不能让她**决定不说**。这是两件事。

真机上更糟：``focus_mode = true`` 时 ``_get_effective_reply_frequency()``
直接 ``return 1.0``（``src/maisaka/runtime.py:1066``），阈值恒为 1 条，
``IdleBackoffController.should_delay()`` 也直接 ``reset(); return False``。
三重叠加的结果是「看到啥都要回」，实测 11-02 回复率 65.5%。

用户的原话：「精力门控即便不开 focus 模式，也不应该是简单地调整发言频率，
所以自主发言闸门应该会更好」。这个判断是对的。

## 闸门怎么实现（唯一有效的硬手段）

``maisaka.planner.before_request`` 的 kwargs 里有 ``tool_definitions``，而
``src/maisaka/chat_loop_service.py:1269-1276`` 会拿它**整体覆盖** ``all_tools``：

```python
raw_tool_definitions = before_request_kwargs.get("tool_definitions")
if isinstance(raw_tool_definitions, list):
    all_tools = [item for item in raw_tool_definitions if isinstance(item, dict)]
```

所以插件只要在 hook 里把 ``reply`` 从中删掉，模型就**没有发送可见回复的手段**
了——不是「劝它别说」，是「它说不出」。``send_emoji`` 要一并考虑，否则她还能
只发表情包。

## 两道判断，硬过滤和软评分分开

这个结构是从 Mai_life 的「契机」机制学的：**硬过滤保安全，软评分挑时机**。
混在一起写，调试时永远说不清「这次为什么没说话」。

硬过滤（任一命中就闭嘴，不看分数）：

- 被 @ / 被提及 → **一律放行**（用户明确要求「@ 还是会回复，做成开关」）
- 能耗尽 → 闭嘴
- 边界感判「不想接话」 → 闭嘴（**和边界感共用同一个闸门**，不能一边说
  「我现在不太想接这个话」一边又主动开口）

软评分（只在硬过滤全过之后才算）：

- 兴趣匹配：当前话题命中她的 ``like`` 越多越高分
- 好感度：越亲近越愿意开口
- 心情：愉悦度 / 精力

## 每次跳过都记原因

主动功能最难排查的就是「它为什么不说话」。所以 :class:`Decision` 必带
``reason``，且每次判定都写一行日志。这在 Mai_life 里叫
``record_proactive_skip``，我们这里就是 ``Decision.reason``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: 会被闸门拦掉的工具名。
#:
#: ``reply`` 是唯一能发可见文字的；``send_emoji`` 只发表情包，但**表情包也是
#: 发言**——她要是「不想说话」却回了个表情，那就不叫不想说话。所以两个一起拦。
GATED_TOOLS: tuple[str, ...] = ("reply", "send_emoji")

#: 软评分的各项权重（加起来 1.0）。改这些会直接改变她的「话痨程度」。
WEIGHT_INTEREST = 0.40
WEIGHT_AFFECTION = 0.30
WEIGHT_MOOD = 0.20
WEIGHT_ENERGY = 0.10

#: 软评分达到这个值才允许开口。0.45 是「三样里至少两样还行」的水平。
DEFAULT_THRESHOLD = 0.45

#: 好感度归一化的上限。超过这个值一律按满分算。
AFFECTION_CEILING = 80.0

#: 被 @ 时的原因标签，写进日志用。
REASON_AT = "被@"
REASON_MENTIONED = "被提及"
REASON_EXHAUSTED = "能耗尽"
REASON_BOUNDARY = "边界感不想接话"
REASON_DISABLED = "闸门未启用"
REASON_LOW_SCORE = "分数不够"
REASON_NOTHING = "无可评内容"
REASON_ALLOW = "可以开口"

#: 判定结果里用来表示「放行」的档位名。
DECISION_ALLOW = "allow"
DECISION_DENY = "deny"


@dataclass(slots=True)
class Decision:
    """一次闸门判定的完整结果。

    Attributes:
        allow: ``True`` 表示这一轮可以说话。
        reason: 人话原因，直接进日志。
        score: 软评分（0~1）；被硬过滤拦掉时是 0。
        hard: 是否为硬过滤拦下（区别于「分数不够」）。
        parts: 各分项明细，调试时看这个能立刻知道哪项拖后腿。
    """

    allow: bool = True
    reason: str = REASON_ALLOW
    score: float = 0.0
    hard: bool = False
    parts: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.parts is None:
            self.parts = {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def interest_score(text: str, *, likes: Iterable[str]) -> float:
    """当前话题命中她「喜欢」的程度（0~1）。

    命中一条给 0.6，之后每多命中一条加 0.2，三条封顶（0.6 / 0.8 / 1.0）。
    做成三档而不是「命中就满分」，是因为命中一个词和整段都在聊她喜欢的东西
    确实不是一回事。

    再加一点长度相关的微弱权重？**不加**——长度和兴趣没关系，加了只会让
    长消息莫名更容易让她开口。
    """

    body = str(text or "")
    if not body:
        return 0.0
    hits = 0
    for item in likes:
        word = str(item or "").strip()
        if not word:
            continue
        if word in body:
            hits += 1
    if hits <= 0:
        return 0.0
    return _clamp01(0.6 + 0.4 * min(1.0, (hits - 1) / 2.0))


def affection_score(affection: float, *, ceiling: float = AFFECTION_CEILING) -> float:
    """好感度归一化到 0~1。"""

    limit = max(1.0, float(ceiling))
    return _clamp01(_as_float(affection) / limit)


def mood_score(*, valence: float, arousal: float = 0.5) -> float:
    """心情分：愉悦度为主，唤醒度（精神头）稍加权。

    ``valence`` 在 -1~1，映射到 0~1；``arousal`` 已经是 0~1。
    """

    v = _clamp01((_as_float(valence, 0.0) + 1.0) / 2.0)
    a = _clamp01(_as_float(arousal, 0.5))
    return _clamp01(v * 0.75 + a * 0.25)


def evaluate(
    *,
    enabled: bool = True,
    at_pending: bool = False,
    mentioned_pending: bool = False,
    at_always_reply: bool = True,
    mentioned_always_reply: bool = False,
    exhausted: bool = False,
    boundary_silent: bool = False,
    text: str = "",
    likes: Sequence[str] = (),
    affection: float = 0.0,
    valence: float = 0.0,
    arousal: float = 0.5,
    energy: float = 1.0,
    threshold: float = DEFAULT_THRESHOLD,
) -> Decision:
    """闸门主判定。

    顺序：开关 → @/提及 → 能耗尽 → 边界感 → 软评分。

    Args:
        enabled: 闸门总开关，关掉时恒放行（退回宿主行为）。
        at_pending: 这一轮里有没有 @ 她的消息。
        mentioned_pending: 有没有提到她的名字。
        at_always_reply: 「被 @ 一定回」的开关（对应宿主 ``inevitable_at_reply``）。
        mentioned_always_reply: 「被提到一定回」的开关（对应宿主
            ``mentioned_bot_reply``）。**和 @ 分开**：宿主本来就是两个独立开关，
            而且现实里「@ 我」和「顺口提到我名字」的强度差很远——前者是叫我，
            后者可能只是提到。
        exhausted: 精力是否耗尽。
        boundary_silent: 边界感是否判「不想接话」。
        text: 当前这轮要说的话（用来算兴趣匹配）。传空字符串就跳过兴趣项。
        likes: 她喜欢的词。
        affection/valence/arousal/energy: 心情与关系输入。
        threshold: 软评分门槛。
    """

    if not enabled:
        return Decision(allow=True, reason=REASON_DISABLED, score=1.0)

    # ── 硬过滤 1：被点名 ────────────────────────────────────────────
    # 这是唯一优先于其他所有硬过滤的规则。被叫名字还不应声，那不是性格，是坏了。
    if at_pending and at_always_reply:
        return Decision(allow=True, reason=REASON_AT, score=1.0)
    if mentioned_pending and mentioned_always_reply:
        return Decision(allow=True, reason=REASON_MENTIONED, score=1.0)

    # ── 硬过滤 2：能耗尽 ────────────────────────────────────────────
    if exhausted:
        return Decision(allow=False, reason=REASON_EXHAUSTED, hard=True)

    # ── 硬过滤 3：边界感 ────────────────────────────────────────────
    # 主动和边界感必须共用同一个闸门：一边注入「你现在不太想接话」，
    # 一边又允许她主动开口，等于自相矛盾。
    if boundary_silent:
        return Decision(allow=False, reason=REASON_BOUNDARY, hard=True)

    # ── 软评分 ──────────────────────────────────────────────────────
    parts: dict[str, float] = {}
    total_weight = 0.0
    total = 0.0

    if str(text or "").strip():
        parts["interest"] = interest_score(text, likes=likes)
        total += parts["interest"] * WEIGHT_INTEREST
        total_weight += WEIGHT_INTEREST

    parts["affection"] = affection_score(affection)
    total += parts["affection"] * WEIGHT_AFFECTION
    total_weight += WEIGHT_AFFECTION

    parts["mood"] = mood_score(valence=valence, arousal=arousal)
    total += parts["mood"] * WEIGHT_MOOD
    total_weight += WEIGHT_MOOD

    parts["energy"] = _clamp01(_as_float(energy, 1.0))
    total += parts["energy"] * WEIGHT_ENERGY
    total_weight += WEIGHT_ENERGY

    if total_weight <= 0:
        return Decision(allow=False, reason=REASON_NOTHING, hard=True)

    # 归一化：没有 text 时 interest 项不参与，剩下的权重要重新拉到 1.0，
    # 否则「没有话题」这个中性情况会被系统性压低分。
    score = _clamp01(total / total_weight)
    allow = score >= max(0.0, float(threshold))
    return Decision(
        allow=allow,
        reason=REASON_ALLOW if allow else REASON_LOW_SCORE,
        score=score,
        hard=False,
        parts=parts,
    )


def _tool_name(definition: Any) -> str:
    """从工具定义里取名字，兼容两种形态。

    OpenAI function schema: ``{"type":"function","function":{"name":...}}``
    裸形态（少数内部路径）: ``{"name":...}``
    """

    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    name = definition.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return ""


def filter_tools(
    tool_definitions: Any,
    *,
    allow_reply: bool,
    drop: Sequence[str] = GATED_TOOLS,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """按判定结果裁剪工具清单。

    Args:
        tool_definitions: hook kwargs 里拿到的那个列表。
        allow_reply: 闸门判定是否放行。
        drop: 需要拦掉的工具名。

    Returns:
        ``(新清单, 被删掉的名字)``。``tool_definitions`` 不是列表时返回
        ``(None, [])``，调用方据此不动 kwargs —— **看不懂就别改**，
        这比猜测性地清空工具列表安全得多。
    """

    if not isinstance(tool_definitions, list):
        return None, []

    items = [item for item in tool_definitions if isinstance(item, dict)]
    if allow_reply:
        return items, []

    blocked = {str(name) for name in drop}
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for item in items:
        name = _tool_name(item)
        if name in blocked:
            removed.append(name)
            continue
        kept.append(item)
    return kept, removed


#: 各档位在日志里的说法。
_REASON_TEXT = {
    REASON_AT: "被 @ 了，必回",
    REASON_MENTIONED: "被点名了，必回",
    REASON_EXHAUSTED: "精力耗尽，这轮不开口",
    REASON_BOUNDARY: "边界感判了不想接话，这轮不开口",
    REASON_DISABLED: "闸门未启用，交给宿主",
    REASON_LOW_SCORE: "分数不够，这轮不开口",
    REASON_NOTHING: "没有可评内容，这轮不开口",
    REASON_ALLOW: "可以开口",
}


def describe(decision: Decision) -> str:
    """一行给人看的判定摘要，直接进日志。"""

    text = _REASON_TEXT.get(decision.reason, decision.reason)
    if decision.parts:
        detail = " ".join(f"{key}={value:.2f}" for key, value in decision.parts.items())
        return f"{text}（score={decision.score:.2f} {detail}）"
    return f"{text}（score={decision.score:.2f}）"
