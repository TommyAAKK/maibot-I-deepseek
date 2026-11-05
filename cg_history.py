"""未读历史：她知道自己错过了什么，也能在被提起时翻回去看。

## 这个模块解决什么

用户第 41 轮的原话：

> 正常人虽然会翻看历史消息，但仅限于几十条的程度，对于 99+ 这种，一般不会
> 从头看完。而 bot 现在抓取会直接全都看完，就失去了离线的感觉，反而会产生
> 一种「我一直看着，但我不说话」的监控感。

先说清事实：抽取从来就没「全都看完」——``cg_tracker._do_extract`` 只把最后
``capture.max_messages_per_extract`` 条（默认 30）交给模型。但用户的直觉指向了
一个真问题：**补抓回来的那 200 条，除了最后 30 条，剩下 170 条被直接丢掉了。**

于是现状既不是「监控感」，也不是「如实缺席」，而是最糟的第三种：**假装没缺过
席。** 她 digest 了回来后的前一小时，然后像从头到尾都在场一样说话，对中间那
170 条一无所知，也永远不会说「那会儿我不在，你们聊了什么」。

## 四层模型

+------+----------------------------+--------------------------+
| L0   | 最近 30 条原文             | 抽取窗口（已有）         |
| L1   | 「昨晚有 199 条我没看见」  | 一行计数 + 时间范围      |
| L2   | 被明确提起时翻出具体几条   | 原文入库，按需检索       |
| L3   | 「我翻了，没找到」         | 检索为空时如实说         |
+------+----------------------------+--------------------------+

**L1 是灵魂。** 它只存一个数字加一段时间，却让她能说真话：「你们昨晚聊了两百
条？我一条都没看到，那会儿我睡着了。」这句话的价值高于「默默记住全部」——因
为后者是假的，前者是真的。

**L2 不调模型。** 它跑在 ``maisaka.replyer.before_request`` 里，整条链路只有
6000ms 预算，此刻还没开始生成回复。再调一次 LLM 既超预算又会和主生成抢配额。
所以用纯字符串相似度（``cg_store.recall_score``），和「想起遗忘记忆」那条路
是同一个形状、同一个理由。

**L3 是防幻觉的。** 她翻了记录却没找到对得上的内容时，得知道该说「我翻过了，
没看到」，而不是编一段出来。静默地什么都不注入，模型就会顺着上下文圆下去。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Iterable

from .cg_store import recall_score

# ── 常量 ──

#: 一次最多把几条原文送进提示词。再多就不是「翻翻看」，是「通读」了。
DEFAULT_RECALL_LIMIT = 3

#: 消息比记忆长得多，同一个阈值会几乎什么都匹配不上。
#: 记忆那边是 0.40，这里放低一点。
DEFAULT_RECALL_SIMILARITY = 0.34

#: 单个洞最多存多少条原文。真正的条数记在 ``gap_unread`` 的 count 里，
#: 这个只限制「能翻出来多少」。
MAX_HOLE_ROWS = 400

#: 让她意识到「该翻记录了」的线索词。
#:
#: 为什么不交给模型判断：这里是 replyer 的 before_request，没有预算。而且这些
#: 词是**人类真的会用来指代过去**的词，误报的代价只是多看一眼记录（可能什么
#: 都没翻到，然后走 L3 说「没找到」），漏报的代价是她对着一句「昨天那个」装傻。
LOOKUP_HINTS: tuple[str, ...] = (
    "昨天",
    "昨晚",
    "前天",
    "上次",
    "之前",
    "刚才",
    "刚刚",
    "那时候",
    "当时",
    "那会儿",
    "说过",
    "提过",
    "聊过",
    "讲过",
    "记录",
    "历史",
    "翻翻",
    "翻看",
    "查查",
    "找找",
    "还记得",
    "记得吗",
)

DEFAULT_UNREAD_HEADER = "<companion-unread>"
DEFAULT_UNREAD_FOOTER = "</companion-unread>"
DEFAULT_LOOKUP_HEADER = "<companion-lookup>"
DEFAULT_LOOKUP_FOOTER = "</companion-lookup>"

#: L1 的语气。陈述事实，不加情绪——和 ``cg_presence`` 的 away 档同一个原则：
#: 她不该因为自己不在而演一场遗憾。
UNREAD_INTRO = "（那段时间你不在。这些消息你一条都没看到：）"

#: L1 的收尾规则。两句话缺一不可：允许她提，但**禁止假装看过**。
UNREAD_OUTRO = (
    "想提就自然地提一句，不想提也不必硬提。"
    "但**绝对不要假装你看过这些内容**——你确实不在，不知道就是不知道。"
)

LOOKUP_LEAD_IN = (
    "（对方提到的事你可能不在场。你翻了一下那段时间的聊天记录，找到这几条。）"
    "只在真的接得上时提一句，别把记录念出来。"
)

LOOKUP_EMPTY_NOTE = (
    "（对方提到的事你可能不在场。你翻了一下那段时间的聊天记录，**没有找到对得上的内容**。）"
    "如实说没翻到就行，不要凭上下文猜一个答案出来。"
)


# ── 时间工具 ──


def as_float(value: Any, default: float = 0.0) -> float:
    """把宿主消息里那个「有时是字符串有时是数字」的时间戳变成 float。"""

    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def message_stamp(message: dict[str, Any]) -> float:
    """取一条消息的时间戳；取不到返回 0.0（排到最前面，不会污染洞的范围）。"""

    return as_float(message.get("timestamp"))


def format_clock(ts: float) -> str:
    """``00:12`` 这样的时刻。"""

    if ts <= 0:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def format_day(ts: float, *, now: float | None = None) -> str:
    """``今天`` / ``昨天`` / ``前天`` / ``11 月 3 日``。

    刻意不用「3 天前」这种相对量：说的是「昨晚 11 点」，写成「3 天前」就没法
    对着时间线讲了。
    """

    if ts <= 0:
        return "?"
    current = time.time() if now is None else now
    try:
        then = datetime.fromtimestamp(ts)
        today = datetime.fromtimestamp(current)
    except (OverflowError, OSError, ValueError):
        return "?"
    delta_days = (today.date() - then.date()).days
    if delta_days <= 0:
        return "今天"
    if delta_days == 1:
        return "昨天"
    if delta_days == 2:
        return "前天"
    if then.year == today.year:
        return f"{then.month} 月 {then.day} 日"
    return f"{then.year} 年 {then.month} 月 {then.day} 日"


def describe_span(start_ts: float, end_ts: float, *, now: float | None = None) -> str:
    """把一段时间说成「昨晚 00:12 到 10:58」。"""

    start_day = format_day(start_ts, now=now)
    end_day = format_day(end_ts, now=now)
    if start_day == end_day:
        return f"{start_day} {format_clock(start_ts)} 到 {format_clock(end_ts)}"
    return f"{start_day} {format_clock(start_ts)} 到 {end_day} {format_clock(end_ts)}"


def describe_duration(seconds: float) -> str:
    """粗粒度时长：不追求精确，追求「昨晚那一段」这种手感。"""

    hours = max(0.0, float(seconds)) / 3600.0
    if hours < 1.0:
        return f"{int(max(1.0, hours * 60))} 分钟"
    if hours < 24.0:
        return f"{hours:.1f} 小时".replace(".0 ", " ")
    days = hours / 24.0
    if days < 30.0:
        return f"{days:.1f} 天".replace(".0 ", " ")
    return f"{days / 30.0:.1f} 个月".replace(".0 ", " ")


# ── L1：拆出「她没读到的那部分」 ──


def split_window(
    messages: Iterable[dict[str, Any]],
    max_read: int,
    *,
    since_ts: float = 0.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把合并后的消息拆成「她要读的窗口」和「她读不到的洞」。

    ``since_ts`` 是上次抽取的时间：比它更早的消息在上一轮已经处理过了，不该
    再算成本次的洞，否则每轮都会重复报同一段。
    """

    ordered = [m for m in messages if isinstance(m, dict)]
    ordered.sort(key=message_stamp)
    keep = max(0, int(max_read))
    if keep <= 0:
        window: list[dict[str, Any]] = []
        rest = ordered
    else:
        window = ordered[-keep:]
        rest = ordered[:-keep]
    floor = max(0.0, float(since_ts))
    hole = [m for m in rest if message_stamp(m) > floor]
    return window, hole


