"""我，deepseek —— 念头。

## 念头是什么

一条念头 = 「她心里有一件还没结束的事」。三样东西构成它：

1. **内容**：她怎么想的（「他说没看过那部片子，我有点不信」）。
2. **来源**：这句话是从聊天记录里的**哪一句**来的。指不回去的念头一律不要。
3. **在场记录**：它被唤起过几次、有没有说出口。

## 最重要的一条纪律：念头不是事实

如果念头能自由地变成记忆，就会出现这个循环：

    猜他不开心 → 想关心他 → 把「我想关心他」当成证据 → 判定他确实不开心

次数多了她不是更有内心，只是更容易臆测。所以这一层的约束是**结构性的**，
不是靠提示词自觉：

- 念头永远写进 ``gap_thoughts``，**不写进** ``gap_items``，不参与记忆注入；
- 每条念头必须带 ``source``，``cg_extract`` 那边没有 source 的直接丢；
- 来源被遗忘或被删掉，念头跟着作废（:meth:`cg_store.GapStore.prune_orphan_thoughts`）；
- 渲染时措辞永远把它说成「你的想法」，不说成「已知的情况」。

## 兑现分三态，不是只有「说出口」

这是从同行的设计里学到的纠正。如果只统计「产生了多少、发出了多少」，系统
会慢慢把念头优化成话术候选池——那恰好是最不像人的东西。

- **自然回响**（:func:`pick_echo`）：当下聊天碰到它的来源，它被重新唤起。
  这时她可以只是想起来，不一定提。
- **主动兑现**（``voice_thought``）：极少数真的被她说了出来。
- **自然消散**：什么都没发生。**不惩罚、不记账、不算失败。**
  「从未说出口」不等于「没有价值」。

第一版只做前两种，而且第一种是主路。原因很实在：主动开口要过时机、对象、
额度、宿主的 ``proactive.trigger`` 好几道关，任何一环出问题都看不出是「没念头」
还是「被挡了」；而「影响下一句话」下一轮就能验证。

## 只做一类：不肯顺着

``kind`` 目前只写 ``dissent``。其他几种去处（去看看 / 留下来 / 搁着 / 说出来）
暂时没有出口：宿主根本没提供上网能力（``src`` 树里 ``web.search`` 零命中），
「留下来」没有任何下游。**生成了却没有出口的代码就是死代码**——这个插件已经
有过五个「写了但从没生效」的 bug，不该再添一个。等哪类有了出口再开。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .cg_store import similarity

#: 念头类型。目前只有一类：不肯顺着。
KIND_DISSENT = "dissent"

#: 念头状态。
STATUS_ACTIVE = "active"
#: 说出口了。注意**不等于**那件事解决了——解决归 ``gap_items`` 的 open_loop 管。
STATUS_VOICED = "voiced"
STATUS_EXPIRED = "expired"

#: TTL 的上下界（秒）。
#:
#: 一时的联想几十分钟就过去了，没聊完的事能留一两天。刻意**不**给统一的
#: 2~6 小时：那会把「刚才那句话让我有点在意」和「他那件事到底怎么样了」
#: 压成同一种东西。真正重要的牵挂本来就该交给 ``gap_items`` 里 30 天寿命的
#: open_loop，念头不该复制第二套长期记忆。
TTL_MIN_SECONDS = 40 * 60.0
TTL_MAX_SECONDS = 36 * 3600.0

#: 唤起判定门槛。比记忆注入的 0.40 低一点：念头的内容是她自己的话，
#: 和聊天原文用词天然对不上，真正该对上的是 ``source_text``。
ECHO_SIMILARITY = 0.28

#: 同一条念头连续被唤起多少次之后，值得考虑说出口。
VOICE_AFTER_AWAKENS = 2


def ttl_seconds(salience: float, *, min_seconds: float = TTL_MIN_SECONDS,
                max_seconds: float = TTL_MAX_SECONDS) -> float:
    """按显著度线性映射 TTL。越在意的念头留得越久。"""

    try:
        score = float(salience)
    except (TypeError, ValueError):
        score = 0.5
    score = min(1.0, max(0.0, score))
    low = max(0.0, float(min_seconds))
    high = max(low, float(max_seconds))
    return low + (high - low) * score


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


@dataclass(slots=True)
class Thought:
    """一条念头的只读视图。"""

    id: int = 0
    session_id: str = ""
    kind: str = KIND_DISSENT
    content: str = ""
    source_kind: str = ""
    source_id: int = 0
    source_text: str = ""
    salience: float = 0.5
    status: str = STATUS_ACTIVE
    awaken_count: int = 0
    last_awaken_ts: float = 0.0
    voiced_at: float = 0.0
    expires_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Thought":
        return cls(
            id=int(row.get("id") or 0),
            session_id=_norm(row.get("session_id")),
            kind=_norm(row.get("kind")) or KIND_DISSENT,
            content=_norm(row.get("content")),
            source_kind=_norm(row.get("source_kind")),
            source_id=int(row.get("source_id") or 0),
            source_text=_norm(row.get("source_text")),
            salience=float(row.get("salience") or 0.0),
            status=_norm(row.get("status")) or STATUS_ACTIVE,
            awaken_count=int(row.get("awaken_count") or 0),
            last_awaken_ts=float(row.get("last_awaken_ts") or 0.0),
            voiced_at=float(row.get("voiced_at") or 0.0),
            expires_at=float(row.get("expires_at") or 0.0),
            created_at=float(row.get("created_at") or 0.0),
            updated_at=float(row.get("updated_at") or 0.0),
        )


@dataclass(slots=True)
class Echo:
    """一次「自然回响」：当前这句话碰到了哪条念头，多像。"""

    thought: Thought
    score: float
    matched_on: str = "source"


def to_thoughts(rows: Iterable[Mapping[str, Any]]) -> list[Thought]:
    return [Thought.from_row(row) for row in rows if row]


def score_echo(thought: Thought, message: str) -> tuple[float, str]:
    """当前消息和这条念头有多像，返回 ``(分数, 命中在哪一边)``。

    两边都算，取高的：``source_text`` 是**她当时听到的原话**，``content``
    是她自己的想法。多数时候撞上的是原话，所以 ``source`` 优先。
    """

    text = _norm(message)
    if not text or not thought.content:
        return 0.0, ""
    on_source = similarity(text, thought.source_text) if thought.source_text else 0.0
    on_content = similarity(text, thought.content)
    if on_source >= on_content:
        return on_source, "source"
    return on_content, "content"


def pick_echo(
    thoughts: Sequence[Thought],
    message: str,
    *,
    threshold: float = ECHO_SIMILARITY,
) -> Echo | None:
    """挑出这轮撞上的那条念头。挑不到返回 None——**大多数轮次挑不到**。"""

    best: Echo | None = None
    for thought in thoughts:
        score, side = score_echo(thought, message)
        if score < threshold:
            continue
        if best is None or score > best.score:
            best = Echo(thought=thought, score=score, matched_on=side)
    return best


def should_voice(thought: Thought, *, min_awaken: int = VOICE_AFTER_AWAKENS) -> bool:
    """这条念头是不是到了「值得说出口」的份上。

    唤起过至少 N 次才算：一次性的联想只是想想，反复被同一段话勾起才说明它
    真的搁在她心里。**这是必要条件不是充分条件**——真正说要不要开口，还得
    过边界感和自主发言闸门，念头没有特权。
    """

    if thought.status != STATUS_ACTIVE:
        return False
    return thought.awaken_count + 1 >= max(1, int(min_awaken))


#: 给 planner 的状态说明。**只给状态，不给命令**——和边界感那段同一个原则。
PLANNER_LEAD_IN = "（你心里还有件事没放下：{content}）"
PLANNER_TAIL = "（这是你自己的想法，不是已经发生的事实。要不要提、要不要顺着对方，你自己判断。）"

#: 给 replyer 的措辞要求。这是「说一句让场面冷下来」的核心指令。
#:
#: 写得很具体是有原因的：用户要的不是「态度差」，是**不为了让人舒服**。
#: 所以这里逐条否掉那些会把她拉回讨好的动作（附和、补缓和的话、解释自己、
#: 追问、加语气词和颜文字），并明确允许不完整、答非所问。
DEFAULT_DISSENT_HEADER = "<companion-dissent>"
DEFAULT_DISSENT_FOOTER = "</companion-dissent>"

DISSENT_INSTRUCTION = (
    "（这一句不必让人舒服。\n"
    "- 可以短，可以冷，可以不接他这一茬；\n"
    "- 可以答非所问，可以说一半就停；\n"
    "- **不要**附和，**不要**补一句缓和的话，**不要**解释自己为什么这样；\n"
    "- **不要**追问，**不要**加「啦」「呀」「哈哈」，**不要**颜文字和表情；\n"
    "- 说完就完了。不需要让这条消息变得更好接。\n"
    "这是你自己的选择，不是被人冒犯了——不要演出委屈，也不要演冷酷。）"
)


def render_planner_note(thought: Thought) -> str:
    """渲染给 planner 的一行状态。

    刻意**不**写「所以你不要顺着他」：给状态、不给命令。planner 自己判断
    这句话算不算该冷下来。
    """

    content = _norm(thought.content)
    if not content:
        return ""
    return PLANNER_LEAD_IN.format(content=content) + PLANNER_TAIL


def render_dissent_block(
    *,
    reason: str = "",
    header: str = DEFAULT_DISSENT_HEADER,
    footer: str = DEFAULT_DISSENT_FOOTER,
) -> str:
    """渲染给 replyer 的那段「不必讨好」要求。

    ``reason`` 只进日志，不进提示词——**念头不能升级成事实**这条同样适用于
    「她为什么不想接」：一旦把理由写进提示词，模型会把它说出来或者解释，
    而用户要的恰恰是「她说不出理由地不想接」。

    包一层标记（和情绪、缺席感那些块一致）：这几行要和记忆、口味、人设
    区分开，模型才知道这是一条**本次回复的额外要求**，而不是又一条记忆。
    """

    body = DISSENT_INSTRUCTION
    if not header or not footer:
        return body
    return f"{header}\n{body}\n{footer}"


@dataclass(slots=True)
class ThoughtStats:
    """调试用的计数，**不对外展示成指标**。

    只以「带原因的行」出现在日志和 WebUI 列表里。一旦汇总成「本周拒绝 3 次」
    这样的分数，之后所有优化都会朝那个数字走——讨好表演只是换成了叛逆表演。
    """

    active: int = 0
    voiced: int = 0
    total: int = 0
    echoes: int = 0


def describe_echo(echo: Echo | None) -> str:
    if echo is None:
        return "没有念头被勾起"
    return (
        f"念头 #{echo.thought.id} 被勾起（{echo.score:.2f}，命中 {echo.matched_on}，"
        f"已唤起 {echo.thought.awaken_count} 次）：{echo.thought.content[:40]}"
    )
