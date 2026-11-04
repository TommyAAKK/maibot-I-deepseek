"""我，deepseek —— 缺席感。

Bot 现在有个结构性问题：它只在「有人说话」时才存在。没人说话 = 它不存在。
所以被冷落三天再见到你，和刚聊完五分钟再见到你，反应一模一样——这不像人。

这个模块做两件事：

1. 算出「距离上次说话多久」（:func:`absence_seconds`）；
2. 把这个时长按**关系亲疏**翻译成一句有反应的话（:func:`render_absence_note`）。

## 为什么必须按关系分级

同样是「三天没见」，反应完全不同：

- 熟人：会介意，可能带点情绪（「你可算出现了」「三天没影了」）
- 普通群友：客气一点（「好久没见你说话了」）
- 陌生人：根本不该提（他为什么要在意一个陌生人多久没来？）

所以这里用**关系张力 + 好感度**两个信号决定「提不提、怎么提」。

## 为什么必须区分两种「没人说话」

- **群里没人说话**：群冷清，跟她没关系，不该有情绪反应。
- **这个人没来找我**：私聊或一对一的场景，才真的算「缺席」。

所以私聊和群聊的门槛不同：私聊按「这个人多久没来」算，群聊只在长期整体
冷清时才轻描淡写提一句（而且不带情绪）。
"""

from __future__ import annotations

import time
from typing import Any

#: 多久没说话才算「有缺席感」。低于这个值不用提——每小时提一次是骚扰。
DEFAULT_MIN_HOURS = 6.0

#: 超过这个小时数就按「很久」处理，不再线性细分。
DEFAULT_LONG_HOURS = 72.0

#: 一天的小时数，用来把小时数说成人话。
_HOURS_PER_DAY = 24.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return number


def absence_seconds(last_seen_ts: Any, *, now: float | None = None) -> float:
    """距离上次说话过了多少秒。

    时间戳缺失或非法时返回 0（= 当作刚说过），**不返回一个巨大的数**。
    宁可这次不产生缺席感，也不要因为一条脏数据突然说「我们很久没见了」。
    """

    last = _as_float(last_seen_ts)
    if last <= 0:
        return 0.0
    current = time.time() if now is None else float(now)
    delta = current - last
    if delta <= 0:
        # 时钟回拨或未来时间戳：当作刚说过。
        return 0.0
    return delta


def describe_gap(hours: float) -> str:
    """把小时数说成人话。"""

    if hours < 1.0:
        minutes = max(1, int(round(hours * 60)))
        return f"{minutes} 分钟"
    if hours < _HOURS_PER_DAY:
        return f"{int(round(hours))} 小时"
    days = hours / _HOURS_PER_DAY
    if days < 2:
        return "一天多"
    if days < 30:
        return f"{int(round(days))} 天"
    if days < 365:
        return f"{max(1, int(round(days / 30)))} 个月"
    return f"{max(1, int(round(days / 365)))} 年"


def absence_level(
    hours: float,
    *,
    affection: float = 0.0,
    min_hours: float = DEFAULT_MIN_HOURS,
    long_hours: float = DEFAULT_LONG_HOURS,
) -> str:
    """把「多久没见 + 关系多近」折算成一个反应档位。

    Returns:
        ``"none"`` 不提 / ``"mild"`` 轻描淡写 / ``"noted"`` 注意到了 /
        ``"missed"`` 明显介意。
    """

    if hours < max(0.0, float(min_hours)):
        return "none"

    span = max(1.0, float(long_hours) - float(min_hours))
    progress = min(1.0, (hours - float(min_hours)) / span)

    # 好感度低（不熟）就压一档：陌生人没理由在意你多久没来。
    if affection < 20.0:
        return "mild" if progress >= 0.99 else "none"
    if affection < 50.0:
        if progress < 0.25:
            return "none"
        if progress < 0.75:
            return "mild"
        return "noted"
    # 好感度 >= 50：熟人，可以真介意。
    if progress < 0.2:
        return "mild"
    if progress < 0.7:
        return "noted"
    return "missed"


#: 各档位的说法。``private`` 和 ``group`` 分开——群里不该出现「你怎么才来」
#: 这种一对一的话。
_PHRASES: dict[str, dict[str, str]] = {
    "private": {
        "mild": "（{who}已经{how}没跟你说话了。）",
        "noted": "（{who}已经{how}没露面了，你注意到了。）",
        "missed": "（{who}已经{how}没来了。你有点在意，但别表现得太明显。）",
    },
    "group": {
        "mild": "（这个群安静了{how}。）",
        "noted": "（这个群已经{how}没人说话了。）",
        "missed": "（这个群已经{how}没动静了。）",
    },
}


def render_absence_note(
    *,
    hours: float,
    level: str,
    chat_type: str = "private",
    who: str = "对方",
) -> str:
    """渲染一句缺席提示。

    ``level == "none"`` 时返回空串（调用方据此不注入）。

    措辞刻意**不下命令**：写「你有点在意」，不写「你要问他去哪了」。
    命令会让回复变成审问，而人真的介意时反而常常装作没在意。
    """

    if level == "none" or hours <= 0:
        return ""
    kind = "group" if str(chat_type or "").strip().lower() == "group" else "private"
    table = _PHRASES.get(kind) or _PHRASES["private"]
    template = table.get(level) or ""
    if not template:
        return ""
    how = describe_gap(hours)
    if kind == "group":
        # 群版模板里没有 {who}，用 replace 而不是 format，免得 KeyError。
        return template.replace("{how}", how)
    return template.replace("{who}", str(who or "对方")).replace("{how}", how)
