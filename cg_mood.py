"""我，deepseek —— Bot 自身的情绪状态。

设计要点：

- **八维情绪基元**（每个 0..1）：``joy`` 开心、``anger`` 愤怒、``anxiety``
  紧张、``sadness`` 伤心、``interest`` 兴趣、``calm`` 平静、``disgust``
  厌恶、``surprise`` 惊讶。
  单一「愉悦度」无法表达「又气又委屈」这种复合状态，也无法区分「平静」
  与「没感觉」。八维各自独立衰减、独立配置，模型可以同时给出多个高值，
  注入时就能说出「你现在又生气又有点难过」而不是笼统的「心情不好」。
- **两个派生量**：``valence``（愉悦 -1..1）与 ``arousal``（情绪强度 0..1）
  不再独立存储，而是由八个基元相对**各自基线**的偏差算出来。这样
  「Bot 平时是什么性格」由基线决定，「此刻偏离了多少」由基元决定，
  两者不会互相污染。
- **情绪强度**（内部键 ``arousal``）表示情绪被激活的程度：兴奋、着急、
  上头、被激怒都会升高，平静、无聊会降低。它决定「反应有多激烈」，
  而精力决定「还有没有力气反应」，两者不能混。
- **全局单例**：Bot 是一个实体。一个群里的坏心情会影响它在别处的表现，
  这才是「像正常人」。按会话分裂成多份情绪反而不像人。
- **读取时衰减**：每次 ``snapshot()`` 把八个基元按各自的半衰期拉回基线。
  精力不走半衰期，而是**线性恢复**（见下）。
- **精力是另一种量**：它会被发言消耗、随时间恢复、情绪高涨时消耗变少
  （极端情况不消耗）、情绪低落时消耗更多；见 :func:`decay` 与
  :func:`reply_energy_cost`。精力耗尽后会进入「没力气主动说话」的状态，
  直到恢复到 ``EXHAUSTED_EXIT`` 才解除——这是刻意做的滞回，
  否则精力在 0 附近抖动会让 Bot 反复「诈尸」。
- **三层驱动**（由廉价到权威）：
  1. 消息启发式：每条入站消息的标点、语气词、长度，微秒级，只在内存里累加；
  2. 回复启发式：Bot 自己说得多长，决定精力消耗与投入度；
  3. LLM 评估：搭车现有的抽取调用，读完整聊天记录后给出权威判断，
     **不额外花 token**。

为什么情绪不进 ``gap_items``：那套条目走相似度去重与 TTL，会把
「有点烦躁」和「有点低落」合并成「最近心情一般」这类空话。情绪是连续量，
必须单独存、单独衰减。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

#: 八个情绪基元，顺序即配置项与展示顺序。
EMOTIONS: tuple[str, ...] = (
    "joy",
    "anger",
    "anxiety",
    "sadness",
    "interest",
    "calm",
    "disgust",
    "surprise",
)

#: 每个基元的中文名（配置页、注入文本、日志都用它）。
EMOTION_LABELS: dict[str, str] = {
    # 用户要求把基元名从「愉悦」改成「开心」：只是换个说法，数值含义完全不变。
    # 注意别和派生量「愉悦度」（``valence``）混淆——那是另一个东西，
    # 由八个基元相对基线的偏差加权算出，名字保持不动。
    "joy": "开心",
    "anger": "愤怒",
    "anxiety": "紧张",
    "sadness": "伤心",
    "interest": "兴趣",
    "calm": "平静",
    "disgust": "厌恶",
    "surprise": "惊讶",
}

#: 每个基元的默认基线：Bot 平时是什么性格。
#: 默认值描述一个「平和、略有兴趣、情绪不激烈」的普通 Bot。
DEFAULT_PRIMITIVES: dict[str, float] = {
    "joy": 0.25,
    "anger": 0.05,
    "anxiety": 0.10,
    "sadness": 0.05,
    "interest": 0.45,
    "calm": 0.55,
    "disgust": 0.03,
    "surprise": 0.10,
}

#: 每个基元的默认半衰期（分钟）。愤怒退得快、伤心退得慢，这是常识。
DEFAULT_HALF_LIVES_MINUTES: dict[str, float] = {
    "joy": 120.0,
    "anger": 30.0,
    "anxiety": 60.0,
    "sadness": 180.0,
    "interest": 240.0,
    "calm": 90.0,
    "disgust": 90.0,
    "surprise": 8.0,
}

#: 基元对派生「愉悦度」的权重。正数抬、负数压，量级按「偏离基线多少」算。
VALENCE_WEIGHTS: dict[str, float] = {
    "joy": 1.6,
    "interest": 0.8,
    "calm": 0.6,
    "surprise": 0.4,
    "anger": -1.4,
    "sadness": -1.3,
    "anxiety": -1.0,
    "disgust": -1.3,
}

#: 基元对派生「情绪强度」的权重（叠加在 ``arousal_baseline`` 之上）。
AROUSAL_WEIGHTS: dict[str, float] = {
    "anger": 0.55,
    "anxiety": 0.55,
    "surprise": 0.60,
    "joy": 0.40,
    "interest": 0.35,
    "disgust": 0.30,
    "sadness": -0.15,
    "calm": -0.45,
}

#: 基元的合法区间（统一 0..1）。
PRIMITIVE_BOUNDS: tuple[float, float] = (0.0, 1.0)

#: 三个派生/机械量的合法区间。
BOUNDS: dict[str, tuple[float, float]] = {
    "valence": (-1.0, 1.0),
    "arousal": (0.0, 1.0),
    "energy": (0.0, 1.0),
}

#: 心情标签的最长字符数（注入文本要短）。
LABEL_MAX_CHARS = 12

#: 单条消息对精力的消耗。读 100 条消息掉 0.2，符合「聊久了会累」。
ENERGY_COST_PER_MESSAGE = 0.002

#: 消息启发式的最大单次增量，防止一句话把情绪推到极端。
HEURISTIC_CAP = 0.06

#: 精力耗尽（<=0）后，要恢复到该值才重新允许主动发言。
#: 用滞回而不是「>0 就恢复」，否则精力在 0 附近抖动会让 Bot 反复诈尸。
EXHAUSTED_EXIT = 0.3

#: 精力恢复速度按「小时恢复多少」配置；默认 0.35/小时 ≈ 3 小时回满。
ENERGY_RECOVER_PER_HOUR = 0.35

#: 愉悦度每升高 0.1，额外补偿的精力恢复量（心情好恢复得更快）。
ENERGY_RECOVER_VALENCE_BONUS = 0.12

#: 单条消息对精力的消耗倍率随情绪变化的区间。
#: 情绪高涨（愉悦高 + 情绪强度高）时最多降到 0.15 倍（接近不消耗），
#: 情绪低落时最多升到 1.8 倍。
ENERGY_COST_FACTOR_MIN = 0.15
ENERGY_COST_FACTOR_MAX = 1.8

#: 判定「这个基元够明显」的门限：低于它的基元不进标签、不进语气。
SALIENT_THRESHOLD = 0.35

#: 判定「真的偏离了」的最小偏离量。光有 ``deviation > 0`` 不够：
#: ``calm`` 的基线是 0.55，稍微高一点（0.60）就会被当成「你现在很平静」，
#: 把真正重要的情绪盖掉。要求偏离至少这么多才算显性。
MIN_DEVIATION = 0.08

#: 判定「强」的门限：用来给语气分级。
STRONG_THRESHOLD = 0.60

_POSITIVE_MARKERS = (
    "哈哈", "嘿嘿", "嘻嘻", "笑死", "好耶", "太棒", "喜欢", "谢谢",
    "辛苦", "可爱", "真棒", "不错", "厉害", "开心", "高兴", "舒服",
    "233", "ww", "awsl", "贴贴", "抱抱",
)
_NEGATIVE_MARKERS = (
    "滚", "烦死", "讨厌", "无语", "垃圾", "闭嘴", "别烦", "恶心",
    "失望", "生气", "气死", "傻", "蠢", "烦人", "难绷", "受不了",
    "无聊", "没意思", "敷衍",
)
_ANGRY_MARKERS = ("气死", "生气", "滚", "闭嘴", "烦死", "别烦", "骂", "火大")
_ANXIOUS_MARKERS = ("紧张", "害怕", "慌", "急死", "怎么办", "会不会", "担心", "忐忑")
_SAD_MARKERS = ("难过", "伤心", "想哭", "呜呜", "委屈", "失望", "算了", "没意思")
_DISGUST_MARKERS = ("恶心", "呕吐", "离谱", "恶心人", "反胃", "恶臭", "低俗")
_SURPRISE_MARKERS = ("居然", "竟然", "没想到", "天啊", "卧槽", "我靠", "啊这")
_CALM_MARKERS = ("安静", "慢慢", "不着急", "随便", "都行", "平静")
_INTEREST_MARKERS = ("为什么", "怎么", "然后呢", "详细", "讲讲", "有趣", "好奇")
_EXCITED_MARKERS = (
    "啊啊", "卧槽", "我靠", "天啊", "居然", "竟然", "！！！", "??",
    "急", "快点", "马上",
)
#: 表示「对话告一段落」的收尾语，用来回一点精力。
_REST_MARKERS = ("晚安", "睡了", "先这样", "拜拜", "回头聊", "下线")


def clamp_primitive(name: str, value: Any) -> float:
    """把某个情绪基元夹到 0..1；非法输入回落到该基元默认基线。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_PRIMITIVES.get(name, 0.0))
    if number != number:  # NaN
        return float(DEFAULT_PRIMITIVES.get(name, 0.0))
    low, high = PRIMITIVE_BOUNDS
    return min(high, max(low, number))


