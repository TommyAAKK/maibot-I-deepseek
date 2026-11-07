"""我，deepseek —— 当场信号与「冷下来没有」。

这个模块负责两件互相独立的事，放在一起是因为它们共用同一批判定词：

1. **当场信号**（不出现在念头表里，每轮现算）：她"说不出理由地不想接这句话"
   的时刻，有相当一部分根本没有长期来源——纯粹是这几分钟里累积出来的。
   比如同一件事今天已经问第三遍了。这些信号不需要建表、不需要生成，
   一条规则就能算出来。

2. **冷场判定**（给 ``after_response`` 的 retry 兜底用）：生成完了，看这句话
   到底冷没冷下来。没冷下来就要求重生成一次。

## 为什么当场信号里没有「精力低」「深夜」

那属于「她本来就做不到」，不是用户要的「她做得到，但不想」。把做不到的事
掺进拒绝里，看起来就像脾气不好或者敷衍，而不是有主见。

## 为什么冷场判定全是确定性规则

不用 LLM 判。理由有三个：``after_response`` 有 6000ms 预算但那是整条 hook
链共用的；判完还要再生成一次，再调一次模型就翻倍了；更重要的是——**这条
规则必须可解释**，用户看到日志要知道她为什么被要求重说。

规则只抓"最明显没冷下来"的几种，宁可漏判也不误判：重生成一次是有代价的，
把一句本来就冷的话拉回去重说，会把它说得更热。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .cg_store import similarity

# ── 当场信号 ────────────────────────────────────────────────────────

#: 同一件事近期被问过几次，就算「问烦了」。
REPEAT_ASK_TIMES = 3

#: 判「同一件事」的相似度门槛。和记忆去重的 0.72 不同——这里是**短句之间**
#: 的比较，同一件事换个说法用词会变，门槛太高就永远撞不上。
REPEAT_SIMILARITY = 0.42

#: 看最近多少条入站消息。
RECENT_WINDOW = 8

#: 多短算「敷衍回复」。
SHORT_REPLY_CHARS = 3

#: 连续几条敷衍才算「她在单方面维持」。
SHORT_REPLY_STREAK = 2

#: 近几轮里问句占比高到这个程度，就算「被当成问答机」。
QUESTION_RATIO = 0.75

#: 看最近多少条消息算「近几轮」。
QUESTION_WINDOW = 6

#: 这些字出现在句尾，基本就是「嗯」「哦」「好」这类敷衍。
_SHORT_FILLERS = frozenset("嗯哦噢喔好行对是哈呵嘿额啊呀呃")

#: 称呼 / 关心她的词。出现任意一个就不算「被当工具」——对方在跟她说话，
#: 不只是要一个答案。
#:
#: 这里**不写具体名字**：名字属于人设，写在代码里等于把某个人设钉进通用逻辑，
#: 换个人设就失效，而且仓库是公开的。「你」几乎总会出现在真正的对话里，
#: 已经够用。
_ADDRESSING = (
    "你", "您", "主人",
    "辛苦", "谢谢", "谢啦", "好久不见", "在吗", "最近",
    "怎么样", "还好吗", "想", "喜欢",
)

_QUESTION_MARK = re.compile(r"[?？]|(?:吗|呢|吧)[\s。！!~～]*$")


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def is_short_reply(text: str) -> bool:
    """这句话是不是「敷衍」：很短，而且基本只是语气词。

    刻意要求"基本只是语气词"：三个字的「不知道」很短但不是敷衍，
    她是真不知道；而「嗯嗯」「哈哈」是接了一下但没内容。
    """

    body = _norm(text)
    if not body:
        return True
    if len(body) > SHORT_REPLY_CHARS:
        return False
    return all(ch in _SHORT_FILLERS or not ch.strip() for ch in body)


def count_short_streak(messages: Sequence[str]) -> int:
    """从最后一条往前数，连续多少条是敷衍。"""

    streak = 0
    for text in reversed(list(messages)):
        if is_short_reply(text):
            streak += 1
        else:
            break
    return streak


def is_question(text: str) -> bool:
    body = _norm(text)
    if not body:
        return False
    return bool(_QUESTION_MARK.search(body))


def has_addressing(text: str) -> bool:
    body = _norm(text)
    return any(word in body for word in _ADDRESSING)


@dataclass(slots=True)
class InstantSignal:
    """这一轮算出来的当场信号。全部为 0 表示没有信号。"""

    #: 同一件事近期被问了几次（含这一次）。
    repeat_count: int = 0
    #: 反复被问的那句话（用于日志，不进提示词）。
    repeat_text: str = ""
    #: 连续敷衍了几条。
    short_streak: int = 0
    #: 最近 N 条里问句的占比。
    question_ratio: float = 0.0
    #: 最近 N 条里有没有人是在「跟她说话」而不只是要答案。
    addressed: bool = False

    @property
    def reason(self) -> str:
        """命中的信号，按强度从高到低。空字符串表示没有信号。"""

        hits: list[str] = []
        if self.repeat_count >= REPEAT_ASK_TIMES:
            hits.append(f"同一件事问到第 {self.repeat_count} 遍")
        if self.short_streak >= SHORT_REPLY_STREAK:
            hits.append(f"对方连着 {self.short_streak} 条在敷衍")
        if self.question_ratio >= QUESTION_RATIO and not self.addressed:
            hits.append(f"近几轮 {self.question_ratio:.0%} 都是问句，没人跟她说别的")
        return "；".join(hits)

    @property
    def fired(self) -> bool:
        return bool(self.reason)


def analyse_signals(
    incoming: Sequence[str],
    *,
    current: str = "",
    repeat_times: int = REPEAT_ASK_TIMES,
    repeat_similarity: float = REPEAT_SIMILARITY,
    recent_window: int = RECENT_WINDOW,
    question_window: int = QUESTION_WINDOW,
    question_ratio: float = QUESTION_RATIO,
    short_streak: int = SHORT_REPLY_STREAK,
) -> InstantSignal:
    """从最近的入站消息里算当场信号。

    ``incoming`` 是**对方说的**消息（按时间升序，最后一条最新），``current``
    是这一轮要回的那句。两者可以重复——调用方不用自己去重，这里按最后一次
    出现算。
    """

    texts = [_norm(t) for t in incoming if _norm(t)]
    head = _norm(current)
    if head and (not texts or texts[-1] != head):
        texts.append(head)

    window = texts[-max(1, int(recent_window)):]
    signal = InstantSignal()

    # ① 同一件事被问了几遍。只比问句——「今天天气不错」说四遍不是问烦了。
    if head:
        questions = [t for t in window if is_question(t)]
        hits = 0
        for text in questions:
            if similarity(head, text) >= repeat_similarity:
                hits += 1
        signal.repeat_count = hits
        signal.repeat_text = head

    # ② 连续敷衍。
    signal.short_streak = count_short_streak(window)

    # ③ 问句占比 + 有没有人在跟她说话。
    q_window = window[-max(1, int(question_window)):]
    if q_window:
        asked = sum(1 for t in q_window if is_question(t))
        signal.question_ratio = asked / len(q_window)
        signal.addressed = any(has_addressing(t) for t in q_window)

    # 阈值可由调用方放宽（配置项），这里统一收口。
    if signal.repeat_count < max(2, int(repeat_times)):
        signal.repeat_count = 0
    if signal.short_streak < max(1, int(short_streak)):
        signal.short_streak = 0
    if signal.question_ratio < float(question_ratio):
        signal.question_ratio = 0.0

    return signal


# ── 冷场判定 ────────────────────────────────────────────────────────

#: 冷下来的话不该超过这个长度。超过就几乎肯定在解释或找补。
DISSENT_MAX_CHARS = 40

#: 颜文字 / emoji。人设里写着「喜欢用颜文字」，这是最可能是罪魁的一项。
_KAOMOJI = re.compile(
    r"[（(][^）)]{0,8}[）)]\s*$"          # 结尾的 (｡･ω･｡) 这类
    r"|[\U0001F300-\U0001FAFF]"           # emoji
    r"|[\u2190-\u21FF\u2600-\u27BF]"      # 箭头、杂项符号
    r"|[｡ω●◕◔ᴗ▽▼△▲◇◆°∀´｀＞＜＾^]+"
)

#: 这些词出现在句子任何位置，都说明还在讨好。
_SOFTENERS = (
    "哈哈", "嘿嘿", "嘻嘻", "啦", "呀", "嘛", "哦哦", "嗯嗯",
    "开玩笑", "没事", "算了", "好吧", "行吧", "别介意", "我不是那个意思",
)

#: 这些词出现在**开头附近**，基本就是「补一句缓和的话」。
#:
#: 不要求正好在第一个字：常见形态是「没看过啊，不过你要是想看……」，
#: 「不过」在第 5 个字。但也不能放宽到整句——「不过」在句尾是转折收尾，
#: 不代表在找补。
_SOFT_TAILS = ("不过", "其实", "只是", "但是", "话说", "要不", "当然", "话说回来")
_SOFT_TAIL_WINDOW = 6

#: 主动提供帮助 / 把选择权交出去。这是最典型的一种「为了让场面好接」，
#: 而且它经常不带任何缓和词——「我可以陪你一起看」一个语气词都没有。
_PLEASING = (
    "要不要我", "要不要", "我可以", "我陪你", "陪你", "帮你", "帮你看看",
    "都行", "随你", "听你的", "你决定", "一起吧", "我可以的", "没关系的",
)

#: 反问式追问 = 继续顺着聊。
#:
#: 只认「呢 / 吗 / 问号」。刻意**不**把「吧」算进来：「没看过就没看过吧」
#: 结尾也是「吧」，但那是句冷淡的收尾，不是追问。误判会让她把一句本来就
#: 冷的话重说一遍，越说越热。
_FOLLOW_UP = re.compile(r"(?:呢|吗)[\s。！!~～]*$|[?？]\s*$")


@dataclass(slots=True)
class Coldness:
    """一句回复冷下来了没有。"""

    cold: bool = True
    #: 没冷下来的原因，给日志和 retry_reason 用。
    reason: str = ""
    #: 具体命中项，便于排查。
    hits: tuple[str, ...] = field(default_factory=tuple)


def _hits(text: str, *, max_chars: int = DISSENT_MAX_CHARS) -> tuple[tuple[bool, str], ...]:
    """逐项检查，返回 ``(是否致命, 说明)``。

    「致命」= 这一项单独出现就说明这句话没冷下来。分两档是因为这些特征
    的份量差得很远：

    - 一句「没看过。(｡･ω･｡)」只多一个颜文字，去掉就够冷了——为此重说一遍
      反而可能说得更热，不值得。
    - 一句「嗯嗯好的呀」带缓和词，那整个语气就是软的，必须重说。

    早先版本一律要求「命中两项」，结果是「带缓和词 + 提供帮助」这种最典型的
    讨好句因为只命中一项而被放过。
    """

    body = _norm(text)
    found: list[tuple[bool, str]] = []
    if len(body) > max_chars:
        found.append((True, f"太长了（{len(body)} 字）"))
    if _KAOMOJI.search(body):
        found.append((False, "带颜文字或表情"))
    softened = [word for word in _SOFTENERS if word in body]
    if softened:
        found.append((True, "带缓和词（" + "、".join(softened[:3]) + "）"))
    head = body[:_SOFT_TAIL_WINDOW]
    if head.startswith(_SOFT_TAILS):
        found.append((True, "开头在找补"))
    pleasing = [word for word in _PLEASING if word in body]
    if pleasing:
        found.append((True, "在提供帮助或交出选择（" + "、".join(pleasing[:3]) + "）"))
    if body.count("！") + body.count("!") >= 2:
        found.append((True, "多个感叹号"))
    if _FOLLOW_UP.search(body):
        found.append((False, "结尾在追问"))
    return tuple(found)


def judge_coldness(text: str, *, max_chars: int = DISSENT_MAX_CHARS) -> Coldness:
    """判断这句话冷下来没有。

    **宁可漏判也不要误判**：把一句本来就冷的话拉回去重说，只会把它说得更热。
    所以判据是「致命项单独成立，或两个轻项同时成立」。轻项（颜文字、结尾
    追问）单独出现不算——「没看过。」加一个颜文字仍然是冷的，去掉它就够了，
    不值得冒重说一遍的风险。
    """

    body = _norm(text)
    if not body:
        # 空回复不是「冷」，是生成失败；这里不管，交给上层。
        return Coldness(cold=True)
    hits = _hits(body, max_chars=max_chars)
    fatal = [label for is_fatal, label in hits if is_fatal]
    mild = [label for is_fatal, label in hits if not is_fatal]
    labels = tuple(label for _, label in hits)
    if fatal or len(mild) >= 2:
        reason = "；".join(fatal or mild)
        return Coldness(cold=False, reason=reason, hits=labels)
    return Coldness(cold=True, hits=labels)


def describe_coldness(result: Coldness) -> str:
    if result.cold:
        return "冷下来了" if result.hits else "没有可判的特征"
    return "没冷下来：" + result.reason
