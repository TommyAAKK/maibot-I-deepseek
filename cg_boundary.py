"""我，deepseek —— 边界感与缺席感。

这两个功能都是「让她有脾气」，但**判定主体完全不同**，所以分两条路：

- **临场沉默**（本模块的 :func:`evaluate_silence`）：插件判定「现在该沉默」，
  然后由 planner 选择「完全不说」还是「冷淡地说一句」。插件只负责**三点同时
  成立**这个闸门和频率上限，不负责措辞。
- **状态提示**（本模块的 :func:`render_boundary_note`）：插件**不判定**冒犯，
  只把「你现在什么心情 / 你跟他关系怎样 / 你讨厌什么」交给 planner，让它自己
  判断这句话算不算冒犯、要不要回。这是用户第 12 轮明确要求的分工。

为什么沉默要三点同时成立：任何一条单独都不该让人不理人——
心情不好就不理人是任性，关系差就不理人是记仇，讨厌话题就不理人是玻璃心。
三条叠加才是「今天就是不想伺候」。

为什么必须有频率上限：没有上限，这个功能一定会退化成「经常不理人」，
用户体感会崩。上限参数由用户定（第 12 轮：「6 小时 1 次还是太严苛了，
可以改成 3 小时 2 次或 6 小时 4 次这种」），默认取 3 小时 2 次。
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Sequence

#: 沉默判定的默认阈值。三个条件各有一个门槛，全部越过才允许沉默。
DEFAULT_MIN_VALENCE = -0.35  #: 愉悦度要低到这个值以下（心情明显不好）
DEFAULT_MIN_ANGER = 0.45  #: 愤怒要高于这个值（是真的在生气，不只是不爽）
DEFAULT_MIN_TENSION = 0.55  #: 关系张力要高于这个值（跟他相处绷着）
DEFAULT_MIN_DISLIKE = 0.55  #: 命中的厌恶口味强度要高于这个值

#: 频率上限的默认值：3 小时内最多沉默 2 次。
DEFAULT_MAX_PER_WINDOW = 2
DEFAULT_WINDOW_HOURS = 3.0

#: 沉默时允许 planner 选的两条路。用户第 12 轮：「只需要告诉 planner
#: 此时需要沉默了，然后给它你说的这两个选项」。
SILENCE_MODE_SILENT = "silent"  #: 完全不发消息
SILENCE_MODE_TACITURN = "taciturn"  #: 回一句极短的冷淡话

SILENCE_INSTRUCTION = (
    "（此刻你确实不太想接这个话。你有两个选择，自己挑一个更像你的：\n"
    "- 干脆不发：什么都不说，让这条过去；\n"
    "- 冷淡地应一声：只回很短的一句（「……」/「嗯」/「不想说这个」这类），"
    "不要解释、不要展开、不要补一句缓和的话。\n"
    "除非被明确点名或 @，否则不需要勉强自己热络。）"
)

TACITURN_INSTRUCTION = (
    "（此刻你确实不太想接这个话。只回很短的一句（「……」/「嗯」/「不想说这个」这类），"
    "不要解释、不要展开、不要补一句缓和的话。）"
)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def matched_dislikes(
    message: str,
    prefs: Iterable[Mapping[str, Any]],
    *,
    min_strength: float = DEFAULT_MIN_DISLIKE,
) -> list[tuple[str, float]]:
    """找出当前这句话踩中了哪些**她讨厌的**口味。

    只返回 ``kind == "dislike"`` 且强度过门槛的条目，按强度降序。

    这里用**子串包含**而不是相似度：口味是很短的词（「被使唤来使唤去」
    「无意义的寒暄」），对方的原话里只要出现这个词就算踩中。用相似度反而会
    因为长短悬殊而漏判——这和「想起遗忘记忆」那边踩过的坑是同一个道理。
    """

    if not message:
        return []
    hits: list[tuple[str, float]] = []
    for row in prefs:
        kind = str(row.get("kind") or "").strip().lower()
        if kind != "dislike":
            continue
        topic = str(row.get("topic") or "").strip()
        if not topic:
            continue
        strength = _as_float(row.get("strength"))
        if strength < min_strength:
            continue
        if topic in message:
            hits.append((topic, strength))
    hits.sort(key=lambda pair: -pair[1])
    return hits


def evaluate_silence(
    *,
    valence: float,
    anger: float,
    tension: float,
    dislikes: Sequence[tuple[str, float]],
    now: float | None = None,
    cooldown: Any = None,
    min_valence: float = DEFAULT_MIN_VALENCE,
    min_anger: float = DEFAULT_MIN_ANGER,
    min_tension: float = DEFAULT_MIN_TENSION,
    max_per_window: int = DEFAULT_MAX_PER_WINDOW,
    window_hours: float = DEFAULT_WINDOW_HOURS,
) -> dict[str, Any]:
    """判断这一轮该不该沉默。

    三个维度**同时**过门槛才允许，缺一个都不行（理由见模块 docstring）。
    过了门槛还要过频率检查：``cooldown`` 是调用方持有的历史记录（一个 dict，
    键是过去的触发时刻列表），这里只读不改；真正的记账由调用方在**确认沉默
    之后**做（见 :func:`record_silence`），否则「判定过但最后没沉默」也会
    吃掉配额。

    Args:
        valence: 当前愉悦度（-1..1）。
        anger: 当前愤怒强度（0..1）。
        tension: 当前对这个人的关系张力（0..1）。
        dislikes: :func:`matched_dislikes` 的结果。
        now: 当前时间戳。
        cooldown: 过去沉默的时刻列表（float 序列）；``None`` 视为没有历史。
        min_valence / min_anger / min_tension: 三个维度的门槛。
        max_per_window: 时间窗内最多沉默几次；``<= 0`` 表示不限制。
        window_hours: 时间窗长度（小时）。

    Returns:
        判定结果字典：``allowed`` 表示最终是否可以沉默；``reasons`` 是**全部**
        满足/未满足的原因（用于日志排查）；``matched`` 是命中的厌恶口味；
        ``mode`` 是建议的沉默方式。
    """

    current = time.time() if now is None else float(now)
    reasons: list[str] = []

    mood_ok = valence <= min_valence and anger >= min_anger
    reasons.append(
        f"心情={'过' if mood_ok else '不过'}"
        f"(愉悦度 {valence:.2f} <= {min_valence:.2f} 且 愤怒 {anger:.2f} >= {min_anger:.2f})"
    )

    relation_ok = tension >= min_tension
    reasons.append(
        f"关系={'过' if relation_ok else '不过'}(张力 {tension:.2f} >= {min_tension:.2f})"
    )

    topic_ok = bool(dislikes)
    reasons.append(
        "话题=" + ("过(踩中 " + "、".join(topic for topic, _ in dislikes) + ")" if topic_ok else "不过")
    )

    gated = mood_ok and relation_ok and topic_ok
    if not gated:
        return {
            "allowed": False,
            "reasons": reasons,
            "matched": list(dislikes),
            "mode": "",
            "budget_left": 0,
        }

    history = [float(t) for t in (cooldown or []) if _as_float(t) > 0]
    if max_per_window <= 0 or window_hours <= 0:
        budget_left = -1  # 无限制
        within = history if max_per_window <= 0 else []
    else:
        cutoff = current - float(window_hours) * 3600.0
        within = [t for t in history if t > cutoff]
        budget_left = max(0, int(max_per_window) - len(within))

    if max_per_window > 0 and window_hours > 0 and budget_left <= 0:
        reasons.append(
            f"频率=不过({len(within)}/{int(max_per_window)} 次已用在 {window_hours:.1f} 小时内)"
        )
        return {
            "allowed": False,
            "reasons": reasons,
            "matched": list(dislikes),
            "mode": "",
            "budget_left": 0,
        }

    reasons.append(
        "频率=过"
        + (f"(窗口内还剩 {budget_left} 次)" if budget_left >= 0 else "(未限制)")
    )
    return {
        "allowed": True,
        "reasons": reasons,
        "matched": list(dislikes),
        "mode": SILENCE_MODE_SILENT,
        "budget_left": budget_left,
    }


def record_silence(history: Iterable[Any], *, now: float | None = None, keep: int = 32) -> list[float]:
    """把「这一次真的沉默了」记进历史，返回新的列表。

    必须只在实际沉默之后调用。判定阶段就记账的话，「判定过但 planner 最后
    还是回了」也会吃掉配额，配额会被白白耗光。

    ``keep`` 给列表一个上限：历史只需要留够覆盖时间窗的量，留太多没意义。
    """

    current = time.time() if now is None else float(now)
    out = [float(t) for t in history if _as_float(t) > 0]
    out.append(current)
    if keep > 0 and len(out) > keep:
        out = out[-keep:]
    return out


def render_boundary_note(
    *,
    valence: float,
    anger: float,
    tension: float,
    dislikes: Sequence[tuple[str, float]],
    label: str = "",
    silence_allowed: bool = False,
    silence_mode: str = SILENCE_MODE_SILENT,
) -> str:
    """渲染给 planner 看的那段状态说明。

    **只给状态，不给命令**（用户第 12 轮明确要求）：不写「如果被冒犯就拒绝」，
    只写「你现在是什么状态、你讨厌什么」，让 planner 自己判断这句话算不算
    冒犯、要不要回、回什么。

    措辞刻意用第二人称（「你现在有点烦」），因为这些文字会直接进 planner 的
    提示词，第一人称会被模型当成「用户在说」而不是「我自己的状态」。

    这里**没有好感度参数**：好感度是派生摘要，只用来分级（缺席感、分级表），
    不参与这段文字——写「你对这个人好感 60」既不可执行，也会诱导模型报数。
    曾经有过一个收了不用的 ``affection`` 形参，已经删掉。

    ``silence_mode`` 只在 ``silence_allowed=True`` 时有意义：

    - ``silent``（默认，对应配置 ``silence_level = "choose"``）：给出「干脆不发」
      与「冷淡应一声」两条路，让模型自己挑。
    - ``taciturn``（对应配置 ``silence_level = "taciturn"``）：不给「干脆不发」，
      只允许冷淡回一句。
    """

    lines: list[str] = []
    mood_bits: list[str] = []
    if valence <= -0.2:
        mood_bits.append("心情不太好")
    elif valence >= 0.35:
        mood_bits.append("心情不错")
    if anger >= 0.4:
        mood_bits.append("有点火")
    if not mood_bits:
        mood_bits.append("心情一般")
    if label:
        lines.append(f"此刻的你：{label}（{('、'.join(mood_bits))}）。")
    else:
        lines.append(f"此刻的你：{'、'.join(mood_bits)}。")

    if tension >= 0.5:
        lines.append("你对这个人：相处起来有点绷着，不像平时那么放松。")
    elif tension >= 0.3:
        lines.append("你对这个人：偶尔有点别扭。")

    if dislikes:
        listed = "、".join(topic for topic, _ in dislikes[:3])
        lines.append(f"你现在不太受得了：{listed}。")

    lines.append(
        "这些只影响你愿不愿意接话、用什么语气，不要把它们说出来，"
        "也不要解释自己心情不好。"
    )
    lines.append(
        "判断这句话是不是在冒犯你、要不要回应、怎么回应，由你自己决定；"
        "如果确实不想接，可以只回很短的一句，或者干脆不说。"
    )
    if silence_allowed:
        # 两种沉默强度的区别就在这里：choose 让模型在两句话之间自己挑，
        # taciturn 直接不给「干脆不发」这个选项。
        lines.append(TACITURN_INSTRUCTION if silence_mode == SILENCE_MODE_TACITURN else SILENCE_INSTRUCTION)
    return "（" + "\n".join(lines) + "）"