def clamp(name: str, value: Any) -> float:
    """把某个派生/机械量夹到合法区间；非法输入回落到区间中位。"""

    low, high = BOUNDS[name]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return (low + high) / 2.0
    if number != number:  # NaN
        return (low + high) / 2.0
    return min(high, max(low, number))


def primitives_of(state: "MoodState") -> dict[str, float]:
    """取出八个基元（只读副本）。"""

    return {name: float(getattr(state, name, 0.0)) for name in EMOTIONS}


def derive_valence(primitives: dict[str, float], baselines: dict[str, float]) -> float:
    """由基元相对基线的偏差算出愉悦度。

    为什么用「偏差」而不是绝对值：同样的 ``anger = 0.3``，对一个平时暴躁的
    人格是常态，对一个平时温和的人格就是大事。基线承担「人格」，基元承担
    「此刻」。
    """

    total = 0.0
    for name in EMOTIONS:
        weight = VALENCE_WEIGHTS.get(name, 0.0)
        if not weight:
            continue
        current = float(primitives.get(name, 0.0))
        base = float(baselines.get(name, DEFAULT_PRIMITIVES.get(name, 0.0)))
        total += weight * (current - base)
    return clamp("valence", total)


def derive_arousal(
    primitives: dict[str, float],
    baselines: dict[str, float],
    *,
    arousal_baseline: float = 0.35,
) -> float:
    """由基元相对基线的偏差算出情绪强度，叠加在 ``arousal_baseline`` 上。"""

    total = float(arousal_baseline)
    for name in EMOTIONS:
        weight = AROUSAL_WEIGHTS.get(name, 0.0)
        if not weight:
            continue
        current = float(primitives.get(name, 0.0))
        base = float(baselines.get(name, DEFAULT_PRIMITIVES.get(name, 0.0)))
        total += weight * (current - base)
    return clamp("arousal", total)


@dataclass(slots=True)
class MoodState:
    """某一时刻的情绪快照。

    八个基元是**真值**，``valence`` / ``arousal`` 是缓存下来的派生量
    （为了渲染、日志、页面与历史数据兼容）。任何改动基元的地方都必须
    调 :func:`refresh` 重算派生量。

    ``exhausted`` 是**滞回锁**：精力掉到 0 时置真，之后即使精力回到 0.2
    仍然为真，直到精力恢复到 :data:`EXHAUSTED_EXIT` 才解除。用锁而不是
    直接判断 ``energy <= 0``，是因为精力会在 0 附近反复抖动，直接判断会让
    Bot 一会儿闭麦一会儿诈尸。
    """

    joy: float = 0.25
    anger: float = 0.05
    anxiety: float = 0.10
    sadness: float = 0.05
    interest: float = 0.45
    calm: float = 0.55
    disgust: float = 0.03
    surprise: float = 0.10
    energy: float = 0.6
    valence: float = 0.0
    arousal: float = 0.35
    label: str = ""
    reason: str = ""
    updated_at: float = 0.0
    exhausted: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {name: round(getattr(self, name), 4) for name in EMOTIONS}
        payload.update(
            {
                "valence": round(self.valence, 4),
                "arousal": round(self.arousal, 4),
                "energy": round(self.energy, 4),
                "label": self.label,
                "reason": self.reason,
                "updated_at": self.updated_at,
                "exhausted": self.exhausted,
            }
        )
        return payload

    def normalized(self) -> "MoodState":
        return MoodState(
            joy=clamp_primitive("joy", self.joy),
            anger=clamp_primitive("anger", self.anger),
            anxiety=clamp_primitive("anxiety", self.anxiety),
            sadness=clamp_primitive("sadness", self.sadness),
            interest=clamp_primitive("interest", self.interest),
            calm=clamp_primitive("calm", self.calm),
            disgust=clamp_primitive("disgust", self.disgust),
            surprise=clamp_primitive("surprise", self.surprise),
            energy=clamp("energy", self.energy),
            valence=clamp("valence", self.valence),
            arousal=clamp("arousal", self.arousal),
            label=str(self.label or "").strip()[:LABEL_MAX_CHARS],
            reason=str(self.reason or "").strip(),
            updated_at=float(self.updated_at or 0.0),
            exhausted=bool(self.exhausted),
        )


