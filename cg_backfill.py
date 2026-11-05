"""我，deepseek —— 群历史补抓。

## 为什么需要这个

宿主只在**收到消息的那一刻**把消息写进 ``mai_messages``。她关机的时候既没有
连接器、也没有人写库，所以那段时间的聊天**永久缺失**——这是拿数据证明过的：
真机上 ``online_time`` 的 8 段离线区间内，``mai_messages`` 计数全部为 0；
``mai_messages`` 在 11-03 一整天是 0 条。

问题在于，她关机的时候群里照样在聊。等她开机，她看到的是「上次说话是昨天
23:26」——**中间那几个小时发生了什么，她一无所知**。这不是记性问题，是感知
范围问题。

好在 QQ 端有历史。SnowLuma / NapCat 都暴露 ``get_group_msg_history``，它的
数据源是客户端本地缓存，**与 MaiBot 是否在线无关**。实测四个群能往前翻
9~10 天。

## 三个必须记住的坑（都是真机上撞出来的）

1. **分页锚点是 ``message_id``，不是 ``message_seq``。**
   传不同的 ``message_seq`` 返回结果一模一样——它被完全忽略。
2. **``count`` 上限 200，且会被内部裁剪。** 传 300/500/1000 都只回 200 条。
   唯一可靠的翻页写法是「取上一页第一条的 ``message_id`` 当锚点，
   ``reverse_order=true``，循环到新增条数为 0」。
3. **空数组不等于不可用。** 用户原话：「私聊应该不是拿不到，而是这几天并没有
   私聊。」``get_friend_msg_history`` 返回 ``[]`` 是「没有新消息」，
   不是「这个能力坏了」。调用方必须按「没新消息」处理，绝不能据此关掉功能。

## 消息形态转换

适调配器返回的是 OneBot 事件对象：``sender.user_id`` / ``message[].data.text``。
而插件内部（``cg_extract``）认的是宿主 ``message_utils`` 那套：
``message_info.user_info.user_id`` / ``raw_message[].data`` 是**字符串**。
两者必须转换，否则 ``_text_of`` 会静默返回空串——一条消息都读不出来，
而且不报错。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

#: 每页拉多少条。实测 100 条里通常 99 条是新的（1 条边界重叠），够用且不慢。
PAGE_SIZE = 100

#: 最多翻多少页。40 页 × 100 条 ≈ 4000 条，远超 10 天实际量。
MAX_PAGES = 40

#: 最多往回补多久。SnowLuma 本地缓存上限约 9~10 天，超过这个只会翻到空页。
MAX_AGE_DAYS = 10.0

#: 同一个会话两次补抓之间的最小间隔。补抓要走一次 WebSocket，
#: 每次抽取都补一遍没必要——它补的是「关机期间」的洞，不会天天有新洞。
MIN_INTERVAL_SECONDS = 1800.0

#: 适配器 API 名。短名 + 明确版本，宿主会按 ``plugin_id.api_name`` 或唯一短名解析。
API_GROUP_HISTORY = "adapter.napcat.message.get_group_msg_history"
API_FRIEND_HISTORY = "adapter.napcat.message.get_friend_msg_history"

#: gap_meta 里记「上次补抓时间」的键前缀。
META_PREFIX = "backfill_at:"


@dataclass(slots=True)
class BackfillResult:
    """一次补抓的结果。

    Attributes:
        messages: 转换好的插件内部消息字典，按时间升序。
        pages: 实际翻了多少页。
        reached_start: 是否翻到了缓存尽头（多页无新增即视为尽头）。
        error: 出错原因；``""`` 表示没出错。**空列表不是错误。**
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    reached_start: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _unwrap_history(payload: Any) -> tuple[list[dict[str, Any]], str]:
    """从适配器的各种返回形态里捞出消息数组。

    见过的形态：
    - 宿主剥壳后直接给的适配器原始响应 ``{"status":"ok","retcode":0,"data":{"messages":[...]}}``
    - 某些版本再包一层 ``{"success":True,"result":{...}}``
    - 直接给数组

    Returns:
        ``(messages, error)``。``error`` 非空时 ``messages`` 恒为空。
    """

    node = payload
    # 最多剥三层壳，够覆盖所有已知形态。
    for _ in range(3):
        if not isinstance(node, dict):
            break
        if "messages" in node or "data" in node:
            break
        for key in ("result", "data", "payload"):
            inner = node.get(key)
            if isinstance(inner, (dict, list)):
                node = inner
                break
        else:
            break

    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)], ""

    if not isinstance(node, dict):
        return [], "返回值不是对象也不是数组"

    status = str(node.get("status") or "").strip().lower()
    retcode = node.get("retcode")
    if status and status != "ok":
        return [], f"适配器返回 status={status}"
    if isinstance(retcode, (int, float)) and int(retcode) != 0:
        wording = str(node.get("wording") or node.get("message") or "").strip()
        return [], f"适配器返回 retcode={int(retcode)} {wording}".strip()

    data = node.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], ""
    if isinstance(data, dict):
        inner = data.get("messages")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)], ""
        return [], ""
    inner = node.get("messages")
    if isinstance(inner, list):
        return [item for item in inner if isinstance(item, dict)], ""
    return [], ""


