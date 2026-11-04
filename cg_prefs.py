"""喜好与厌恶：Bot 自己的口味，从人设里长出来。

用户第 32 轮第 5 点问：「插件是否能让 bot 根据人设产生自己的喜好和厌恶？
如果可以，增加这一模块，UI 中放在『认识的人』和『记忆』两个模块中间。」

能，而且和情绪模块是一套逻辑：**人设是种子，聊天是养料**。

- 人设（宿主 ``bot_config.toml`` 的 ``[personality]``）决定「一开始喜欢什么」：
  一个「喜欢颜文字、会发表情包、微微傲娇的鲸鱼娘女仆」天然喜欢可爱的东西、
  被人夸、有人接她的梗；讨厌被无视、被当工具人、被强行塞奇怪的设定。
- 之后的聊天再微调强度：聊到某个话题时她兴致高，那个喜好就变强一点；
  被这个话题冒犯了，厌恶就变强一点。

**为什么不直接读人设字符串然后正则抠词**：人设是给人看的自然语言
（实测那句 66 个字里塞了性格、语气、说话习惯、能力偏好四类信息），
抠出来的词既不准也不像人。所以这里只在**首次**用一次 LLM 从人设里提炼
初始喜好，之后靠搭车抽取（和情绪、人物共用同一次 LLM 调用）慢慢演化。

这一层的取舍和人物标签一致：**只存「喜欢/讨厌什么」这一件事**，
不存理由的长篇解释，也不存具体经历——那些属于记忆模块。
"""

from __future__ import annotations

import time
from typing import Any

#: 两种取向。用 like / dislike 而不是布尔值，是因为页面上要分别配色，
#: 而且以后可能加「无所谓」，布尔值不好扩展。
KINDS: tuple[str, ...] = ("like", "dislike")

KIND_LABELS: dict[str, str] = {"like": "喜欢", "dislike": "讨厌"}

#: 单条话题的长度上限。用户要的是「喜欢猫」「讨厌被叫阿姨」这种一句话，
#: 不是一段描述。
TOPIC_MAX_CHARS = 24

#: 一个 Bot 最多同时挂多少条喜好。太多会互相稀释，注入时也塞不下。
MAX_PREFS = 20

#: 强度范围。0 表示「淡到快忘了」，1 表示「非常强烈」。
STRENGTH_BOUND = (0.0, 1.0)

#: 来源标记：人设提炼 / 用户手写 / 聊天里演化出来的。
SOURCE_PERSONA = "persona"
SOURCE_MANUAL = "manual"
SOURCE_CHAT = "chat"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_PERSONA: "人设",
    SOURCE_MANUAL: "手写",
    SOURCE_CHAT: "聊天",
}