def refresh(
    state: MoodState,
    *,
    baselines: dict[str, float],
    arousal_baseline: float = 0.35,
) -> MoodState:
    """重算派生量（``valence`` / ``arousal``）并归一化。

    **任何改动基元的地方都必须调它**，否则页面上会出现「基元很高但愉悦度
    没动」这种自相矛盾的展示。
    """

    primitives = primitives_of(state)
    state.valence = derive_valence(primitives, baselines)
    state.arousal = derive_arousal(primitives, baselines, arousal_baseline=arousal_baseline)
    return state.normalized()


def dominant_emotions(
    primitives: dict[str, float],
    *,
    baselines: dict[str, float] | None = None,
    threshold: float = SALIENT_THRESHOLD,
    limit: int = 2,
) -> list[tuple[str, float]]:
    """挑出此刻最明显的几个基元（按「偏离基线多少」排序）。

    排序用偏离量而不是绝对值：一个基线就是 0.55 的 ``calm`` 不该天天
    被当成「你现在很平静」的重点，只有它明显高于平时才算。
    """

    bases = baselines or DEFAULT_PRIMITIVES
    scored: list[tuple[str, float]] = []
    for name in EMOTIONS:
        current = float(primitives.get(name, 0.0))
        base = float(bases.get(name, DEFAULT_PRIMITIVES.get(name, 0.0)))
        deviation = current - base
        # 只有「明显偏高」才算：绝对值要过 SALIENT_THRESHOLD，且相对自己的
        # 基线至少高出 MIN_DEVIATION。后者是关键——calm 的基线本来就高
        # （0.55），没有这个下限的话 0.60 就会被当成「你现在很平静」，
        # 把真正重要的情绪盖掉。
        if current < threshold or deviation < MIN_DEVIATION:
            continue
        scored.append((name, deviation))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[: max(0, limit)]


def energy_cost_factor(
    primitives: dict[str, float] | None = None,
    *,
    valence: float = 0.0,
    arousal: float = 0.5,
) -> float:
    """当前情绪下，说话/活动的精力消耗倍率。

    情绪高涨（心情好 + 情绪强度高）时倍率降到最低（接近不消耗），
    情绪低落时倍率升到最高（同样的活更费劲）。这是用户明确要求的语义：
    「情绪高涨时会降低发言的精力消耗（甚至不消耗），情绪低落时则会消耗更多精力」。
    """

    if primitives:
        val = derive_valence(primitives, DEFAULT_PRIMITIVES)
        aro = derive_arousal(primitives, DEFAULT_PRIMITIVES)
    else:
        val, aro = valence, arousal
    # 愉悦度贡献 0.65，情绪强度贡献 0.35：心情好坏是主要因素。
    drive = 0.65 * clamp("valence", val) + 0.35 * (clamp("arousal", aro) - 0.5) * 2.0
    drive = max(-1.0, min(1.0, drive))
    # drive=+1 → MIN；drive=-1 → MAX
    ratio = (1.0 - drive) / 2.0
    return ENERGY_COST_FACTOR_MIN + (ENERGY_COST_FACTOR_MAX - ENERGY_COST_FACTOR_MIN) * ratio


def reply_energy_cost(
    text: str,
    *,
    valence: float = 0.0,
    arousal: float = 0.5,
    primitives: dict[str, float] | None = None,
) -> float:
    """Bot 说一句话要花多少精力。

    基准随字数增长（说得越多越累），再乘当前情绪的消耗倍率。
    返回正数，调用方从 ``energy`` 里减。
    """

    length = len(str(text or ""))
    base = 0.010 + min(0.030, 0.0006 * length)
    return base * energy_cost_factor(primitives, valence=valence, arousal=arousal)


