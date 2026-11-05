"""我，deepseek —— 配置模型。

配置结构说明：

- ``[plugin]`` 由宿主保留，必须同时提供 ``enabled`` 与 ``config_version``，
  否则 Runner 只加载配置管理、不激活业务组件。
- 其余每个 ``PluginConfigBase`` 子类对应 WebUI 配置页的一个分区（tab）。
- 所有字段都必须有默认值，否则 ``build_plugin_default_config`` 会直接报错。

关于中文显示（很重要）：

WebUI 配置页显示的字段名与说明**不是**取自 ``Field(description=...)``，而是取自
``json_schema_extra`` 里的 ``label`` 与 ``hint`` 两个键 —— 见宿主
``maibot_sdk/config.py`` 的 ``_build_field_schema``：

    "label": str(json_extra.get("label") or field_name),
    "hint": _optional_str(json_extra.get("hint")),

也就是说只写 ``description`` 的话，页面上会退回显示英文裸键名。所以本文件里
每个字段都同时写了：

- ``description``：给读代码的人看，也会进 schema 的 ``description`` 键；
- ``json_schema_extra=_ui("中文名", "中文说明")``：给配置页看。

``_ui()`` 只是把这件事写得短一点，没有其他魔法。
"""

from __future__ import annotations

from typing import Any

try:  # SDK 正常安装时走这里
    from maibot_sdk import Field, PluginConfigBase
except Exception:  # pragma: no cover - 仅用于脱离 Runner 的本地导入
    from pydantic import BaseModel as PluginConfigBase  # type: ignore[assignment]
    from pydantic import Field  # type: ignore[assignment]

from .cg_person import DEFAULT_INTIMACY_LEVELS, DEFAULT_ROLES


CONFIG_VERSION = "1.1.0"

#: 本插件负责补全的五类情境记忆。顺序即注入时的展示顺序。
ALL_CATEGORIES: tuple[str, ...] = (
    "open_loop",
    "emotion",
    "relationship",
    "self",
    "creative",
)

#: 类别中文名，用于注入文本与管理命令。
CATEGORY_LABELS: dict[str, str] = {
    "open_loop": "未闭合话题",
    "emotion": "对方情绪",
    "relationship": "关系线索",
    "self": "自身连续性",
    "creative": "共创线索",
}


def _ui(label: str, hint: str = "", **extra: Any) -> dict[str, Any]:
    """构造 WebUI 配置页用的字段展示信息。

    Args:
        label: 配置页上显示的字段中文名。
        hint: 字段下方的中文说明；留空则不显示说明行。
        **extra: 其他 ``json_schema_extra`` 键，如 ``placeholder``、``step``。

    Returns:
        Dict[str, Any]: 可直接传给 ``Field(json_schema_extra=...)`` 的字典。
    """

    payload: dict[str, Any] = {"label": label}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


class PluginSectionConfig(PluginConfigBase):
    """宿主保留节。

    Runner 从 ``[plugin]`` 读 ``enabled``（启停开关）与 ``config_version``（配置版本号）；
    缺任一都会判定为配置错误，只加载配置管理、不激活业务组件。
    """

    __ui_label__ = "插件"
    __ui_icon__ = "plug"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件（MaiBot 启停开关）",
        json_schema_extra=_ui("启用插件", "关闭后不采集、不抽取、也不注入"),
    )
    config_version: str = Field(
        default=CONFIG_VERSION,
        description="配置版本，改配置结构时递增",
        json_schema_extra=_ui("配置版本", "改配置结构时递增；必须与 manifest 的版本一致"),
    )


class CaptureConfig(PluginConfigBase):
    """采集：决定哪些聊天流被观察、多久抽一次。"""

    __ui_label__ = "采集"
    __ui_icon__ = "inbox"
    __ui_order__ = 10

    enabled: bool = Field(
        default=True,
        description="是否采集聊天消息用于抽取",
        json_schema_extra=_ui("启用采集", "关闭后不再从聊天流里收集消息"),
    )
    capture_groups: bool = Field(
        default=True,
        description="是否采集群聊",
        json_schema_extra=_ui("采集群聊", "是否观察群聊消息"),
    )
    capture_private: bool = Field(
        default=True,
        description="是否采集私聊",
        json_schema_extra=_ui("采集私聊", "是否观察私聊消息"),
    )
    recent_hours: float = Field(
        default=12.0,
        description="每次抽取回看最近多少小时的消息（太大会拖慢后台任务）",
        json_schema_extra=_ui("回看时长（小时）", "每次抽取向前回看多少小时的消息，太大拖慢后台任务"),
    )
    recent_limit: int = Field(
        default=60,
        description="每次抽取最多取多少条历史消息",
        json_schema_extra=_ui("历史条数上限", "每次抽取最多取多少条历史消息"),
    )
    max_messages_per_extract: int = Field(
        default=30,
        description="实际送进模型的条数上限（取最新的）",
        json_schema_extra=_ui("送入模型条数", "实际交给模型的聊天条数上限，取最新的那些"),
    )
    min_new_messages: int = Field(
        default=8,
        description="攒够多少条新消息触发一次抽取",
        json_schema_extra=_ui("触发条数", "攒够这么多条新消息才触发一次抽取"),
    )
    max_wait_seconds: int = Field(
        default=900,
        description="即使没攒够，最多等多少秒也要抽一次",
        json_schema_extra=_ui("最长等待（秒）", "即使没攒够，最多等这么久也要抽一次"),
    )
    min_interval_seconds: int = Field(
        default=180,
        description="同一个会话两次抽取之间的最小间隔",
        json_schema_extra=_ui("最小间隔（秒）", "同一个会话两次抽取之间至少隔这么久"),
    )
    refresh_tick_seconds: int = Field(
        default=30,
        description="后台任务轮询间隔",
        json_schema_extra=_ui("轮询间隔（秒）", "后台任务多久检查一次有没有到期的会话"),
    )
    max_session_chars: int = Field(
        default=6000,
        description="单次抽取送入模型的聊天记录最大字符数",
        json_schema_extra=_ui("聊天记录字数上限", "单次送入模型的聊天记录最多多少字符"),
    )