def clamp_strength(value: Any) -> float:
    """把强度夹到 0..1；非法值回落 0.5。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if number != number:  # NaN
        return 0.5
    low, high = STRENGTH_BOUND
    return min(high, max(low, number))


def normalize_kind(value: Any) -> str:
    """把各种写法收敛成 like / dislike；认不出来返回空串。"""

    text = str(value or "").strip().lower()
    if text in KINDS:
        return text
    return {
        "喜欢": "like",
        "爱": "like",
        "好感": "like",
        "讨厌": "dislike",
        "厌恶": "dislike",
        "反感": "dislike",
    }.get(text, "")


def normalize_topic(value: Any) -> str:
    """清洗一条话题：压空白、去首尾标点、限长。"""

    text = " ".join(str(value or "").split())
    text = text.strip("。．.!！?？,，、;；:：\"'“”‘’()（）[]【】")
    return text[:TOPIC_MAX_CHARS]


def prefs_key(kind: str, topic: str) -> str:
    """一条喜好的主键：取向 + 话题。同一话题不能既喜欢又讨厌。"""

    return f"{kind}:{topic}"


def parse_prefs(raw: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    """解析模型给的 likes / dislikes。

    模型可能给出 ``["猫", "被夸"]`` 这样的纯字符串数组，也可能给
    ``[{"topic": "猫", "strength": 0.8}]``。两种都接。

    Args:
        raw: 模型输出的原始值。
        limit: 最多取几条。

    Returns:
        规范化的 ``[{kind, topic, strength}]``；非法条目直接丢掉。
    """

    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            kind = ""
            topic = normalize_topic(entry)
            strength = 0.6
        elif isinstance(entry, dict):
            kind = normalize_kind(entry.get("kind"))
            topic = normalize_topic(entry.get("topic"))
            strength = clamp_strength(entry.get("strength", 0.6))
        else:
            continue
        if not topic:
            continue
        key = topic
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "topic": topic, "strength": strength})
        if len(out) >= max(0, int(limit)):
            break
    return out


def render_prefs_block(
    prefs: list[dict[str, Any]],
    *,
    header: str = "<companion-prefs>",
    footer: str = "</companion-prefs>",
    lead_in: str = "（这些是你自己的口味，不是别人告诉你的）",
    max_chars: int = 200,
    max_items: int = 6,
    min_strength: float = 0.35,
) -> tuple[str, list[dict[str, Any]]]:
    """把喜好渲染成注入文本。

    用户第 32 轮第 5 点的原话是「让 bot 根据人设产生自己的喜好和厌恶」——
    重点是**她有自己的口味**，所以这里写成人称句（「你挺喜欢猫」），
    而不是清单（「喜欢：猫」）。后者读起来像设定文档，前者才像她自己的想法。

    Args:
        prefs: ``[{kind, topic, strength, source}]``。
        header: 块起始标记。
        footer: 块结束标记。
        lead_in: 引导语。
        max_chars: 整块字数上限。
        max_items: 最多几条。
        min_strength: 低于这个强度的不注入（太淡的口味不值得占预算）。

    Returns:
        ``(text, used)``；没有可用条目时返回空串。
    """

    usable = [
        row
        for row in prefs
        if normalize_kind(row.get("kind")) and str(row.get("topic") or "").strip()
    ]
    usable.sort(key=lambda row: -clamp_strength(row.get("strength")))
    usable = [row for row in usable if clamp_strength(row.get("strength")) >= min_strength]
    if not usable:
        return "", []

    head = header or ""
    tail = footer or ""
    intro = lead_in or ""
    budget = max_chars - len(head) - len(tail) - len(intro) - 8
    if budget <= 0:
        return "", []

    used: list[dict[str, Any]] = []
    lines: list[str] = []
    for row in usable[:max(0, int(max_items))]:
        kind = normalize_kind(row.get("kind"))
        topic = normalize_topic(row.get("topic"))
        strength = clamp_strength(row.get("strength"))
        if kind == "like":
            # 强度分档换措辞，避免每条都是同一个句式读起来像列表。
            verb = "挺喜欢" if strength >= 0.7 else "有点喜欢"
        else:
            verb = "很讨厌" if strength >= 0.7 else "不太喜欢"
        line = f"- 你{verb}{topic}。"
        if len(line) > budget - sum(len(item) + 1 for item in lines):
            break
        lines.append(line)
        used.append(row)
    if not lines:
        return "", []

    body = "\n".join(lines)
    text = f"{head}\n{intro}\n{body}\n{tail}" if intro else f"{head}\n{body}\n{tail}"
    if len(text) > max_chars:
        # 兜底：按整块上限硬截，保证一定不超预算（宁可少一句也不撑爆）。
        keep = max_chars - len(tail) - 2
        text = text[: max(0, keep)].rstrip() + "\n" + tail
    return text, used


def merge_pref(
    existing: dict[str, Any] | None,
    observation: dict[str, Any],
    *,
    now: float | None = None,
    decay: float = 0.85,
) -> dict[str, Any]:
    """把一次观察合并进已有喜好。

    合并规则（和人物好感度同构，理由也一样）：

    - **手写的永远优先**：用户自己写的口味不该被模型改掉。
    - **强度用衰减加权平均**，不是直接覆盖：一次聊天里的一时兴起不该
      把长期的偏好整个改掉，但反复出现会慢慢把它推过去。
    - 取向不同（本来喜欢、这次讨厌）不合并成一条，交给调用方按 topic
      分别存——「喜欢猫」和「讨厌猫毛」是可以同时成立的。

    Args:
        existing: 库里已有的行，``None`` 表示新建。
        observation: 本次观察 ``{kind, topic, strength}``。
        now: 当前时间戳。
        decay: 旧值的权重。0.85 表示「新证据占 15%」，大概六七次一致
            观察才能把强度从 0.5 推到 0.8。

    Returns:
        合并后的行（可直接交给 ``upsert_pref``）。
    """

    current = time.time() if now is None else now
    kind = normalize_kind(observation.get("kind"))
    topic = normalize_topic(observation.get("topic"))
    strength = clamp_strength(observation.get("strength"))
    if not kind or not topic:
        return {}

    if existing is None:
        return {
            "pref_key": prefs_key(kind, topic),
            "kind": kind,
            "topic": topic,
            "strength": strength,
            "source": str(observation.get("source") or SOURCE_CHAT),
            "created_at": current,
            "updated_at": current,
        }

    merged = dict(existing)
    # 手写的口味不接受自动观察的强度改动，只更新时间戳。
    if str(existing.get("source") or "") == SOURCE_MANUAL:
        merged["updated_at"] = current
        return merged

    old_strength = clamp_strength(existing.get("strength"))
    merged["strength"] = round(old_strength * decay + strength * (1.0 - decay), 4)
    merged["updated_at"] = current
    if not merged.get("created_at"):
        merged["created_at"] = current
    return merged


def describe_pref(row: dict[str, Any]) -> str:
    """命令输出用的一行描述。"""

    kind = normalize_kind(row.get("kind"))
    topic = normalize_topic(row.get("topic"))
    strength = clamp_strength(row.get("strength"))
    source = SOURCE_LABELS.get(str(row.get("source") or ""), "未知")
    return f"{KIND_LABELS.get(kind, kind)} {topic}（强度 {strength:.2f}，{source}）"


def build_persona_prompt(persona: str, *, limit: int = 6) -> str:
    """从人设文本里提炼初始喜好的提示词。

    只在**首次**（或用户点「从人设重新生成」时）调用一次，不是每轮都调：
    人设不会天天变，反复提炼既浪费配额又会让口味飘来飘去。

    Args:
        persona: 宿主 ``[personality]`` 节的文本（人格 + 行为准则 + 说话风格）。
        limit: 每个方向最多几条。

    Returns:
        提示词全文。
    """

    text = " ".join(str(persona or "").split())
    return (
        "下面是一个聊天机器人的角色设定。请根据这份设定，推断**这个角色自己**"
        "会喜欢什么、讨厌什么。\n"
        "注意：\n"
        "1. 是「她自己的口味」，不是「设定里明确写了喜欢 X」。设定里写「喜欢用颜文字」，"
        "就该推断出她喜欢可爱、轻松的表达；写「微微傲娇」，就该推断出她讨厌被随便使唤。\n"
        "2. 只写**话题**，不要写长句。每条 12 字以内，例如「猫」「被夸奖」「深夜的安静」"
        "「被当工具人」「强行塞设定」。\n"
        "3. 喜欢和讨厌各给 3~6 条，不要重复，不要写「聊天」「交流」这种什么都能套的废话。\n"
        "4. 强度 0 到 1：设定里明确体现的给 0.8 以上，只是合理推断的给 0.5 左右。\n"
        f"5. 只输出 JSON，不要解释。\n"
        "\n"
        "角色设定：\n"
        "<<<\n"
        f"{text[:1200]}\n"
        ">>>\n"
        "\n"
        "输出格式：\n"
        '{"likes":[{"topic":"猫","strength":0.8},{"topic":"被夸奖","strength":0.7}],'
        '"dislikes":[{"topic":"被当工具人","strength":0.8}]}\n'
        f"（每边最多 {limit} 条）"
    )


def parse_persona_response(raw: str, *, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    """解析人设提炼结果。

    Returns:
        ``{"likes": [...], "dislikes": [...]}``，两边的元素都带 ``kind``。
        解析失败时两个列表都是空的（调用方据此判断「没提炼出来」）。
    """

    from .cg_extract import extract_json_object  # 复用同一套容错解析

    payload = extract_json_object(raw)
    if not isinstance(payload, dict):
        return {"likes": [], "dislikes": []}

    likes = parse_prefs(payload.get("likes"), limit=limit)
    for row in likes:
        row["kind"] = "like"
    dislikes = parse_prefs(payload.get("dislikes"), limit=limit)
    for row in dislikes:
        row["kind"] = "dislike"
    return {"likes": likes, "dislikes": dislikes}