def decay(
    state: MoodState,
    *,
    now: float,
    baselines: dict[str, float],
    half_lives: dict[str, float],
    arousal_baseline: float = 0.35,
    energy_recover_per_hour: float = ENERGY_RECOVER_PER_HOUR,
    offline_reset_hours: float = 4.0,
    exhausted_exit: float = EXHAUSTED_EXIT,
) -> MoodState:
    """把状态推进到 ``now``。

    八个基元各自按自己的半衰期朝基线回落；**精力不走半衰期而是线性恢复**
    （睡一觉回满，而不是「回一半」）。``half_lives`` 以秒为单位，
    非正值表示该基元不衰减。

    离线重置：``now - updated_at >= offline_reset_hours`` 时认为 Bot 下线了
    一段时间（睡觉/关机），精力直接拉满，情绪也回到基线。用户明确要求
    「Bot 离线数小时后再上线时重置为最大值」。
    """

    # 注意 ``updated_at or now`` 是错的：0 是合法时间戳（表示「没有起点」），
    # 但它是 falsy，会退化成 now 让 elapsed 恒为 0，情绪永远不衰减。
    # 这里把 0 当成纪元起点，等价于「已经过了很久」→ 直接回到基线。
    elapsed = max(0.0, float(now) - float(state.updated_at or 0.0))
    out = MoodState(
        **{name: float(getattr(state, name, 0.0)) for name in EMOTIONS},
        energy=state.energy,
        valence=state.valence,
        arousal=state.arousal,
        label=state.label,
        reason=state.reason,
        updated_at=now,
        exhausted=bool(state.exhausted),
    )

    # 离线足够久：当作睡了一觉，精力回满、情绪归位。
    reset_seconds = max(0.0, float(offline_reset_hours)) * 3600.0
    if reset_seconds > 0.0 and elapsed >= reset_seconds:
        for name in EMOTIONS:
            setattr(out, name, float(baselines.get(name, DEFAULT_PRIMITIVES.get(name, 0.0))))
        out.energy = 1.0
        out.exhausted = False
        out.label = ""
        out.reason = ""
        return refresh(out, baselines=baselines, arousal_baseline=arousal_baseline)

    if elapsed <= 0.0:
        return refresh(out, baselines=baselines, arousal_baseline=arousal_baseline)

    for name in EMOTIONS:
        half_life = float(half_lives.get(name) or 0.0)
        if half_life <= 0.0:
            continue
        baseline = float(baselines.get(name, DEFAULT_PRIMITIVES.get(name, 0.0)))
        current = float(getattr(out, name))
        # 剩余偏差每过一个半衰期减半。
        factor = 0.5 ** (elapsed / half_life)
        setattr(out, name, baseline + (current - baseline) * factor)

    # 精力线性恢复；心情越好恢复越快（愉悦度提供额外补偿）。
    hours = elapsed / 3600.0
    rate = max(0.0, float(energy_recover_per_hour))
    # 先用更新后的基元算出愉悦度，再据此加恢复补偿。
    valence_now = derive_valence(primitives_of(out), baselines)
    valence_bonus = max(0.0, valence_now) * ENERGY_RECOVER_VALENCE_BONUS
    out.energy = min(1.0, out.energy + (rate + valence_bonus) * hours)

    # 滞回：耗尽锁只在精力回到 exhausted_exit 以上才解除。
    exit_level = max(0.0, min(1.0, float(exhausted_exit)))
    # 注意要同时看**进入衰减前**的精力：精力刚掉到 0 时，哪怕这一秒的恢复
    # 已经把它抬到 0.0001，也必须立刻上锁，否则锁永远等不到置真的时机。
    if out.energy >= exit_level:
        out.exhausted = False
    elif out.energy <= 0.0 or float(state.energy) <= 0.0:
        out.exhausted = True

    return refresh(out, baselines=baselines, arousal_baseline=arousal_baseline)


def blend(
    state: MoodState,
    *,
    target: dict[str, Any],
    weight: float,
    now: float,
    baselines: dict[str, float] | None = None,
    arousal_baseline: float = 0.35,
) -> MoodState:
    """把状态朝 LLM 给出的目标值按权重混合。

    用「混合到目标」而不是「累加增量」，是因为增量会随调用次数漂移，
    而目标值天然有界，多抽几次也只会收敛到同一个判断。

    **只混合八个情绪基元**。精力不参与混合：它是机械量（说话消耗、
    随时间恢复、情绪缩放倍率），让模型去改会和消耗/恢复相互打架，
    出现「越聊越有精神」这种明显不对的结果。
    """

    ratio = min(1.0, max(0.0, float(weight)))
    bases = baselines or DEFAULT_PRIMITIVES
    out = MoodState(
        **{name: float(getattr(state, name, 0.0)) for name in EMOTIONS},
        energy=state.energy,
        valence=state.valence,
        arousal=state.arousal,
        label=state.label,
        reason=state.reason,
        updated_at=now,
        exhausted=state.exhausted,
    )
    for name in EMOTIONS:
        if name not in target:
            continue
        goal = clamp_primitive(name, target.get(name))
        setattr(out, name, getattr(out, name) + (goal - getattr(out, name)) * ratio)
    label = str(target.get("label") or "").strip()
    if label:
        out.label = label[:LABEL_MAX_CHARS]
    reason = str(target.get("reason") or "").strip()
    if reason:
        out.reason = reason[:60]
    return refresh(out, baselines=bases, arousal_baseline=arousal_baseline)


#: 单一情绪对应的中文短语（用于标签与语气）。
_EMOTION_PHRASES: dict[str, str] = {
    "joy": "开心",
    "anger": "生气",
    "anxiety": "紧张",
    "sadness": "难过",
    "interest": "来兴趣",
    "calm": "平静",
    "disgust": "嫌弃",
    "surprise": "惊讶",
}

#: 单一情绪对应的语气要求。这是「明显影响回复」的关键：给行为指令，不给数值。
_EMOTION_TONES: dict[str, str] = {
    "joy": "你现在心情很好，语气轻快，愿意主动接话、多说两句，可以用颜文字。",
    "anger": "你现在有点火，语气会冲一点、话更短，不主动哄人；但不要骂人、不要突然爆发。",
    "anxiety": "你现在有点不安，说话会带犹豫，容易追问一句确认；不要装作没事。",
    "sadness": "你现在情绪偏低落，回复会偏简短内敛，少用感叹号和颜文字，但不冷淡。",
    "interest": "你现在对话题很感兴趣，愿意追问细节、把话展开聊。",
    "calm": "你现在很平静，语气平和、话不多，回答干脆。",
    "disgust": "你现在有点嫌弃，语气会带刺、评价更直白，但不必刻薄到攻击人。",
    "surprise": "你现在有点意外，会先表达惊讶再接话，反应比平时快。",
}

#: 高唤醒情绪叠加在语气后面的一句（表示「反应更激烈」）。
_AROUSAL_TONE_HIGH = "整体反应比平时激烈一些，但不要刷屏。"
_AROUSAL_TONE_LOW = "整体反应比平时平淡一些。"


def mood_branch(
    primitives: dict[str, float],
    energy: float,
    *,
    exhausted: bool = False,
    baselines: dict[str, float] | None = None,
) -> str:
    """把八维基元归到一个状态分支上（用于兼容旧的单标签接口）。

    这是标签与语气要求的**唯一判定入口**：两个函数都从这里取分支，
    就不可能再出现「标签写着烦躁、指令却说累了」这种自相矛盾。

    判定顺序：耗尽锁优先（覆盖性状态），然后是显性情绪（取偏离最大的
    那个基元），再是精力，最后是中性。
    """

    if exhausted or energy <= 0.0:
        return "exhausted"
    top = dominant_emotions(primitives, baselines=baselines, limit=1)
    if top:
        return top[0][0]
    if energy <= 0.28:
        return "tired"
    return "neutral"


def label_for(
    primitives: dict[str, float],
    energy: float,
    *,
    exhausted: bool = False,
    baselines: dict[str, float] | None = None,
) -> str:
    """由八维基元拼一个中文短语。LLM 没给标签时用它兜底。

    两个以上显性情绪时拼成「又 X 又 Y」——这正是用户要的「复合调节」：
    「又生气又难过」比「心情不好」信息量大得多。
    """

    if exhausted or energy <= 0.0:
        return "累到不想说话"
    top = dominant_emotions(primitives, baselines=baselines, limit=2)
    if not top:
        if energy <= 0.28:
            return "有点累了"
        return "还算平稳"
    if len(top) == 1:
        return _EMOTION_PHRASES.get(top[0][0], "心情复杂")
    first = _EMOTION_PHRASES.get(top[0][0], "复杂")
    second = _EMOTION_PHRASES.get(top[1][0], "复杂")
    if first == second:
        return first
    text = f"又{first}又{second}"
    return text[:LABEL_MAX_CHARS]