class ExtractConfig(PluginConfigBase):
    """抽取：调用哪个模型、抽取哪些类别。"""

    __ui_label__ = "抽取"
    __ui_icon__ = "sparkles"
    __ui_order__ = 20

    enabled: bool = Field(
        default=True,
        description="是否启用 LLM 抽取（关掉则只保留已有记忆的注入）",
        json_schema_extra=_ui("启用抽取", "关闭后只注入已有记忆，不再调用模型抽取"),
    )
    task_name: str = Field(
        default="utils",
        description=(
            "使用的模型任务名。宿主内置 utils 一定可用；"
            "若你在 model_task_config 里配了 memory 任务，建议改成 memory"
        ),
        json_schema_extra=_ui(
            "模型任务名",
            "宿主内置 utils 一定可用；在 model_task_config 里配了 memory 任务的话可以改成 memory",
        ),
    )
    model_name: str = Field(
        default="",
        description="直选具体模型名；留空则按 task_name 路由",
        json_schema_extra=_ui("指定模型名", "留空则按上面的任务名路由"),
    )
    temperature: float = Field(
        default=0.2,
        description="抽取温度，建议低温",
        json_schema_extra=_ui("温度", "抽取建议低温，0.2 左右比较稳"),
    )
    max_tokens: int = Field(
        default=1200,
        description=(
            "单次抽取的最大输出 token。设小了会把模型的思考/输出截断，宿主判定为空回复并抛 "
            "EmptyResponseException；填 0 表示不覆盖，完全沿用宿主 model_task_config 里该任务的设置"
        ),
        json_schema_extra=_ui(
            "最大输出 token",
            "设小了会把输出截断成空回复；填 0 表示不覆盖，沿用宿主任务配置",
        ),
    )
    categories: list[str] = Field(
        default_factory=lambda: list(ALL_CATEGORIES),
        description="要抽取的类别；默认全部五类",
        json_schema_extra=_ui("抽取类别", "要抽取哪几类情境；默认全部五类"),
    )
    max_items_per_extract: int = Field(
        default=12,
        description="单次抽取最多接受多少条结果",
        json_schema_extra=_ui("单次最多条数", "一次抽取最多接受多少条结果"),
    )
    max_content_chars: int = Field(
        default=60,
        description="单条记忆的正文长度上限",
        json_schema_extra=_ui("单条字数上限", "每条记忆的正文最多多少字"),
    )
    rpc_timeout_ms: int = Field(
        default=150000,
        description=(
            "抽取调用的 RPC 超时毫秒数。宿主 cap.call 默认只有 30 秒，"
            "而模型任务 hard_timeout 通常 120~240 秒，太小会在模型返回前就先 E_TIMEOUT"
        ),
        json_schema_extra=_ui(
            "RPC 超时（毫秒）",
            "宿主默认只有 30 秒，而模型任务通常要 120~240 秒，太小会在模型返回前先超时",
        ),
    )
    empty_response_retries: int = Field(
        default=1,
        description="遇到「响应内容为空」时额外重试几次。小模型结构化抽取时偶发，宿主自己也会重试",
        json_schema_extra=_ui("空回复重试次数", "遇到响应内容为空时额外重试几次"),
    )


class InjectConfig(PluginConfigBase):
    """注入：在 replyer 阶段追加什么、追加多少。"""

    __ui_label__ = "注入"
    __ui_icon__ = "syringe"
    __ui_order__ = 30

    enabled: bool = Field(
        default=True,
        description="是否在 replyer 阶段注入情境连续性",
        json_schema_extra=_ui("启用注入", "是否在回复前把情境连续性追加给模型"),
    )
    header: str = Field(
        default="<companion-state>",
        description="注入块起始标记",
        json_schema_extra=_ui("起始标记", "注入块的开头，模型靠它识别边界"),
    )
    footer: str = Field(
        default="</companion-state>",
        description="注入块结束标记",
        json_schema_extra=_ui("结束标记", "注入块的结尾，与起始标记成对出现"),
    )
    lead_in: str = Field(
        default=(
            "【情境连续性参考】以下内容来自你们过往的互动，只在自然合适时融入；"
            "不要逐条复述，也不要声称自己『记得』或『查了记录』。"
        ),
        description="注入块前的引导语",
        json_schema_extra=_ui("引导语", "注入块前的说明，告诉模型该怎么用这些内容"),
    )
    max_chars: int = Field(
        default=600,
        description="注入正文总长度上限",
        json_schema_extra=_ui("注入字数上限", "注入正文的总长度上限"),
    )
    max_items_per_category: int = Field(
        default=3,
        description="每类最多注入几条",
        json_schema_extra=_ui("每类条数上限", "每个类别最多注入几条"),
    )
    min_salience: float = Field(
        default=0.35,
        description="低于该显著度的条目不再注入",
        json_schema_extra=_ui("显著度下限", "低于这个显著度的条目不再注入"),
    )
    record_injections: bool = Field(
        default=False,
        description="是否把每次注入写入 gap_injections 表（便于排查）",
        json_schema_extra=_ui("记录注入日志", "把每次注入写进数据库，排查时很有用"),
    )
    cross_session: bool = Field(
        default=True,
        description="是否让群聊记忆流入其他会话（含私聊）",
        json_schema_extra=_ui(
            "群聊记忆跨流",
            "开启后在一个群里记住的事，在别的群和私聊里也能想起来",
        ),
    )
    cross_session_limit: int = Field(
        default=3,
        ge=0,
        le=20,
        description="跨会话最多补几条记忆",
        json_schema_extra=_ui("跨流条数上限", "从别的会话最多补几条，0 表示不补"),
    )
    cross_session_min_salience: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="跨会话记忆的显著度下限，通常要比同会话更高，避免噪声",
        json_schema_extra=_ui(
            "跨流显著度下限",
            "比同会话的门槛高一些，避免别的群的闲聊冲进来",
        ),
    )
    private_to_group: bool = Field(
        default=False,
        description="私聊内容是否允许流入群聊；开启后由模型自己判断要不要提起",
        json_schema_extra=_ui(
            "私聊记忆可流入群聊",
            "默认关闭（私聊内容更私密）。开启后仍由模型自己决定要不要在群里提起",
        ),
    )
    rotation: bool = Field(
        default=True,
        description="是否让注入的记忆轮换，避免同一批条目连续多轮重复",
        json_schema_extra=_ui(
            "注入轮换",
            "开启后刚说过的记忆会往后排，别的话题有机会进来；关掉就永远按显著度取前几条",
        ),
    )
    rotation_streak_weight: float = Field(
        default=0.35,
        ge=0.0,
        le=2.0,
        description="连续注入的折价权重：连续 N 轮的记忆竞争分乘 1/(1+权重*N)",
        json_schema_extra=_ui(
            "连刷折价强度",
            "越大越积极换话题。0 表示不管连续刷了几轮，只按显著度排",
        ),
    )
    rotation_cooldown_hours: float = Field(
        default=2.0,
        ge=0.0,
        le=48.0,
        description="刚注入过的记忆在这个时长内打折，过了就不再打折",
        json_schema_extra=_ui(
            "冷却时长（小时）",
            "上一轮刚说过的记忆在这么久内会被压后；安静过这个时长就能重新排前面",
        ),
    )
    inject_salience_decay: float = Field(
        default=0.92,
        ge=0.5,
        le=1.0,
        description="每被注入一轮，显著度乘上这个系数（1.0 表示不衰减）",
        json_schema_extra=_ui(
            "注入损耗",
            "反复被说的事会缓慢掉一点显著度，把位置让给新记忆。别调太低，注入不是遗忘",
        ),
    )
    relevance_weight: float = Field(
        default=0.6,
        ge=0.0,
        le=3.0,
        description="当前话题与某条记忆相关时，该条目的竞争分最多乘以 (1 + 这个值)",
        json_schema_extra=_ui(
            "话题相关加权",
            "对方正在聊某件事时，跟它有关的记忆会排前面（只加分、不淘汰）。0 表示不看话题",
        ),
    )


