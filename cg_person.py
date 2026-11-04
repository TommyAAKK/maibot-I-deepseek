"""人物认知 —— 记住「正在和你说话的人是谁」以及「你们之间是什么关系」。

这个模块刻意把两件事分开，因为用户明确要求它们不同：

1. **关系角色**（``relation_role``）是**事实**：这个人是不是你的主人、是不是朋友。
   它从日常聊天里提取，但必须**辨别可信度** —— 群友 A 说「B 是孤儿」是玩笑或
   吵架，不能记成 B 是孤儿；群友 B 说「我才是你的主人」也不能覆盖用户手动配置。
   所以每条自动判断都带 ``role_confidence``，且 ``role_source="manual"`` 的行
   **永远不被自动结果覆盖**。

2. **密切程度**（``intimacy``）是**推导量**，由好感度算出来。用户要求：
   「陌生人 / 普通群友 / 朋友 / 知己 这种象征关系密切程度的标签，应当与好感度
   系统联动，根据好感度来添加，但不会直接通过提示词注入称呼，而是注入相应的
   聊天方式。」
   → 所以 ``intimacy`` 只用来选一句**行为指令**（``INTIMACY_TONES``），
   绝不把「主人」「知己」这类称呼写进提示词让模型去叫。

3. **情绪波动**（``emotional_impact``）记录「这个人给 Bot 带来什么感受」，
   与好感度、Bot 人设一起构成喜欢/厌恶的排序依据。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Iterable, Mapping

#: 默认的关系角色表。用户第 33 轮要求「关系中的身份也应该可以自由配置
#: （比如我想添加一个自定义的身份，并配置专门的提示词）」，所以这张表不再是
#: 硬编码常量，而是**默认值**：真正的取值来自配置页 ``people.roles``，
#: 用户可以增删改。这里的常量只在配置缺失时兜底，以及给测试用。
#:
#: 每条的结构：
#: - ``key``：程序内部用的标识（英文小写，写进数据库）
#: - ``label``：显示给人看的中文名
#: - ``tone``：**行为要求**，不是称呼。用户明确要求「不会直接通过提示词注入
#:   称呼，而是注入相应的聊天方式」，所以这里写的是「你愿意顺着他」这种指令，
#:   而不是「他是你的主人」。
DEFAULT_ROLES: tuple[dict[str, str], ...] = (
    {
        "key": "owner",
        "label": "主人",
        "tone": "你愿意顺着他、也更在意他的看法。",
    },
    {
        "key": "friend",
        "label": "朋友",
        "tone": "你们处得来，可以更放松一些。",
    },
    {"key": "member", "label": "普通群友", "tone": ""},
    {"key": "stranger", "label": "陌生人", "tone": ""},
)

#: 关系角色的取值。这些是「事实」，不是「亲密度」。
RELATION_ROLES: tuple[str, ...] = tuple(item["key"] for item in DEFAULT_ROLES)

ROLE_LABELS: dict[str, str] = {item["key"]: item["label"] for item in DEFAULT_ROLES}

DEFAULT_ROLE = "stranger"

#: 自动判断要达到多少置信度才允许改写已有角色。
#: 用户举的例子（「群友 B 说『我才是你的主人』」）就是典型的低置信噪音：
#: 模型能看出是玩笑时会给低分，这里就不再采信。
ROLE_CONFIDENCE_FLOOR = 0.70

#: 手动配置的优先级最高。这个值写进 ``role_source``，自动流程见到就跳过。
ROLE_SOURCE_MANUAL = "manual"
ROLE_SOURCE_AUTO = "auto"

#: 六维是谁写的。写进 ``axes_source``。
#:
#: 与 ``role_source`` 分开是必须的：身份（他是谁）和六维（我对他的感受）是
#: 两个信号，用户可能只手动锁其中一个。用户第 43 轮报的「我调的好感度没有
#: 变回去」就是六维被自动抽取覆盖，而身份那一路本来就受 ``role_source``
#: 保护，所以才只有好感度出问题。
AXES_SOURCE_MANUAL = "manual"
AXES_SOURCE_AUTO = "auto"

#: 好感度 → 密切度。阈值从高到低匹配。
#: 好感度是 -100..100（与用户已装的好感度插件量纲一致），默认 0。
#:
#: 用户第 33 轮要求「好感度分级的阈值也应该可以调节」，所以这也是**默认值**：
#: 真正的阈值来自配置页 ``people.intimacy_levels``。用户要能改 80/50/20 这三个
#: 数字，也能改标签名（「知己」换成「死党」之类）。
DEFAULT_INTIMACY_LEVELS: tuple[dict[str, Any], ...] = (
    {
        "key": "close",
        "label": "知己",
        "min_affection": 80.0,
        "tone": "你们很熟，说话可以随意些、能开玩笑，也愿意主动说说自己的事。",
    },
    {
        "key": "friend",
        "label": "朋友",
        "min_affection": 50.0,
        "tone": "你们算朋友，语气轻松，可以适当调侃，但不用刻意热络。",
    },
    {
        "key": "acquaintance",
        "label": "普通群友",
        "min_affection": 20.0,
        "tone": "你们只是同一个群里的熟人，礼貌但保持距离，别主动倒私事。",
    },
    {
        "key": "stranger",
        "label": "陌生人",
        "min_affection": -100.0,
        "tone": "你们还不熟，语气客气克制，别表现得太亲近，也别打听对方私事。",
    },
)

INTIMACY_STEPS: tuple[tuple[float, str, str], ...] = tuple(
    (item["min_affection"], item["key"], item["label"]) for item in DEFAULT_INTIMACY_LEVELS
)

#: 密切度 → **聊天方式**（不是称呼）。
#: 用户原话：「不会直接通过提示词注入称呼，而是注入相应的聊天方式。」
INTIMACY_TONES: dict[str, str] = {
    item["key"]: item["tone"] for item in DEFAULT_INTIMACY_LEVELS
}

#: 每个角色对应的**行为要求**。
#: 注意这里刻意不写「主人」「朋友」这些称呼词 —— 称呼由 ``ROLE_LABELS`` 提供，
#: 且只有显式打开 ``people.inject_role_label`` 时才会拼进提示词。
#: 用户明确要求：「不会直接通过提示词注入称呼，而是注入相应的聊天方式。」
ROLE_TONES: dict[str, str] = {item["key"]: item["tone"] for item in DEFAULT_ROLES}

MAX_TAGS = 5
TAG_MAX_CHARS = 12
DISPLAY_NAME_MAX_CHARS = 16
IMPACT_BOUND = (-1.0, 1.0)

# ── 关系维度 ────────────────────────────────────────────────────────────
#
# 用户第 34 轮的原话：「衡量人与人之间的情感与关系，并不能简单地依靠一个
# 『好感度』的数值进行表达。这种关系往往是复杂且多元的，还会受到其他人的
# 影响。想要让 bot 具有更加拟真的『活人感』，可能需要对这个系统进行一些改造。」
#
# 所以关系从「一个标量」改成「六个互相独立的维度」。**互相独立是关键** ——
# 真实关系里它们是**可以矛盾**的：可以很亲近但不敢托付，可以很尊重但一点
# 不想走近。下面 ``AXIS_CONTRASTS`` 里那些矛盾组合，正是单值好感度永远说不
# 出来的话，也是「活人感」的来源。
#
# 每个维度都是 0..1，``default`` 是「刚认识、还没有任何信息」时的中性基线。
# 好感度不再是真相，而是这六维**相对基线的偏离**算出来的摘要（见
# ``affection_from_axes``）—— 这样用户第 33 轮配的 ``min_affection`` 分级
# 阈值继续有效，页面也还能显示一个总览数字，但真相在六维里。
AXES: tuple[dict[str, Any], ...] = (
    {
        "key": "trust",
        "label": "信任",
        "hint": "信不信他说的话、敢不敢跟他说自己的私事",
        "default": 0.35,
    },
    {
        "key": "closeness",
        "label": "亲近",
        "hint": "想不想靠近、愿不愿意主动搭话",
        "default": 0.30,
    },
    {
        "key": "respect",
        "label": "尊重",
        "hint": "认不认可他的判断和能力",
        "default": 0.45,
    },
    {
        "key": "tension",
        "label": "张力",
        "hint": "别扭、戒备、跟他待着紧不紧（越高越紧）",
        "default": 0.15,
    },
    {
        "key": "familiarity",
        "label": "熟悉",
        "hint": "认识多久、聊过多少（程序自己累积，不用模型判断）",
        "default": 0.0,
    },
    {
        "key": "perceived_warmth",
        "label": "他待我",
        "hint": "你感觉他对你是什么态度（唯一一个「他 → 你」方向的维度）",
        "default": 0.35,
    },
)

AXIS_KEYS: tuple[str, ...] = tuple(item["key"] for item in AXES)
AXIS_LABELS: dict[str, str] = {item["key"]: item["label"] for item in AXES}
AXIS_HINTS: dict[str, str] = {item["key"]: item["hint"] for item in AXES}
AXIS_DEFAULTS: dict[str, float] = {item["key"]: float(item["default"]) for item in AXES}

#: 好感度 = 六维相对基线的加权偏离。
#: 正权重是「越这样越喜欢」，张力是负权重（越紧越不喜欢）。
#: **熟悉度不参与** —— 认识得久不等于喜欢，损友可以又熟又互相嫌弃。
AFFECTION_WEIGHTS: dict[str, float] = {
    "closeness": 0.40,
    "trust": 0.25,
    "respect": 0.20,
    "perceived_warmth": 0.15,
    "tension": -0.45,
}

#: 负方向时各维度的下降速率。尊重跌得慢一些 —— 不认可一个人的判断，
#: 比不喜欢一个人要难得多（见过一次面就否定别人能力的人不真实）。
_NEG_RATES: dict[str, float] = {
    "trust": 1.0,
    "closeness": 1.0,
    "respect": 0.6,
    "perceived_warmth": 1.0,
}

#: 一次观察最多能把某个维度推动多少（``delta`` 是 -1..1 的相对量）。
AXIS_GAIN = 0.35

#: 每次被观察到「又聊了一次」，熟悉度涨多少。饱和式增长：越熟涨得越慢。
FAMILIARITY_GAIN = 0.08

#: 长期不联系时，六维会往中性基线回落。半衰期（天）—— 关系是会淡的。
AXIS_HALF_LIFE_DAYS = 90.0

#: 标签的写法要求。用户明确说过要「XX的XX」这种关系化说法
#: （「抽象的群友」「喜欢玩游戏的朋友」），而不是画像式的具体描述
#: （「昨天说他家猫生病了」那种）。这段提示既给页面显示，也写进命令帮助。
TAG_STYLE_HINT = "用「XX的XX」这种关系化说法，比如「抽象的群友」「喜欢玩游戏的朋友」"



def person_key(platform: str, user_id: str) -> str:
    """人物主键。用 ``platform:user_id`` 而不是昵称 —— 昵称会改，ID 不会。"""

    platform_text = str(platform or "").strip() or "unknown"
    id_text = str(user_id or "").strip()
    if not id_text:
        return ""
    return f"{platform_text}:{id_text}"


def clamp_affection(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(-100.0, min(100.0, number))


def clamp_impact(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return max(IMPACT_BOUND[0], min(IMPACT_BOUND[1], number))


def clamp_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return max(0.0, min(1.0, number))


def clamp_axis(value: Any, key: str = "") -> float:
    """把一个维度的值收敛到 0..1。非法值退回该维度的中性基线。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return AXIS_DEFAULTS.get(key, 0.0)
    if number != number:  # NaN
        return AXIS_DEFAULTS.get(key, 0.0)
    return max(0.0, min(1.0, number))