def desire_for(energy: float, *, exhausted: bool = False) -> str:
    """精力对**回复欲望**的要求：这一轮想不想接这个话茬。

    用户明确要求精力影响的是「对当前话题的回复欲望」，而不是发言频率 ——
    按频率压会让 Bot 时灵时不灵，很不自然；按欲望写则每一轮都会照常出现，
    只是态度上「想聊」和「不想聊」的差别。
    """

    if exhausted or energy <= 0.0:
        # 一定要写清「被 @ 或点名仍然要回」：宿主门控只压主动发言的频率，
        # 不拦被点名的轮次；模型不知道这一点就会连点名也不理。
        return (
            "你现在一点都不想接话，只想安静待着。"
            "除非有人点你名或者 @ 你，否则不要主动搭话。"
        )
    if energy <= 0.28:
        return (
            "你现在提不起劲，对聊天没什么兴趣：可以只应一声，"
            "别主动追问、别开新话题、也别接梗往下延。"
        )
    # 用 < 而不是 <=：正好 0.5 是「平常状态」，不该开始限制。
    if energy < 0.50:
        return "你现在精力一般，愿意接话但不太想主动展开：顺着对方说就行，不必抛新话题。"
    if energy >= 0.80:
        return "你现在精力充沛，很有兴致接话，也愿意主动把话题往下带。"
    return ""


def length_for(energy: float, *, exhausted: bool = False) -> str:
    """精力对**本次回复字数**的要求。"""

    if exhausted or energy <= 0.0:
        return (
            "这一轮如果非说不可，就只说一句类似「先去忙点别的，等下回来」的话，"
            "然后安静下来，不要再补充。"
        )
    if energy <= 0.28:
        return "回复短一些，一两句就够，不要硬撑长篇大论。"
    if energy < 0.50:
        return "回复控制在两三句，别铺开讲。"
    if energy >= 0.80:
        return "可以多说几句。"
    return ""


def effort_for(energy: float, *, exhausted: bool = False) -> str:
    """精力对「回复欲望 + 本次回复字数」的要求。

    这一句和语气要求是**两条独立的轴**：心情决定「用什么语气说」，
    精力决定「想不想聊、说多少」。所以心情好但很累时，两条指令会同时出现，
    这才是对的。

    **刻意不写「少说话 / 降低发言频率」**：用户明确说过按频率压很不自然
    （Bot 会时灵时不灵）。真正压频率的只有「耗尽」这一档，由
    :meth:`MoodEngine.voice_level` 交给宿主门控处理。
    """

    return desire_for(energy, exhausted=exhausted) + length_for(energy, exhausted=exhausted)


def tone_for(
    primitives: dict[str, float],
    *,
    exhausted: bool = False,
    baselines: dict[str, float] | None = None,
    arousal: float | None = None,
) -> str:
    """只给**语气**要求，不含精力那两句。

    拆出来的原因：WebUI 面板要把「语气」和「精力」分成两块显示，
    而 :func:`guidance_for` 是把两者拼在一起的（注入时必须是完整一段，
    分成两段会让模型以为有两条独立要求）。面板如果直接显示 ``guidance``，
    精力那句就会出现两遍——用户看到的和真正注入的不一致。
    """

    if exhausted:
        return "你现在累得不想说话，语气会明显敷衍、能省则省。"

    top = dominant_emotions(primitives, baselines=baselines, limit=2)
    parts: list[str] = []
    for name, _deviation in top:
        tone = _EMOTION_TONES.get(name)
        if tone:
            parts.append(tone)
    if not parts:
        parts.append("你现在状态平稳，按平常的语气回复就好。")

    # 情绪强度决定「反应有多激烈」，叠加一句量级说明。
    if arousal is not None:
        if arousal >= 0.68:
            parts.append(_AROUSAL_TONE_HIGH)
        elif arousal <= 0.18:
            parts.append(_AROUSAL_TONE_LOW)
    return "".join(parts)


def guidance_for(
    primitives: dict[str, float],
    energy: float,
    *,
    exhausted: bool = False,
    baselines: dict[str, float] | None = None,
    arousal: float | None = None,
) -> str:
    """把状态翻译成**可执行**的语气与行为要求。

    这一段是「明显影响回复」的关键：只报数值模型不会照做，必须给行为指令。
    复合情绪会给出**组合**指令（「有点火但更多是难过」这种层次感）。

    语气 + 精力两段拼成**一段**注入（不拆成两条要求，免得模型当成两件事）。
    面板要分开显示时用 :func:`tone_for` 与 ``effort_for``。
    """

    tone = tone_for(
        primitives,
        # energy 归零和 exhausted 等价：标签那边也是这么判的，两边不一致会出现
        # 「标签说累瘫了、语气还在点名某个情绪」的怪状态。
        exhausted=exhausted or energy <= 0.0,
        baselines=baselines,
        arousal=arousal,
    )
    return tone + effort_for(energy, exhausted=exhausted)


DEFAULT_HEADER = "<companion-mood>"
DEFAULT_FOOTER = "</companion-mood>"


def render_block(
    state: MoodState,
    *,
    header: str = DEFAULT_HEADER,
    footer: str = DEFAULT_FOOTER,
    max_chars: int = 180,
    include_guidance: bool = True,
    baselines: dict[str, float] | None = None,
) -> str:
    """渲染成一小段可追加到 ``extra_prompt`` 的文本。"""

    if max_chars <= 0:
        return ""

    bases = baselines or DEFAULT_PRIMITIVES
    head = " ".join(str(header or "").split())
    tail = " ".join(str(footer or "").split())
    primitives = primitives_of(state)
    label = " ".join(str(state.label or "").split()) or label_for(
        primitives, state.energy, exhausted=state.exhausted, baselines=bases
    )

    lines = [f"【你此刻的心境】{label}"]
    if include_guidance:
        lines.append(
            guidance_for(
                primitives,
                state.energy,
                exhausted=state.exhausted,
                baselines=bases,
                arousal=state.arousal,
            )
        )

    body = "\n".join(lines)
    text = f"{head}\n{body}\n{tail}"
    if len(text) > max_chars:
        # 只截正文，保住头尾标记，否则模型会把标记当正文内容。
        keep = max(0, max_chars - len(head) - len(tail) - 2)
        if keep <= 0:
            return ""
        text = f"{head}\n{body[:keep]}\n{tail}"
    return text