class BoundaryConfig(PluginConfigBase):
    """边界感：她可以不愿意接话。

    两个**独立**机制，别混为一谈：

    1. **状态提示**（``note_*``）：插件**不判定**冒犯，只把「你现在什么心情 /
       跟他关系怎样 / 你讨厌什么」交给 planner，由它自己判断算不算冒犯、
       要不要回。这是更轻的那条路，默认开着。
    2. **临场沉默**（``silence_*``）：插件判定「现在该沉默」，再让 planner
       选「完全不说」还是「冷淡应一声」。判定要求**心情、关系、话题三点
       同时成立**，外加频率上限。默认关。

    ``enabled`` 关掉时两者都不生效。
    """

    __ui_label__ = "边界感"
    __ui_icon__ = "shield"
    __ui_order__ = 32

    enabled: bool = Field(
        default=True,
        description="是否启用边界感（让她有不愿意接话的时候）",
        json_schema_extra=_ui(
            "启用边界感",
            "开启后她会因为心情、关系、话题而偶尔不想接话；关掉则永远热心",
        ),
    )
    note_enabled: bool = Field(
        default=True,
        description="是否把「此刻的心情 / 跟他的关系 / 讨厌什么」告诉 planner",
        json_schema_extra=_ui(
            "把状态告诉 planner",
            "只提供状态信息，由 planner 自己判断这句话算不算冒犯、要不要回。建议保持开启",
        ),
    )
    note_to_planner: bool = Field(
        default=False,
        description="状态提示是否也注入 planner（关闭则只注入 replyer，即只影响怎么说）",
        json_schema_extra=_ui(
            "状态提示进 planner",
            "默认关：只影响回复语气。开启后会影响「要不要回」这个决策，"
            "每轮多几十个 token，建议想清楚再开",
        ),
    )
    silence_enabled: bool = Field(
        default=False,
        description="是否允许插件判定「这一轮该沉默」，并让 planner 选择不回或冷淡回",
        json_schema_extra=_ui(
            "允许真的沉默",
            "默认关。开启后满足条件时她会真的不回，或只回很短一句。建议先只开状态提示观察",
        ),
    )
    silence_level: str = Field(
        default="choose",
        description="沉默的强度：choose（让 planner 二选一）/ taciturn（只肯冷淡回一句）",
        json_schema_extra=_ui(
            "沉默方式",
            "choose = 交给 planner 在「完全不发」和「冷淡应一声」之间自己挑；"
            "taciturn = 不允许完全不发，只能冷淡回一句",
        ),
    )
    silence_min_valence: float = Field(
        default=-0.35,
        ge=-1.0,
        le=1.0,
        description="愉悦度要低到这个值以下（配合愤怒门槛一起用）",
        json_schema_extra=_ui("心情门槛·愉悦度", "低于这个值才算心情差，要和愤怒门槛同时满足"),
    )
    silence_min_anger: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="愤怒强度要高于这个值（配合愉悦度门槛一起用）",
        json_schema_extra=_ui("心情门槛·愤怒", "高于这个值才算真的在生气，要和愉悦度门槛同时满足"),
    )
    silence_min_tension: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="跟这个人的关系张力要高于这个值",
        json_schema_extra=_ui("关系门槛·张力", "相处绷到这个程度才可能不想理他。陌生人张力天然低，不会误伤"),
    )
    silence_min_dislike: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="命中的厌恶口味强度要高于这个值",
        json_schema_extra=_ui("话题门槛·厌恶强度", "对方踩中的正是她讨厌的东西，且够强"),
    )
    silence_max_per_window: int = Field(
        default=2,
        ge=0,
        le=20,
        description="时间窗内最多沉默几次；0 表示不限制",
        json_schema_extra=_ui(
            "沉默次数上限",
            "没有上限这个功能会退化成「经常不理人」。0 表示不限制（不推荐）",
        ),
    )
    silence_window_hours: float = Field(
        default=3.0,
        ge=0.5,
        le=72.0,
        description="沉默频率的时间窗（小时）",
        json_schema_extra=_ui(
            "沉默频率时间窗（小时）",
            "默认 3 小时内最多 2 次。想更活跃就调大次数或调小窗口",
        ),
    )


class AbsenceConfig(PluginConfigBase):
    """缺席感：被冷落之后再见到你，反应应该不一样。

    数据来自 ``gap_state.last_seen_ts``（每个会话上次有人说话的时间）。
    反应档位由「隔了多久」和「关系多近」共同决定——陌生人没理由在意你
    多久没来。
    """

    __ui_label__ = "缺席感"
    __ui_icon__ = "clock"
    __ui_order__ = 31

    enabled: bool = Field(
        default=True,
        description="是否感知「多久没说话了」，并影响回复",
        json_schema_extra=_ui(
            "启用缺席感",
            "开启后被冷落几天再见到你，她的反应会不一样（会注意到、会有点在意）",
        ),
    )
    inject: bool = Field(
        default=True,
        description="是否把缺席提示注入",
        json_schema_extra=_ui("注入缺席提示", "关掉则只记录不注入"),
    )
    min_hours: float = Field(
        default=6.0,
        ge=0.5,
        le=720.0,
        description="多久没说话才算「有缺席感」，低于这个值不提",
        json_schema_extra=_ui("最少间隔（小时）", "低于这个时长不提——每小时提一次是骚扰"),
    )
    long_hours: float = Field(
        default=72.0,
        ge=1.0,
        le=8760.0,
        description="超过这个小时数就按「很久」处理，不再线性细分",
        json_schema_extra=_ui("「很久」的阈值（小时）", "到这么长就是最高一档的反应了"),
    )
    group_enabled: bool = Field(
        default=False,
        description="群聊是否也提示缺席（默认关：群里冷清跟她没关系）",
        json_schema_extra=_ui(
            "群聊也提缺席",
            "默认关。群冷清是群的事，不是「你冷落我」；开启后只在长期冷清时轻描淡写提一句",
        ),
    )
    max_chars: int = Field(
        default=60,
        ge=10,
        le=300,
        description="缺席提示的字数上限",
        json_schema_extra=_ui("字数上限", "这段提示本身很短，一般不用改"),
    )
    inject_header: str = Field(
        default="<companion-absence>",
        description="缺席提示的起始标记",
        json_schema_extra=_ui("起始标记", "独立成段，不占记忆块预算"),
    )
    inject_footer: str = Field(
        default="</companion-absence>",
        description="缺席提示的结束标记",
        json_schema_extra=_ui("结束标记", "与起始标记成对出现"),
    )


class PresenceConfig(PluginConfigBase):
    """在场：区分「她不在」和「对方没来」。

    原来的缺席感只算 ``now - last_seen``，**假设她一直醒着**。真机上这就翻车了：
    用户每天 00:00 关机、11:00 开机，某天上午开机后她看到「73 小时没说话」，
    第一句是「你怎么这么久没来」——可其中 33.8 小时是她自己不在。

    这里从宿主 ``online_time`` 表读出她真正在线的时间，把间隔拆成两段：
    「她不在」的那部分该说「我这几天没开机」，「她醒着但对方没来」的那部分
    才该有缺席感。
    """

    __ui_label__ = "在场感知"
    __ui_icon__ = "presence"
    __ui_order__ = 33

    enabled: bool = Field(
        default=True,
        description="是否区分「她不在」与「对方没来」",
        json_schema_extra=_ui(
            "区分她不在与对方没来",
            "开启后读宿主的在线时段，把「多久没说话」拆成两段；关掉则退回旧算法"
            "（一律按「对方没来」处理）",
        ),
    )
    online_days: float = Field(
        default=30.0,
        ge=1.0,
        le=365.0,
        description="往回读多少天的在线时段",
        json_schema_extra=_ui(
            "读多少天在线时段",
            "宿主每 5 分钟写一行，30 天约 8600 行。读不到就当没有在线数据（退回旧算法）",
        ),
    )
    offline_dominant: float = Field(
        default=0.5,
        ge=0.1,
        le=0.9,
        description="离线占比超过这个值就不算「被冷落」",
        json_schema_extra=_ui(
            "离线占比阈值",
            "间隔里她不在的时间超过这个比例，就判成「我不在」而不是「你冷落我」",
        ),
    )
    away_enabled: bool = Field(
        default=True,
        description="是否注入「那段时间我不在」的提示",
        json_schema_extra=_ui(
            "提「我不在」",
            "关掉则只修正缺席感判断、不额外注入这句话",
        ),
    )


