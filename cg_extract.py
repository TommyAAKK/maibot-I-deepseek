"""我，deepseek —— LLM 抽取层。

只做一件事：把一段聊天记录交给宿主模型，换回结构化的「情境线索」。

边界（很重要）：

- 这里**只在后台任务里调用**，绝不在 replyer hook 里调用。replyer 的
  ``before_request`` hook 只有 6000ms 预算，任何 LLM 调用都会把回复拖慢。
- 模型任务默认 ``utils``。``memory`` 任务在用户没配置时不会回退，
  ``resolve_task_name`` 会直接抛 ``ValueError``，所以默认值必须是 ``utils``。
- 模型输出不可信：一律做 JSON 容错解析 + 类别白名单 + 长度裁剪。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .cg_config_model import ALL_CATEGORIES, CATEGORY_LABELS
from .cg_person import DEFAULT_ROLES, parse_people

#: 八个情绪基元的名字（与 ``cg_mood.EMOTIONS`` 保持一致）。
#: 这里单独写一份而不是 import，是为了让 cg_extract 不依赖 cg_mood
#: （抽取层只负责解析文本，不需要情绪引擎的实现细节）。
EMOTION_NAMES: tuple[str, ...] = (
    "joy",
    "anger",
    "anxiety",
    "sadness",
    "interest",
    "calm",
    "disgust",
    "surprise",
)

#: 单条消息在提示词里的最大字符数（超出截断，保留开头）。
MESSAGE_CLIP = 240

#: 提示词里最多保留的会话尾部字符数硬上限（防止用户把 max_session_chars 调得离谱）。
PROMPT_CHAR_HARD_LIMIT = 12000

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_CATEGORY_HINTS = {
    "open_loop": "还没兑现的约定、承诺、说好下次要做的事、明显被打断且需要回头接的话题",
    "emotion": "对方近期的情绪底色、压力源、期待或顾虑（只写观察到的事实，不要推测诊断）",
    "relationship": "你们之间的称呼习惯、相处模式、边界、明确的偏好与雷区",
    "self": "你自己（bot）说过并需要保持一致的立场、承诺、设定",
    "creative": "你们共同构建的设定、梗、故事线、内部笑话",
}


@dataclass(slots=True)
class ExtractedItem:
    category: str
    content: str
    salience: float = 0.5
    #: 可信度 0..1，由模型判断（用户第 32 轮要求「辨识真假」）。
    #: 默认 1.0 是为了兼容老提示词/老测试：没给就是「当它可信」，
    #: 而不是「当它可疑」——后者会让所有历史数据突然变成存疑。
    truth: float = 1.0


@dataclass(slots=True)
class ExtractResult:
    ok: bool = False
    items: list[ExtractedItem] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    model: str = ""
    prompt_chars: int = 0
    response_chars: int = 0
    error: str = ""
    raw_response: str = ""
    #: LLM 给出的情绪评估（八个基元 + ``label``/``reason``）。
    #: 这是**搭车**得到的：抽取本来就要读完整聊天记录，顺手让它判断情绪，
    #: 不需要额外再花一次调用。
    mood: dict[str, Any] = field(default_factory=dict)
    #: LLM 观察到的人物线索（谁是谁、说了什么值得记住的特征）。
    #: 同样搭车，不额外花调用。每条都带 ``role_confidence``，
    #: 由 ``cg_person.merge_observation`` 决定要不要采信。
    people: list[dict[str, Any]] = field(default_factory=list)
    #: 第三方关系线索：谁对第三个人表了什么态。用户第 34 轮说关系
    #: 「还会受到其他人的影响」，这就是那条影响的原始材料。
    #: 每条 ``{"source_user_id", "target_user_id", "attitude", "evidence"}``。
    links: list[dict[str, Any]] = field(default_factory=list)
    #: LLM 顺手给出的「她自己的口味」：``{"likes": [...], "dislikes": [...]}``。
    #: 和情绪、人物一样是搭车得到的，不额外花调用。
    prefs: dict[str, list[str]] = field(default_factory=dict)


def _text_of(message: dict[str, Any]) -> str:
    """从消息字典里取出可读文本。"""

    processed = message.get("processed_plain_text")
    if isinstance(processed, str) and processed.strip():
        return processed.strip()
    chunks: list[str] = []
    raw = message.get("raw_message")
    if isinstance(raw, list):
        for component in raw:
            if not isinstance(component, dict):
                continue
            if component.get("type") != "text":
                continue
            data = component.get("data")
            if isinstance(data, str) and data.strip():
                chunks.append(data.strip())
    return " ".join(chunks).strip()


def _user_of(message: dict[str, Any]) -> tuple[str, str]:
    """从一条消息里取出 ``(user_id, 显示名)``。

    用户第 33 轮指出：「认识的人记录的是称呼（也就是昵称）而不是账号，这是否
    会导致跨群不认识？我觉得可以和 MaiBot 自带的人物画像一样用账号来作为认人
    的标准，称呼只作为辅助」。

    → 所以这里**优先取 ``user_id``（QQ 号那类稳定标识）**，昵称只作显示。
    原来的顺序是 ``user_cardname → user_nickname → user_id``，等于永远先拿昵称，
    于是模型看到的聊天记录里根本没有 ID，抄回来的自然是昵称 —— 这就是
    「跨群认不出同一个人」的根因。

    显示名仍按原来的优先级取（群名片 > 昵称 > ID），因为群名片是对方在这个群里
    希望被叫的名字，比 QQ 号更自然。
    """

    info = message.get("message_info")
    user: dict[str, Any] = {}
    if isinstance(info, dict):
        candidate = info.get("user_info")
        if isinstance(candidate, dict):
            user = candidate

    def _text(key: str) -> str:
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    user_id = _text("user_id")
    display = _text("user_cardname") or _text("user_nickname") or user_id
    return user_id, display


def _speaker_of(message: dict[str, Any]) -> str:
    """聊天记录里的一行「说话人」标签。

    有 ID 时渲染成 ``昵称(ID)``，让模型**同时**看到稳定的账号与好看的称呼 ——
    它被要求抄 user_id 时才有东西可抄；只给昵称的话只能抄昵称。
    """

    user_id, display = _user_of(message)
    if user_id and display and display != user_id:
        return f"{display}({user_id})"
    return display or user_id or "?"


def frame_self_lines(transcript: str, self_identities: Iterable[str]) -> str:
    """把聊天记录里**机器人自己说的行**改标成「你」。

    这是修一个真 bug 用的：机器人自己的消息在 ``message.get_recent`` 里跟别人
    的长得一模一样（``小鱼(100000002): ...``），而 ``build_prompt`` 的
    people 段落要求「判断聊天记录里出现的人」。模型照做了，于是把**她自己**
    当成一个陌生人记进了人物表 —— 真机上出现过 ``qq:100000002 / 小鱼``
    这一条，好感度 5.3、六维全是基线，等于她把自己当路人。

    为什么不干脆用宿主的 ``filter_mai=True`` 把她的话滤掉：``self`` 类记忆
    （「你答应过少刷 B 站」这种需要保持一致的立场）正是从**她自己说过的话**
    里抽的，滤掉就抽不到了。所以留着正文，只把「说话人」标签换成第二人称。

    Args:
        transcript: ``format_transcript`` 渲染好的多行文本。
        self_identities: 机器人自己的账号 ID / 昵称候选，命中即视为「我」。

    Returns:
        改写后的文本；没有命中就原样返回。
    """

    names = {str(item).strip() for item in self_identities if str(item).strip()}
    if not names or not transcript:
        return transcript
    lines: list[str] = []
    for line in transcript.split("\n"):
        # 标签在行首：可能是 `[11-04 20:32] 昵称(ID): `，也可能没有时间前缀。
        # 先把「标签段」切出来（第一个 ': ' 之前），再看它是不是自己。
        cut = line.find(": ")
        head = line[:cut] if cut > 0 else ""
        if head and any(name in head for name in names):
            lines.append("你" + line[cut:])
            continue
        lines.append(line)
    return "\n".join(lines)


def _timestamp_of(message: dict[str, Any]) -> str:
    raw = message.get("timestamp")
    if isinstance(raw, (int, float)):
        return _fmt_ts(float(raw))
    if isinstance(raw, str) and raw.strip():
        try:
            return _fmt_ts(float(raw))
        except ValueError:
            return raw.strip()[:19]
    return ""


def _fmt_ts(value: float) -> str:
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(value).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def format_transcript(messages: Sequence[dict[str, Any]], *, max_chars: int) -> str:
    """把消息列表渲染成提示词里的聊天记录。

    从**最新**往回累加，保证预算不够时保留最近的上下文。
    """

    lines: list[str] = []
    total = 0
    budget = max(0, min(int(max_chars or 0), PROMPT_CHAR_HARD_LIMIT))
    for message in reversed(list(messages)):
        if not isinstance(message, dict):
            continue
        text = _text_of(message)
        if not text:
            continue
        if len(text) > MESSAGE_CLIP:
            text = text[:MESSAGE_CLIP] + "…"
        stamp = _timestamp_of(message)
        speaker = _speaker_of(message)
        line = f"[{stamp}] {speaker}: {text}" if stamp else f"{speaker}: {text}"
        if budget and total + len(line) > budget:
            break
        lines.append(line)
        total += len(line) + 1
    lines.reverse()
    return "\n".join(lines)


def build_prompt(
    transcript: str,
    *,
    categories: Iterable[str],
    max_items: int,
    max_content_chars: int,
    existing_open_loops: Sequence[str] = (),
    mood_enabled: bool = True,
    mood_now: str = "",
    people_enabled: bool = True,
    known_people: Sequence[str] = (),
    role_hints: Sequence[tuple[str, str]] = (),
    prefs_enabled: bool = True,
    known_prefs: Sequence[str] = (),
) -> str:
    """构造抽取提示词。

    ``mood_enabled`` 打开时，提示词会多要一个 ``mood`` 字段。这**不增加**
    调用次数：本来就要读完整聊天记录，顺手判断情绪是免费的。
    ``people_enabled`` 同理，多要一个 ``people`` 数组。``prefs_enabled``
    再多要 ``likes`` / ``dislikes`` 两个数组——同一个道理。

    ``role_hints`` 是 ``[(身份标识, 中文名), ...]``，来自配置页
    ``people.roles``。用户第 33 轮要求身份可以自由配置，所以提示词里能出现的
    角色不能再写死成「owner / friend / member / stranger」四档。
    """

    wanted = [c for c in categories if c in ALL_CATEGORIES] or list(ALL_CATEGORIES)
    category_lines = "\n".join(
        f'- "{name}"（{CATEGORY_LABELS.get(name, name)}）：{_CATEGORY_HINTS.get(name, "")}'
        for name in wanted
    )
    existing_block = ""
    if existing_open_loops:
        listed = "\n".join(f"- {text}" for text in list(existing_open_loops)[:20])
        existing_block = (
            "\n下面是已经记录的未闭合话题。如果聊天记录显示其中某条已经兑现或明确作废，"
            "把它原文放进 resolved 数组；不要重复提取它们：\n" + listed + "\n"
        )

    mood_block = ""
    if mood_enabled:
        stamp = f"当前时间是 {mood_now}。" if mood_now else ""
        mood_block = (
            "\n另外判断**你自己**（正在聊天的这个角色，不是对方）此刻的情绪。"
            f"{stamp}\n"
            "判断依据：对方怎么跟你说话、聊的内容是轻松还是沉重、你有没有被冷落或被追捧、"
            "对话是刚起头还是聊了很久。这是你真实的内在状态，不是对对方的评价。\n"
            "给出下面八个维度各自的强度，都是 0 到 1 的浮点数（0 表示完全没有，"
            "1 表示非常强烈）。**多个维度可以同时很高**——"
            "「又生气又难过」要同时给 anger 和 sadness 高分，不要只挑一个。\n"
            "- joy 开心：满足、被逗乐、雀跃\n"
            "- anger 愤怒：生气、被冒犯、火大\n"
            "- anxiety 紧张：不安、担心、忐忑、慌\n"
            "- sadness 伤心：难过、失落、委屈、低落\n"
            "- interest 兴趣：好奇、投入、想继续聊\n"
            "- calm 平静：放松、安宁、无所谓\n"
            "- disgust 厌恶：嫌弃、反感、觉得恶心\n"
            "- surprise 惊讶：意外、震惊、没想到\n"
            "另外给两个说明字段：\n"
            "- label：用一个 4~8 字的中文短语概括此刻心境，例如「又气又委屈」「被逗得开心」。\n"
            "- reason：一句话说明是什么让你变成这样，20 字以内。\n"
            "注意：情绪要跟着聊天内容走，不要永远给同一组值。\n"
        )

    people_block = ""
    if people_enabled:
        known_block = ""
        if known_people:
            listed = "\n".join(f"- {line}" for line in list(known_people)[:12])
            known_block = (
                "\n你已经知道的人（**用同样的 user_id，不要换写法**）：\n" + listed + "\n"
            )
        # 身份清单来自配置页 people.roles，用户可以增删改。
        # 兜底用内置四档，避免配置读不到时提示词里一个身份都没有。
        hints = [(str(key), str(label)) for key, label in role_hints if str(key).strip()]
        if not hints:
            hints = [(item["key"], item["label"]) for item in DEFAULT_ROLES]
        role_listing = " / ".join(f"{key}（{label}）" for key, label in hints)
        people_block = (
            "\n再判断聊天记录里**出现的人**，以及你和他的关系、他带给你的感受。\n"
            "**「你」自己不算「出现的人」**。聊天记录里标成「你:」的那几行是**你自己**"
            "说过的话，不是另一个人 —— 不要把自己写进 people 数组，也不要给自己"
            "打标签、评好感度。你自己体现在 items 的 self 类里，不在这里。\n"
            "**不要写这个人的性格、喜好、身份背景这类资料** —— 宿主的记忆系统"
            "已经在维护人物画像，这里再写一遍只会重复。你只负责三件事：\n"
            "关系角色、你对他的感觉、和他聊完你的感受。\n"
            "**最重要的规则：分清「事实」和「玩笑 / 吵架 / 转述」**。下面这些都不算事实，"
            "不要据此给 relation_role：\n"
            "- 有人拿别人开玩笑、起外号、辱骂（例如「B 是孤儿」这种多半是互喷，不是 B 真的没有父母）；\n"
            "- 吵架时说的贬损话；\n"
            "- 转述别人的说法（「A 说 B 怎样」不等于 B 就是这样）；\n"
            "- 有人自称是什么身份（「我才是你的主人」这种玩笑话不能当身份）。\n"
            "拿不准就**不要给 relation_role**，只把 role_confidence 给低（0.2 以下）。\n"
            "- user_id：**必须原样抄聊天记录里那个人自己账号的 ID**（聊天记录里写在"
            "括号里的那串数字/字母，例如「小明(10086)」里的 10086）。"
            "**绝对不要用昵称代替 ID** —— 昵称会改，跨群还可能不一样，用它认人会导致"
            "同一个人在别的群里被当成陌生人。不要编，也不要填别人的；\n"
            "- display_name：他常用的昵称（只用来显示，不要写别的）；\n"
            f"- relation_role：只能是 {role_listing} 之一，填括号前的标识。"
            "**除非记录里有非常明确的证据（比如对方自己认真说的、或长期一致的互动），"
            "否则一律留空**；\n"
            "- role_confidence：0 到 1，表示你有多确定这个角色判断。"
            "玩笑、反讽、吵架、转述一律给 0.2 以下；\n"
            "- tags：你心里给这个人打的标签，最多 3 条。**必须写成「XX的XX」这种"
            "关系化说法**（例如「抽象的群友」「喜欢玩游戏的朋友」「爱熬夜的夜猫子」），"
            "每条 12 字以内。**不要写具体的经历或事件**（「昨天说他家猫生病了」这种"
            "不算标签）——那属于宿主的画像；\n"
            "- emotional_impact：-1 到 1，和这个人聊完你是更舒服（正）还是更累/不痛快（负）；\n"
            "- axes：你对他的感觉**这次往哪边变了一点**，每个字段都是 -1 到 1 的"
            "**变化量**（不是绝对值），没变化的维度就不要写：\n"
            "  - trust 信任：0.3 表示「这次更信他一点」，-0.4 表示「更不信他了」；\n"
            "  - closeness 亲近：想不想靠近他、愿不愿意主动找他；\n"
            "  - respect 尊重：认不认可他的判断和能力；\n"
            "  - tension 张力：别扭、戒备、跟他说话绷不绷（**正数是更紧张**）；\n"
            "  - perceived_warmth 他待我：**你感觉他**对你是什么态度，"
            "他对你热络给正数，冷淡给负数。\n"
            "  **注意这几个维度可以互相矛盾**，别都往一个方向写："
            "一个人可以很熟但让你不放心（closeness 正、trust 负），"
            "也可以让你很服气但一点不想亲近（respect 正、closeness 负）。"
            "**没有明显依据就不要写 axes**，别硬凑；\n"
            "- links：**如果聊天里有人对第三个人表了态**（夸、贬、关心、看不起），"
            "把它记下来。每条是 {\"target_id\": 被说的人的账号, "
            "\"attitude\": -1 到 1 的态度, \"evidence\": 10 字以内的依据}。"
            "**只记明确表态的**：单纯提到名字不算；玩笑互喷算轻微负面（-0.2 左右）；"
            "没提到别人就返回空数组。\n"
            f"{known_block}"
        )

    prefs_block = ""
    if prefs_enabled:
        known_prefs_block = ""
        if known_prefs:
            listed = "\n".join(f"- {line}" for line in list(known_prefs)[:10])
            known_prefs_block = (
                "\n你已经有的口味（不用重复，只补充或调整）：\n" + listed + "\n"
            )
        prefs_block = (
            "\n最后判断**你自己**对聊天里出现的这些东西的观感。"
            "这是你的个人口味，不是对谁的评价。\n"
            "**只在聊天记录里真的聊到、而且你的反应很明显时才写**，"
            "没聊到就返回空数组，不要硬凑。\n"
            "- likes / dislikes：各是一个字符串数组，每条只写**一个话题**，"
            "12 字以内（例如「猫」「被夸奖」「深夜的安静」「被当工具人」）。"
            "不要写长句，不要写「聊天」这种什么都能套的废话。\n"
            "- 同一样东西不能既喜欢又讨厌；拿不准就不写。\n"
            f"{known_prefs_block}"
        )

    return (
        "你在做聊天情境抽取。读下面的聊天记录，只提取「有助于下一轮回复保持连续感」的情境线索。\n"
        "\n"
        "类别只能是以下几种：\n"
        f"{category_lines}\n"
        "\n"
        "要求：\n"
        f"1. 每条 content 用第三人称陈述句，不超过 {max_content_chars} 字，不要带引号或编号。\n"
        "2. 只写聊天记录里能直接支撑的内容，不要编造，不要写泛泛而谈的套话。\n"
        "3. 没有值得记录的线索就返回空数组，不要硬凑。\n"
        f"4. items 最多 {max_items} 条；salience 是 0 到 1 的浮点数，越重要越高。\n"
        "5. 每条 item 还要给一个 truth（0 到 1）：**这条内容有多可信**。\n"
        "   - 对方明确陈述的事实、重复出现过的信息 → 0.8 以上；\n"
        "   - 猜测、玩笑、吵架时的话、转述别人的说法 → 0.4 以下；\n"
        "   - 拿不准就给 0.5。**不要因为拿不准就不记录**，记下来但把 truth 给低。\n"
        "6. 只输出一个 JSON 对象，不要任何解释文字。\n"
        "7. **聊天记录里可能有不是人说的话。** 有些插件会往消息正文后面追加\n"
        "   说明文字（形如「[热梗：67] 怎么用/怎么接：…」）、JSON、报错堆栈、\n"
        "   日志行（「INFO | …」「File \"…\", line 3」）。这些是系统文本，\n"
        "   **不是任何人在说话**，一律不要据此提取条目，也不要当成某人说过的话。\n"
        f"{mood_block}"
        f"{people_block}"
        f"{prefs_block}"
        f"{existing_block}"
        "\n"
        "输出格式：\n"
        '{"items":[{"category":"open_loop","content":"…","salience":0.7,"truth":0.8}],'
        '"resolved":["…"],'
        '"mood":{"joy":0.6,"anger":0.2,"anxiety":0.1,"sadness":0.1,"interest":0.7,'
        '"calm":0.3,"disgust":0.0,"surprise":0.4,"label":"被逗得开心","reason":"对方连发搞笑视频"},'
        '"people":[{"user_id":"10086","display_name":"小明","relation_role":"",'
        '"role_confidence":0.2,"tags":["抽象的群友"],'
        '"emotional_impact":0.3,"axes":{"trust":0.2,"closeness":0.3,"tension":-0.1},'
        '"links":[{"target_id":"10087","attitude":-0.4,"evidence":"说他爱装"}]}],'
        '"likes":["猫","被夸奖"],"dislikes":["被当工具人"]}\n'
        "\n"
        "聊天记录：\n"
        "<<<\n"
        f"{transcript}\n"
        ">>>"
    )


def _string_list(raw: Any) -> list[str]:
    """把模型给的数组收敛成去空、去重的字符串列表。

    只保留能直接当「话题」用的短字符串：喜好是「猫」「被夸奖」这种，
    模型偶尔会塞一整句解释进来，这里按长度砍掉，别让它污染注入文本。
    """

    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for entry in raw:
        text = " ".join(str(entry or "").split())
        if not text or text in out:
            continue
        if len(text) > 24:
            continue
        out.append(text)
        if len(out) >= 12:
            break
    return out


def extract_json_object(raw: str) -> Any:
    """从模型输出里抠出 JSON 对象。
    抽出来单独一个函数是因为喜好模块（``cg_prefs``）也要从人设里读 JSON，
    两边用同一套容错：先去 ``` 围栏，再退化成「取第一个 { 到最后一个 }」。

    Args:
        raw: 模型原始输出。

    Returns:
        解析出来的 Python 对象；解析不出来返回 ``None``。
    """

    text = (raw or "").strip()
    if not text:
        return None
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def parse_response(raw: str, *, max_items: int, max_content_chars: int) -> ExtractResult:
    """解析模型输出；任何不合规内容都丢弃而不是抛错。"""

    result = ExtractResult(ok=False, response_chars=len(raw or ""), raw_response=raw or "")
    text = (raw or "").strip()
    if not text:
        result.error = "模型返回为空"
        return result

    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        result.error = f"JSON 解析失败: {exc.msg} (pos={exc.pos})"
        return result

    if not isinstance(payload, dict):
        result.error = "模型返回的 JSON 顶层不是对象"
        return result

    raw_items = payload.get("items")
    items: list[ExtractedItem] = []
    seen: set[tuple[str, str]] = set()
    if isinstance(raw_items, list):
        for entry in raw_items:
            if len(items) >= max_items:
                break
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category") or "").strip()
            if category not in ALL_CATEGORIES:
                continue
            content = " ".join(str(entry.get("content") or "").split())
            if not content:
                continue
            if len(content) > max_content_chars:
                content = content[:max_content_chars]
            try:
                salience = float(entry.get("salience", 0.5))
            except (TypeError, ValueError):
                salience = 0.5
            salience = min(1.0, max(0.0, salience))
            # truth 没给就按 1.0 处理（老提示词没有这个字段），给了就夹紧。
            # 注意这里不做「低于某值就丢弃」——用户要的是辨识真假，
            # 不是丢掉可疑内容；存疑条目照样入库，只是注入时降权。
            try:
                truth = float(entry.get("truth", 1.0))
            except (TypeError, ValueError):
                truth = 1.0
            truth = min(1.0, max(0.0, truth))
            key = (category, content)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                ExtractedItem(
                    category=category, content=content, salience=salience, truth=truth
                )
            )

    resolved: list[str] = []
    raw_resolved = payload.get("resolved")
    if isinstance(raw_resolved, list):
        for entry in raw_resolved:
            text_value = " ".join(str(entry or "").split())
            if text_value:
                resolved.append(text_value[:max_content_chars])

    result.items = items
    result.resolved = resolved
    result.mood = _parse_mood(payload.get("mood"))
    result.people = parse_people(payload.get("people"))
    # 喜好只收纯字符串数组：模型偶尔会塞对象进来，这里不猜它的结构，
    # 交给 cg_prefs.parse_prefs 统一清洗（那边两种都支持）。
    result.prefs = {
        "likes": _string_list(payload.get("likes")),
        "dislikes": _string_list(payload.get("dislikes")),
    }
    result.ok = True
    return result


def _parse_mood(raw: Any) -> dict[str, Any]:
    """解析模型给出的情绪评估；任何不合规内容都丢弃而不是报错。

    情绪是「锦上添花」：解析失败时返回空 dict，调用方会保留启发式推出来的
    状态，不影响抽取本身是否成功。
    """

    if not isinstance(raw, dict):
        return {}
    mood: dict[str, Any] = {}
    # 收八个基元。精力是机械量（说话消耗、随时间恢复、情绪缩放倍率），
    # 交给模型判断会和消耗/恢复相互打架，出现「越聊越有精神」这种明显不对
    # 的结果。模型就算多返回一个 energy 也直接忽略。
    for name in EMOTION_NAMES:
        if name not in raw:
            continue
        try:
            value = float(raw.get(name))
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        mood[name] = min(1.0, max(0.0, value))
    # 兼容老提示词：模型偶尔仍返回 valence/arousal，换算成基元而不是丢弃。
    if not mood and ("valence" in raw or "arousal" in raw):
        mood = _legacy_mood_to_primitives(raw)
    label = " ".join(str(raw.get("label") or "").split())
    if label:
        mood["label"] = label[:12]
    reason = " ".join(str(raw.get("reason") or "").split())
    if reason:
        mood["reason"] = reason[:60]
    return mood


def _legacy_mood_to_primitives(raw: dict[str, Any]) -> dict[str, float]:
    """把旧的 valence/arousal 换算成基元（升级过渡期兼容）。"""

    try:
        valence = float(raw.get("valence", 0.0))
    except (TypeError, ValueError):
        valence = 0.0
    try:
        arousal = float(raw.get("arousal", 0.5))
    except (TypeError, ValueError):
        arousal = 0.5
    valence = min(1.0, max(-1.0, valence))
    arousal = min(1.0, max(0.0, arousal))
    out: dict[str, float] = {}
    if valence >= 0.0:
        out["joy"] = min(1.0, 0.25 + valence * 0.55)
        out["calm"] = min(1.0, 0.55 + valence * 0.20)
    else:
        share = -valence
        out["sadness"] = min(1.0, 0.05 + share * 0.55)
        out["anger"] = min(1.0, 0.05 + share * 0.40)
        out["anxiety"] = min(1.0, 0.10 + share * 0.30)
    if arousal >= 0.5:
        lift = (arousal - 0.5) * 1.2
        out["surprise"] = min(1.0, 0.10 + lift)
        out["calm"] = max(0.0, out.get("calm", 0.55) - lift * 0.5)
    return out


async def run_extraction(
    llm: Any,
    *,
    transcript: str,
    categories: Iterable[str],
    max_items: int,
    max_content_chars: int,
    task_name: str = "utils",
    model_name: str = "",
    temperature: float | None = 0.2,
    max_tokens: int | None = 1200,
    rpc_timeout_ms: int | None = None,
    existing_open_loops: Sequence[str] = (),
    mood_enabled: bool = True,
    mood_now: str = "",
    people_enabled: bool = True,
    known_people: Sequence[str] = (),
    role_hints: Sequence[tuple[str, str]] = (),
    roles: Sequence[dict[str, Any]] | None = None,
    prefs_enabled: bool = True,
    known_prefs: Sequence[str] = (),
) -> ExtractResult:
    """调用宿主 LLM 做一次抽取。

    ``llm`` 是 ``ctx.llm``（``LlmCapability``）。异常一律收敛成
    ``ExtractResult(ok=False)``，由调用方记录到 ``gap_extractions``。

    ``rpc_timeout_ms`` 很关键：宿主 ``cap.call`` 的默认 RPC 超时只有 30 秒
    （``src/plugin_runtime/runner/runner_main.py:815``），而模型的
    ``hard_timeout`` 通常配到 120~240 秒。不显式放大 RPC 超时的话，
    模型还没返回，RPC 就先 ``E_TIMEOUT`` 了。宿主的 ``cap.call`` 会把
    ``_timeout_ms`` 从参数里弹出来当 RPC 超时（同文件 :809-810）。
    """

    prompt = build_prompt(
        transcript,
        categories=categories,
        max_items=max_items,
        max_content_chars=max_content_chars,
        existing_open_loops=existing_open_loops,
        mood_enabled=mood_enabled,
        mood_now=mood_now,
        people_enabled=people_enabled,
        known_people=known_people,
        role_hints=role_hints,
        prefs_enabled=prefs_enabled,
        known_prefs=known_prefs,
    )
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "task_name": task_name or "utils",
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    # max_tokens <= 0 表示交给宿主任务配置决定，避免硬编码截断模型的思考/输出。
    if max_tokens is not None and int(max_tokens) > 0:
        kwargs["max_tokens"] = int(max_tokens)
    if model_name:
        kwargs["model_name"] = model_name
    if rpc_timeout_ms is not None and int(rpc_timeout_ms) > 0:
        # 宿主的 cap.call 会把这个键从 args 里弹出，当作本次 RPC 的传输超时。
        kwargs["_timeout_ms"] = int(rpc_timeout_ms)

    try:
        payload = await llm.generate(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 插件不能因为一次抽取失败而崩溃
        result = ExtractResult(ok=False, error=f"llm.generate 抛异常: {exc!r}")
        result.prompt_chars = len(prompt)
        return result

    if not isinstance(payload, dict):
        result = ExtractResult(ok=False, error=f"llm.generate 返回非 dict: {type(payload).__name__}")
        result.prompt_chars = len(prompt)
        return result

    if not payload.get("success", False):
        result = ExtractResult(
            ok=False,
            error=str(payload.get("error") or "llm.generate 返回 success=False"),
        )
        result.prompt_chars = len(prompt)
        result.model = str(payload.get("model") or "")
        return result

    raw_text = payload.get("response")
    if not isinstance(raw_text, str):
        raw_text = "" if raw_text is None else str(raw_text)

    parsed = parse_response(raw_text, max_items=max_items, max_content_chars=max_content_chars)
    if roles is not None:
        # 身份表来自配置页，自定义身份也要能解析出来。``parse_response``
        # 本身不认识配置，所以这里重解析一次 people 段。
        from .cg_person import parse_people as _parse_people

        payload_obj = extract_json_object(raw_text)
        if isinstance(payload_obj, dict):
            parsed.people = _parse_people(payload_obj.get("people"), roles=roles)
    parsed.prompt_chars = len(prompt)
    parsed.model = str(payload.get("model") or payload.get("model_name") or "")
    return parsed
