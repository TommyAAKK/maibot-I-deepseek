"""我，deepseek —— 在场（缺席判据）。

## 这个模块解决什么问题

原来的缺席感只算一个数：``now - last_seen``。看着挺合理，但它有个致命的
错误前提——**假设她一直醒着**。

真机上就是这么翻车的：用户每天 00:00 关机、11:00 开机。某天上午开机后，
BOT 看到「私人聊天 73 小时没说话」，于是第一句话是「你怎么这么久没来」。
可其中 33.8 小时是她自己不在——**不在的是她，不是对方**。

所以这里把「多久没说话」拆成两段：

- ``offline``：这段时间里**她自己不在**（宿主 ``online_time`` 表说的）；
- ``awake``：这段时间里**她醒着、但对方没来**。

真正该触发缺席感的是第二个。第一个该说「我不在」，而不是「你冷落我」。

## 四种判定

===========  ==========================================
``away``     大半时间她不在 —— 该说「我这几天没开机」
``none``     刚聊过 —— 不提
``cold``     她醒着等了很久 —— **这才是真的被冷落**
``pending``  这段时间里有她没看见的消息 —— 先去补抓
===========  ==========================================

## 为什么读宿主库而不是自己存

她什么时候开机是宿主的事（``online_time`` 表一直在写），插件重复记一遍
没有意义，还会因为插件重装而丢历史。直接读现成的，代价只是一次
``database.query``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

#: 相邻两段在线时间的合并容差。
#:
#: 宿主心跳会把上一段的 ``end_timestamp`` 往后推一分钟，所以正常情况下
#: 相邻两段必然重叠。但进程重启、时钟抖动都可能留下几秒空隙，这里给 3 分钟
#: 余量——**太严会把一整段连续的在线切成几十段，离线时长就会被算爆**。
MERGE_SLACK = timedelta(seconds=180)

#: 判「此刻她在线」的余量。最近一段的结束时间离现在不到这么久，就算还醒着。
ONLINE_SLACK = timedelta(seconds=180)

#: 离线占比超过这个值，就不算「被冷落」。
OFFLINE_DOMINANT = 0.5

#: 与 :data:`cg_absence.DEFAULT_MIN_HOURS` 对齐。低于这个时长一律不提。
DEFAULT_MIN_HOURS = 6.0

#: 判「这段是离线」的最短时长（秒）。
#:
#: 宿主的 ``online_time`` 是分段记录的，相邻两段之间本来就会有几秒钟到几分钟
#: 的空档（进程重启、切会话、心跳间隔）。那不是「她不在」。真实缺席是小时级，
#: 所以 10 分钟的线足够干净，而且不会把一堆碎片当成洞。
MIN_OFFLINE_SECONDS = 600.0


@dataclass(slots=True)
class Presence:
    """一段「上次说话到现在」的时间解剖。

    Attributes:
        gap_seconds: 总间隔（``now - last_seen``），和旧实现算的是同一个数。
        offline_seconds: 其中她**不在**的那部分。
        awake_seconds: 其中她**醒着、但对方没来**的那部分。
        online_now: 此刻她是否在线。
        level: 判定档位，见模块 docstring。
    """

    gap_seconds: float = 0.0
    offline_seconds: float = 0.0
    awake_seconds: float = 0.0
    online_now: bool = False
    level: str = "none"

    @property
    def gap_hours(self) -> float:
        return self.gap_seconds / 3600.0

    @property
    def offline_hours(self) -> float:
        return self.offline_seconds / 3600.0

    @property
    def awake_hours(self) -> float:
        return self.awake_seconds / 3600.0

    @property
    def awake_ratio(self) -> float:
        """醒着的时间占总间隔的比例。``gap`` 为 0 时返回 0。"""

        if self.gap_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, self.awake_seconds / self.gap_seconds))


#: 时间戳解析：宿主的 ``_to_dict`` 会把 ``datetime`` 转成 ISO 字符串，
#: 但也不能排除某些版本给的是数字时间戳或 ``datetime`` 对象本身。
def _to_datetime(value: Any, *, tz: timezone | None = None) -> datetime | None:
    """把宿主给的任意时间表示统一成 **aware** ``datetime``。

    宿主的 ``online_time`` 列是 ``datetime``，经 ``_to_dict`` 之后变成
    ISO 字符串（``2026-11-05T11:33:12``，**没有时区**）。裸 datetime 当
    本地时间处理——因为宿主写的就是 ``datetime.now()``，也就是本地时间。
    """

    zone = tz or datetime.now().astimezone().tzinfo or timezone.utc
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=zone)
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        return datetime.fromtimestamp(number, tz=zone)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # 纯数字字符串：可能是 unix 时间戳。
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            if number > 0:
                return datetime.fromtimestamp(number, tz=zone)
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            # 退一步：只取前 19 个字符（``YYYY-MM-DD HH:MM:SS``）。
            try:
                parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)
    return None


def merge_online_windows(
    rows: Iterable[Any],
    *,
    slack: timedelta = MERGE_SLACK,
) -> list[tuple[datetime, datetime]]:
    """把 ``online_time`` 的原始行合并成互不重叠的在线区间。

    Args:
        rows: 每行可以是 dict（含 ``start_timestamp`` / ``end_timestamp``），
            也可以是 ``(start, end)`` 二元组。
        slack: 相邻区间间隔小于它就合并（见 :data:`MERGE_SLACK`）。

    Returns:
        按起点升序排列、两两间隔大于 ``slack`` 的 ``(start, end)`` 列表。
        脏行（缺字段、起点晚于终点）被静默丢弃——宁可少算在线时长，
        也不要因为一行脏数据算出一个负的离线时间。
    """

    spans: list[tuple[datetime, datetime]] = []
    for row in rows:
        if isinstance(row, dict):
            start = _to_datetime(row.get("start_timestamp") or row.get("timestamp"))
            end = _to_datetime(row.get("end_timestamp") or row.get("timestamp"))
        elif isinstance(row, (tuple, list)) and len(row) >= 2:
            start = _to_datetime(row[0])
            end = _to_datetime(row[1])
        else:
            continue
        if start is None or end is None:
            continue
        if end < start:
            # 宿主偶尔会留下 end 早于 start 的残行（进程被强杀）。
            end = start
        spans.append((start, end))

    if not spans:
        return []

    spans.sort(key=lambda item: item[0])
    merged: list[tuple[datetime, datetime]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= slack:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def overlap_seconds(
    windows: Sequence[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> float:
    """``[start, end]`` 落在 ``windows`` 里的总秒数。"""

    if end <= start:
        return 0.0
    total = 0.0
    for win_start, win_end in windows:
        overlap_start = max(win_start, start)
        overlap_end = min(win_end, end)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds()
    return total


def is_online_now(
    windows: Sequence[tuple[datetime, datetime]],
    *,
    now: datetime | None = None,
    slack: timedelta = ONLINE_SLACK,
) -> bool:
    """此刻她是否在线（最近一段还没凉透）。"""

    if not windows:
        return False
    current = now or datetime.now().astimezone()
    _, last_end = windows[-1]
    return current - last_end <= slack


def classify(
    gap_hours: float,
    awake_hours: float,
    offline_hours: float,
    *,
    pending: int = 0,
    min_hours: float = DEFAULT_MIN_HOURS,
    offline_dominant: float = OFFLINE_DOMINANT,
) -> str:
    """把时间解剖折成一个档位（``pending`` / ``away`` / ``none`` / ``cold``）。

    顺序很重要：**待补消息优先**——那段时间里她没看见的东西还没抽取，
    这时候提「好久没见」是拿没读过的消息当感情依据，很假。

    两条门槛都要过才有话可说：

    - ``gap_hours >= min_hours``：总间隔太短就**什么都不提**。她离开两小时
      回来说「我刚才不在」是废话，跟「你三天没来」不是一个量级。
    - ``awake_hours < min_hours``：醒着等的时间够久才算「被冷落」，否则
      只是「她人不在」，见 :func:`classify_hint`。

    所以四档的完整含义：

    ``pending``  那段时间她有没看见的消息，先补洞别谈感情
    ``away``     她大半时间不在（``offline_hours`` 占主导）→ 「我这几天没在」
    ``none``     间隔太短，或者醒着的时间够久但离线没占主导（正常作息）
    ``cold``     她醒着、等了够久，对方一直没来 → 这才该有缺席感
    """

    floor = max(0.0, float(min_hours))
    if pending > 0:
        return "pending"
    if gap_hours < floor:
        # 太短了，什么都没必要说。
        return "none"
    if awake_hours < floor:
        if gap_hours > 0 and offline_hours > gap_hours * max(0.0, float(offline_dominant)):
            return "away"
        return "none"
    return "cold"


def analyse(
    *,
    last_seen_ts: Any,
    windows: Sequence[tuple[datetime, datetime]],
    pending: int = 0,
    now: datetime | None = None,
    min_hours: float = DEFAULT_MIN_HOURS,
    offline_dominant: float = OFFLINE_DOMINANT,
) -> Presence:
    """算出完整的 :class:`Presence`。

    ``last_seen_ts`` 是插件自己记的「上次有人说话」的 unix 时间戳；非法或
    在未来时返回全零的 :class:`Presence`（档位 ``none``，调用方据此不注入）。
    """

    current = now or datetime.now().astimezone()
    try:
        last_ts = float(last_seen_ts)
    except (TypeError, ValueError):
        return Presence(online_now=is_online_now(windows, now=current))

    if last_ts <= 0:
        return Presence(online_now=is_online_now(windows, now=current))

    zone = current.tzinfo or timezone.utc
    last = datetime.fromtimestamp(last_ts, tz=zone)
    if last >= current:
        # 时钟回拨或未来时间戳：当作刚说过。
        return Presence(online_now=is_online_now(windows, now=current))

    gap_seconds = (current - last).total_seconds()
    # ``overlap_seconds`` 算的是「这段时间里她**在线**了多久」，也就是醒着的
    # 那部分；离线是剩下的。这两个概念很容易写反——写反之后「关机一整夜」
    # 会被算成「她在线等了一整夜」，缺席判断就整个翻过来。
    awake_seconds = min(gap_seconds, overlap_seconds(windows, last, current))
    offline_seconds = max(0.0, gap_seconds - awake_seconds)
    level = classify(
        gap_seconds / 3600.0,
        awake_seconds / 3600.0,
        offline_seconds / 3600.0,
        pending=pending,
        min_hours=min_hours,
        offline_dominant=offline_dominant,
    )
    return Presence(
        gap_seconds=gap_seconds,
        offline_seconds=offline_seconds,
        awake_seconds=awake_seconds,
        online_now=is_online_now(windows, now=current),
        level=level,
    )


#: 各档位在**群里**的说法。群里不该出现「你怎么才来」这种一对一的话。
_AWAY_PHRASES: dict[str, str] = {
    "private": "（你这{how}没在——不是对方没来，是你不在。）",
    "group": "（这个群{how}没动静，不过那段时间你也不在。）",
}


def offline_spans(
    windows: Sequence[tuple[datetime, datetime]],
    *,
    start_ts: float,
    end_ts: float,
    min_seconds: float = MIN_OFFLINE_SECONDS,
) -> list[tuple[float, float]]:
    """把「在线区间」翻成「离线区间」，只保留 ``start_ts`` 之后的部分。

    这是给「洞」判定用的：一条消息如果落在这里面，就说明她当时根本不在，
    那条消息她**一条都没看见**。

    三条刻意的保守设计：

    1. **只取第一段在线区间之后的部分。** ``online_time`` 只保留 30 天，读出
       来之前的时间是「不知道」而不是「不在线」。把不知道算成不在线，会让
       每个老会话一启动就报「你错过了一大段」。
    2. ``min_seconds`` 以下的小空档不算离线。宿主的 ``online_time`` 是分段
       记录，相邻两段之间本来就可能有几分钟空档（进程重启、切会话），那不算
       「她不在」。真实缺席是小时级，10 分钟的线足够干净。
    3. 末尾敞口不封口：最后一段在线区间之后到现在，如果它够长，也是离线。
       这正是「关机一整夜」的形状。

    Returns:
        ``(start_ts, end_ts)`` 浮点 unix 秒列表，按起点升序。
    """

    if not windows:
        return []
    floor = max(0.0, float(start_ts))
    ceiling = max(floor, float(end_ts))
    minimum = max(0.0, float(min_seconds))
    zone = windows[0][0].tzinfo or timezone.utc

    spans: list[tuple[float, float]] = []
    cursor = max(floor, windows[0][0].timestamp())
    if cursor > floor:
        # 第一段在线记录之前是「不知道」，不是离线——把游标推到第一段起点，
        # 这样它前面的空档不会变成洞。
        cursor = max(floor, windows[0][0].timestamp())
    for win_start, win_end in windows:
        begin = win_start.timestamp()
        finish = win_end.timestamp()
        if finish <= floor:
            continue
        if begin > cursor and begin - cursor >= minimum:
            spans.append((cursor, min(begin, ceiling)))
        cursor = max(cursor, finish)
        if cursor >= ceiling:
            break
    if ceiling - cursor >= minimum:
        spans.append((cursor, ceiling))
    _ = zone  # 时区只用于解释输入，输出统一是 unix 秒
    return [(max(0.0, s), max(0.0, e)) for s, e in spans if e > s]


def render_away_note(*, hours: float, how: str, chat_type: str = "private") -> str:
    """渲染「我不在」那句。

    和「被冷落」完全相反的措辞——它**不该带任何情绪**，只陈述事实。
    她要意识到「是我没在」，而不是「你怎么这么久没来」。
    """

    if hours <= 0 or not how:
        return ""
    kind = "group" if str(chat_type or "").strip().lower() == "group" else "private"
    template = _AWAY_PHRASES.get(kind) or _AWAY_PHRASES["private"]
    return template.replace("{how}", how)


async def fetch_online_windows(
    ctx: Any,
    *,
    days: float = 30.0,
    limit: int = 4000,
    logger: Any = None,
) -> list[tuple[datetime, datetime]]:
    """从宿主 ``online_time`` 表读出在线区间（已合并）。

    读失败一律返回空列表——**没有在线数据时，离线时长算 0**，于是行为退回
    改造前的 ``gap = now - last_seen``。宁可变回老样子，也不要因为读不到
    就把每一段间隔都说成「你不在」。

    Args:
        ctx: 插件上下文（需要 ``database.query`` 能力）。
        days: 往回读多少天。``online_time`` 每 5 分钟一行，30 天约 8600 行。
        limit: 单次查询上限。
        logger: 可选的日志对象，只有 ``warning`` 会被用到。
    """

    try:
        rows = await ctx.call_capability(
            "database.query",
            model_name="OnlineTime",
            query_type="get",
            filters=None,
            order_by=["-timestamp"],
            limit=max(1, int(limit)),
            single_result=False,
        )
    except Exception as exc:  # noqa: BLE001 - 读不到就当没有在线数据
        if logger is not None:
            try:
                logger.warning(f"[companion-gap] 读取宿主在线时段失败（缺席判据退回旧算法）: {exc!r}")
            except Exception:  # noqa: BLE001
                pass
        return []

    if isinstance(rows, dict):
        raw = rows.get("result")
        rows = raw if isinstance(raw, list) else [rows]
    if not isinstance(rows, list):
        return []

    windows = merge_online_windows([row for row in rows if isinstance(row, dict)])
    if not windows:
        return []

    # 只保留最近 ``days`` 天内、且还没结束的部分。
    current = datetime.now().astimezone()
    cutoff = current - timedelta(days=max(1.0, float(days)))
    trimmed = [
        (max(start, cutoff), min(end, current + MERGE_SLACK))
        for start, end in windows
        if end >= cutoff
    ]
    return [(start, end) for start, end in trimmed if end > start]


def describe_presence(presence: Presence) -> str:
    """一行给人看的摘要，给调试日志用。"""

    return (
        f"间隔 {presence.gap_hours:.1f}h"
        f" / 离线 {presence.offline_hours:.1f}h"
        f" / 醒着 {presence.awake_hours:.1f}h"
        f"（醒着占 {presence.awake_ratio * 100:.0f}%）"
        f" / 档位 {presence.level}"
        f" / 此刻{'在线' if presence.online_now else '不在'}"
    )