class BackfillConfig(PluginConfigBase):
    """历史补抓：把「她没看见的那几天」从 QQ 端捞回来。

    宿主只在收到消息时写库，所以关机期间的消息**永久缺失**（真机实测：
    ``online_time`` 的 8 段离线区间内 ``mai_messages`` 计数全为 0）。
    而 QQ 客户端本地缓存里有——实测能往前翻 9~10 天。

    只有群聊。私聊 ``get_friend_msg_history`` 返回空数组是「这几天没有私聊」，
    **不是能力不可用**。
    """

    __ui_label__ = "历史补抓"
    __ui_icon__ = "download"
    __ui_order__ = 34

    enabled: bool = Field(
        default=True,
        description="是否从 QQ 端补抓历史消息",
        json_schema_extra=_ui(
            "补抓历史消息",
            "开启后遇到「有待补消息」时会向适配器要群历史。需要 manifest 声明 api.call",
        ),
    )
    min_interval_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="同一会话两次补抓的最小间隔（分钟）",
        json_schema_extra=_ui(
            "补抓间隔（分钟）",
            "补的是「关机期间的洞」，不会天天有新洞，不需要每次抽取都补",
        ),
    )
    max_age_days: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        description="最多往回补多少天",
        json_schema_extra=_ui(
            "最多补多少天",
            "适配器本地缓存上限约 9~10 天，设更大只会翻到空页",
        ),
    )
    max_pages: int = Field(
        default=40,
        ge=1,
        le=200,
        description="最多翻多少页",
        json_schema_extra=_ui("最多翻页数", "每页 100 条，40 页 ≈ 4000 条，远超 10 天实际量"),
    )
    max_messages: int = Field(
        default=200,
        ge=10,
        le=2000,
        description="单次补抓最多保留多少条",
        json_schema_extra=_ui("单次保留上限", "补抓结果要和宿主库合并后一起抽取，太多会撑爆提示词"),
    )


class InitiativeConfig(PluginConfigBase):
    """自主发言：让她自己决定这一轮要不要开口。

    宿主的发言频率只管**触发**（攒够几条才让 Planner 跑），管不了「跑起来之后
    要不要说」。而 ``focus_mode = true`` 时频率被硬编码成 1.0，阈值恒为 1 条、
    空闲退避也失效——三重叠加的结果是「看到啥都要回」。

    这里的做法是在 ``maisaka.planner.before_request`` 里**从工具清单里删掉
    ``reply``**：模型不是「被劝别说」，是**没有说的手段**。
    """

    __ui_label__ = "自主发言"
    __ui_icon__ = "speak"
    __ui_order__ = 36

    enabled: bool = Field(
        default=False,
        description="是否启用自主发言闸门",
        json_schema_extra=_ui(
            "启用自主发言闸门",
            "开启后按兴趣、好感度、心情决定这一轮要不要开口。默认关——它改变的是"
            "「她会说话吗」这件事，建议先观察日志再开",
        ),
    )
    at_always_reply: bool = Field(
        default=True,
        description="被 @ 时一定回复，不看分数",
        json_schema_extra=_ui(
            "被 @ 一定回",
            "对应宿主的 inevitable_at_reply。被叫名字还不应声，那不是性格，是坏了",
        ),
    )
    mentioned_always_reply: bool = Field(
        default=False,
        description="被提到名字时也一定回复",
        json_schema_extra=_ui(
            "被提及一定回",
            "对应宿主的 mentioned_bot_reply。默认关——群里提名字太常见了",
        ),
    )
    threshold: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="软评分门槛，低于它就不开口",
        json_schema_extra=_ui(
            "开口门槛",
            "兴趣 0.40 + 好感度 0.30 + 心情 0.20 + 精力 0.10 加权之后要过这条线。"
            "0.45 大致是「三样里至少两样还行」",
        ),
    )
    gate_send_emoji: bool = Field(
        default=True,
        description="拦掉时是否连同发表情包的权限一起收回",
        json_schema_extra=_ui(
            "连表情包一起拦",
            "表情包也是发言。她要是「不想说话」却回了个表情，那就不叫不想说话",
        ),
    )
    log_decisions: bool = Field(
        default=True,
        description="是否把每次判定结果写进日志",
        json_schema_extra=_ui(
            "记录判定原因",
            "强烈建议开着。主动功能最难排查的就是「它为什么不说话」，"
            "日志里要能直接看到原因和各项分数",
        ),
    )


class HistoryConfig(PluginConfigBase):
    """未读历史：她知道自己错过了什么，也能在被提起时翻回去看。

    用户第 41 轮的原话：「正常人虽然会翻看历史消息，但仅限于几十条的程度，
    对于 99+ 这种，一般不会从头看完……更远的就需要在别人明确提起时，bot 才会
    一边说『我翻翻看哦』，一边回看这些消息。」

    抽取窗口一直只有最后 ``capture.max_messages_per_extract`` 条，所以「全都
    看完」从来不是真的。真问题是**补抓回来的其余部分被直接丢掉了**——她 digest
    了回来后的一小时，然后像从头到尾都在场一样说话。这里把丢掉的部分记成
    「洞」（条数 + 时间范围）和原文，让「那会儿我不在」这件事有据可依。
    """

    __ui_label__ = "未读历史"
    __ui_icon__ = "history"
    __ui_order__ = 37

    enabled: bool = Field(
        default=True,
        description="是否记录「她没看见的那几段」",
        json_schema_extra=_ui(
            "记录未读历史",
            "开启后抽取时会把窗口之外的未读消息记成一段洞（只有条数和时间，"
            "不含内容），原文另存一份供需要时翻看",
        ),
    )
    inject_unread: bool = Field(
        default=True,
        description="是否把「你有 N 条没看到」注入提示词",
        json_schema_extra=_ui(
            "提「没看到」",
            "关掉则只记录不提醒。开着时她会知道「昨晚那 199 条我一条没赶上」，"
            "于是只能说真话，不能假装看过",
        ),
    )
    unread_max_chars: int = Field(
        default=180,
        ge=60,
        le=600,
        description="未读提示的字符上限",
        json_schema_extra=_ui("未读提示字数", "只报条数和时间段，不需要很长"),
    )
    lookup_enabled: bool = Field(
        default=True,
        description="对方提到过去的事时，是否去翻聊天记录",
        json_schema_extra=_ui(
            "允许翻记录",
            "对方说「昨天那个」「上次说的」时，翻出对得上的几条原文。"
            "翻不到就如实说没翻到，而不是顺着上下文编",
        ),
    )
    lookup_limit: int = Field(
        default=3,
        ge=1,
        le=8,
        description="一次最多翻出几条记录",
        json_schema_extra=_ui("翻出几条", "再多就不是「翻翻看」，是「通读」了"),
    )
    lookup_similarity: float = Field(
        default=0.34,
        ge=0.1,
        le=0.9,
        description="翻记录时的相似度门槛",
        json_schema_extra=_ui(
            "翻记录门槛",
            "消息比记忆长得多，所以比记忆那边的 0.40 低一点。太高会什么都翻不到",
        ),
    )
    history_keep: int = Field(
        default=1200,
        ge=100,
        le=20000,
        description="每个会话最多保留多少条原文",
        json_schema_extra=_ui(
            "原文保留条数",
            "只用于翻记录，不参与记忆衰减。更早的还能从连接器现捞",
        ),
    )
    max_holes: int = Field(
        default=40,
        ge=4,
        le=500,
        description="每个会话最多保留几段洞",
        json_schema_extra=_ui("最多保留几段洞", "太久以前的洞没有提的价值"),
    )


