"""脏文本防御：把「系统文本」挡在记忆库和提示词之外。

## 为什么需要这个文件

用户反馈过一件事：加了这个插件之后，「运行久了会把日志发送出去」。查遍了
插件自己的日志出口（``cg_log.log`` 只写 ``ctx.logger``）、记忆库（768 条
``gap_items`` 扫时间戳前缀 / XML 标签 / ``jsonl`` / ``Traceback`` 全 0 命中）
都没复现出来。

但顺着「有没有别的插件往消息文本里塞东西」查下去，找到了一条**确实存在**的
链路：真机 ``mai_messages`` 里 bot 收到的消息中，有几条 ``processed_plain_text``
长这样——

    [热梗：67]
    怎么用/怎么接：……

那不是 QQ 原消息，是 ``meme-insight-plugin`` 往 ``processed_plain_text``
后面追加的提示文本。而本插件抽取时 ``cg_extract._text_of()`` **优先读
``processed_plain_text``** —— 于是：

    别的插件追加文本 → transcript → 抽成 creative / self 类记忆
    → 注入 extra_prompt → 模型照着复述 → 发到聊天里

这正好解释了「运行久了才出现」：要先攒够这类记忆。也解释了为什么是「日志
样」的东西——被追加的本来就是给人看的说明文字。

## 做法

不猜具体是哪个插件，只做通用防御：认「长得像系统输出」的片段。

- 堆栈与日志：``Traceback (most recent call last)``、``File "...", line N``、
  ``INFO |`` / ``DEBUG |`` / ``WARNING |`` / ``ERROR |``、``.jsonl`` / ``.log`` 路径
- JSON / 配置片段：整段以大括号或方括号开头并含 ``":``
- 插件注入标记：``[companion-``、``</plugin``、``<plugin_``、``[热梗：`` 这类
  方括号 + 冒号的短标签（**这是真机实际出现过的那一种**）

**只认高置信度的形态。** 宁可漏掉一条，也不要把正常聊天判成脏数据——误杀
的代价是「她忘了你说过的话」，那比多记一条脏文本严重得多。
"""

from __future__ import annotations

import re
from typing import Any

#: 高置信度的「这段是系统输出」特征。用 ``re`` 而不是子串匹配，
#: 因为要区分「提到的词」和「日志的形态」。
_SYSTEM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Traceback \(most recent call last\)"), "python 堆栈"),
    (re.compile(r'^\s*File ".+?", line \d+', re.MULTILINE), "python 堆栈帧"),
    (re.compile(r"^\s*(?:INFO|DEBUG|WARNING|ERROR|CRITICAL)\s*\|", re.MULTILINE), "日志行"),
    (re.compile(r"\.(?:jsonl|log|log\.\d+)\b"), "日志文件路径"),
    (re.compile(r"<plugin_proactive_task\b"), "主动任务标签"),
    (re.compile(r"</?plugin_[a-z_]+\b"), "插件 XML 标签"),
    (re.compile(r"\[companion-[a-z]+\]"), "本插件自己的块标记"),
)

#: 「方括号 + 冒号」的短标签，例如 ``[热梗：67]``、``[插件: xxx]``。
#:
#: 单独一条规则，因为它最容易误伤（正常的「[图片]」「[表情]」不能算）。
#: 约束：方括号里 2~12 个字符，必须含中文冒号或英文冒号，且后面还有内容。
_TAGGED_BLOCK = re.compile(r"^\s*\[[^\]\n]{2,12}[:：][^\]\n]{0,12}\]")

#: 整段是 JSON 对象/数组的开头。
_JSON_START = re.compile(r"^\s*[{\[]\s*[\"{]")

#: 判定为「最多扫前多少个字符」。脏片段通常在开头，长文本中间偶尔出现一个
#: 冒号不该被误杀。
_SCAN_CHARS = 400


def system_text_reason(text: Any) -> str:
    """这段文本为什么像系统输出；不像则返回空串。

    返回的是**人能看懂的原因**，会写进日志和 ``gap_extractions.error``——
    「为什么这条没进记忆」必须能事后归因，否则下次还得重新查一遍。
    """

    raw = str(text or "")
    if not raw.strip():
        return ""
    head = raw[:_SCAN_CHARS]

    for pattern, reason in _SYSTEM_PATTERNS:
        if pattern.search(head):
            return reason

    # 方括号标签：只有出现在**开头**才算。正常聊天里「[图片] 你看这个」
    # 也可能命中，所以额外要求后面还有正文（标签本身不是全部内容）。
    tag = _TAGGED_BLOCK.match(head)
    if tag and len(head.strip()) > tag.end() + 2:
        return "方括号系统标签"

    if _JSON_START.match(head) and len(head.strip()) > 8:
        return "JSON 片段"

    return ""


def looks_like_system_text(text: Any) -> bool:
    """便捷判定。"""

    return bool(system_text_reason(text))


def is_clean(text: Any) -> bool:
    """可读性更好的反向写法，给调用方用。"""

    return not system_text_reason(text)