def heuristic_delta(
    text: str,
    *,
    from_bot: bool = False,
    valence: float = 0.0,
    arousal: float = 0.5,
    primitives: dict[str, float] | None = None,
) -> dict[str, float]:
    """从一条消息里读出八个基元的增量。

    刻意保守：每个基元的单次变化最多 ``HEURISTIC_CAP``。宁可让 LLM 那条
    慢速校正来主导，也不要让几条感叹号把情绪推到极端。

    **精力不设这个上限**：它由 :func:`reply_energy_cost` 按字数与情绪倍率
    算出，本身就有界（单条最多约 0.07），再套 0.06 的帽子会把「长篇大论
    更费精力」这个语义抹掉。
    """

    content = str(text or "")
    if not content.strip():
        return {}

    deltas: dict[str, float] = {name: 0.0 for name in EMOTIONS}
    energy_delta = 0.0

    hits_pos = sum(1 for marker in _POSITIVE_MARKERS if marker in content)
    hits_neg = sum(1 for marker in _NEGATIVE_MARKERS if marker in content)
    if hits_pos:
        deltas["joy"] += 0.025 * hits_pos
        deltas["interest"] += 0.010 * hits_pos
    if hits_neg:
        deltas["joy"] -= 0.015 * hits_neg

    # 细分情绪各自的关键词（比笼统的「负面」更能区分生气/难过/紧张）。
    for markers, name, step in (
        (_ANGRY_MARKERS, "anger", 0.030),
        (_ANXIOUS_MARKERS, "anxiety", 0.028),
        (_SAD_MARKERS, "sadness", 0.028),
        (_DISGUST_MARKERS, "disgust", 0.030),
        (_SURPRISE_MARKERS, "surprise", 0.032),
        (_CALM_MARKERS, "calm", 0.024),
        (_INTEREST_MARKERS, "interest", 0.022),
    ):
        hits = sum(1 for marker in markers if marker in content)
        if hits:
            deltas[name] += step * hits

    excited = sum(1 for marker in _EXCITED_MARKERS if marker in content)
    excited += content.count("！") + content.count("!")
    excited += min(3, content.count("？") + content.count("?")) * 0.5
    if excited:
        boost = 0.010 * min(6.0, float(excited))
        deltas["surprise"] += boost * 0.5
        deltas["interest"] += boost * 0.5
        # 兴奋会压一点平静。
        deltas["calm"] -= boost * 0.4

    # 长消息说明对方愿意展开讲，是投入信号。
    if len(content) >= 60:
        deltas["interest"] += 0.012
    # 收尾语意味着对话告一段落，回一点精力、也回一点平静。
    if any(marker in content for marker in _REST_MARKERS):
        energy_delta += 0.030
        deltas["calm"] += 0.020

    if from_bot:
        # Bot 自己说话：说得越多越费精力，也略微更投入。
        # 消耗量按当前情绪缩放——心情高涨时几乎不累，低落时同样的活更费劲。
        energy_delta -= reply_energy_cost(
            content, valence=valence, arousal=arousal, primitives=primitives
        )
        deltas["interest"] += min(0.010, 0.0002 * len(content))
    else:
        energy_delta -= ENERGY_COST_PER_MESSAGE

    def _cap(value: float) -> float:
        return max(-HEURISTIC_CAP, min(HEURISTIC_CAP, value))

    delta: dict[str, float] = {}
    for name in EMOTIONS:
        value = deltas[name]
        if value:
            delta[name] = _cap(value)
    if energy_delta:
        delta["energy"] = energy_delta
    return delta