class RetentionConfig(PluginConfigBase):
    """保留：各类记忆的过期策略。"""

    __ui_label__ = "保留"
    __ui_icon__ = "archive"
    __ui_order__ = 40

    open_loop_ttl_days: int = Field(
        default=30,
        description="未闭合话题默认保留天数",
        json_schema_extra=_ui("未闭合话题保留（天）", "还没接上的话题保留多少天"),
    )
    emotion_ttl_hours: int = Field(
        default=72,
        description="对方情绪默认保留小时数",
        json_schema_extra=_ui("对方情绪保留（小时）", "对方当时的情绪状态保留多少小时"),
    )
    relationship_ttl_days: int = Field(
        default=180,
        description="关系线索默认保留天数",
        json_schema_extra=_ui("关系线索保留（天）", "相处方式、称呼习惯这类线索保留多少天"),
    )
    self_ttl_days: int = Field(
        default=180,
        description="自身连续性默认保留天数",
        json_schema_extra=_ui("自身连续性保留（天）", "自己的人设与设定一致性保留多少天"),
    )
    creative_ttl_days: int = Field(
        default=180,
        description="共创线索默认保留天数",
        json_schema_extra=_ui("共创线索保留（天）", "一起玩过的梗、共创设定保留多少天"),
    )
    max_items_per_session: int = Field(
        default=120,
        description="单个会话最多保留多少条活跃记忆，超出后淘汰最旧、显著度最低的",
        json_schema_extra=_ui("单会话条数上限", "单个会话最多保留多少条活跃记忆，超出后淘汰最旧、显著度最低的"),
    )
    decay_enabled: bool = Field(
        default=True,
        description="记忆随时间衰减：闲置越久显著度越低，低于阈值就转为「遗忘」",
        json_schema_extra=_ui(
            "记忆会衰减",
            "关掉后记忆的显著度不再随时间下降，也就不会自然遗忘（置顶条目本来就不衰减）",
        ),
    )
    decay_half_life_days: float = Field(
        default=45.0,
        ge=0.0,
        le=3650.0,
        description="闲置多少天显著度掉一半",
        json_schema_extra=_ui(
            "衰减半衰期（天）",
            "闲置多少天记忆强度掉一半。设得太大等于不衰减；设为 0 关闭衰减",
        ),
    )
    forget_below: float = Field(
        default=0.12,
        ge=0.0,
        le=1.0,
        description="衰减到这个显著度以下就转为「遗忘」状态",
        json_schema_extra=_ui(
            "遗忘阈值",
            "显著度掉到这个值以下就标为「遗忘」。遗忘不是删除：不会被主动提起，但别人明确说到时还能想起来",
        ),
    )
    forget_after_days: float = Field(
        default=180.0,
        ge=0.0,
        le=3650.0,
        description="闲置超过这么多天直接遗忘，兜底那些原本显著度就很高的条目",
        json_schema_extra=_ui(
            "闲置多久直接遗忘（天）",
            "兜底规则：就算显著度还很高，闲置这么久也直接标为遗忘。0 表示只按阈值判断",
        ),
    )
    recall_enabled: bool = Field(
        default=True,
        description="别人明确提到某件事时，把对应的遗忘条目重新想起来",
        json_schema_extra=_ui(
            "能被想起来",
            "关掉后遗忘的条目就真的想不起来了。开着时只有对方消息里明确说到相关内容才会触发",
        ),
    )
    recall_similarity: float = Field(
        default=0.40,
        ge=0.1,
        le=1.0,
        description="触发「想起来」所需的最低相似度",
        json_schema_extra=_ui(
            "想起的相似度门槛",
            "对方这句话和遗忘的记忆要多像才算「明确强调」。按「记忆被覆盖了多少」算："
            "完整提到核心内容约 0.45，只沾一个词约 0.27，完全无关 0。"
            "调高更保守（很难想起来），调低更容易被无意中提到就唤醒",
        ),
    )
    recall_limit: int = Field(
        default=2,
        ge=0,
        le=10,
        description="单轮最多想起几条遗忘的记忆",
        json_schema_extra=_ui("单轮最多想起几条", "一轮回复里最多唤醒几条遗忘的记忆，0 表示不注入"),
    )


class DebugConfig(PluginConfigBase):
    """调试。"""

    __ui_label__ = "调试"
    __ui_icon__ = "bug"
    __ui_order__ = 50

    log_level: str = Field(
        default="info",
        description="日志级别：debug / info / warning（对所有模块生效，含后台抽取循环）",
        json_schema_extra=_ui(
            "日志级别",
            "可选 debug / info / warning；对后台抽取等全部日志生效",
        ),
    )
    log_injection_text: bool = Field(
        default=False,
        description="是否把最终注入文本写进日志（含隐私，默认关）",
        json_schema_extra=_ui("记录注入原文", "会把聊天内容写进日志，默认关闭"),
    )
    log_extract_raw: bool = Field(
        default=False,
        description="是否把模型原始返回写进日志",
        json_schema_extra=_ui("记录模型原始返回", "排查抽取问题时打开"),
    )