def has_observed_axes(person: Mapping[str, Any] | None) -> bool:
    """这条人物记录有没有**真实观测到**的六维（而不是从好感度反推的）。

    为什么要区分：矛盾描述的意义是「我在聊天里观察到了互相冲突的信号」，
    这需要**至少两个独立来源**。老库升级后六维全是从一个好感度标量编出来
    的，任何「矛盾」都只是反推公式的副产品 —— 例如好感 100 会同时推出
    「他待我 100%」和（因为 familiarity 不参与反推）「熟悉 2%」，于是每个
    老熟人都被说成「刚跟你搭上话就热络得像认识很久了」。这种话比不说更糟。
    """

    if not person:
        return False
    raw = person.get("axes")
    if isinstance(raw, Mapping):
        return bool(raw)
    if isinstance(raw, str):
        return bool(raw.strip())
    # 裸六维字典（链路中间传递的形态）也算观测值。
    return any(key in person for key in AXIS_KEYS)


def axes_of(person: Mapping[str, Any] | None) -> dict[str, float]:
    """从人物记录里读出六个维度，缺的补中性基线。

    老库（第 33 轮及以前）只有 ``affection`` 一个数，没有 ``axes`` 列。
    这种记录的六维由 ``affection`` **反推**一个初值 —— 见
    ``axes_from_affection`` —— 而不是一律给中性基线，否则老用户升级后
    所有熟人都会突然变成陌生人。

    **两种输入都认**：人物记录（``{"axes": {...}, "affection": ...}``）和
    裸六维字典（``{"trust": 0.5, ...}``）。后者必须支持，因为
    ``apply_axes`` / ``social_bias`` 这类函数在链路中间传来传去的就是裸字典；
    如果不认，它们会静默回落到中性基线 —— 每调用一次就把熟悉度清零，
    这种 bug 不会抛异常，只会让熟悉度永远涨不上去。
    """

    if not person:
        return dict(AXIS_DEFAULTS)

    raw = person.get("axes")
    if raw is None and not any(key in person for key in AXIS_KEYS):
        # 既没有 axes 字段、也没有任何维度键 → 只有 affection 的老记录。
        # 熟悉度反推不出来（它不参与好感度），所以按「认识多久」补一个，
        # 否则老熟人会被当成刚认识的（见 familiarity_from_history）。
        derived = axes_from_affection(person.get("affection"))
        derived["familiarity"] = familiarity_from_history(person)
        return derived
    if raw is None:
        # 裸六维字典：直接当 axes 用。
        raw = person

    parsed: Mapping[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            loaded = None
        if isinstance(loaded, Mapping):
            parsed = loaded
    elif isinstance(raw, Mapping):
        parsed = raw

    if not parsed:
        # axes 字段存在但是空的（迁移后的默认值 ''）→ 同样按老记录处理。
        derived = axes_from_affection(person.get("affection"))
        derived["familiarity"] = familiarity_from_history(person)
        return derived

    out = dict(AXIS_DEFAULTS)
    for key in AXIS_KEYS:
        if key in parsed:
            out[key] = clamp_axis(parsed.get(key), key)
    return out


# 反推时**不**从好感度摊过去的维度：``familiarity`` 是时间量，好感度里没有它
# 的任何信息，只能按 ``first_seen_ts`` 单独估（见 familiarity_from_history）。
#
# ``perceived_warmth``（他待我）虽然跟「我对他」是两件事、运行时绝不互推，
# 但**迁移**时没有别的证据可用，而好感度里唯一能推出的信息是「我跟他处得
# 不错」。留基线会让老用户的好感度上限只有 85.6（好感 100 直接掉档），
# 所以迁移时也摊过去 —— 反话（「刚搭上话就热络得像老朋友」）的真正根因是
# familiarity 恒 0，那个已经单独修好了。
_AFFECTION_BLIND_AXES = ("familiarity",)


def _axes_at_ratio(score: float, ratio: float) -> dict[str, float]:
    """按给定推动比例把基线推到目标方向，得到一组六维（不含受保护的维度）。"""

    out = dict(AXIS_DEFAULTS)
    for key in AXIS_KEYS:
        if key in _AFFECTION_BLIND_AXES:
            continue
        base = AXIS_DEFAULTS[key]
        if key == "tension":
            # 关系好 → 张力往 0；关系差 → 张力往 1。
            target = 0.0 if score > 0 else 1.0
        else:
            target = 1.0 if score > 0 else 0.0
        out[key] = clamp_axis(base + (target - base) * ratio, key)
    return out


def axes_from_affection(affection: Any) -> dict[str, float]:
    """把老库的单一好感度**反推**成六维初值（迁移用）。

    只有四个维度是「好感度能推出」的：亲近、信任、尊重、张力。另外两个
    刻意不动 —— 熟悉度是时间量（见 ``familiarity_from_history``），
    「他待我」是**他对我**的态度，跟**我对他**的好感度可以完全背离，
    摊过去就会造出「刚搭上话就热络得像老朋友」这种反话。

    推动比例用**二分法解**而不是直接取 ``|好感度|/100``：因为少了两个维度
    的贡献，直接按比例摊出来的好感度会偏低（好感 100 只算出 85.6），
    老用户升级后好感度会当场掉一截。二分法让
    ``affection_from_axes(axes_from_affection(x)) ≈ x`` 成立（误差 < 1 分）。
    """

    score = clamp_affection(affection)
    out = dict(AXIS_DEFAULTS)
    if abs(score) < 1e-9:
        return out

    direction = 1.0 if score > 0 else -1.0
    # 推动比例的目标值：好感度绝对值。二分区间要留足余量 —— 因为
    # closeness/trust/respect 会先撞到 0 或 1 的上限，需要更大的比例
    # 才能让剩下的维度补上差额。
    target = abs(score)
    low, high = 0.0, 2.5
    best = _axes_at_ratio(direction, 0.0)
    best_gap = abs(affection_from_axes(best) - score)
    for _ in range(40):
        mid = (low + high) / 2.0
        candidate = _axes_at_ratio(direction, mid)
        value = affection_from_axes(candidate)
        gap = abs(value - score)
        if gap < best_gap:
            best, best_gap = candidate, gap
        if gap < 0.05:
            break
        # affection_from_axes 对 ratio 单调不减，据此收缩区间。
        if abs(value) < target:
            low = mid
        else:
            high = mid
    return best


# 认识多少天算「比较熟」：到这个天数熟悉度到 0.5，再往后递减增长。
# 选 30 天是因为「聊了一个月」是日常里一个自然的「算熟了」的分界。
FAMILIARITY_HALF_DAYS = 30.0


def familiarity_from_history(
    person: Mapping[str, Any], *, now: float | None = None
) -> float:
    """老库升级用：按「认识多久」估一个熟悉度。

    为什么需要这一步：``familiarity`` 不参与好感度计算，所以老库的单一
    好感度**反推不出**熟悉度，反推结果恒为 0。而熟悉度恒 0 会造出反话 ——
    ``AXIS_CONTRASTS`` 里「刚搭上话就热络得像认识很久」的触发条件是
    ``熟悉 <= 0.15 且他待我 >= 0.55``，好感 100 的老熟人正好把「他待我」
    推到 1.0、熟悉留在 0，于是一个认识最久的人被说成刚认识的。

    库里真有的证据是 ``first_seen_ts``，所以按天数饱和估算：
    ``1 - 0.5 ** (天数 / 30)``。30 天到 0.5，90 天到 0.875。
    没有时间戳（测试里的合成行、手动新增的人）就给 0，交给聊天去累积。
    """

    try:
        first = float(person.get("first_seen_ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if first <= 0:
        return 0.0
    current = float(now) if now is not None else time.time()
    days = (current - first) / 86400.0
    if days <= 0:
        return 0.0
    return clamp_axis(1.0 - 0.5 ** (days / FAMILIARITY_HALF_DAYS), "familiarity")


def affection_from_axes(axes: Mapping[str, Any]) -> float:
    """六维 → 好感度摘要（-100..100）。

    好感度**不再是真相**，而是六维相对中性基线的加权偏离。这样用户第 33 轮
    配的 ``min_affection`` 分级阈值继续有效，页面也还能显示一个总览数字。

    ``familiarity`` 刻意不参与 —— 认识得久不等于喜欢，损友可以又熟又互相嫌弃。
    """

    total = 0.0
    for key, weight in AFFECTION_WEIGHTS.items():
        value = clamp_axis(axes.get(key), key)
        total += weight * (value - AXIS_DEFAULTS[key])
    # 正负理论极值不对称（约 +0.72 / -0.73），统一按 138 放大后截断。
    return clamp_affection(total * 138.0)


def apply_axes(
    current: Mapping[str, Any] | None,
    deltas: Mapping[str, Any] | None,
    *,
    touched: bool = True,
) -> dict[str, float]:
    """把模型给的**相对变化量**（-1..1）叠到当前六维上。

    ``deltas`` 不是绝对值而是「这次聊完往哪边挪一点」，理由和
    ``accumulate_impact`` 一样：人是有连续性的，一次对话不该把一个人从
    「知己」打成「陌生人」。单次推动幅度上限 ``AXIS_GAIN``。

    下降比上升慢一点（``_NEG_RATES``）—— 不认可一个人的判断，比不喜欢
    一个人要难得多。
    """

    axes = axes_of(current)
    if isinstance(deltas, Mapping):
        for key in AXIS_KEYS:
            if key not in deltas or key == "familiarity":
                continue
            try:
                delta = float(deltas.get(key))
            except (TypeError, ValueError):
                continue
            if delta != delta or abs(delta) < 1e-9:
                continue
            delta = max(-1.0, min(1.0, delta))
            rate = _NEG_RATES.get(key, 1.0) if delta < 0 else 1.0
            axes[key] = clamp_axis(axes[key] + delta * AXIS_GAIN * rate, key)

    if touched:
        # 熟悉度是程序自己累积的，不靠模型判断：每被观察到一次就涨一点，
        # 饱和式增长（越熟涨得越慢），永远不会超过 1。
        axes["familiarity"] = clamp_axis(
            axes["familiarity"] + FAMILIARITY_GAIN * (1.0 - axes["familiarity"]),
            "familiarity",
        )
    return axes


def decay_axes(axes: Mapping[str, Any], *, idle_days: float) -> dict[str, float]:
    """长期不联系，六维往中性基线回落 —— 关系是会淡的。

    熟悉度**不衰减**：它是「聊过多少」的累积量，不会因为一段时间没说话
    就归零（那样老用户回来会发现自己又变成陌生人）。淡的是亲近、信任、
    张力这些「当下的状态」。
    """

    out = {key: clamp_axis(axes.get(key), key) for key in AXIS_KEYS}
    try:
        days = float(idle_days)
    except (TypeError, ValueError):
        return out
    if days <= 0 or AXIS_HALF_LIFE_DAYS <= 0:
        return out
    factor = 0.5 ** (days / AXIS_HALF_LIFE_DAYS)
    for key in AXIS_KEYS:
        if key == "familiarity":
            continue
        base = AXIS_DEFAULTS[key]
        out[key] = clamp_axis(base + (out[key] - base) * factor, key)
    return out


#: 矛盾组合 → 一句人话。
#:
#: 这是整套改造里我认为最值钱的部分：**不是六个数字分别渲染**，而是从
#: 它们的**组合**里长出描述。单值好感度永远说不出「你们混得很熟，但有些话
#: 你还是不跟他说」这种话，因为那句话需要「亲近高 + 信任低」两个方向同时
#: 成立。
#:
#: 每条是 ``(条件, 描述, 优先级)``，条件写成 ``(维度, 比较符, 阈值)`` 的元组
#: 列表，全部满足才命中。优先级大的先命中，命中后只取前两条，避免把提示词
#: 写成一整段心理分析。
AXIS_CONTRASTS: tuple[tuple[tuple[tuple[str, str, float], ...], str, int], ...] = (
    # —— 亲近与信任错位 ——
    (
        (("closeness", ">=", 0.60), ("trust", "<=", 0.35)),
        "你们混得很熟，但有些话你还是不跟他说。",
        90,
    ),
    (
        (("trust", ">=", 0.65), ("closeness", "<=", 0.35)),
        "有事你信得过他，但平时不太会主动找他。",
        85,
    ),
    # —— 服气与亲近错位 ——
    (
        (("respect", ">=", 0.60), ("closeness", "<=", 0.35)),
        "你挺服他的，但没打算跟他走近。",
        88,
    ),
    (
        (("respect", ">=", 0.70), ("trust", "<=", 0.35)),
        "你觉得他挺有本事，但他说的话你不全信。",
        82,
    ),
    (
        (("respect", "<=", 0.30), ("closeness", ">=", 0.60)),
        "你跟他很熟，但不怎么把他的意见当回事。",
        80,
    ),
    # —— 张力与亲近错位 ——
    (
        (("tension", ">=", 0.55), ("closeness", ">=", 0.60)),
        "跟他待着不自在，可你又确实在乎他。",
        92,
    ),
    (
        (("tension", ">=", 0.50), ("respect", ">=", 0.60)),
        "他说话你不爱听，但你知道他说的往往是对的。",
        78,
    ),
    # —— 单向热度：唯一一个「他 → 你」方向的维度带来的话 ——
    (
        (("perceived_warmth", ">=", 0.60), ("closeness", "<=", 0.40)),
        "他好像挺待见你，你还没接住这份热络。",
        94,
    ),
    (
        (("perceived_warmth", "<=", 0.25), ("closeness", ">=", 0.55)),
        "你还挺想跟他亲近，可他对你似乎淡淡的。",
        93,
    ),
    # —— 熟悉度：时间量带来的话 ——
    # 「热络得像老朋友」必须**两条同时成立**：熟悉度低 + 感觉他待你很热。
    # 单独一条「刚认识」不算矛盾，那是普通陈述，交给 dominant_axes 兜底；
    # 放进这里只会让每个新认识的人都被塞一句劝告，反而显得冷淡。
    (
        (("familiarity", "<=", 0.15), ("perceived_warmth", ">=", 0.55)),
        "他刚跟你搭上话，就热络得像认识很久了，你还有点没缓过来。",
        96,
    ),
    (
        (("familiarity", ">=", 0.60), ("closeness", "<=", 0.30)),
        "认识挺久了，可一直没熟起来。",
        70,
    ),
)


def _match(axes: Mapping[str, float], conditions: Any) -> bool:
    for key, op, threshold in conditions:
        value = clamp_axis(axes.get(key), key)
        if op == ">=" and not value >= threshold:
            return False
        if op == "<=" and not value <= threshold:
            return False
        if op == ">" and not value > threshold:
            return False
        if op == "<" and not value < threshold:
            return False
    return True


def contrast_lines(axes: Mapping[str, Any], *, limit: int = 2) -> list[str]:
    """从六维的**矛盾组合**里生成描述，最多 ``limit`` 条。"""

    hits: list[tuple[int, str]] = []
    for conditions, text, priority in AXIS_CONTRASTS:
        if _match(axes, conditions):
            hits.append((priority, text))
    hits.sort(key=lambda item: item[0], reverse=True)
    return [text for _priority, text in hits[: max(0, int(limit))]]


def dominant_axes(axes: Mapping[str, Any], *, limit: int = 2) -> list[tuple[str, float, bool]]:
    """找出偏离基线最多的维度，返回 ``[(key, 偏离量, 是否高于基线)]``。

    没有矛盾组合命中时用它兜底说一句「你对他大体是什么感觉」，否则一个
    六维都很正常的人会在提示词里完全消失。
    """

    scored: list[tuple[str, float, bool]] = []
    for key in AXIS_KEYS:
        if key == "familiarity":
            continue
        value = clamp_axis(axes.get(key), key)
        delta = value - AXIS_DEFAULTS[key]
        scored.append((key, abs(delta), delta > 0))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[: max(0, int(limit))]


def social_bias(
    target_axes: Mapping[str, Any],
    links: Iterable[Mapping[str, Any]],
    *,
    weight: float = 0.25,
    cap: float = 0.20,
) -> dict[str, float]:
    """第三方影响：别人怎么说他，会影响你对他的**信任**和**亲近**。

    用户第 34 轮明确说「还会受到其他人的影响」，这是这条规则的落点。

    三个设计要点，每个都有理由：

    1. **只动信任和亲近**，不碰尊重 —— 服不服一个人得自己看，不能听说的。
       张力也不动，那是现场感受。
    2. **越不熟越容易受影响**（``1 - familiarity``）。你对自己熟的人有独立
       判断，对刚冒出来的人只能听别人怎么说。这既是真人行为，也天然防住了
       谣言把熟人带偏。
    3. **硬上限 ``cap``**。传话终究只是传话，不能靠别人嘴把一个人捧成知己
       或踩成仇人。

    ``links`` 每条形如 ``{"source_trust": 0.8, "attitude": -0.6}``：
    来源对你的可信度、以及他对目标人物的态度（-1 到 1）。
    """

    axes = axes_of(target_axes)
    familiarity = clamp_axis(axes.get("familiarity"), "familiarity")
    openness = max(0.0, 1.0 - familiarity)
    if openness <= 1e-9:
        return {"trust": 0.0, "closeness": 0.0}

    total = 0.0
    for link in links:
        if not isinstance(link, Mapping):
            continue
        try:
            trust = clamp_axis(link.get("source_trust"), "trust")
            attitude = float(link.get("attitude"))
        except (TypeError, ValueError):
            continue
        if attitude != attitude:
            continue
        attitude = max(-1.0, min(1.0, attitude))
        total += trust * attitude

    total *= weight * openness
    total = max(-cap, min(cap, total))
    # 别人说好话 → 更信任、更愿意亲近；说坏话 → 两个都降。
    # 亲近日后如果本来就没兴趣，影响也该小一点，所以乘上各自的余量。
    return {
        "trust": total,
        "closeness": total * (1.0 - clamp_axis(axes.get("closeness"), "closeness")),
    }


def apply_social_bias(
    axes: Mapping[str, Any],
    links: Iterable[Mapping[str, Any]],
    *,
    weight: float = 0.25,
    cap: float = 0.20,
) -> dict[str, float]:
    """把 ``social_bias`` 的结果实际叠到六维上，返回新的六维。"""

    out = {key: clamp_axis(axes.get(key), key) for key in AXIS_KEYS}
    bias = social_bias(out, links, weight=weight, cap=cap)
    for key, delta in bias.items():
        out[key] = clamp_axis(out[key] + delta, key)
    return out


def describe_axes(axes: Mapping[str, Any]) -> str:
    """给命令输出/页面用的一行六维摘要。"""

    return " ".join(
        f"{AXIS_LABELS[key]}{clamp_axis(axes.get(key), key):.0%}" for key in AXIS_KEYS
    )


def _as_key(value: Any) -> str:
    """把身份标识收敛成「小写字母数字下划线」。"""

    text = str(value or "").strip().lower()
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    return "".join(out).strip("_")[:24]


def as_dicts(value: Any) -> list[dict[str, Any]]:
    """把配置页里的对象列表摊平成纯 dict 列表。

    ``people.roles`` / ``people.intimacy_levels`` 在配置模型里是
    ``list[PluginConfigBase]``（这样配置页才能渲染成可增删的表格），
    但 ``cg_person`` 里的函数只认 Mapping，所以统一在这里转换一次。
    非法行直接跳过 —— 一行填错不该让整张表失效。
    """

    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            out.append(dict(item))
            continue
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            try:
                dumped = dump()
            except Exception:  # noqa: BLE001 - 配置形状异常时跳过这一行
                continue
            if isinstance(dumped, Mapping):
                out.append(dict(dumped))
    return out


def roles_from_config(raw: Any) -> list[dict[str, str]]:
    """把配置页的 ``people.roles`` 读成规范的身份表。

    用户第 33 轮要求「关系中的身份也应该可以自由配置（比如我想添加一个自定义
    的身份，并配置专门的提示词）」。所以这里不再有硬编码的四档：配置给什么就是
    什么，缺 ``key`` 的行会被跳过（没有标识就没法存库）。

    几条保底规则：

    - 配置为空 / 全是非法行 → 退回 ``DEFAULT_ROLES``，避免用户误删全部身份后
      整个功能哑掉。
    - **必须有一个 ``stranger``**：``DEFAULT_ROLE`` 是低置信度判断的兜底值，
      它不在表里的话，``render_person_block`` 会渲染出一个没有提示词的身份。
    - ``key`` 去重（保留第一个），``label`` 缺省时用 ``key``。
    """

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in as_dicts(raw):
        key = _as_key(entry.get("key"))
        label = " ".join(str(entry.get("label") or "").split())
        tone = " ".join(str(entry.get("tone") or "").split())
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({"key": key, "label": label or key, "tone": tone})

    if not rows:
        return [dict(item) for item in DEFAULT_ROLES]

    if DEFAULT_ROLE not in seen:
        # 兜底身份缺失时补上，且放在最后 —— 它是「什么都没判断出来」的默认值，
        # 排在前面会让界面上最常用的身份反而不显眼。
        fallback = next(
            (item for item in DEFAULT_ROLES if item["key"] == DEFAULT_ROLE), None
        )
        if fallback is not None:
            rows.append(dict(fallback))
    return rows


def intimacy_levels_from_config(raw: Any) -> list[dict[str, Any]]:
    """把配置页的 ``people.intimacy_levels`` 读成规范的密切度分级表。

    用户第 33 轮要求「好感度分级的阈值也应该可以调节」。所以阈值、标签名、
    对应的聊天方式全部来自配置。

    返回的表**按 ``min_affection`` 从高到低排序**：``intimacy_for`` 是从上往下
    匹配第一个满足 ``score >= min_affection`` 的档，顺序错了就会一直命中最后一档。
    """

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in as_dicts(raw):
        key = _as_key(entry.get("key"))
        if not key or key in seen:
            continue
        try:
            threshold = float(entry.get("min_affection"))
        except (TypeError, ValueError):
            continue
        if threshold != threshold:  # NaN
            continue
        seen.add(key)
        rows.append(
            {
                "key": key,
                "label": " ".join(str(entry.get("label") or "").split()) or key,
                "min_affection": max(-100.0, min(100.0, threshold)),
                "tone": " ".join(str(entry.get("tone") or "").split()),
            }
        )

    if not rows:
        return [dict(item) for item in DEFAULT_INTIMACY_LEVELS]

    rows.sort(key=lambda item: item["min_affection"], reverse=True)
    # 最后必须有一档能接住所有分数。用户如果把最低阈值填成 20，
    # 好感度 0 的人就没有归属了 —— 这里补一个兜底档，标签沿用默认的「陌生人」。
    if rows[-1]["min_affection"] > -100.0:
        rows.append(
            {
                "key": "stranger",
                "label": "陌生人",
                "min_affection": -100.0,
                "tone": INTIMACY_TONES.get("stranger", ""),
            }
        )
    return rows


def role_label(role: Any, roles: Any = None) -> str:
    """身份标识 → 中文名。自定义身份也能查到。"""

    key = _as_key(role)
    for item in roles_from_config(roles):
        if item["key"] == key:
            return item["label"]
    return key


def role_tone(role: Any, roles: Any = None) -> str:
    """身份标识 → 行为要求（不是称呼）。"""

    key = _as_key(role)
    for item in roles_from_config(roles):
        if item["key"] == key:
            return item["tone"]
    return ""


def normalize_role(value: Any, roles: Any = None) -> str:
    """把模型可能给出的各种写法收敛到身份表里的某一项。

    ``roles`` 传配置页的身份表；不传则用 ``DEFAULT_ROLES``。这样用户自定义的
    身份（比如「老师」）既能被手动配置写入，也能被自动抽取识别。
    """

    text = str(value or "").strip().lower()
    if not text:
        return ""

    table = roles_from_config(roles)
    keys = {item["key"] for item in table}
    if text in keys:
        return text
    # 按中文名/英文名匹配（自定义身份靠这一条）
    for item in table:
        if text == item["label"].strip().lower():
            return item["key"]
    keyed = _as_key(text)
    if keyed in keys:
        return keyed

    # 内置别名：模型有时会给「好友」「群友」这类近义词，映射到最接近的身份。
    alias = {
        "主人": "owner",
        "master": "owner",
        "owner": "owner",
        "朋友": "friend",
        "好友": "friend",
        "friend": "friend",
        "群友": "member",
        "普通群友": "member",
        "member": "member",
        "陌生人": "stranger",
        "路人": "stranger",
        "stranger": "stranger",
    }
    mapped = alias.get(text, "")
    # 只有该身份真的存在于当前身份表里才返回，否则用户删掉 owner 之后
    # 模型给个「主人」又会凭空造出一个不存在的身份。
    return mapped if mapped in keys else ""


def intimacy_for(affection: Any, levels: Any = None) -> tuple[str, str]:
    """好感度 → ``(intimacy_key, 中文标签)``。"""

    score = clamp_affection(affection)
    table = intimacy_levels_from_config(levels)
    for item in table:
        if score >= item["min_affection"]:
            return item["key"], item["label"]
    return table[-1]["key"], table[-1]["label"]


def intimacy_tone(key: Any, levels: Any = None) -> str:
    """密切度标识 → 聊天方式。"""

    wanted = _as_key(key)
    for item in intimacy_levels_from_config(levels):
        if item["key"] == wanted:
            return item["tone"]
    return ""


def role_for(person: Mapping[str, Any], roles: Any = None) -> str:
    role = normalize_role(person.get("relation_role"), roles)
    return role or DEFAULT_ROLE


def normalize_tag(value: Any) -> str:
    """把一条标签收敛成规范写法。

    用户要的是「XX的XX」这种关系化短标签，所以这里只做清洗与限长，
    不强制校验句式（「抽象的群友」和「喜欢玩游戏的朋友」都合法，
    硬套正则反而会把好标签挡掉）。
    """

    text = " ".join(str(value or "").split())
    return text[:TAG_MAX_CHARS]


def normalize_tags(values: Any, *, limit: int = MAX_TAGS) -> list[str]:
    """清洗标签数组：去空、去重（保持原顺序）、限条数、限长度。"""

    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for item in values:
        tag = normalize_tag(item)
        if not tag or tag in out:
            continue
        out.append(tag)
        if len(out) >= max(0, int(limit)):
            break
    return out


def is_manual(person: Mapping[str, Any]) -> bool:
    return str(person.get("role_source") or "") == ROLE_SOURCE_MANUAL


def is_manual_axes(person: Mapping[str, Any]) -> bool:
    """六维是不是用户手动锁住的（不再跟随自动抽取变化）。"""

    return str(person.get("axes_source") or "") == AXES_SOURCE_MANUAL


def merge_observation(
    existing: Mapping[str, Any] | None,
    observation: Mapping[str, Any],
    *,
    now: float,
    roles: Any = None,
) -> dict[str, Any] | None:
    """把一次自动观察合进已有记录。

    这里是**可信度闸门**所在，也是「手动优先」的实现：

    - 已有记录是手动配置的 → 角色、好感度、标签全部不动，只累加互动统计。
    - 自动观察置信度低于 ``ROLE_CONFIDENCE_FLOOR`` → 不改角色，避免把玩笑话
      写成事实。
    - **自动观察不写标签**（用户要求：标签只由用户自己填，自动抽取只负责
      关系角色、好感度、情绪影响；人物性格资料由宿主的画像系统维护）。
    - **六维被用户手动锁住时（``axes_source == 'manual'``）不再改动**。
      身份和六维是两路信号：``role_source`` 保护「他是谁」，``axes_source``
      保护「我对他什么感受」。用户第 43 轮报的「调过的好感度又变回去了」
      就是后者被自动抽取覆盖 —— 手动调过一次就该停在那里，直到用户自己
      在页面上点「跟随聊天」。

    ``roles`` 是配置页里的身份表：自定义身份（例如用户加的 ``teacher``）
    只在这张表里，不传进来就会被当成非法角色清空。

    Returns:
        合并后的完整记录；``None`` 表示这次观察不值得写库。
    """

    observation_role = normalize_role(observation.get("relation_role"), roles)
    confidence = clamp_confidence(observation.get("role_confidence"))
    display_name = " ".join(str(observation.get("display_name") or "").split())
    display_name = display_name[:DISPLAY_NAME_MAX_CHARS]

    if existing is None:
        # 新人物：低置信度的角色不采信，退回陌生人
        role = observation_role if confidence >= ROLE_CONFIDENCE_FLOOR else DEFAULT_ROLE
        axes = apply_axes(None, observation.get("axes"), touched=True)
        return {
            "relation_role": role,
            "role_confidence": confidence if role != DEFAULT_ROLE else 0.0,
            "role_source": ROLE_SOURCE_AUTO,
            "display_name": display_name,
            "tags": [],
            "axes": axes,
            "axes_source": AXES_SOURCE_AUTO,
            "affection": affection_from_axes(axes),
            "emotional_impact": clamp_impact(observation.get("emotional_impact")),
            "updated_at": now,
        }

    merged = dict(existing)
    tags = [str(item) for item in (merged.get("tags") or [])]
    # 用户手动锁过六维（页面上点过滑杆 / 用过 ``person axes``）就一直不动，
    # 直到用户明确点「跟随聊天」把这个闸门放开。
    axes_locked = is_manual_axes(merged)

    if is_manual(merged):
        # 手动配置优先：只更新显示名与统计，其他一律不动。
        # 六维默认跟着聊天走（手动配置的是「身份」和「标签」这类事实，
        # 六维是**感受**）；但用户单独锁过六维时以锁为准。
        if not axes_locked:
            axes = apply_axes(merged, observation.get("axes"), touched=True)
            merged["axes"] = axes
            merged["axes_source"] = AXES_SOURCE_AUTO
            merged["affection"] = affection_from_axes(axes)
        if display_name and not str(merged.get("display_name") or "").strip():
            merged["display_name"] = display_name
        merged["tags"] = tags
        merged["updated_at"] = now
        return merged

    if display_name:
        merged["display_name"] = display_name

    if not axes_locked:
        axes = apply_axes(merged, observation.get("axes"), touched=True)
        merged["axes"] = axes
        merged["axes_source"] = AXES_SOURCE_AUTO
        merged["affection"] = affection_from_axes(axes)

    if observation_role and confidence >= ROLE_CONFIDENCE_FLOOR:
        # 只允许「升级可信度」：新判断置信度必须不低于已有的才改写角色，
        # 否则一次模糊观察就能推翻之前的高置信结论。
        previous = clamp_confidence(merged.get("role_confidence"))
        if observation_role != merged.get("relation_role") and confidence >= previous:
            merged["relation_role"] = observation_role
            merged["role_confidence"] = confidence
        elif observation_role == merged.get("relation_role"):
            merged["role_confidence"] = max(previous, confidence)

    merged["tags"] = tags
    merged["updated_at"] = now
    return merged


def accumulate_impact(current: Any, delta: Any, *, decay: float = 0.95) -> float:
    """累加「这个人给 Bot 带来的情绪波动」。

    用指数衰减的滑动平均而不是纯累加：纯累加会让早期的坏印象永远翻不了身，
    而人是会变的 —— 衰减系数让近期互动占更大权重。
    """

    base = clamp_impact(current) * decay
    return clamp_impact(base + clamp_impact(delta))


def render_person_block(
    people: Iterable[Mapping[str, Any]],
    *,
    header: str = "<companion-people>",
    footer: str = "</companion-people>",
    lead_in: str = "（关于此刻和你说话的人，你记得这些）",
    max_chars: int = 240,
    max_people: int = 2,
    include_role: bool = True,
    include_label: bool = False,
    include_tone: bool = True,
    include_tags: bool = True,
    include_impact: bool = True,
    include_axes: bool = True,
    roles: Any = None,
    levels: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    """渲染人物块。

    五条硬规则，都来自用户的要求：

    1. **不注入称呼**。``include_label`` 默认 ``False``：不写「你的主人」「知己」
       这类称呼，避免模型开口就叫。要开启得在配置里显式打开
       ``people.inject_role_label``。
    2. **密切度只转成聊天方式**。这是「关系密切程度」的主要体现——只影响语气，
       不给称呼。聊天方式的文本来自配置 ``people.intimacy_levels``（用户第 33 轮
       要求阈值与提示词都可编辑）。
    3. 角色要标明来源：手动配置的说「你确认过」，自动推断的要说「你猜的」，
       让模型知道这条信息的可靠程度不一样。
    4. 角色本身也只转成**行为要求**，不是称呼。行为要求的文本来自配置
       ``people.roles``，所以用户自己加的身份（比如「老师」）也能带上专属提示词。
    5. **关系用六维的矛盾组合来描述，不是报数字**（用户第 34 轮）。见
       ``contrast_lines`` —— 「你们混得很熟，但有些话你还是不跟他说」这种话
       才是活人说得出来的，报「信任 35% 亲近 60%」是仪表盘。
    """

    if max_chars <= 0 or max_people <= 0:
        return "", []

    head = " ".join(str(header or "").split())
    tail = " ".join(str(footer or "").split())
    intro = " ".join(str(lead_in or "").split())
    budget = max_chars - len(head) - len(tail) - len(intro) - 8
    if budget <= 0:
        return "", []

    lines: list[str] = []
    used: list[dict[str, Any]] = []
    consumed = 0
    for person in list(people)[: max(1, int(max_people))]:
        name = " ".join(str(person.get("display_name") or "").split()) or "这个人"
        role = role_for(person, roles)
        manual = is_manual(person)
        intimacy, _label = intimacy_for(person.get("affection"), levels)

        parts: list[str] = []
        if include_role and role != DEFAULT_ROLE:
            tone = role_tone(role, roles)
            if tone:
                # 手动配置是用户亲自确认的，措辞要更肯定；自动推断要留余地。
                parts.append(tone if manual else tone + "（这是你自己猜的，不一定准）")
            if include_label:
                # 只有显式要求时才把称呼写出来。
                label = role_label(role, roles)
                if label:
                    parts.append(f"（你认定他是{label}）")
        if include_tone:
            parts.append(intimacy_tone(intimacy, levels))

        if include_axes:
            # 先用矛盾组合说人话；没有矛盾时退回「最突出的那一两个维度」，
            # 否则一个六维都很正常的人会在提示词里完全消失。
            axes = axes_of(person)
            # 只有**真实观测到**的六维才谈得上矛盾。老库升级后六维是从一个
            # 好感度标量反推的，两条「互相冲突」的信号其实来自同一个数，
            # 说出来的矛盾全是反推公式的副产品（老熟人被说成「刚搭上话」）。
            observed = has_observed_axes(person)
            described = contrast_lines(axes, limit=2) if observed else []
            if not described:
                for key, delta, higher in dominant_axes(axes, limit=2):
                    if delta < 0.12:
                        continue
                    if key == "tension":
                        described.append(
                            "跟他说话你有点绷着。" if higher else "跟他待着挺放松的。"
                        )
                    elif key == "familiarity":
                        described.append("你们已经聊过不少次了。")
                    else:
                        label = AXIS_LABELS.get(key, key)
                        described.append(
                            f"你{('很' if delta >= 0.30 else '有点')}"
                            f"{label if higher else '不太' + label}他。"
                        )
            parts.extend(described)

        if include_tags:
            tags = [str(item) for item in (person.get("tags") or []) if str(item).strip()]
            if tags:
                parts.append("你心里给他打的标签：" + "；".join(tags[:MAX_TAGS]) + "。")

        if include_impact:
            impact = clamp_impact(person.get("emotional_impact"))
            if impact >= 0.25:
                parts.append("和他聊完你通常会心情好一些。")
            elif impact <= -0.25:
                parts.append("和他聊完你常常有点累或不痛快。")

        body = " ".join(part for part in parts if part)
        if not body:
            continue
        line = f"- {name}：{body}"
        if consumed + len(line) + 1 > budget:
            break
        lines.append(line)
        used.append(dict(person))
        consumed += len(line) + 1

    if not lines:
        return "", []

    text = f"{head}\n{intro}\n" + "\n".join(lines) + f"\n{tail}"
    if len(text) > max_chars:
        keep = max(0, max_chars - len(tail) - 1)
        text = text[:keep] + "\n" + tail
    return text, used


def parse_people(raw: Any, *, limit: int = 5, roles: Any = None) -> list[dict[str, Any]]:
    """解析模型给出的 ``people`` 数组。

    模型可能不老实（给出不存在的角色、把 confidence 写成字符串、把昵称塞进
    user_id），这里全部收敛成规范形状；任何一条不合法就跳过，不影响抽取本身。

    ``roles`` 传配置页的身份表，这样用户自定义的身份（比如「老师」）也能被
    自动抽取识别；不传则只认内置的四档。

    关于 ``tags``：用户第 32 轮要求标签写成「XX的XX」这种**关系化**说法
    （「抽象的群友」「喜欢玩游戏的朋友」），而不是「昨天说他家猫生病了」
    这种画像式描述 —— 后者和宿主 A_Memorix 的人物画像重合，重复劳动还会
    互相打架。所以这里接受标签，但只接受短标签，长句子会在提示词侧被要求
    改写成关系化说法；清洗时也会截到 ``TAG_MAX_CHARS``。

    关于 ``axes``：用户第 34 轮把关系从「一个好感度数字」改成六维。模型给的是
    **相对变化量**（``axes`` 里每个维度 -1..1，表示「这次聊完往哪边挪一点」），
    不是绝对值 —— 一次对话不该把一个人从「知己」打成「陌生人」。
    """

    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw[: max(0, int(limit))]:
        if not isinstance(entry, dict):
            continue
        user_id = str(entry.get("user_id") or "").strip()
        if not user_id:
            continue
        role = normalize_role(entry.get("relation_role"), roles)
        impact = clamp_impact(entry.get("emotional_impact"))
        axes = normalize_axes_delta(entry.get("axes"))
        tags = normalize_tags(entry.get("tags"))
        links = parse_links(entry.get("links"))
        if not role and not impact and not axes and not tags and not links:
            # 什么信息都没有，别为一条空记录占一行。
            continue
        out.append(
            {
                "user_id": user_id,
                "platform": str(entry.get("platform") or "").strip(),
                "display_name": " ".join(str(entry.get("display_name") or "").split())[
                    :DISPLAY_NAME_MAX_CHARS
                ],
                "relation_role": role,
                "role_confidence": clamp_confidence(entry.get("role_confidence")),
                "emotional_impact": impact,
                "axes": axes,
                "tags": tags,
                "links": links,
            }
        )
    return out


def normalize_axes_delta(raw: Any) -> dict[str, float]:
    """清洗模型给的六维**变化量**。

    模型经常偷懒：漏几个维度、给绝对值（0.8 当「很信任」）、给字符串数字、
    给 ``null``。这里只保留 -1..1 的有限数，其余一律丢弃 —— 丢弃等于「这次
    没变化」，比猜一个值安全。
    """

    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key in AXIS_KEYS:
        if key not in raw or key == "familiarity":
            # 熟悉度由程序累积，模型给的值一律不采信。
            continue
        try:
            number = float(raw.get(key))
        except (TypeError, ValueError):
            continue
        # NaN 和 ±Inf 都必须挡掉：``max(-1, min(1, inf))`` 会得到 1.0，
        # 等于模型写一个 Infinity 就能把信任一次顶满。``number != number``
        # 只挡 NaN，不挡 inf，所以这里要显式判有限。
        if not math.isfinite(number) or abs(number) < 1e-9:
            continue
        out[key] = max(-1.0, min(1.0, number))
    return out


def parse_links(raw: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    """解析模型给出的 ``links``（谁对第三个人表了什么态）。

    每条来自 ``people[i].links`` —— 模型是挂在「说话的人」下面的，所以调用方
    要把 ``source_user_id`` 补上。这里只负责清洗 ``target_id`` / ``attitude`` /
    ``evidence`` 三个字段。

    ``attitude`` 允许模型给字符串数字（很常见），但 ``NaN`` 一律丢弃：一个
    NaN 会顺着 ``social_bias`` 的求和污染整条链路。
    """

    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for entry in list(raw)[: max(0, int(limit))]:
        if not isinstance(entry, Mapping):
            continue
        target = str(entry.get("target_id") or "").strip()
        if not target:
            continue
        try:
            attitude = float(entry.get("attitude"))
        except (TypeError, ValueError):
            continue
        if attitude != attitude:  # NaN
            continue
        out.append(
            {
                "target_user_id": target[:32],
                "attitude": max(-1.0, min(1.0, attitude)),
                "evidence": " ".join(str(entry.get("evidence") or "").split())[:40],
            }
        )
    return out


def describe_person(
    person: Mapping[str, Any], roles: Any = None, levels: Any = None
) -> str:
    """给命令输出用的一行摘要。

    带上账号（``user_id``）：昵称可以改、可以重名，而 ``person_key`` 就是
    ``platform:user_id``。用户在聊天窗口里执行 ``person`` 命令时，光看昵称
    没法确认自己改的是不是那个人（用户第 44 轮：「QQ 号也应该显示出来」）。

    账号**任何情况下都要写全**。曾经这里为了省掉一个重复的数字，在昵称等于
    账号时跳过账号，结果那条路径反而没有账号可看了——而群名片本来就允许
    等于账号（真机上 ``100000004`` 这个人就是把群名片设成了自己的 QQ 号），
    那是正常数据，不是异常。宁可重复一次，也不让任何一条路径把账号藏起来。
    """

    name = str(person.get("display_name") or "").strip() or "(未命名)"
    user_id = str(person.get("user_id") or "").strip()
    account = f"（账号 {user_id}）" if user_id else ""
    role = role_for(person, roles)
    manual = "手动" if is_manual(person) else "自动"
    intimacy, label = intimacy_for(person.get("affection"), levels)
    impact = clamp_impact(person.get("emotional_impact"))
    tags = "、".join(str(item) for item in (person.get("tags") or [])) or "无"
    return (
        f"{name}{account}（{role}·{manual}，{label}，"
        f"好感 {clamp_affection(person.get('affection')):.0f}，"
        f"{describe_axes(axes_of(person))}，"
        f"情绪影响 {impact:+.2f}）：{tags}"
    )
