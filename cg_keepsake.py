"""我，deepseek —— 留念（浪费）。

## 这是什么

用户第 58 轮的原话：「留一样完全没用的东西；对一个人好，而不是对一个任务好；
在没有任何产出的一天里，也不觉得自己欠了谁。……人身上最有生命气息的部分，
或许并不是单纯的喜怒哀乐，而是在这些不产生价值，看似毫无意义的地方。」

一条留念 = 她**没有任何用处**地留着的一样东西。一句话、一个画面、一段她自己
也没想明白的联想。它不解决问题、不是待办、不是为了以后用。

## 为什么它需要一条新规则，而不是调参数

整套记忆都是显著度驱动的：``decay_salience`` 按半衰期衰减，``trim_session``
按条数淘汰，注入按预算挑分高的。它们回答的永远是同一个问题——**值不值得占
地方**。把参数调松只会让她「记得更多」，不会让她「留一样没用的东西」：只要
还在问值不值得，答案迟早是「不值得」。

所以这里换一条保留理由：**她不想扔**。配套的淘汰规则也必须是反的：

- 不收显著度，**不衰减**（和 ``gap_history`` 同一条理由——聊天记录是事实，
  留念是她自己的东西，两者都不该被淡忘）；
- 满了之后**不按条数删最旧**。越旧的留念越该留着，按时间淘汰恰好会淘汰掉
  最像留念的那些；
- 满了就变成「需要她自己挑一个放掉」：系统只把状态标出来，**永远不会替她
  决定扔掉哪一条**。这一条是刻意的——只要自动淘汰存在，这个功能就会退化成
  一个有 12 个格子的收藏夹。

## 出口只有一个，而且它不能「有用」

她可以把某条留念说出来（走 :mod:`cg_proactive` 的主动开口），或者由用户在
WebUI 里手动放掉。除此以外它不影响任何东西：不进记忆注入、不参与排序、不计分。

**一个东西被收下来之后如果立刻用来改善回复，那它就不是没用的东西了。**
所以这个模块刻意不提供任何 ``render_*`` 往 replyer 注入的函数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 默认最多留几件。满了不删，变成「该放掉一个了」。
MAX_ITEMS = 12

#: 留念的类型。第一版只区分「一句话」和「一样东西」。
#:
#: 刻意不加「一个想法」：念头（``gap_thoughts``）已经在管她自己的想法了，
#: 再开一类只会让两边互相抢，而且念头是**有用的**（会改变她下一句话），
#: 留念恰恰要求没用。
KIND_LINE = "line"
KIND_THING = "thing"
KINDS = (KIND_LINE, KIND_THING)

KIND_LABELS = {
    KIND_LINE: "一句话",
    KIND_THING: "一样东西",
}

#: 状态。
STATUS_KEPT = "kept"
STATUS_RELEASED = "released"


@dataclass(slots=True)
class Keepsake:
    """一条留念的只读视图。"""

    id: int = 0
    session_id: str = ""
    kind: str = KIND_LINE
    content: str = ""
    source_text: str = ""
    status: str = STATUS_KEPT
    look_count: int = 0
    last_looked_ts: float = 0.0
    released_at: float = 0.0
    created_at: float = 0.0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Keepsake":
        kind = str(row.get("kind") or KIND_LINE)
        return cls(
            id=int(row.get("id") or 0),
            session_id=str(row.get("session_id") or ""),
            kind=kind if kind in KINDS else KIND_LINE,
            content=" ".join(str(row.get("content") or "").split()),
            source_text=" ".join(str(row.get("source_text") or "").split()),
            status=str(row.get("status") or STATUS_KEPT),
            look_count=int(row.get("look_count") or 0),
            last_looked_ts=float(row.get("last_looked_ts") or 0.0),
            released_at=float(row.get("released_at") or 0.0),
            created_at=float(row.get("created_at") or 0.0),
        )


def to_keepsakes(rows: Iterable[Mapping[str, Any]]) -> list[Keepsake]:
    return [Keepsake.from_row(row) for row in rows if row]


def kept(keepsakes: Sequence[Keepsake]) -> list[Keepsake]:
    return [item for item in keepsakes if item.status == STATUS_KEPT]


def is_full(keepsakes: Sequence[Keepsake], *, cap: int = MAX_ITEMS) -> bool:
    """还能不能再收一件。

    满了**不是错误**，也不触发任何清理——只是从这一件开始不再自动收，
    等她（或用户）挑一个放掉。
    """

    return len(kept(keepsakes)) >= max(1, int(cap))


def overflow_count(keepsakes: Sequence[Keepsake], *, cap: int = MAX_ITEMS) -> int:
    return max(0, len(kept(keepsakes)) - max(1, int(cap)))


def describe_keepsake(item: Keepsake) -> str:
    kind = KIND_LABELS.get(item.kind, item.kind)
    looked = f"，看过 {item.look_count} 次" if item.look_count else ""
    return f"留念 #{item.id}（{kind}{looked}）：{item.content[:40]}"


#: 满了之后给 planner 的一行。**只说状态，不说该怎么办**——「挑一个放掉」
#: 是她的决定，不是一条待办。也刻意不列出哪几条：一旦列出来，模型就会开始
#: 按「哪条最没用」排序，而那正是这个功能要避免的价值观。
OVERFLOW_LEAD = "（你收着的东西已经放不下了，一共 {count} 件。）"
OVERFLOW_TAIL = "（可以挑一件放掉，也可以什么都不做。不着急。）"


def render_overflow_note(count: int, *, cap: int = MAX_ITEMS) -> str:
    total = max(0, int(count))
    if total <= max(1, int(cap)):
        return ""
    return OVERFLOW_LEAD.format(count=total) + OVERFLOW_TAIL


#: 主动开口时，如果素材是一条留念，用这段来说明「想起它」是什么感觉。
#:
#: 注意措辞：说「没什么用」，是因为它真的没什么用；如果写成「很重要的一句话」，
#: 模型就会给它编一个意义，那就又变成有用的东西了。
MENTION_HINT = "（你留着这么一件没什么用的东西：{content}。不用为它找个理由。）"


def render_mention_hint(item: Keepsake) -> str:
    content = item.content.strip()
    if not content:
        return ""
    return MENTION_HINT.format(content=content)