class MoodConfig(PluginConfigBase):
    """情绪：Bot 自身的持续心境。"""

    __ui_label__ = "情绪"
    __ui_icon__ = "heart"
    __ui_order__ = 35

    enabled: bool = Field(
        default=True,
        description="是否启用情绪模拟",
        json_schema_extra=_ui("启用情绪", "关闭后不更新也不注入情绪"),
    )
    arousal_baseline: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="情绪强度基线：激活程度会衰减回这个值",
        json_schema_extra=_ui("情绪强度基线", "平时有多容易被激起，0 到 1"),
    )
    emotion_joy_baseline: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="开心基线的强度（0 到 1）",
        json_schema_extra=_ui("开心 · 基线", "平时有多容易开心，0 到 1"),
    )
    emotion_anger_baseline: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="愤怒基线的强度（0 到 1）",
        json_schema_extra=_ui("愤怒 · 基线", "平时有多容易生气，0 到 1"),
    )
    emotion_anxiety_baseline: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="紧张基线的强度（0 到 1）",
        json_schema_extra=_ui("紧张 · 基线", "平时有多容易不安，0 到 1"),
    )
    emotion_sadness_baseline: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="伤心基线的强度（0 到 1）",
        json_schema_extra=_ui("伤心 · 基线", "平时有多容易低落，0 到 1"),
    )
    emotion_interest_baseline: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="兴趣基线的强度（0 到 1）",
        json_schema_extra=_ui("兴趣 · 基线", "平时有多容易对话题感兴趣，0 到 1"),
    )
    emotion_calm_baseline: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="平静基线的强度（0 到 1）",
        json_schema_extra=_ui("平静 · 基线", "平时的放松程度，0 到 1"),
    )
    emotion_disgust_baseline: float = Field(
        default=0.03,
        ge=0.0,
        le=1.0,
        description="厌恶基线的强度（0 到 1）",
        json_schema_extra=_ui("厌恶 · 基线", "平时有多容易嫌弃，0 到 1"),
    )
    emotion_surprise_baseline: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="惊讶基线的强度（0 到 1）",
        json_schema_extra=_ui("惊讶 · 基线", "平时有多容易被惊到，0 到 1"),
    )
    emotion_joy_half_life_minutes: float = Field(
        default=120.0,
        ge=0.0,
        description="开心半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("开心 · 半衰期（分钟）", "开心多久冲淡一半，0 表示永不衰减"),
    )
    emotion_anger_half_life_minutes: float = Field(
        default=30.0,
        ge=0.0,
        description="愤怒半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("愤怒 · 半衰期（分钟）", "生气多久消下去，0 表示永不消"),
    )
    emotion_anxiety_half_life_minutes: float = Field(
        default=60.0,
        ge=0.0,
        description="紧张半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("紧张 · 半衰期（分钟）", "不安多久平复一半，0 表示永不衰减"),
    )
    emotion_sadness_half_life_minutes: float = Field(
        default=180.0,
        ge=0.0,
        description="伤心半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("伤心 · 半衰期（分钟）", "难过多久淡一半，0 表示永不淡"),
    )
    emotion_interest_half_life_minutes: float = Field(
        default=240.0,
        ge=0.0,
        description="兴趣半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("兴趣 · 半衰期（分钟）", "兴致多久退一半，0 表示不退"),
    )
    emotion_calm_half_life_minutes: float = Field(
        default=90.0,
        ge=0.0,
        description="平静半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("平静 · 半衰期（分钟）", "平静多久被打破，0 表示不衰减"),
    )
    emotion_disgust_half_life_minutes: float = Field(
        default=90.0,
        ge=0.0,
        description="厌恶半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("厌恶 · 半衰期（分钟）", "嫌弃多久过去，0 表示不过去"),
    )
    emotion_surprise_half_life_minutes: float = Field(
        default=8.0,
        ge=0.0,
        description="惊讶半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("惊讶 · 半衰期（分钟）", "惊讶很快就过去，默认 8 分钟"),
    )
    energy_baseline: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="精力初值：首次启用时从该值开始",
        json_schema_extra=_ui("精力初值", "第一次运行时的精力，之后由说话消耗与时间恢复决定"),
    )
    arousal_half_life_minutes: float = Field(
        default=45.0,
        ge=0.0,
        description="情绪强度半衰期（分钟），0 表示不衰减",
        json_schema_extra=_ui("情绪强度半衰期（分钟）", "激动多久平复一半，0 表示永不衰减"),
    )
    energy_recover_per_hour: float = Field(
        default=0.35,
        ge=0.0,
        le=10.0,
        description="精力每小时恢复多少（0.35 约 3 小时回满）",
        json_schema_extra=_ui(
            "精力恢复速度（每小时）",
            "离线、挂机时按这个速度回精力；心情好会恢复得更快",
        ),
    )
    offline_reset_hours: float = Field(
        default=4.0,
        ge=0.0,
        description="离线超过多少小时，再上线时精力直接回满、情绪归位；0 表示不重置",
        json_schema_extra=_ui(
            "离线重置（小时）",
            "下线超过这么久再上线，当作睡了一觉：精力拉满、情绪回到基线",
        ),
    )
    exhausted_exit: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="精力耗尽后，要恢复到该值才重新允许主动发言",
        json_schema_extra=_ui(
            "复活精力阈值",
            "精力掉到 0 后不会主动说话，直到恢复到该值；设得太低会频繁闭麦",
        ),
    )
    heuristic_gain: float = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="关键词启发式的强度倍率，0 表示只用模型判断",
        json_schema_extra=_ui("启发式强度", "靠关键词微调心情的力度，0 表示只用模型判断"),
    )
    llm_blend_weight: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="模型判断与当前状态的混合权重",
        json_schema_extra=_ui("模型判断权重", "越大越听模型，越小越平滑"),
    )
    inject: bool = Field(
        default=True,
        description="是否把情绪写进回复提示词",
        json_schema_extra=_ui("注入情绪", "让心情真的影响回复语气"),
    )
    header: str = Field(
        default="<companion-mood>",
        description="情绪块起始标记",
        json_schema_extra=_ui("起始标记", "包住情绪块的开始标签"),
    )
    footer: str = Field(
        default="</companion-mood>",
        description="情绪块结束标记",
        json_schema_extra=_ui("结束标记", "包住情绪块的结束标签"),
    )
    max_chars: int = Field(
        default=180,
        ge=0,
        description="情绪块字数上限，0 表示不注入",
        json_schema_extra=_ui("情绪字数上限", "情绪块的文字长度上限"),
    )
    include_guidance: bool = Field(
        default=True,
        description="是否附上「该怎么表现」的行为指令",
        json_schema_extra=_ui("附行为指令", "强烈建议开启：没有指令模型不会真的改变语气"),
    )
    gate_enabled: bool = Field(
        default=True,
        description="精力耗尽时是否真的调低宿主的发言频率",
        json_schema_extra=_ui(
            "耗尽时压频率",
            "只在精力耗尽这一档生效：日常精力低不再压频率，改由提示词影响回复欲望与字数；被 @ 或点名仍会回",
        ),
    )
    gate_floor: float = Field(
        default=0.05,
        gt=0.0,
        le=1.0,
        description="精力耗尽时把发言频率压到该值。必须大于 0：宿主在频率为 0 时会进入静默模式，连 @ 都不回",
        json_schema_extra=_ui(
            "耗尽频率下限",
            "压到多低。不能填 0——填 0 连被 @ 都不会回，因为宿主会先进静默模式",
        ),
    )
    farewell_intent: str = Field(
        default=(
            "你现在精力见底了，马上要安静一段时间。请用一句话自然地说明你要去做什么"
            "（比如去休息、去忙别的、晚点回来），语气按你当前的心情来，不要解释这是设定，"
            "也不要在这条消息里继续聊别的话题。"
        ),
        description="精力耗尽瞬间，让 Bot 交代一句「先去忙别的」的指令；留空则不说",
        json_schema_extra=_ui(
            "耗尽前的告别语",
            "精力见底时先说一句话再安静下来，否则突然闭麦显得很怪；留空则不说",
        ),
    )


class RoleConfig(PluginConfigBase):
    """一条关系身份。

    用户第 33 轮要求「关系中的身份也应该可以自由配置（比如我想添加一个自定义
    的身份，并配置专门的提示词）」，所以身份不再写死成四档，而是配置页里可以
    增删的表格行。

    字段刻意只有三个，且 ``tone`` 是**行为要求**不是称呼 —— 用户反复强调过
    「不会直接通过提示词注入称呼，而是注入相应的聊天方式」。
    """

    key: str = Field(
        default="",
        description="身份标识，程序内部用（小写英文）",
        json_schema_extra=_ui("标识", "英文小写，写进数据库用，例如 owner / teacher"),
    )
    label: str = Field(
        default="",
        description="显示名",
        json_schema_extra=_ui("称呼", "显示给你看的中文名，例如「主人」「老师」"),
    )
    tone: str = Field(
        default="",
        description="这个身份对应的行为要求",
        json_schema_extra=_ui(
            "提示词",
            "写**该怎么对待他**，不是写称呼。例如「你愿意顺着他、也更在意他的看法。」",
        ),
    )