class MoodEngine:
    """情绪状态的持有者与驱动器。

    热路径（``on_message`` / ``on_reply``）只做内存运算，零 IO；
    ``snapshot()`` 也是纯内存；只有 ``load`` / ``persist`` 碰数据库。
    """

    def __init__(self, config_getter: Callable[[], Any]) -> None:
        self._config = config_getter
        self._state = MoodState(updated_at=time.time())
        self._loaded = False

    # ── 配置 ──

    def _cfg(self) -> Any:
        try:
            return getattr(self._config(), "mood", None)
        except Exception:  # noqa: BLE001
            return None

    def baselines(self) -> dict[str, float]:
        """八个基元的基线。配置页每一项都能单独调。"""

        cfg = self._cfg()
        out: dict[str, float] = {}
        for name in EMOTIONS:
            default = float(DEFAULT_PRIMITIVES[name])
            out[name] = clamp_primitive(name, getattr(cfg, f"emotion_{name}_baseline", default))
        return out

    def _arousal_baseline(self) -> float:
        cfg = self._cfg()
        return clamp("arousal", getattr(cfg, "arousal_baseline", 0.35))

    def half_lives(self) -> dict[str, float]:
        """八个基元的半衰期（秒）。0 表示该基元不衰减。"""

        cfg = self._cfg()
        out: dict[str, float] = {}
        for name in EMOTIONS:
            default = float(DEFAULT_HALF_LIVES_MINUTES[name])
            try:
                minutes = float(getattr(cfg, f"emotion_{name}_half_life_minutes", default) or 0.0)
            except (TypeError, ValueError):
                minutes = default
            out[name] = max(0.0, minutes) * 60.0
        return out

    def _energy_recover_per_hour(self) -> float:
        cfg = self._cfg()
        try:
            return max(0.0, float(getattr(cfg, "energy_recover_per_hour", ENERGY_RECOVER_PER_HOUR) or 0.0))
        except (TypeError, ValueError):
            return ENERGY_RECOVER_PER_HOUR

    def _offline_reset_hours(self) -> float:
        cfg = self._cfg()
        try:
            return max(0.0, float(getattr(cfg, "offline_reset_hours", 4.0) or 0.0))
        except (TypeError, ValueError):
            return 4.0

    def _exhausted_exit(self) -> float:
        cfg = self._cfg()
        try:
            return max(0.0, min(1.0, float(getattr(cfg, "exhausted_exit", EXHAUSTED_EXIT) or 0.0)))
        except (TypeError, ValueError):
            return EXHAUSTED_EXIT

    def _energy_baseline(self) -> float:
        cfg = self._cfg()
        return clamp("energy", getattr(cfg, "energy_baseline", 0.6))

    @property
    def enabled(self) -> bool:
        cfg = self._cfg()
        return bool(getattr(cfg, "enabled", True))

    # ── 持久化 ──

    def load(self, store: Any) -> None:
        """从数据库恢复上次的情绪；失败则保持默认值。"""

        self._loaded = False
        if store is None:
            return
        try:
            row = store.get_mood()
        except Exception:  # noqa: BLE001
            return
        if not row:
            self._state = MoodState(
                **self.baselines(),
                energy=self._energy_baseline(),
                updated_at=time.time(),
            )
            self._state = refresh(
                self._state,
                baselines=self.baselines(),
                arousal_baseline=self._arousal_baseline(),
            )
            self._loaded = True
            return

        primitives: dict[str, float] = {}
        for name in EMOTIONS:
            raw = row.get(f"emotion_{name}") if hasattr(row, "get") else None
            if raw is None:
                raw = row.get(name) if hasattr(row, "get") else None
            if raw is None:
                primitives[name] = float(DEFAULT_PRIMITIVES[name])
            else:
                primitives[name] = clamp_primitive(name, raw)

        # 判断这行是「老库迁移来的」还是「真的写了基元」。
        # 不能只看列是否存在：_migrate 用 ALTER TABLE ... DEFAULT 补列，
        # 老行的基元列会被填成默认值而不是 NULL，于是「有没有基元」永远为真，
        # 反推分支就成了死代码，老用户升级后情绪会被静默清零。
        # 可靠的判据是**自相矛盾**：老行只写了 valence/arousal，派生量与
        # 按默认基元算出来的值差得很远。
        if self._looks_like_legacy(row, primitives):
            primitives = self._reconstruct_from_legacy(row)

        self._state = MoodState(
            **primitives,
            energy=clamp("energy", row.get("energy")),
            label=str(row.get("label") or ""),
            reason=str(row.get("reason") or ""),
            updated_at=float(row.get("updated_at") or 0.0) or time.time(),
            exhausted=bool(row.get("exhausted") or False),
        )
        self._state = refresh(
            self._state,
            baselines=self.baselines(),
            arousal_baseline=self._arousal_baseline(),
        )
        self._loaded = True

    def _looks_like_legacy(self, row: Any, primitives: dict[str, float]) -> bool:
        """这行是不是「只有 valence/arousal、没有真基元」的老数据。

        判据是自相矛盾：库里存的派生量和按当前基元重算出来的派生量差得
        太远，说明基元列是被 ALTER TABLE 的 DEFAULT 填出来的假值。
        """

        try:
            stored_valence = clamp("valence", row.get("valence"))
        except Exception:  # noqa: BLE001
            return False
        derived = derive_valence(primitives, self.baselines())
        # 0.25 是很宽的容差：正常的浮点误差、四舍五入都不会到这里，
        # 而「valence 存的是 -0.8、基元全等于默认基线」这种矛盾必然超过它。
        return abs(stored_valence - derived) > 0.25

    def _reconstruct_from_legacy(self, row: Any) -> dict[str, float]:
        """老库只有 valence/arousal 时，反推一套合理的基元。"""

        primitives = dict(self.baselines())
        try:
            valence = clamp("valence", row.get("valence"))
            arousal = clamp("arousal", row.get("arousal"))
        except Exception:  # noqa: BLE001
            return primitives
        if valence >= 0.0:
            primitives["joy"] = clamp_primitive("joy", primitives["joy"] + valence * 0.45)
            primitives["calm"] = clamp_primitive("calm", primitives["calm"] + valence * 0.20)
        else:
            share = -valence
            primitives["sadness"] = clamp_primitive("sadness", primitives["sadness"] + share * 0.30)
            primitives["anger"] = clamp_primitive("anger", primitives["anger"] + share * 0.25)
            primitives["anxiety"] = clamp_primitive("anxiety", primitives["anxiety"] + share * 0.20)
        if arousal >= 0.5:
            lift = (arousal - 0.5) * 0.5
            primitives["surprise"] = clamp_primitive("surprise", primitives["surprise"] + lift)
            primitives["calm"] = clamp_primitive("calm", primitives["calm"] - lift)
        else:
            primitives["calm"] = clamp_primitive("calm", primitives["calm"] + (0.5 - arousal) * 0.3)
        return primitives

    def persist(self, store: Any, *, now: float | None = None) -> None:
        """把当前（已衰减到 now 的）状态写回数据库。"""

        if store is None or not self._loaded:
            return
        current = time.time() if now is None else now
        self._state = self._decay_to(current)
        payload: dict[str, Any] = {name: getattr(self._state, name) for name in EMOTIONS}
        payload.update(
            {
                "valence": self._state.valence,
                "arousal": self._state.arousal,
                "energy": self._state.energy,
                "label": self._state.label,
                "reason": self._state.reason,
                "exhausted": self._state.exhausted,
                "now": current,
            }
        )
        try:
            store.upsert_mood(**payload)
        except TypeError:
            # 老版本 store 不认 emotion_* 参数时退化为只写三维，
            # 免得整个落库失败（升级过程中可能短暂不一致）。
            try:
                store.upsert_mood(
                    valence=self._state.valence,
                    arousal=self._state.arousal,
                    energy=self._state.energy,
                    label=self._state.label,
                    reason=self._state.reason,
                    exhausted=self._state.exhausted,
                    now=current,
                )
            except Exception:  # noqa: BLE001 - 落库失败不能影响回复
                pass
        except Exception:  # noqa: BLE001 - 落库失败不能影响回复
            pass

    # ── 读取 ──

    def _decay_to(self, now: float) -> MoodState:
        self._state = decay(
            self._state,
            now=now,
            baselines=self.baselines(),
            half_lives=self.half_lives(),
            arousal_baseline=self._arousal_baseline(),
            energy_recover_per_hour=self._energy_recover_per_hour(),
            offline_reset_hours=self._offline_reset_hours(),
            exhausted_exit=self._exhausted_exit(),
        )
        return self._state

    def snapshot(self, *, now: float | None = None) -> MoodState:
        """返回衰减后的只读快照（不修改内部状态）。"""

        current = time.time() if now is None else now
        bases = self.baselines()
        state = decay(
            self._state,
            now=current,
            baselines=bases,
            half_lives=self.half_lives(),
            arousal_baseline=self._arousal_baseline(),
            energy_recover_per_hour=self._energy_recover_per_hour(),
            offline_reset_hours=self._offline_reset_hours(),
            exhausted_exit=self._exhausted_exit(),
        )
        if not state.label:
            state.label = label_for(
                primitives_of(state), state.energy, exhausted=state.exhausted, baselines=bases
            )
        return state

    # ── 驱动 ──

    def _apply_delta(self, delta: dict[str, float], *, now: float) -> MoodState:
        self._decay_to(now)
        for name, amount in delta.items():
            if name in EMOTIONS:
                setattr(self._state, name, clamp_primitive(name, getattr(self._state, name) + amount))
            elif name in BOUNDS:
                setattr(self._state, name, clamp(name, getattr(self._state, name) + amount))
        # 精力落到 0 时上锁；回到阈值以上才解锁（滞回）。
        if self._state.energy >= self._exhausted_exit():
            self._state.exhausted = False
        elif self._state.energy <= 0.0:
            self._state.exhausted = True
        self._state.label = ""
        self._state.updated_at = now
        self._state = refresh(
            self._state,
            baselines=self.baselines(),
            arousal_baseline=self._arousal_baseline(),
        )
        return self._state

    def on_message(self, text: str, *, now: float | None = None) -> MoodState:
        """入站消息的启发式影响。由 capture hook 调用，必须快且不抛异常。"""

        current = time.time() if now is None else now
        if not self.enabled:
            return self.snapshot(now=current)
        gain = 1.0
        cfg = self._cfg()
        try:
            gain = max(0.0, float(getattr(cfg, "heuristic_gain", 1.0) or 0.0))
        except (TypeError, ValueError):
            gain = 1.0
        delta = heuristic_delta(text)
        scaled = {name: value * gain for name, value in delta.items()}
        return self._apply_delta(scaled, now=current)

    def on_reply(self, text: str, *, now: float | None = None) -> MoodState:
        """Bot 自己发出回复后的影响：按字数与当前情绪扣精力。"""

        current = time.time() if now is None else now
        if not self.enabled:
            return self.snapshot(now=current)
        # 用衰减后的当前值算消耗倍率：心情好时同一句话更省力。
        self._decay_to(current)
        delta = heuristic_delta(
            text,
            from_bot=True,
            valence=self._state.valence,
            arousal=self._state.arousal,
            primitives=primitives_of(self._state),
        )
        return self._apply_delta(delta, now=current)

    def apply_llm(self, target: dict[str, Any] | None, *, now: float | None = None) -> MoodState:
        """应用 LLM 给出的权威评估。"""

        current = time.time() if now is None else now
        if not self.enabled or not isinstance(target, dict) or not target:
            return self.snapshot(now=current)
        cfg = self._cfg()
        try:
            weight = float(getattr(cfg, "llm_blend_weight", 0.7) or 0.0)
        except (TypeError, ValueError):
            weight = 0.7
        self._decay_to(current)
        self._state = blend(
            self._state,
            target=target,
            weight=weight,
            now=current,
            baselines=self.baselines(),
            arousal_baseline=self._arousal_baseline(),
        )
        return self._state

    def set_manual(
        self,
        *,
        primitives: dict[str, float] | None = None,
        energy: float | None = None,
        label: str = "",
        reason: str = "",
        now: float | None = None,
    ) -> MoodState:
        """人工覆盖（管理页面用）。

        ``primitives`` 是 ``{"joy": 0.8, ...}`` 形式的局部更新，没给的键不动。
        """

        current = time.time() if now is None else now
        self._decay_to(current)
        if isinstance(primitives, dict):
            for name, value in primitives.items():
                if name in EMOTIONS:
                    setattr(self._state, name, clamp_primitive(name, value))
        if energy is not None:
            self._state.energy = clamp("energy", energy)
        # 手改精力也要重算耗尽锁，否则页面上写着 0.8 却被锁着。
        if self._state.energy >= self._exhausted_exit():
            self._state.exhausted = False
        elif self._state.energy <= 0.0:
            self._state.exhausted = True
        self._state.label = str(label or "").strip()[:LABEL_MAX_CHARS]
        self._state.reason = str(reason or "").strip()[:60]
        self._state.updated_at = current
        self._state = refresh(
            self._state,
            baselines=self.baselines(),
            arousal_baseline=self._arousal_baseline(),
        )
        return self._state

    def reset(self, *, now: float | None = None) -> MoodState:
        """回到基线。"""

        current = time.time() if now is None else now
        self._state = MoodState(
            **self.baselines(),
            energy=self._energy_baseline(),
            label="",
            reason="",
            updated_at=current,
        )
        self._state = refresh(
            self._state,
            baselines=self.baselines(),
            arousal_baseline=self._arousal_baseline(),
        )
        return self._state

    # ── 门控 ──

    def can_initiate(self, *, now: float | None = None) -> bool:
        """现在允许不允许**主动**说话。

        精力耗尽时返回 False：这时 Bot 不该自己找话题，但被点名/@ 仍然要回
        （那条路径由上层 hook 判断，不经过这里）。
        """

        if not self.enabled:
            return True
        return not self.snapshot(now=now).exhausted

    def voice_level(self, *, now: float | None = None) -> float:
        """要不要**彻底停下来**（1.0 = 照常，0.0 = 压到下限）。

        只有两档，**不再按精力连续压频率**：用户明确说过「精力影响的应该是对
        当前话题的回复欲望和本次回复字数的多少，而不是简单的影响发言频率
        （这样会很不自然）」。按比例压频率会让 Bot 时灵时不灵，同一句话有时
        回有时不回，看起来像掉线而不是像累了。

        现在精力对「说什么」的影响全部走 :func:`effort_for` 的提示词指令
        （回复欲望 + 字数），这里只保留「耗尽 = 真的安静下来」这一个硬开关：
        耗尽时提示词劝不住（模型仍会接话），必须靠宿主频率兜底。
        """

        if not self.enabled:
            return 1.0
        return 0.0 if self.snapshot(now=now).exhausted else 1.0

    # ── 展示 ──

    def describe(self) -> str:
        state = self.snapshot()
        text = (
            f"{state.label}（愉悦度 {state.valence:+.2f} / "
            f"情绪强度 {state.arousal:.2f} / 精力 {state.energy:.2f}"
        )
        return f"{text}，已耗尽）" if state.exhausted else f"{text}）"

    def describe_primitives(self) -> str:
        """八个基元的一行摘要（日志/诊断用）。"""

        state = self.snapshot()
        parts = [f"{EMOTION_LABELS[name]} {getattr(state, name):.2f}" for name in EMOTIONS]
        return " / ".join(parts)