def summarize_hole(hole: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any] | None:
    """把一段洞压缩成「时间范围 + 条数」。

    返回 ``None`` 表示这段洞没有可用的时间戳（拿不到时间就没法说「昨晚」，
    与其瞎猜不如不记）。
    """

    stamps = [message_stamp(m) for m in hole]
    stamps = [s for s in stamps if s > 0]
    if not stamps:
        return None
    start = min(stamps)
    end = max(stamps)
    current = time.time() if now is None else now
    return {
        "start_ts": start,
        "end_ts": end,
        "count": len(hole),
        "duration_seconds": max(0.0, end - start),
        "age_seconds": max(0.0, current - end),
    }


def to_history_rows(hole: list[dict[str, Any]], *, limit: int = MAX_HOLE_ROWS) -> list[dict[str, Any]]:
    """把洞里的消息转成入库行。

    只留**最近**的 ``limit`` 条：真要回溯时，「昨晚十点那几条」比「上周三那几
    条」有用得多，而且总量得有个上限。
    """

    rows: list[dict[str, Any]] = []
    for message in hole:
        stamp = message_stamp(message)
        if stamp <= 0:
            continue
        text = message_text(message)
        if not text:
            continue
        rows.append(
            {
                "message_id": str(message.get("message_id") or ""),
                "ts": stamp,
                "speaker": speaker_of(message),
                "user_id": user_id_of(message),
                "text": text,
            }
        )
    rows.sort(key=lambda row: row["ts"])
    keep = max(1, int(limit))
    if len(rows) > keep:
        rows = rows[-keep:]
    return rows