class IntimacyConfig(PluginConfigBase):
    """一档密切程度。

    用户第 33 轮要求「好感度分级的阈值也应该可以调节」，所以阈值、标签名、
    对应的聊天方式都是配置项。

    ``min_affection`` 是**下限**：好感度大于等于它的第一个人就是这个档。
    程序会按这个值从大到小排序，所以填的顺序不影响结果。
    """

    key: str = Field(
        default="",
        description="分级标识",
        json_schema_extra=_ui("标识", "英文小写，例如 close / friend"),
    )
    label: str = Field(
        default="",
        description="显示名",
        json_schema_extra=_ui("称呼", "显示给你看的中文名，例如「知己」"),
    )
    min_affection: float = Field(
        default=0.0,
        ge=-100.0,
        le=100.0,
        description="进入这一档所需的最低好感度",
        json_schema_extra=_ui("好感度下限", "好感度到多少算这一档。-100 到 100"),
    )
    tone: str = Field(
        default="",
        description="这一档对应的聊天方式",
        json_schema_extra=_ui(
            "聊天方式",
            "写该怎么说话，不是写称呼。例如「你们很熟，说话可以随意些、能开玩笑。」",
        ),
    )


class PeopleConfig(PluginConfigBase):
    """人物认知。

    用户的要求分三层，配置项也按这三层组织：

    1. **自动识别要辨别可信度** —— ``auto_role_min_confidence`` 是闸门，
       玩笑、吵架、转述这类低置信判断进不来。
    2. **手动配置优先** —— ``manual_override`` 关掉之后自动结果才能改写
       已有人物（默认关，即手动优先）。
    3. **密切度只注入聊天方式** —— ``inject_tone`` / ``inject_notes`` 控制
       注入什么，``inject_role_label`` 默认关（不把「主人」「知己」这类称呼
       塞给模型，避免它开口就叫）。
    """

    __ui_label__ = "人物"
    __ui_icon__ = "users"
    __ui_order__ = 45

    enabled: bool = Field(
        default=True,
        description="是否记录人物认知",
        json_schema_extra=_ui("记录人物", "从聊天里记住谁是谁、你们关系如何"),
    )
    self_identities: list[str] = Field(
        default_factory=list,
        description="机器人自己的账号/昵称，用来避免把她自己记进人物表",
        json_schema_extra=_ui(
            "机器人自己的账号",
            "填机器人在各平台的 QQ 号或昵称，多个用逗号分隔。"
            "插件一般能从宿主的消息过滤里自动学到，这里只是兜底。"
            "不填也不会把她当成别人——只是自动学习需要先跑过一次抽取",
        ),
    )
    auto_role_min_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="自动判断关系角色所需的最低置信度",
        json_schema_extra=_ui(
            "自动判断可信度门槛",
            "低于这个值的判断不采信（玩笑、吵架、转述都会被挡在这里）；越高越保守",
        ),
    )
    roles: list[RoleConfig] = Field(
        default_factory=lambda: [RoleConfig(**item) for item in DEFAULT_ROLES],
        description="关系身份表（可增删改）",
        json_schema_extra=_ui(
            "关系身份",
            "可以增删。每一项要填：标识（英文小写）、称呼（显示用）、提示词（该怎么对待他）。"
            "标识必须有一个 stranger 作为兜底。提示词写行为要求，不要写称呼",
        ),
    )
    intimacy_levels: list[IntimacyConfig] = Field(
        default_factory=lambda: [
            IntimacyConfig(
                key=item["key"],
                label=item["label"],
                min_affection=float(item["min_affection"]),
                tone=item["tone"],
            )
            for item in DEFAULT_INTIMACY_LEVELS
        ],
        description="好感度分级表（阈值与聊天方式都可改）",
        json_schema_extra=_ui(
            "好感度分级",
            "好感度到多少算哪一档，以及这一档该怎么说话。程序会按「好感度下限」"
            "从大到小自动排序，所以填的顺序不影响结果",
        ),
    )
    manual_override: bool = Field(
        default=False,
        description="允许自动结果覆盖手动配置",
        json_schema_extra=_ui(
            "自动覆盖手动配置",
            "默认关闭，即手动配置的角色与好感度优先。开启后自动判断也能改写",
        ),
    )
    inject: bool = Field(
        default=True,
        description="是否注入人物认知",
        json_schema_extra=_ui("注入人物", "在回复前告诉模型「此刻和你说话的是谁」"),
    )
    inject_tone: bool = Field(
        default=True,
        description="注入密切度对应的聊天方式",
        json_schema_extra=_ui(
            "注入聊天方式",
            "按好感度决定语气亲近还是客气。这是「关系密切程度」的主要体现",
        ),
    )
    inject_role_label: bool = Field(
        default=False,
        description="把关系角色名称直接写进提示词",
        json_schema_extra=_ui(
            "注入角色称呼",
            "默认关闭。开启后会把「主人」「朋友」这类词写进提示词，模型可能开口就叫",
        ),
    )
    deference_owner_only: bool = Field(
        default=True,
        description="只允许称呼 owner 为「主人」，其他人注入时把称呼泛化掉",
        json_schema_extra=_ui(
            "称呼只给主人",
            "默认开启。记忆里存着真实发生过的「叫她主人」的对话，这些条目每轮"
            "都会被注入，等于每轮都在提醒模型「你有主人」。开启后，只有当轮说话"
            "人是 owner 时才保留「主人」二字，其他人一律换成「你」。关掉则恢复"
            "旧行为（谁都可能被叫主人）",
        ),
    )
    inject_notes: bool = Field(
        default=True,
        description="注入用户手写的人物标签",
        json_schema_extra=_ui(
            "注入人物标签",
            "标签只能由你自己在页面上写（「XX的XX」那种关系化说法）。"
            "关掉后提示词里只留关系与情绪影响",
        ),
    )
    max_people: int = Field(
        default=2,
        ge=1,
        le=5,
        description="一次注入最多几个人",
        json_schema_extra=_ui("注入人数上限", "群聊里同时说话的人多，别都塞进去"),
    )
    max_chars: int = Field(
        default=240,
        ge=0,
        le=800,
        description="人物块字数上限",
        json_schema_extra=_ui("注入字数上限", "人物块单独占的预算，不挤占记忆块"),
    )
    header: str = Field(
        default="<companion-people>",
        description="人物块起始标记",
        json_schema_extra=_ui("起始标记", "人物块开头的标记，用来和记忆块区分"),
    )
    footer: str = Field(
        default="</companion-people>",
        description="人物块结束标记",
        json_schema_extra=_ui("结束标记", "人物块结尾的标记，要和起始标记成对"),
    )
    lead_in: str = Field(
        default="（关于此刻和你说话的人，你记得这些）",
        description="人物块引导语",
        json_schema_extra=_ui("引导语", "人物块前面那句提示，告诉模型下面是什么"),
    )
    track_impact: bool = Field(
        default=True,
        description="记录每个人给 Bot 带来的情绪波动",
        json_schema_extra=_ui(
            "记录情绪影响",
            "记下和谁聊完心情变好或变差，用于形成喜欢/厌恶的排序依据",
        ),
    )
    impact_decay: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="情绪影响的衰减系数",
        json_schema_extra=_ui(
            "情绪影响衰减",
            "每次更新时旧值乘这个系数。越小越看重近期互动；1.0 表示永不衰减",
        ),
    )
    # ── 关系六维（第 34 轮）──────────────────────────────────────────
    #
    # 用户原话：「衡量人与人之间的情感与关系，并不能简单地依靠一个『好感度』
    # 的数值进行表达。这种关系往往是复杂且多元的，还会受到其他人的影响。」
    inject_axes: bool = Field(
        default=True,
        description="注入关系六维的描述",
        json_schema_extra=_ui(
            "注入关系描述",
            "把信任/亲近/尊重/张力/熟悉/他待我六个维度里**互相矛盾**的组合"
            "说成一句话（比如「你们混得很熟，但有些话你还是不跟他说」），"
            "而不是报数字。关掉后只注入好感度分级对应的聊天方式",
        ),
    )
    social_influence: bool = Field(
        default=True,
        description="让别人的评价影响你对某人的看法",
        json_schema_extra=_ui(
            "第三方影响",
            "A 夸 B 或说 B 坏话时，会影响你对 B 的信任与亲近。"
            "只动信任和亲近，不动尊重——服不服一个人得自己看",
        ),
    )
    social_weight: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="第三方影响的基础强度",
        json_schema_extra=_ui(
            "第三方影响强度",
            "别人说一句能推动多少。0 等于关掉，1 等于别人说一句你就信一半",
        ),
    )
    social_cap: float = Field(
        default=0.20,
        ge=0.0,
        le=0.5,
        description="第三方影响的最大幅度",
        json_schema_extra=_ui(
            "第三方影响上限",
            "传话终究只是传话：无论别人怎么说，最多只能把信任/亲近推动这么多。"
            "防止靠别人嘴把一个人捧成知己或踩成仇人",
        ),
    )
    axis_half_life_days: float = Field(
        default=90.0,
        ge=1.0,
        le=3650.0,
        description="关系淡化的半衰期（天）",
        json_schema_extra=_ui(
            "关系淡化半衰期",
            "多久不联系，六维会往中性值回落一半。熟悉度不受影响——"
            "聊过的次数不会因为一段时间没说话就归零",
        ),
    )
    list_limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="管理页与命令列出的人物数量上限",
        json_schema_extra=_ui("列表条数上限", "管理页和命令一次最多显示多少人"),
    )


