"""我，deepseek —— 注入文本组装层。

职责单一：把已落库的条目拼成一小段可追加到 replyer ``extra_prompt`` 的文本。

硬约束：

- **纯字符串拼接，零 await、不访问数据库**。这个模块跑在 replyer 的
  ``before_request`` hook 里，整条链路只有 6000ms 预算。它只从 ``cg_store``
  借一个纯文本打分函数（``recall_score``），不碰任何 SQL。
- 总长度必须收敛到 ``inject.max_chars``，宁可不注入也不能把提示词撑爆。
- 语气要「提示」而不是「命令」：注入内容会被模型当成背景知识，
  写成指令反而会让回复变得僵硬。
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Sequence

from .cg_store import recall_score

#: 类别的展示顺序与标题。顺序即重要性递减。
SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("open_loop", "还没接上的话题"),
    ("emotion", "对方最近的状态"),
    ("relationship", "你们之间的相处线索"),
    ("self", "你自己要保持一致的设定"),
    ("creative", "你们共同的东西"),
)

DEFAULT_LEAD_IN = "（以下是你私下记得的线索，不要直接复述，只在自然的时候体现）"

DEFAULT_CROSS_HEADER = "<companion-cross-session>"
DEFAULT_CROSS_FOOTER = "</companion-cross-session>"
DEFAULT_CROSS_LEAD_IN = (
    "（下面这些是你**在别的聊天里**记得的事，不是当前这个聊天里发生的。"
    "只有当话题真的接得上时才自然带一句，否则当作不知道。）"
)

#: 私聊记忆流入群聊时追加的那句。用户要求「开启后由模型自主决定要不要在群里
#: 聊起」—— 所以这里不是命令它说，也不是命令它不说，而是把决定权交给模型，
#: 并提醒它私聊内容更私密。
PRIVATE_TO_GROUP_NOTE = (
    "（标着「私聊」的条目来自一对一聊天，内容更私密：是否在群里提起由你判断，"
    "拿不准就不提。）"
)

#: 可信度低的条目不删，但在注入文本里标一句，让模型别把它当成板上钉钉的事实。
#: 用户第 32 轮要求「辨识真假」——真假的正确用法是「拿它当参考时打个折」，
#: 不是「直接把可疑的扔掉」（那样模型连「我不太确定」都说不出来）。
DOUBTED_SUFFIX = "（这条你自己也不太确定）"

#: 触发「想起」时用的块标记，与记忆块、跨流块都分开。
DEFAULT_RECALL_HEADER = "<companion-recalled>"
DEFAULT_RECALL_FOOTER = "</companion-recalled>"
DEFAULT_RECALL_LEAD_IN = (
    "（下面这些事你本来已经想不起来了，是对方刚才的话让你重新想了起来。"
    "只在真的接得上时提一句，别一口气全倒出来。）"
)


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


#: 会被「泛化」掉的称呼词。只处理这几个最容易被记忆带进提示词的。
DEFERENCE_WORDS: tuple[str, ...] = ("主人", "主人大人", "老公", "达令", "爸爸")

#: 泛化后用的替换词。
#:
#: 为什么是「对方」而不是「你」：记忆条目是**第三人称记录**（「小鱼称呼
#: 阿汤 为主人」）。换成「你」会变成「小鱼称呼 阿汤 为你」——
#: 这不是消除暗示，而是**加强**暗示：等于直接告诉模型「她管你叫主人」。
#: 「对方」保持第三人称，既不断句也不指向当前说话人。
DEFERENCE_SUBSTITUTE = "对方"


def soften_deference(text: str, *, keep: bool) -> str:
    """把记忆里的「主人」换掉，除非当前说话人就是主人。

    ## 为什么需要这个函数

    插件本身**不注入称呼**（``people.inject_role_label`` 默认关），但记忆里
    会存着真实发生过的对话：「小鱼称呼阿汤为主人」「坚持自己是主人
    群友A的小女仆」。这些是群里真实的玩笑互动，不该当脏数据删掉 ——
    可它们每轮被注入，就等于每轮都在提醒模型「你有主人，叫主人」。

    真机上的后果：她对**所有人**都开始叫主人。

    ## 取舍

    用户选定的方案是「只允许对 owner 叫」。所以：

    - 当前说话人 ``relation_role == "owner"`` → ``keep=True``，原样保留
    - 其他人 → 把称呼词换成一个中性说法

    为什么不用「对方」：念起来别扭（「小鱼称呼对方为主人」还留着「为主人」
    这个动作）。改成「你」或去掉更自然 —— 这里用「你」，因为注入文本本身就
    是第二人称写给模型看的。

    Args:
        text: 记忆条目正文。
        keep: 为真时原样返回（说话人就是主人）。

    Returns:
        处理后的文本。
    """

    if keep or not text:
        return text
    out = text
    # 长的先替换，避免「主人大人」被「主人」拆成「对方大人」。
    for word in sorted(DEFERENCE_WORDS, key=len, reverse=True):
        if word in out:
            out = out.replace(word, DEFERENCE_SUBSTITUTE)
    return " ".join(out.split())


def deference_allowed(person: Mapping[str, Any] | None, *, enabled: bool) -> bool:
    """当前这个人能不能被叫主人。

    只有 ``enabled`` 打开、且这个人的 ``relation_role`` 确实是 ``owner``
    时才允许。身份是手动设的（``role_source == "manual"``）也一样算 ——
    用户亲手标的 owner 当然可以叫。
    """

    if not enabled:
        # 闸门关掉了，退回旧行为：不限制谁可以被叫主人。
        return True
    if not person:
        # 连人物记录都没有，当然不能叫。
        return False
    role = str(person.get("relation_role") or "").strip().lower()
    return role == "owner"


#: 轮换用的默认参数。
#:
#: 为什么需要轮换：只按显著度排序时，高分条目的分数不会自己掉下来（衰减是
#: 按天的，而且只在显著度真降时才落库），于是它每轮都排第一。README 第 8 节
#: 第 7 条实测：124 条不同记忆里 93.5% 至少被注入 2 轮，相邻两轮完全相同占
#: 54.2%，最长一条连刷 32 轮。模型每轮看到同一批背景，回复自然越来越像模板。
DEFAULT_ROTATION_WEIGHT = 0.35
DEFAULT_COOLDOWN_HOURS = 2.0
#: 相关性加分的默认强度。
#:
#: 为什么不是「过滤」：大多数轮次的对话是「在吗」「哈哈」「晚安」，跟任何记忆
#: 都不相关。按相关性过滤会让注入块整个变空，Bot 又退回「你不提我就什么都不
#: 记得」——那正是这个插件要解决的问题。所以相关性只做**加权**，永远不淘汰。
DEFAULT_RELEVANCE_WEIGHT = 0.6


def relevance_bonus(
    row: Mapping[str, Any],
    *,
    message: str,
    weight: float = DEFAULT_RELEVANCE_WEIGHT,
) -> float:
    """算一条记忆与「对方这一轮说的话」的相关度加成。

    这是轮换机制的必要补丁，不是锦上添花：轮换会惩罚「刚说过」，于是当对方
    正好接着聊那件事时，最该出现的那条反而被按在冷却里。相关性把这种情况
    拉回来。

    用 ``recall_score`` 而不是对称的 ``similarity``：当前消息通常很长（模型写的
    ``reply_reason`` 是一整段），记忆是短的，对称 Jaccard 的并集会被长句里大量
    无关二元组撑大，分数永远低得没用。

    返回值进的是**乘法**位置（见 :func:`rotation_score`），所以这里返回的是一个
    约 1.0 的倍率，而不是分数本身：

    - 完全无关 → 1.0，不打折也不加分（**这一点很重要**：不相关不该被惩罚）
    - 高度相关 → 最高 ``1 + weight``，足以把一条正在冷却的条目顶回前面

    Args:
        row: 记忆行。
        message: 当前这一轮对方说的话；空串表示拿不到，直接返回 1.0。
        weight: 加分强度，0 表示关闭相关性。

    Returns:
        乘法倍率，范围 ``[1.0, 1.0 + weight]``。
    """

    if weight <= 0 or not message:
        return 1.0
    content = str(row.get("content") or "")
    if not content:
        return 1.0
    try:
        score = recall_score(message, content)
    except Exception:  # noqa: BLE001 - 打分失败不能拖垮注入
        return 1.0
    score = max(0.0, min(1.0, float(score)))
    return 1.0 + max(0.0, float(weight)) * score


def rotation_score(
    row: Mapping[str, Any],
    *,
    now: float,
    streak_weight: float = DEFAULT_ROTATION_WEIGHT,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
    message: str = "",
    relevance_weight: float = DEFAULT_RELEVANCE_WEIGHT,
) -> float:
    """算一条记忆这一轮的「竞争分」，用于替代裸显著度排序。

    做法是把显著度乘上一个「刚说过」的折价系数，折价来自两个互相独立的信号：

    - **连击**（``inject_streak``）：连续被注入几轮了。连着说 3 轮的话题，
      第 4 轮该让位给别的了，所以按 ``1/(1+w*streak)`` 递减。
    - **冷却**（``last_injected_ts``）：上一轮刚被注入过。两小时内的条目
      打对折，越接近冷却结束折价越小，出了冷却期就完全不打折。

    两个系数是相乘的：一条记忆既在连击又在冷却里，会被压到很低；而只要它
    安静两小时，冷折扣价就归零，只剩连击那一点，很容易重新排回前面。

    置顶条目**不参与折价**：置顶的语义就是「我就是要它一直在」，让它轮换
    出去等于把置顶功能废掉。

    Args:
        row: ``GapStore.list_active()`` 返回的行。
        now: 当前时间戳。
        streak_weight: 连击的权重，0 表示忽略连击。
        cooldown_hours: 冷却时长（小时），0 表示不冷却。
        message: 对方这一轮说的话，用于算相关性加成；空串表示不加成。
        relevance_weight: 相关性加成强度，0 表示忽略相关性。

    Returns:
        竞争分；越大越该进这一轮。
    """

    try:
        salience = float(row.get("salience") or 0.0)
    except (TypeError, ValueError):
        salience = 0.0
    bonus = relevance_bonus(row, message=message, weight=relevance_weight)
    if row.get("pinned"):
        # 置顶免折价，但相关性仍然生效：对方正在聊这件事时，置顶条目
        # 更该排前面；不聊它时也仍然带着（置顶的语义）。
        return salience * bonus

    penalty = 1.0

    if streak_weight > 0:
        try:
            streak = int(row.get("inject_streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        if streak > 0:
            penalty *= 1.0 / (1.0 + float(streak_weight) * streak)

    if cooldown_hours > 0:
        try:
            last = float(row.get("last_injected_ts") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        window = float(cooldown_hours) * 3600.0
        if last > 0 and window > 0:
            age = now - last
            if age < 0:
                # 时钟回拨或未来时间戳：当作刚注入过，给最重的折价，
                # 否则一条时间戳坏掉的条目会永久霸榜。
                age = 0.0
            if age < window:
                penalty *= 0.5 + 0.5 * (age / window)

    return salience * penalty * bonus


def compose_block(
    items: Iterable[Mapping[str, Any]],
    *,
    header: str = "<companion-state>",
    footer: str = "</companion-state>",
    lead_in: str = DEFAULT_LEAD_IN,
    max_chars: int = 600,
    max_items_per_category: int = 3,
    min_salience: float = 0.35,
    skip_categories: Iterable[str] = (),
    now: float | None = None,
    rotation: bool = True,
    streak_weight: float = DEFAULT_ROTATION_WEIGHT,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
    message: str = "",
    relevance_weight: float = DEFAULT_RELEVANCE_WEIGHT,
    allow_deference: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """把条目拼成注入文本。

    Args:
        items: ``GapStore.list_active()`` 返回的行（含 ``category``/``content``/``salience``）。
        skip_categories: 需要跳过的类别（由避让层根据宿主配置给出）。
        now: 排序用的当前时间；``None`` 表示取系统时间。轮换折算按它算「冷却」。
        rotation: 是否启用轮换折算。关掉就退回「纯显著度排序」——只用于
            对比排查，正常配置不该关。
        streak_weight: 连击折价权重。
        cooldown_hours: 冷却时长（小时）。
        allow_deference: 当前说话人能不能被叫「主人」。``False`` 时把条目里的
            「主人」等称呼泛化掉（见 :func:`soften_deference`）。只有这个人是
            ``owner`` 时才该传 ``True``。
        message: 对方这一轮说的话，用于相关性加分。**只加分不淘汰**——不相关
            的记忆照常参与竞争，只是不会因为「刚好被提到」而插到前面。
        relevance_weight: 相关性加分强度，0 表示忽略相关性。

    Returns:
        ``(text, used_items)``；``text`` 为空字符串表示本次不注入。
    """

    if max_chars <= 0:
        return "", []

    skipped = {str(name) for name in skip_categories}
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in items:
        category = str(row.get("category") or "")
        if category in skipped:
            continue
        try:
            salience = float(row.get("salience") or 0.0)
        except (TypeError, ValueError):
            salience = 0.0
        if salience < min_salience:
            continue
        content = _clean(row.get("content"))
        if not content:
            continue
        grouped.setdefault(category, []).append(row)

    if not grouped:
        return "", []

    current = time.time() if now is None else float(now)

    head = _clean(header)
    tail = _clean(footer)
    intro = _clean(lead_in)
    # 预留结构开销：头部 + 尾部 + 引导句 + 若干换行
    reserved = len(head) + len(tail) + len(intro) + 8
    budget = max(0, max_chars - reserved)
    if budget <= 0:
        return "", []

    lines: list[str] = []
    used: list[dict[str, Any]] = []
    consumed = 0
    for category, title in SECTION_ORDER:
        rows = grouped.get(category)
        if not rows:
            continue
        if rotation:
            # 轮换：按「这一轮的竞争分」排，而不是裸显著度。
            # 分数相同的用显著度和新鲜度兜底，保证顺序稳定可复现。
            ordered = sorted(
                rows,
                key=lambda row: (
                    -rotation_score(
                        row,
                        now=current,
                        streak_weight=streak_weight,
                        cooldown_hours=cooldown_hours,
                        message=message,
                        relevance_weight=relevance_weight,
                    ),
                    -float(row.get("salience") or 0.0),
                    -float(row.get("last_seen_ts") or 0.0),
                ),
            )
        else:
            ordered = sorted(
                rows,
                key=lambda row: (
                    -float(row.get("salience") or 0.0) * relevance_bonus(row, message=message, weight=relevance_weight),
                    -float(row.get("last_seen_ts") or 0.0),
                ),
            )
        # 先给标题留出预算：标题 + 一个换行。放不下就整节跳过，
        # 避免出现只有标题的空段。
        header_line = f"【{title}】"
        header_cost = len(header_line) + 1
        if consumed + header_cost > budget:
            continue

        picked: list[str] = []
        picked_rows: list[dict[str, Any]] = []
        spent = 0
        for row in ordered[: max(1, int(max_items_per_category))]:
            content = _clean(row.get("content"))
            # 称呼闸门：只有 owner 能被叫「主人」，其他人一律泛化。
            # 记忆里存的是真实发生过的对话（「称呼 阿汤 为主人」），
            # 不处理的话每轮都在提醒模型「你有主人，叫主人」。
            content = soften_deference(content, keep=allow_deference)
            line = f"- {content}"
            # 存疑条目加一句提示。只影响这一行的长度，不影响是否收录 ——
            # 收录与否由显著度决定，可信度只影响「怎么用」。
            try:
                truth = float(row.get("truth_score") if row.get("truth_score") is not None else 1.0)
            except (TypeError, ValueError):
                truth = 1.0
            if truth < 0.5:
                line += DOUBTED_SUFFIX
            # 注意：这里只累加「还没计入 consumed」的行文本。
            # 早期版本在下面又拿 consumed + len(整节) 比了一次预算，
            # 把行文本重复计算，导致后续本该放得下的节被整节丢弃
            # （实测 max_chars=600 时浪费 46% 预算，self/creative 长期缺席）。
            if consumed + header_cost + spent + len(line) + 1 > budget:
                break
            picked.append(line)
            picked_rows.append(row)
            spent += len(line) + 1
        if not picked:
            continue

        consumed += header_cost + spent
        used.extend(dict(row) for row in picked_rows)
        lines.append(f"{header_line}\n" + "\n".join(picked))

    if not lines:
        return "", []

    body = "\n".join(lines)
    text = f"{head}\n{intro}\n{body}\n{tail}"
    if len(text) > max_chars:
        # 理论上不会走到这里，兜底截断并补回尾部标记。
        keep = max(0, max_chars - len(tail) - 1)
        text = text[:keep] + "\n" + tail
    return text, used


def merge_extra_prompt(existing: str, block: str) -> str:
    """把注入块追加到已有 ``extra_prompt`` 后面。

    ``extra_prompt`` 是**追加**语义：宿主会把它整体包进
    ``【额外回复要求】`` 块，所以这里必须保留原值。
    """

    original = existing if isinstance(existing, str) else ""
    addition = block if isinstance(block, str) else ""
    if not addition:
        return original
    if not original.strip():
        return addition
    return original.rstrip() + "\n\n" + addition


def compose_cross_session_block(
    items: Iterable[Mapping[str, Any]],
    *,
    labels: Mapping[str, str] | None = None,
    header: str = DEFAULT_CROSS_HEADER,
    footer: str = DEFAULT_CROSS_FOOTER,
    lead_in: str = DEFAULT_CROSS_LEAD_IN,
    max_chars: int = 300,
    max_items: int = 3,
    private_note: bool = False,
    allow_deference: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """把**别的聊天流**里的记忆拼成一段独立文本。

    为什么独立成块而不是混进 ``companion-state``：那是「当前这个会话的记忆」，
    混进去模型分不清哪句话是这个群里说的。独立成块并标出来源（「在「某某群」
    里说过：…」），模型才知道这些是别处听来的，也才知道要不要提起。

    Args:
        items: ``GapStore.list_other_sessions()`` 返回的行，需带
            ``source_chat_type``（group/private）与可选 ``source_group_name``。
        labels: 会话 ID → 人话名字，用于来源缺名字时兜底。
        private_note: 是否追加「私聊内容更私密，是否提起由你判断」那句。
            只有本会话是群聊、且确实混进了私聊条目时才该是 True。
        allow_deference: 当前说话人能不能被叫「主人」。别处聊天的记忆一样会
            带出「主人」二字，所以这一层也要过同一道闸门。

    Returns:
        ``(text, used_items)``；空字符串表示本次不注入。
    """

    if max_chars <= 0 or max_items <= 0:
        return "", []

    label_map = dict(labels or {})
    cleaned: list[tuple[Mapping[str, Any], str, str]] = []
    for row in items:
        content = _clean(row.get("content"))
        if not content:
            continue
        source_sid = str(row.get("session_id") or "")
        name = str(row.get("source_group_name") or "").strip()
        if not name:
            name = str(label_map.get(source_sid) or "").strip()
        if not name:
            name = source_sid[:8] or "别处"
        chat_type = str(row.get("source_chat_type") or "").strip()
        cleaned.append((row, name, chat_type))

    if not cleaned:
        return "", []

    head = _clean(header)
    tail = _clean(footer)
    intro = _clean(lead_in)
    note = _clean(PRIVATE_TO_GROUP_NOTE) if private_note else ""
    reserved = len(head) + len(tail) + len(intro) + len(note) + 8
    budget = max(0, max_chars - reserved)
    if budget <= 0:
        return "", []

    lines: list[str] = []
    used: list[dict[str, Any]] = []
    consumed = 0
    for row, name, chat_type in cleaned[: max(1, int(max_items))]:
        content = _clean(row.get("content"))
        # 称呼闸门：别的流里说的话同样可能带「主人」，不能只拦当前流。
        content = soften_deference(content, keep=allow_deference)
        # 私聊来源额外标一下，否则模型分不清「某某群」和「和某人的私聊」。
        if chat_type == "private":
            line = f"- （私聊「{name}」里提过：{content}）"
        else:
            line = f"- （在「{name}」里说过：{content}）"
        if consumed + len(line) + 1 > budget:
            break
        lines.append(line)
        used.append(dict(row))
        consumed += len(line) + 1

    if not lines:
        return "", []

    body = "\n".join(lines)
    text = f"{head}\n{intro}\n{body}\n{tail}"
    if note:
        text = f"{text}\n{note}"
    if len(text) > max_chars:
        keep = max(0, max_chars - len(tail) - 1)
        text = text[:keep] + "\n" + tail
    return text, used


def compose_recalled_block(
    items: Iterable[Mapping[str, Any]],
    *,
    header: str = DEFAULT_RECALL_HEADER,
    footer: str = DEFAULT_RECALL_FOOTER,
    lead_in: str = DEFAULT_RECALL_LEAD_IN,
    max_chars: int = 240,
    max_items: int = 2,
    allow_deference: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """把「刚才被对方一句话勾起来」的遗忘记忆拼成一段独立文本。

    为什么要单独一段而不是混进 ``companion-state``：这些事的性质完全不同 ——
    记忆块是「我一直记得的」，这段是「我本来忘了、刚才才想起来」。混在一起
    模型会当成普通记忆平淡带过，就体现不出「忽然想起来」这个感觉。

    Args:
        items: ``GapStore`` 里 ``status='forgotten'`` 的行。
        max_chars: 整段字数上限。
        max_items: 最多唤醒几条。
        allow_deference: 当前说话人能不能被叫「主人」。想起来的旧事一样会带
            称呼，所以这一层也要过闸门。

    Returns:
        ``(text, used_items)``；空字符串表示本次没有想起任何事。
    """

    if max_chars <= 0 or max_items <= 0:
        return "", []

    cleaned: list[tuple[Mapping[str, Any], str]] = []
    for row in items:
        content = _clean(row.get("content"))
        if content:
            cleaned.append((row, content))
    if not cleaned:
        return "", []

    head = _clean(header)
    tail = _clean(footer)
    intro = _clean(lead_in)
    reserved = len(head) + len(tail) + len(intro) + 8
    budget = max(0, max_chars - reserved)
    if budget <= 0:
        return "", []

    lines: list[str] = []
    used: list[dict[str, Any]] = []
    consumed = 0
    for row, content in cleaned[: max(1, int(max_items))]:
        # 称呼闸门：想起来的旧事同样可能带「主人」。
        content = soften_deference(content, keep=allow_deference)
        line = f"- {content}"
        if consumed + len(line) + 1 > budget:
            break
        lines.append(line)
        used.append(dict(row))
        consumed += len(line) + 1

    if not lines:
        return "", []

    body = "\n".join(lines)
    text = f"{head}\n{intro}\n{body}\n{tail}"
    if len(text) > max_chars:
        keep = max(0, max_chars - len(tail) - 1)
        text = text[:keep] + "\n" + tail
    return text, used