def _text_of_raw(raw: dict[str, Any]) -> str:
    """OneBot 消息段的文本：``data`` 是对象，插件内部要的是字符串。"""

    message = raw.get("message")
    if not isinstance(message, list):
        # 某些实现给的是 CQ 码字符串，直接当文本用。
        text = raw.get("raw_message")
        return text.strip() if isinstance(text, str) else ""
    chunks: list[str] = []
    for component in message:
        if not isinstance(component, dict):
            continue
        if component.get("type") != "text":
            continue
        data = component.get("data")
        if isinstance(data, str):
            if data.strip():
                chunks.append(data.strip())
        elif isinstance(data, dict):
            value = data.get("text")
            if isinstance(value, str) and value.strip():
                chunks.append(value.strip())
    return " ".join(chunks).strip()


def _normalize_components(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """把 OneBot 消息段转成宿主那套 ``{"type":..., "data": "字符串"}``。"""

    message = raw.get("message")
    if not isinstance(message, list):
        return []
    out: list[dict[str, Any]] = []
    for component in message:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("type") or "").strip()
        data = component.get("data")
        if kind == "text":
            text = ""
            if isinstance(data, str):
                text = data
            elif isinstance(data, dict):
                text = str(data.get("text") or "")
            out.append({"type": "text", "data": text})
            continue
        # 非文本段只留摘要，避免把 base64 之类的东西塞进提示词。
        summary = kind
        if isinstance(data, dict):
            for key in ("file", "summary", "url", "qq"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    summary = f"{kind}({value.strip()[:40]})"
                    break
        out.append({"type": kind, "data": summary})
    return out


def to_plugin_message(
    raw: dict[str, Any],
    *,
    platform: str,
    session_id: str,
    group_id: str = "",
    group_name: str = "",
) -> dict[str, Any]:
    """把 OneBot 事件对象转成插件内部使用的消息字典。

    转换的核心是 ``message_info.user_info`` / ``raw_message`` 两处——``cg_extract``
    的 ``_text_of`` 和 ``_speaker_of`` 只认这个形态。

    Args:
        raw: 适调配器返回的单条消息。
        platform: 平台标识，如 ``"qq"``。
        session_id: 目标会话 ID（补抓是「塞进某个会话」，不是「还原原始会话」）。
        group_id: 群号；私聊留空。
        group_name: 群名，用于人物表与提示词。

    Returns:
        插件内部消息字典；无法识别时返回 ``{}``。
    """

    if not isinstance(raw, dict):
        return {}

    message_id = raw.get("message_id") or raw.get("real_id") or raw.get("id")
    sender = raw.get("sender")
    if not isinstance(sender, dict):
        sender = {}

    user_id = sender.get("user_id") or raw.get("user_id") or ""
    nickname = sender.get("nickname") or raw.get("user_nickname") or ""
    cardname = sender.get("card") or raw.get("user_cardname") or ""

    text = _text_of_raw(raw)
    components = _normalize_components(raw)
    stamp = _as_float(raw.get("time") or raw.get("timestamp"), 0.0)

    group_info: dict[str, Any] | None = None
    if group_id:
        group_info = {"group_id": str(group_id), "group_name": str(group_name or "")}

    message: dict[str, Any] = {
        "message_id": str(message_id) if message_id not in (None, "") else "",
        "timestamp": str(stamp) if stamp > 0 else "",
        "platform": str(platform or "qq"),
        "message_info": {
            "user_info": {
                "user_id": str(user_id),
                "user_nickname": str(nickname),
                "user_cardname": str(cardname),
            },
            "group_info": group_info,
        },
        "raw_message": components,
        "is_mentioned": bool(raw.get("is_mentioned") or False),
        "is_at": bool(raw.get("is_at") or False),
        "is_emoji": False,
        "is_picture": any(
            isinstance(item, dict) and item.get("type") == "image" for item in components
        ),
        "is_command": False,
        "is_notify": False,
        "session_id": str(session_id),
        "processed_plain_text": text,
        # 补抓来的消息标记来源，方便排查「这条到底是宿主给的还是从 QQ 捞的」。
        "_backfilled": True,
    }
    if not message["message_id"]:
        # 没有 message_id 就没法去重，宁可丢掉。
        return {}
    return message


def _stamp_of(message: dict[str, Any]) -> float:
    return _as_float(message.get("timestamp"), 0.0)


def merge_messages(
    primary: Sequence[dict[str, Any]],
    extra: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 ``message_id`` 去重合并，按时间升序返回。

    ``primary`` 优先（宿主库里的更可信，它带 ``reply_to`` 等宿主加工过的字段）。
    """

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for collection in (primary, extra):
        for message in collection:
            if not isinstance(message, dict):
                continue
            key = str(message.get("message_id") or "")
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            merged.append(message)
    merged.sort(key=_stamp_of)
    return merged


async def _call_history_api(
    ctx: Any,
    api_name: str,
    params: dict[str, Any],
) -> Any:
    """调用适配器 API，兼容 ``ctx.api.call`` 与裸 ``call_capability`` 两种写法。"""

    api_proxy = getattr(ctx, "api", None)
    if api_proxy is not None and hasattr(api_proxy, "call"):
        return await api_proxy.call(api_name, version="1", **params)
    return await ctx.call_capability("api.call", api_name=api_name, version="1", args=params)


async def fetch_history(
    ctx: Any,
    *,
    api_name: str,
    params_factory: Any,
    since_ts: float = 0.0,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    logger: Any = None,
) -> BackfillResult:
    """通用的历史翻页逻辑，群聊/私聊共用。

    Args:
        api_name: 适配器 API 全名。
        params_factory: ``(anchor, count) -> dict``，由调用方决定锚点字段名。
        since_ts: 只要比这个时间新的消息（unix 秒）。``0`` 表示不限。
    """

    result = BackfillResult()
    anchor: Any = None
    collected: dict[str, dict[str, Any]] = {}
    size = max(1, min(int(page_size or PAGE_SIZE), 200))

    for page in range(1, max(1, int(max_pages)) + 1):
        params = params_factory(anchor, size)
        try:
            payload = await _call_history_api(ctx, api_name, params)
        except Exception as exc:  # noqa: BLE001 - 适配器不在/超时，都当补抓失败
            result.error = f"调用 {api_name} 失败: {exc!r}"
            result.pages = page - 1
            break

        rows, error = _unwrap_history(payload)
        result.pages = page
        if error:
            result.error = error
            break
        if not rows:
            # 空数组 = 到底了（或没有新消息）。**不是错误。**
            result.reached_start = True
            break

        added = 0
        oldest_stamp = 0.0
        for raw in rows:
            message = to_plugin_message(
                raw,
                platform=str(params.get("platform") or "qq"),
                session_id=str(params.get("_session_id") or ""),
                group_id=str(params.get("_group_id") or ""),
                group_name=str(params.get("_group_name") or ""),
            )
            if not message:
                continue
            key = message["message_id"]
            stamp = _stamp_of(message)
            if stamp > 0:
                oldest_stamp = stamp if oldest_stamp <= 0 else min(oldest_stamp, stamp)
            if key in collected:
                continue
            collected[key] = message
            added += 1

        if added == 0:
            result.reached_start = True
            break
        if since_ts > 0 and oldest_stamp > 0 and oldest_stamp <= since_ts:
            # 已经翻到目标时间之前，不用再往前。
            break

        next_anchor = rows[0].get("message_id") or rows[0].get("real_id")
        if next_anchor in (None, "", anchor):
            result.reached_start = True
            break
        anchor = next_anchor

    messages = list(collected.values())
    if since_ts > 0:
        messages = [m for m in messages if _stamp_of(m) >= since_ts]
    messages.sort(key=_stamp_of)
    result.messages = messages
    if result.error and logger is not None:
        try:
            logger.warning(f"[companion-gap] 补抓历史失败（{api_name}）: {result.error}")
        except Exception:  # noqa: BLE001
            pass
    return result


async def fetch_group_history(
    ctx: Any,
    *,
    group_id: str,
    session_id: str,
    group_name: str = "",
    since_ts: float = 0.0,
    platform: str = "qq",
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    logger: Any = None,
) -> BackfillResult:
    """补抓群历史。

    锚点用上一页第一条的 ``message_id``，``reverse_order=True`` 表示
    「这个锚点以及更早的」。这两个参数是实测出来的，见模块 docstring 的坑 1/2。
    """

    payload_summary: dict[str, Any] = {}

    def params_factory(anchor: Any, count: int) -> dict[str, Any]:
        payload_summary.update(
            {
                "platform": platform,
                "_session_id": session_id,
                "_group_id": group_id,
                "_group_name": group_name,
            }
        )
        params: dict[str, Any] = {
            "group_id": str(group_id),
            "count": count,
            "reverse_order": True,
            "platform": platform,
            "_session_id": session_id,
            "_group_id": group_id,
            "_group_name": group_name,
        }
        if anchor not in (None, ""):
            params["message_id"] = anchor
        return params

    return await fetch_history(
        ctx,
        api_name=API_GROUP_HISTORY,
        params_factory=params_factory,
        since_ts=since_ts,
        page_size=page_size,
        max_pages=max_pages,
        logger=logger,
    )


async def fetch_friend_history(
    ctx: Any,
    *,
    user_id: str,
    session_id: str,
    since_ts: float = 0.0,
    platform: str = "qq",
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    logger: Any = None,
) -> BackfillResult:
    """补抓私聊历史。

    **返回空列表是正常结果**，代表「这几天没有私聊」——用户明确纠正过这一点。
    调用方不要因为空就认为功能不可用。
    """

    def params_factory(anchor: Any, count: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "user_id": str(user_id),
            "count": count,
            "reverse_order": True,
            "platform": platform,
            "_session_id": session_id,
        }
        if anchor not in (None, ""):
            params["message_id"] = anchor
        return params

    return await fetch_history(
        ctx,
        api_name=API_FRIEND_HISTORY,
        params_factory=params_factory,
        since_ts=since_ts,
        page_size=page_size,
        max_pages=max_pages,
        logger=logger,
    )


def should_backfill(
    *,
    last_backfill_ts: float,
    now: float | None = None,
    min_interval: float = MIN_INTERVAL_SECONDS,
) -> bool:
    """按时间节流：同一个会话隔一段时间才补一次。"""

    current = time.time() if now is None else float(now)
    if last_backfill_ts <= 0:
        return True
    return current - last_backfill_ts >= max(0.0, float(min_interval))


def default_since(
    *,
    last_extract_ts: float,
    now: float | None = None,
    max_age_days: float = MAX_AGE_DAYS,
) -> float:
    """算补抓的时间下界。

    有上次抽取时间就从那里开始（补的正好是她没看见的那段）；没有就从
    ``max_age_days`` 天前开始（适配器缓存上限，再往前也翻不到）。
    """

    current = time.time() if now is None else float(now)
    floor = current - max(0.0, float(max_age_days)) * 86400.0
    if last_extract_ts <= 0:
        return floor
    return max(floor, float(last_extract_ts))


def describe_result(result: BackfillResult) -> str:
    """调试用一行摘要。"""

    if not result.ok:
        return f"补抓失败：{result.error}（翻了 {result.pages} 页）"
    span = ""
    if result.messages:
        first = _stamp_of(result.messages[0])
        last = _stamp_of(result.messages[-1])
        if first > 0 and last > 0:
            span = (
                f"，{datetime.fromtimestamp(first):%m-%d %H:%M}"
                f"..{datetime.fromtimestamp(last):%m-%d %H:%M}"
            )
    tail = "，已到缓存尽头" if result.reached_start else ""
    return f"补抓 {len(result.messages)} 条（{result.pages} 页{span}）{tail}"