def message_text(message: dict[str, Any]) -> str:
    """取消息正文。宿主会带 ``processed_plain_text``；补抓回来的没有，得自己拼。"""

    plain = message.get("processed_plain_text")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    parts: list[str] = []
    raw = message.get("raw_message")
    if isinstance(raw, list):
        for segment in raw:
            if not isinstance(segment, dict):
                continue
            kind = str(segment.get("type") or "")
            data = segment.get("data")
            if kind == "text" and isinstance(data, str):
                parts.append(data)
            elif isinstance(data, str) and data and kind in {"at", "face"}:
                continue
            elif isinstance(data, dict):
                text = data.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return " ".join(part.strip() for part in parts if part.strip()).strip()


def _user_info(message: dict[str, Any]) -> dict[str, Any]:
    info = message.get("message_info")
    if isinstance(info, dict):
        user = info.get("user_info")
        if isinstance(user, dict):
            return user
    return {}


def user_id_of(message: dict[str, Any]) -> str:
    return str(_user_info(message).get("user_id") or "").strip()


def speaker_of(message: dict[str, Any]) -> str:
    """取发言人名字。优先群名片——群里大家认的是名片不是昵称。"""

    user = _user_info(message)
    for key in ("user_cardname", "user_nickname"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    uid = str(user.get("user_id") or "").strip()
    return uid or "某人"


def group_name_of(message: dict[str, Any]) -> str:
    info = message.get("message_info")
    if isinstance(info, dict):
        group = info.get("group_info")
        if isinstance(group, dict):
            value = group.get("group_name")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ── L1：渲染 ──


def render_unread_note(
    gaps: list[dict[str, Any]],
    *,
    header: str = DEFAULT_UNREAD_HEADER,
    footer: str = DEFAULT_UNREAD_FOOTER,
    chat_type: str = "private",
    who: str = "对方",
    max_chars: int = 180,
    now: float | None = None,
) -> str:
    """渲染 L1 提示：只给条数和时间范围，**不给内容**。

    这是整个模块的核心取舍。给了内容就变成「她其实全看过了」，不给内容她才
    只能诚实地说「那会儿我不在」。
    """

    if not gaps:
        return ""
    lines: list[str] = [UNREAD_INTRO]
    budget = max(40, int(max_chars))
    used = len(header) + len(footer) + len(UNREAD_INTRO) + len(UNREAD_OUTRO) + 8
    for gap in gaps:
        start = as_float(gap.get("start_ts"))
        end = as_float(gap.get("end_ts"))
        count = int(gap.get("count") or 0)
        if count <= 0:
            continue
        where = "" if chat_type == "group" else f"{who}"
        span = describe_span(start, end, now=now)
        if where:
            line = f"- {where}在{span}之间说了 {count} 条，你不在。"
        else:
            line = f"- 这个群在{span}之间积了 {count} 条消息，你不在。"
        if used + len(line) > budget:
            break
        used += len(line)
        lines.append(line)
    if len(lines) == 1:
        return ""
    lines.append(UNREAD_OUTRO)
    return "\n".join([header, *lines, footer])


# ── L2 / L3：按需回看 ──


def looks_like_lookup(text: str) -> bool:
    """这句话是不是在指过去的事？"""

    value = str(text or "").strip()
    if len(value) < 2:
        return False
    return any(hint in value for hint in LOOKUP_HINTS)


def pick_history_hits(
    text: str,
    rows: Iterable[dict[str, Any]],
    *,
    limit: int = DEFAULT_RECALL_LIMIT,
    threshold: float = DEFAULT_RECALL_SIMILARITY,
) -> list[dict[str, Any]]:
    """按字符串相似度挑出「翻到的那几条」。

    复用 ``cg_store.recall_score`` 而不是 Jaccard：这边同样是「长句 vs 短句」，
    Jaccard 会因为长度差把分数压到永远过不了门槛。
    """

    value = str(text or "").strip()
    if not value:
        return []
    floor = max(0.0, float(threshold))
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        content = str(row.get("text") or "").strip()
        if not content:
            continue
        score = recall_score(value, content)
        if score >= floor:
            scored.append((score, row))
    if not scored:
        return []
    scored.sort(key=lambda pair: pair[0], reverse=True)
    keep = max(1, int(limit))
    return [row for _, row in scored[:keep]]


def format_hit(row: dict[str, Any], *, now: float | None = None) -> str:
    """一条翻到的记录：``昨天 22:14 阿汤：……``"""

    stamp = as_float(row.get("ts"))
    speaker = str(row.get("speaker") or "某人").strip() or "某人"
    text = str(row.get("text") or "").strip()
    return f"{format_day(stamp, now=now)} {format_clock(stamp)} {speaker}：{text}"


def render_lookup_block(
    hits: list[dict[str, Any]],
    *,
    header: str = DEFAULT_LOOKUP_HEADER,
    footer: str = DEFAULT_LOOKUP_FOOTER,
    max_chars: int = 420,
    now: float | None = None,
) -> str:
    """渲染 L2（翻到了）或 L3（翻了没找到）。

    空 ``hits`` 也**必须**返回提示——这就是 L3：她得知道自己翻过了并且没找到，
    否则会顺着上下文编一段出来。
    """

    if not hits:
        return "\n".join([header, LOOKUP_EMPTY_NOTE, footer])
    lines = [LOOKUP_LEAD_IN]
    budget = max(60, int(max_chars))
    used = len(header) + len(footer) + len(LOOKUP_LEAD_IN) + 8
    for row in hits:
        line = f"- {format_hit(row, now=now)}"
        if used + len(line) > budget:
            break
        used += len(line)
        lines.append(line)
    if len(lines) == 1:
        # 一条都塞不下时，宁可说「没找到」也不要空手而归
        return "\n".join([header, LOOKUP_EMPTY_NOTE, footer])
    return "\n".join([header, *lines, footer])


def describe_gaps(gaps: list[dict[str, Any]], *, now: float | None = None) -> str:
    """调试用一行摘要。"""

    if not gaps:
        return "无未读洞"
    pieces = []
    for gap in gaps:
        count = int(gap.get("count") or 0)
        span = describe_span(as_float(gap.get("start_ts")), as_float(gap.get("end_ts")), now=now)
        pieces.append(f"{count} 条（{span}）")
    return " / ".join(pieces)
