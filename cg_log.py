"""我，deepseek —— 统一的日志出口与级别闸门。

为什么单独一个模块：``plugin.py`` 和 ``cg_tracker.py`` 都要写日志，但
``cg_tracker`` 是被 ``plugin`` 导入的，反过来导入就会成环。阈值放在这里，
两边共享同一份状态，``debug.log_level`` 才对**所有** [companion-gap] 日志生效。

在此之前 ``cg_tracker.py`` 有一份自己的 ``_log()``，直接 ``getattr(logger, level)``
调用，完全绕过了 ``plugin.py`` 里的 ``_MIN_LOG_LEVEL``——结果是把
``log_level`` 调成 ``info`` 之后，tracker 每 30 秒仍照刷一条 debug
「记忆衰减：N 条降低显著度」，实测 1.5 小时刷了 143 条。
"""

from __future__ import annotations

from typing import Any

#: 级别名 → 数值。与 logging 模块的习惯一致，方便对照。
LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}

#: 默认阈值：info（与 ``config.toml`` 里 ``[debug] log_level`` 的默认值一致）。
DEFAULT_MIN_LEVEL = 20

#: 所有日志统一加这个前缀，方便在宿主日志里 grep。
PREFIX = "[companion-gap] "

_min_level = DEFAULT_MIN_LEVEL


def set_min_level(name: Any) -> int:
    """按配置里的 ``debug.log_level`` 设置阈值；认不出的值回落到 info。

    返回实际生效的数值，方便调用方打一条「当前日志级别」。
    """

    global _min_level
    _min_level = LOG_LEVELS.get(str(name or "info").strip().lower(), DEFAULT_MIN_LEVEL)
    return _min_level


def get_min_level() -> int:
    """当前阈值（数值形式）。"""

    return _min_level


def is_enabled(level: str) -> bool:
    """这条日志在当前阈值下会不会真的写出去。"""

    return LOG_LEVELS.get(level, DEFAULT_MIN_LEVEL) >= _min_level


def log(logger: Any, level: str, message: str) -> None:
    """安全写日志：先过阈值，再容忍 logger 缺方法或抛异常。

    日志失败绝不能影响业务，所以这里不向外抛任何异常。
    """

    if not is_enabled(level):
        return
    try:
        method = getattr(logger, level, None)
        if callable(method):
            method(f"{PREFIX}{message}")
            return
        # 宿主代理可能没有 warning/debug 这类方法，退回 info 至少不丢消息。
        fallback = getattr(logger, "info", None)
        if callable(fallback):
            fallback(f"{PREFIX}{message}")
    except Exception:  # noqa: BLE001 - 日志失败绝不能影响业务
        pass