class PrefsConfig(PluginConfigBase):
    """喜好与厌恶：Bot 自己的口味。

    和人物那节的区别：人物是「对别人」的，这里是「关于她自己」的。
    用户第 32 轮第 5 点要求「根据人设产生自己的喜好和厌恶」，
    所以这一节的配置重点是「人设怎么变成口味」和「口味怎么注入」。
    """

    __ui_label__ = "喜好"
    __ui_icon__ = "thumbs-up"
    __ui_order__ = 42

    enabled: bool = Field(
        default=True,
        description="启用喜好与厌恶",
        json_schema_extra=_ui(
            "启用喜好",
            "让 Bot 有自己的口味。关掉后不抽取也不注入，已有的口味保留但不生效",
        ),
    )
    persona_seed: bool = Field(
        default=True,
        description="首次启动时从宿主的人设里提炼初始喜好",
        json_schema_extra=_ui(
            "从人设起步",
            "第一次跑的时候让模型读一遍宿主的人设（[personality] 那节），"
            "提炼出「她会喜欢什么、讨厌什么」作为起点。之后靠聊天慢慢演化",
        ),
    )
    persona_key: str = Field(
        default="personality.personality",
        description="宿主配置里人设所在的点号路径",
        json_schema_extra=_ui(
            "人设配置路径",
            "去宿主的 global_config 里按这个路径找人设文本。默认是"
            " personality.personality，改了宿主配置结构才需要动",
        ),
    )
    merge_decay: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="旧强度的权重",
        json_schema_extra=_ui(
            "口味稳定度",
            "每次聊天更新时旧值乘这个系数。越大口味越稳定（不容易被一次聊天带偏）；"
            "越小越随性",
        ),
    )
    max_prefs: int = Field(
        default=20,
        ge=1,
        le=200,
        description="最多保留多少条口味",
        json_schema_extra=_ui("最多条数", "超出后按强度从低到高淘汰，只留最鲜明的"),
    )
    inject: bool = Field(
        default=True,
        description="把口味注入提示词",
        json_schema_extra=_ui("注入提示词", "关掉后只记录不告诉模型"),
    )
    max_items: int = Field(
        default=6,
        ge=0,
        le=20,
        description="注入最多几条",
        json_schema_extra=_ui("注入条数", "一轮最多带几条口味。太多会喧宾夺主"),
    )
    min_strength: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="低于这个强度的不注入",
        json_schema_extra=_ui(
            "注入强度门槛",
            "太淡的口味不值得占预算。调低会把「只是有点在意」的东西也带上",
        ),
    )
    max_chars: int = Field(
        default=200,
        ge=0,
        le=800,
        description="喜好块字数上限",
        json_schema_extra=_ui("注入字数上限", "喜好块单独占的预算，不挤占记忆块"),
    )
    header: str = Field(
        default="<companion-prefs>",
        description="喜好块起始标记",
        json_schema_extra=_ui("起始标记", "喜好块开头的标记"),
    )
    footer: str = Field(
        default="</companion-prefs>",
        description="喜好块结束标记",
        json_schema_extra=_ui("结束标记", "喜好块结尾的标记，要和起始标记成对"),
    )
    lead_in: str = Field(
        default="（这些是你自己的口味，不是别人告诉你的）",
        description="喜好块引导语",
        json_schema_extra=_ui("引导语", "喜好块前面那句提示，强调这是她自己的想法"),
    )
    list_limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="管理页与命令列出的喜好数量上限",
        json_schema_extra=_ui("列表条数上限", "管理页和命令一次最多显示多少条"),
    )


class CompanionGapConfig(PluginConfigBase):
    """插件总配置。"""
    plugin: PluginSectionConfig = Field(
        default_factory=PluginSectionConfig,
        description="插件启停与配置版本（宿主保留节）",
        json_schema_extra=_ui("插件"),
    )
    capture: CaptureConfig = Field(
        default_factory=CaptureConfig,
        description="采集",
        json_schema_extra=_ui("采集"),
    )
    extract: ExtractConfig = Field(
        default_factory=ExtractConfig,
        description="抽取",
        json_schema_extra=_ui("抽取"),
    )
    inject: InjectConfig = Field(
        default_factory=InjectConfig,
        description="注入",
        json_schema_extra=_ui("注入"),
    )
    retention: RetentionConfig = Field(
        default_factory=RetentionConfig,
        description="保留",
        json_schema_extra=_ui("保留"),
    )
    mood: MoodConfig = Field(
        default_factory=MoodConfig,
        description="情绪",
        json_schema_extra=_ui("情绪"),
    )
    people: PeopleConfig = Field(
        default_factory=PeopleConfig,
        description="人物",
        json_schema_extra=_ui("人物"),
    )
    prefs: PrefsConfig = Field(
        default_factory=PrefsConfig,
        description="喜好",
        json_schema_extra=_ui("喜好"),
    )
    absence: AbsenceConfig = Field(
        default_factory=AbsenceConfig,
        description="缺席感",
        json_schema_extra=_ui("缺席感"),
    )
    boundary: BoundaryConfig = Field(
        default_factory=BoundaryConfig,
        description="边界感",
        json_schema_extra=_ui("边界感"),
    )
    presence: PresenceConfig = Field(
        default_factory=PresenceConfig,
        description="在场感知",
        json_schema_extra=_ui("在场感知"),
    )
    backfill: BackfillConfig = Field(
        default_factory=BackfillConfig,
        description="历史补抓",
        json_schema_extra=_ui("历史补抓"),
    )
    initiative: InitiativeConfig = Field(
        default_factory=InitiativeConfig,
        description="自主发言",
        json_schema_extra=_ui("自主发言"),
    )
    history: HistoryConfig = Field(
        default_factory=HistoryConfig,
        description="未读历史",
        json_schema_extra=_ui("未读历史"),
    )
    debug: DebugConfig = Field(
        default_factory=DebugConfig,
        description="调试",
        json_schema_extra=_ui("调试"),
    )
