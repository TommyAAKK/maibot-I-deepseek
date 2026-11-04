"""我，deepseek —— 本地存储层。

设计约束（来自 MaiBot 插件边界）：

- 插件**不能**在宿主数据库里建表（``ctx.db`` 只允许操作宿主已注册的
  ``database_model`` 类名），所以这里自带一个 SQLite 文件，落在
  ``ctx.paths.data_dir`` 下。
- 所有方法都是同步的：调用方是同一个 asyncio 事件循环，方法内部没有
  ``await``，因此单连接复用是安全的；反过来说，**不要**在这些方法里
  插入任何 await，否则会和后台任务交错。
- 检索只用主键 / 普通索引 + 一次内存内相似度比较，单会话条目量级在
  百条以内，耗时在毫秒级，可以放进 replyer 热路径。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

SCHEMA_VERSION = "companion-gap-v1"

#: 「老 notes 列搬进 tags」这步做完的标记位（存在 ``gap_meta`` 里）。
#:
#: 必须有这个标记位。``notes`` 列在 SQLite 里删不掉（老版本没有
#: ``DROP COLUMN``，只能靠 ``ADD COLUMN`` 单向演进），而 ``_migrate``
#: 每次 ``GapStore.open()`` 都会重跑。没有标记位的话，用户把标签清空后
#: **下一次插件重启又会被陈旧的 notes 填回去** —— 这正是「删掉的标签
#: 自己又回来了」的成因（用户第 43 轮报的问题）。
NOTES_MIGRATED_KEY = "notes_to_tags_migrated"

#: 判定「同一条记忆」的字符二元组 Jaccard 阈值。
DEDUPE_SIMILARITY = 0.55

#: 显著度衰减的写库阈值：变化量小于它就攒着不写。
#:
#: 不写库也**不推进衰减基准**，所以这段时间会在下一轮一并计入，精度无损。
#: 取 5e-4 是为了让写库频率落在十分钟量级（半衰期 45 天时一个 30 秒 tick
#: 只掉约 5e-7），而不是每 30 秒就写一次盘。
_DECAY_EPSILON = 5e-4

#: 五类情境线索。顺序即 WebUI 里的展示顺序。
CATEGORIES = ("open_loop", "emotion", "relationship", "self", "creative")

#: 条目状态。active 参与注入，resolved / expired 只保留作历史。
#: ``forgotten`` 是第 32 轮新增的：记忆淡到一定程度会被「遗忘」，但**不是删除**。
#: 遗忘的条目不会被主动读出来（``list_active`` 只取 active），只有别人明确
#: 强调时才会被「想起」（``recall_item``）。用户原话：「标注为遗忘，并非删除，
#: 不主动读取，只有他人明确强调才会想起」。
STATUSES = ("active", "resolved", "expired", "forgotten")

CATEGORY_LABELS = {
    "open_loop": "未闭合话题",
    "emotion": "对方情绪",
    "relationship": "关系线索",
    "self": "自身连续性",
    "creative": "共创线索",
}

STATUS_LABELS = {
    "active": "活跃",
    "resolved": "已解决",
    "expired": "已过期",
    "forgotten": "遗忘",
}

#: 八个情绪基元的默认基线（与 ``cg_mood.DEFAULT_PRIMITIVES`` 保持一致）。
#: 放在这里是为了让 ``_migrate`` 能在不 import cg_mood 的前提下补列。
_EMOTION_DEFAULTS: tuple[tuple[str, float], ...] = (
    ("joy", 0.25),
    ("anger", 0.05),
    ("anxiety", 0.10),
    ("sadness", 0.05),
    ("interest", 0.45),
    ("calm", 0.55),
    ("disgust", 0.03),
    ("surprise", 0.10),
)

#: 情绪基元的列名顺序（读写共用）。
EMOTION_COLUMNS: tuple[str, ...] = tuple(f"emotion_{name}" for name, _ in _EMOTION_DEFAULTS)

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS gap_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gap_state (
        session_id      TEXT PRIMARY KEY,
        platform        TEXT NOT NULL DEFAULT '',
        chat_type       TEXT NOT NULL DEFAULT '',
        group_id        TEXT NOT NULL DEFAULT '',
        group_name      TEXT NOT NULL DEFAULT '',
        user_id         TEXT NOT NULL DEFAULT '',
        user_nickname   TEXT NOT NULL DEFAULT '',
        last_seen_ts    REAL NOT NULL DEFAULT 0,
        last_extract_ts REAL NOT NULL DEFAULT 0,
        pending_count   INTEGER NOT NULL DEFAULT 0,
        updated_at      REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gap_items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   TEXT NOT NULL,
        category     TEXT NOT NULL,
        content      TEXT NOT NULL,
        salience     REAL NOT NULL DEFAULT 0.5,
        status       TEXT NOT NULL DEFAULT 'active',
        first_seen_ts REAL NOT NULL DEFAULT 0,
        last_seen_ts  REAL NOT NULL DEFAULT 0,
        expires_at    REAL NOT NULL DEFAULT 0,
        source_count  INTEGER NOT NULL DEFAULT 1,
        created_at    REAL NOT NULL DEFAULT 0,
        updated_at    REAL NOT NULL DEFAULT 0,
        pinned        INTEGER NOT NULL DEFAULT 0,
        truth_score   REAL NOT NULL DEFAULT 1.0,
        last_recalled_ts REAL NOT NULL DEFAULT 0,
        forgotten_at  REAL NOT NULL DEFAULT 0,
        last_injected_ts REAL NOT NULL DEFAULT 0,
        inject_streak INTEGER NOT NULL DEFAULT 0,
        inject_count  INTEGER NOT NULL DEFAULT 0,
        salience_decayed_at REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gap_items_session
        ON gap_items (session_id, status, category)
    """,
    """
    CREATE TABLE IF NOT EXISTS gap_extractions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id     TEXT NOT NULL,
        ts             REAL NOT NULL DEFAULT 0,
        model          TEXT NOT NULL DEFAULT '',
        ok             INTEGER NOT NULL DEFAULT 0,
        item_count     INTEGER NOT NULL DEFAULT 0,
        resolved_count INTEGER NOT NULL DEFAULT 0,
        prompt_chars   INTEGER NOT NULL DEFAULT 0,
        response_chars INTEGER NOT NULL DEFAULT 0,
        error          TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gap_injections (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        ts         REAL NOT NULL DEFAULT 0,
        chars      INTEGER NOT NULL DEFAULT 0,
        item_count INTEGER NOT NULL DEFAULT 0,
        categories TEXT NOT NULL DEFAULT '',
        elapsed_ms REAL NOT NULL DEFAULT 0
    )
    """,
    # 情绪是**全局单例**（scope 目前恒为 'global'），不按会话分裂：
    # Bot 是一个实体，一个群里的坏心情会带到别处，这才像人。
    # 留 scope 列是为了以后真要按平台/人格分开时不用改表结构。
    """
    CREATE TABLE IF NOT EXISTS gap_mood (
        scope      TEXT PRIMARY KEY,
        valence    REAL NOT NULL DEFAULT 0,
        arousal    REAL NOT NULL DEFAULT 0.35,
        energy     REAL NOT NULL DEFAULT 0.6,
        label      TEXT NOT NULL DEFAULT '',
        reason     TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL DEFAULT 0,
        exhausted  INTEGER NOT NULL DEFAULT 0,
        emotion_joy      REAL NOT NULL DEFAULT 0.25,
        emotion_anger    REAL NOT NULL DEFAULT 0.05,
        emotion_anxiety  REAL NOT NULL DEFAULT 0.10,
        emotion_sadness  REAL NOT NULL DEFAULT 0.05,
        emotion_interest REAL NOT NULL DEFAULT 0.45,
        emotion_calm     REAL NOT NULL DEFAULT 0.55,
        emotion_disgust  REAL NOT NULL DEFAULT 0.03,
        emotion_surprise REAL NOT NULL DEFAULT 0.10
    )
    """,
    # 情绪历史，用于在管理页面画曲线、以及事后判断情绪是否变化合理。
    """
    CREATE TABLE IF NOT EXISTS gap_mood_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         REAL NOT NULL DEFAULT 0,
        valence    REAL NOT NULL DEFAULT 0,
        arousal    REAL NOT NULL DEFAULT 0,
        energy     REAL NOT NULL DEFAULT 0,
        label      TEXT NOT NULL DEFAULT '',
        source     TEXT NOT NULL DEFAULT '',
        reason     TEXT NOT NULL DEFAULT '',
        exhausted  INTEGER NOT NULL DEFAULT 0,
        emotion_joy      REAL NOT NULL DEFAULT 0.25,
        emotion_anger    REAL NOT NULL DEFAULT 0.05,
        emotion_anxiety  REAL NOT NULL DEFAULT 0.10,
        emotion_sadness  REAL NOT NULL DEFAULT 0.05,
        emotion_interest REAL NOT NULL DEFAULT 0.45,
        emotion_calm     REAL NOT NULL DEFAULT 0.55,
        emotion_disgust  REAL NOT NULL DEFAULT 0.03,
        emotion_surprise REAL NOT NULL DEFAULT 0.10
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gap_mood_log_ts ON gap_mood_log (ts)
    """,
    # 人物认知。key 用 ``platform:user_id`` —— 昵称会改，ID 不会。
    # ``role_source`` 区分手动/自动：手动配置的行自动流程永不覆盖。
    """
    CREATE TABLE IF NOT EXISTS gap_person (
        person_key       TEXT PRIMARY KEY,
        platform         TEXT NOT NULL DEFAULT '',
        user_id          TEXT NOT NULL DEFAULT '',
        display_name     TEXT NOT NULL DEFAULT '',
        relation_role    TEXT NOT NULL DEFAULT 'stranger',
        role_confidence  REAL NOT NULL DEFAULT 0,
        role_source      TEXT NOT NULL DEFAULT 'auto',
        affection        REAL NOT NULL DEFAULT 0,
        emotional_impact REAL NOT NULL DEFAULT 0,
        tags             TEXT NOT NULL DEFAULT '[]',
        axes_source      TEXT NOT NULL DEFAULT 'auto',
        first_seen_ts    REAL NOT NULL DEFAULT 0,
        last_seen_ts     REAL NOT NULL DEFAULT 0,
        updated_at       REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gap_person_updated ON gap_person (updated_at)
    """,
    # 关系网络：谁提到谁、什么态度。用户第 34 轮说关系「还会受到其他人的
    # 影响」，这张表就是那条影响的来源。``source_key`` 是说话的人，
    # ``target_key`` 是被提到的人，``attitude`` 是说话人对目标的褒贬（-1..1）。
    """
    CREATE TABLE IF NOT EXISTS gap_person_links (
        link_key      TEXT PRIMARY KEY,
        source_key    TEXT NOT NULL DEFAULT '',
        target_key    TEXT NOT NULL DEFAULT '',
        attitude      REAL NOT NULL DEFAULT 0,
        evidence      TEXT NOT NULL DEFAULT '',
        seen_count    INTEGER NOT NULL DEFAULT 1,
        created_at    REAL NOT NULL DEFAULT 0,
        updated_at    REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gap_person_links_target
        ON gap_person_links (target_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS gap_prefs (
        pref_key    TEXT PRIMARY KEY,
        kind        TEXT NOT NULL DEFAULT 'like',
        topic       TEXT NOT NULL DEFAULT '',
        strength    REAL NOT NULL DEFAULT 0.5,
        source      TEXT NOT NULL DEFAULT 'chat',
        created_at  REAL NOT NULL DEFAULT 0,
        updated_at  REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_gap_prefs_updated ON gap_prefs (updated_at)
    """,
)

_ITEM_COLUMNS = (
    "id, session_id, category, content, salience, status, "
    "first_seen_ts, last_seen_ts, expires_at, source_count, created_at, updated_at, "
    "pinned, truth_score, last_recalled_ts, forgotten_at, "
    "last_injected_ts, inject_streak, inject_count, salience_decayed_at"
)

#: JOIN 时用的带表别名版本。``gap_state`` 也有 ``session_id`` 列，
#: 不加前缀会让 SQLite 报 ambiguous column name。
_ITEM_COLUMNS_ALIASED = ", ".join(f"i.{name.strip()}" for name in _ITEM_COLUMNS.split(","))


def _bigrams(text: str) -> set[str]:
    """返回字符串的字符二元组集合；短文本退化为单字集合。"""

    normalized = "".join(ch for ch in text if not ch.isspace())
    if len(normalized) <= 1:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def similarity(left: str, right: str) -> float:
    """字符二元组 Jaccard 相似度，用于中文短句去重。"""

    a = _bigrams(left)
    b = _bigrams(right)
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def _bigrams_no_punct(text: str) -> set[str]:
    """去标点后的字符二元组，用于「想起」的宽松匹配。"""

    normalized = "".join(ch for ch in text if ch.isalnum() or _is_cjk(ch))
    if len(normalized) <= 1:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def recall_score(message: str, memory: str) -> float:
    """判断「对方这句话有多像在明确提到这条记忆」。

    和 ``similarity`` 用的不是同一把尺子，因为两者要回答的问题不同：

    * 去重问的是「这两条记忆是不是同一件事」——两边都是提炼过的短句，
      对称的 Jaccard 合适。
    * 想起问的是「这句**很长的话**里有没有明确提到那条记忆」——两边长度差
      一个数量级，Jaccard 的并集会被长句里大量无关的二元组撑大，得出来的
      分数永远很低（实测「之前是不是说过要一起去看海来着？」对「说好下个月
      一起去看海」只有 0.2），按 0.55 的门槛根本不可能触发。

    所以这里改成「记忆被覆盖了多少」：拿记忆的二元组当分子，用**记忆自己**
    的二元组数量当分母（不是并集），再额外看最长公共子串的占比。这样只要
    对方的话里确实出现了那条记忆的核心内容，分数就会上去；而完全无关的话
    仍然接近 0。

    Args:
        message: 对方这一轮说的话（或模型概括的回复理由）。
        memory: 遗忘条目里的内容。

    Returns:
        0..1 的分数，越大越像在明确提到。
    """

    mem_grams = _bigrams_no_punct(memory)
    msg_grams = _bigrams_no_punct(message)
    if not mem_grams or not msg_grams:
        return 0.0
    coverage = len(mem_grams & msg_grams) / len(mem_grams)

    # 最长公共子串占比：防止「记忆里全是高频字」时 coverage 虚高。
    memory_text = "".join(ch for ch in memory if ch.isalnum() or _is_cjk(ch))
    message_text = "".join(ch for ch in message if ch.isalnum() or _is_cjk(ch))
    longest = 0
    for start in range(len(memory_text)):
        for end in range(start + longest + 1, len(memory_text) + 1):
            if memory_text[start:end] in message_text:
                longest = end - start
            else:
                break
    substring_ratio = longest / len(memory_text) if memory_text else 0.0

    # 两个信号取较大值：短记忆可能被完整包含（coverage 高），
    # 长记忆往往只命中核心片段（子串占比高）。
    return max(coverage, substring_ratio * 0.9)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回表已有的列名集合；表不存在时返回空集。"""

    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补列。

    ``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表是空操作，不会补列。
    所以给老表加字段必须显式 ``ALTER TABLE``，否则老用户升级后一读
    ``exhausted`` 就报 ``no such column``。
    """

    additions: tuple[tuple[str, str, str], ...] = (
        # (表名, 列名, 列定义)
        ("gap_mood", "exhausted", "INTEGER NOT NULL DEFAULT 0"),
        ("gap_mood_log", "exhausted", "INTEGER NOT NULL DEFAULT 0"),
        # 人物标签：老库只有 notes（自由文本备注），第 32 轮改成「XX的XX」
        # 这种关系化标签。加列后下面会把老的 notes 内容搬过去。
        ("gap_person", "tags", "TEXT NOT NULL DEFAULT '[]'"),
        # 关系六维（第 34 轮）：把「一个好感度数字」换成六个互相独立的关系
        # 维度。老库补列后是空字符串，``axes_of`` 会按旧 affection 反推初值，
        # 所以老用户升级后熟人不会突然变成陌生人。
        ("gap_person", "axes", "TEXT NOT NULL DEFAULT ''"),
        # 六维是谁写的：``auto``（抽取）还是 ``manual``（用户手调）。
        #
        # 用户第 43 轮报「我调的好感度没有变回去」的根因就在这里：六维是
        # 活的，任何一次自动抽取都会 ``apply_axes`` 重算并覆盖。用户手调的
        # 值必须能被认出来并保护住，否则「我明明调过」永远不成立。
        ("gap_person", "axes_source", "TEXT NOT NULL DEFAULT 'auto'"),
        # 上一次对六维做时间衰减的时刻。单独一列而不是复用 updated_at：
        # updated_at 是「最近一次互动」，衰减要按它算 idle，但衰减本身不该
        # 把「最近互动」往后推，否则这个人会一直排在列表最前面。
        ("gap_person", "axes_decayed_at", "REAL NOT NULL DEFAULT 0"),
        # 记忆生命周期（第 32 轮）：置顶、可信度、最近被想起、遗忘时间。
        # 老库补列后默认值就是「没置顶、完全可信、从没被想起、没被遗忘」，
        # 语义上正好是老数据该有的样子，不需要额外回填。
        ("gap_items", "pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("gap_items", "truth_score", "REAL NOT NULL DEFAULT 1.0"),
        ("gap_items", "last_recalled_ts", "REAL NOT NULL DEFAULT 0"),
        ("gap_items", "forgotten_at", "REAL NOT NULL DEFAULT 0"),
        # 注入轮换（用户第 45 轮：「同一批记忆连续多轮原样重复」）。
        #
        # 现象：README 第 8 节第 7 条实测 124 条不同记忆里 93.5% 至少被注入
        # 2 轮，相邻两轮完全相同占 54.2%，最长一条连刷 32 轮。根因是
        # ``compose_block`` 只按 ``(-salience, -last_seen_ts)`` 排序取前 N 条，
        # 高分条目的显著度不衰减就永远占位，后来者一辈子进不来。
        #
        # 这三列让「刚说过」变成一种代价：
        # - ``last_injected_ts``：上一次被注入的时刻，用来算「冷却」
        # - ``inject_streak``：连续被注入了几轮，连续越久越该让位
        # - ``inject_count``：总共被注入过几次，用于区分「老面孔」和「新人」
        ("gap_items", "last_injected_ts", "REAL NOT NULL DEFAULT 0"),
        ("gap_items", "inject_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("gap_items", "inject_count", "INTEGER NOT NULL DEFAULT 0"),
        # 上一次对显著度做时间衰减的时刻（衰减基准）。
        #
        # **这是修一个真 bug 用的列**：原来 ``decay_salience`` 每次都拿
        # ``last_seen_ts`` 现算「闲置了多久」，乘完衰减系数却不把基准往前推。
        # 而 ``cg_tracker.tick`` 每 ``refresh_tick_seconds``（默认 30 秒）就调
        # 一次，于是同一段闲置时间被反复计息 —— 每 30 秒都把「闲置 N 天」
        # 的衰减量重算一遍。实测一条 0.7 显著度、闲置 25 小时的记忆，一小时
        # 内就被压到 0.118 并标成遗忘，而正确值应仍是 0.6999。真机数据里
        # 297 条记忆因此被误判为遗忘（active 归零）。
        #
        # 加这一列后，每次只对「上次衰减之后新流逝的时间」计息，
        # 数学上等价于连续衰减，调多少次都不会放大。
        ("gap_items", "salience_decayed_at", "REAL NOT NULL DEFAULT 0"),
    )
    for table, column, definition in additions:
        existing = _table_columns(conn, table)
        if not existing or column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # 老库的 notes 是用户手写的备注，不能因为改名就丢掉：搬进 tags。
    #
    # **只搬一次**。这段原来没有标记位，注释写着「只在 tags 还是空的时搬，
    # 之后用户改过就不再覆盖」—— 但那个条件根本不成立：用户把 tags 清成
    # ``[]`` 正好满足「tags 是空的」，于是每次插件重启都会用陈旧的 notes
    # 把标签写回去。用户第 43 轮报的「删掉标签后更新又出现」就是这个。
    # 现在用 ``gap_meta`` 里的标记位锁死：搬完一次就再也不碰 tags。
    person_columns = _table_columns(conn, "gap_person")
    migrated = conn.execute(
        "SELECT value FROM gap_meta WHERE key = ?", (NOTES_MIGRATED_KEY,)
    ).fetchone()
    if migrated is None and "notes" in person_columns and "tags" in person_columns:
        conn.execute(
            "UPDATE gap_person SET tags = notes "
            "WHERE tags IN ('', '[]') AND notes NOT IN ('', '[]')"
        )
        conn.execute(
            "INSERT INTO gap_meta (key, value) VALUES (?, 'done') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (NOTES_MIGRATED_KEY,),
        )

    # 八维情绪基元：老库只有 valence/arousal，这里补列并填上默认基线，
    # 否则升级后一读 emotion_joy 就报 no such column。
    for table in ("gap_mood", "gap_mood_log"):
        existing = _table_columns(conn, table)
        if not existing:
            continue
        for name, default in _EMOTION_DEFAULTS:
            column = f"emotion_{name}"
            if column in existing:
                continue
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} REAL NOT NULL DEFAULT {default}"
            )


class GapStore:
    """``gap_*.db`` 的读写封装。"""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None

    # ── 生命周期 ──

    def open(self) -> None:
        """打开连接并建表（幂等）。"""

        if self._conn is not None:
            return
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False：Runner 里同一个循环线程复用连接，
        # 但关闭阶段可能在别的线程触发，这里放宽线程检查以避免误报。
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        for statement in _DDL:
            conn.execute(statement)
        _migrate(conn)
        conn.execute(
            "INSERT INTO gap_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        """关闭连接。"""

        if self._conn is not None:
            try:
                self._conn.commit()
            except Exception:
                pass
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("GapStore 尚未 open()")
        return self._conn

    # ── 键值元数据 ──

    def set_meta(self, key: str, value: str) -> None:
        """写入一条键值元数据（``gap_meta`` 表）。"""

        self.conn.execute(
            "INSERT INTO gap_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(key), str(value)),
        )
        self.conn.commit()

    def delete_meta(self, key: str) -> None:
        """删除一条键值元数据；不存在时静默返回。"""

        self.conn.execute("DELETE FROM gap_meta WHERE key = ?", (str(key),))
        self.conn.commit()

    def meta_prefix(self, prefix: str) -> dict[str, str]:
        """取出所有以 ``prefix`` 开头的键值，用于批量恢复（如记录过的发言频率原值）。"""

        rows = self.conn.execute(
            "SELECT key, value FROM gap_meta WHERE key LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    # ── gap_state ──

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM gap_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_state(
        self,
        session_id: str,
        *,
        platform: str | None = None,
        chat_type: str | None = None,
        group_id: str | None = None,
        group_name: str | None = None,
        user_id: str | None = None,
        user_nickname: str | None = None,
        last_seen_ts: float | None = None,
        last_extract_ts: float | None = None,
        pending_count: int | None = None,
    ) -> None:
        """按需更新会话状态；只覆盖显式传入的字段。"""

        now = time.time()
        self.conn.execute(
            "INSERT INTO gap_state (session_id, updated_at) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO NOTHING",
            (session_id, now),
        )
        assignments: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]
        for column, value in (
            ("platform", platform),
            ("chat_type", chat_type),
            ("group_id", group_id),
            ("group_name", group_name),
            ("user_id", user_id),
            ("user_nickname", user_nickname),
            ("last_seen_ts", last_seen_ts),
            ("last_extract_ts", last_extract_ts),
            ("pending_count", pending_count),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        values.append(session_id)
        self.conn.execute(
            f"UPDATE gap_state SET {', '.join(assignments)} WHERE session_id = ?",
            values,
        )
        self.conn.commit()

    def list_states(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM gap_state ORDER BY last_seen_ts DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    # ── gap_items ──

    def list_active(
        self,
        session_id: str,
        *,
        categories: Iterable[str] | None = None,
        now: float | None = None,
        min_salience: float = 0.0,
    ) -> list[dict[str, Any]]:
        """返回该会话当前仍有效的记忆条目，按显著度与新鲜度降序。"""

        current = time.time() if now is None else now
        sql = (
            f"SELECT {_ITEM_COLUMNS} FROM gap_items "
            "WHERE session_id = ? AND status = 'active' "
            "AND (expires_at = 0 OR expires_at > ?) AND salience >= ?"
        )
        params: list[Any] = [session_id, current, min_salience]
        category_list = [c for c in (categories or []) if c]
        if category_list:
            placeholders = ", ".join("?" for _ in category_list)
            sql += f" AND category IN ({placeholders})"
            params.extend(category_list)
        sql += " ORDER BY pinned DESC, salience DESC, last_seen_ts DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_by_category(
        self,
        session_id: str,
        category: str,
        *,
        limit: int = 50,
        include_forgotten: bool = False,
    ) -> list[dict[str, Any]]:
        """按类别列出条目。

        Args:
            session_id: 会话 id。
            category: 类别。
            limit: 最多几条。
            include_forgotten: 是否把「遗忘」的条目也算进来。去重时需要它：
                忘了的事又被提到，应该合并进原来那条并复活，而不是另起一条。

        Returns:
            条目列表。
        """

        statuses = ("active", "forgotten") if include_forgotten else ("active",)
        placeholders = ", ".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM gap_items "
            f"WHERE session_id = ? AND category = ? AND status IN ({placeholders}) "
            "ORDER BY pinned DESC, salience DESC, last_seen_ts DESC LIMIT ?",
            (session_id, category, *statuses, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_other_sessions(
        self,
        exclude_session_id: str,
        *,
        chat_types: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        now: float | None = None,
        min_salience: float = 0.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """返回**别的聊天流**里仍然有效的记忆，供跨流注入用。

        为什么要跨流：用户明确说过「Bot 刚在私聊或另一个群里聊过一件事，转头到
        另一个群就完全不记得」很出戏。记忆在写入时按会话分开是对的（不同群的
        上下文不能混），但读取时应该能流通 —— 这两件事不矛盾。

        ``chat_types`` 用来实现用户定的边界：
        - 群聊记忆 → 私聊：允许
        - 私聊记忆 → 群聊：默认关（``inject.private_to_group`` 打开后由模型自己
          决定要不要提起）

        返回的每一行额外带 ``source_session_id`` 与 ``source_group_name``，
        供上层渲染成「（在「某某群」里说过：…）」，让模型知道这话的出处。
        """

        current = time.time() if now is None else now
        # gap_items 本身没有 chat_type（聊天类型记在 gap_state），
        # 所以要用 LEFT JOIN 把 chat_type / group_name 一起带出来。
        # LEFT 而不是 INNER：条目可能在 gap_state 还没写之前就存在。
        sql = (
            f"SELECT {_ITEM_COLUMNS_ALIASED}, "
            "COALESCE(s.chat_type, '') AS source_chat_type, "
            "COALESCE(s.group_name, '') AS source_group_name, "
            "COALESCE(s.user_nickname, '') AS source_user_nickname "
            "FROM gap_items AS i LEFT JOIN gap_state AS s ON s.session_id = i.session_id "
            "WHERE i.session_id != ? AND i.status = 'active' "
            "AND (i.expires_at = 0 OR i.expires_at > ?) AND i.salience >= ?"
        )
        params: list[Any] = [exclude_session_id, current, min_salience]

        type_list = [c for c in (chat_types or []) if c]
        if type_list:
            placeholders = ", ".join("?" for _ in type_list)
            sql += f" AND COALESCE(s.chat_type, '') IN ({placeholders})"
            params.extend(type_list)

        category_list = [c for c in (categories or []) if c]
        if category_list:
            placeholders = ", ".join("?" for _ in category_list)
            sql += f" AND i.category IN ({placeholders})"
            params.extend(category_list)

        sql += " ORDER BY i.pinned DESC, i.salience DESC, i.last_seen_ts DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def session_labels(self, session_ids: Iterable[str]) -> dict[str, str]:
        """给一批会话取「人话名字」，用于跨流注入时标注出处。

        优先群名，其次私聊对方的昵称，都没有就用会话 ID 的短前缀 —— 宁可显示
        一串短码，也不要让模型看到裸 32 位 session_id。
        """

        ids = [str(s) for s in session_ids if s]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self.conn.execute(
            "SELECT session_id, chat_type, group_name, user_nickname "
            f"FROM gap_state WHERE session_id IN ({placeholders})",
            ids,
        ).fetchall()
        labels: dict[str, str] = {}
        for row in rows:
            data = dict(row)
            sid = str(data.get("session_id") or "")
            name = str(data.get("group_name") or "").strip()
            if not name and str(data.get("chat_type") or "") != "group":
                name = str(data.get("user_nickname") or "").strip()
            labels[sid] = name or sid[:8]
        for sid in ids:
            labels.setdefault(sid, sid[:8])
        return labels

    def upsert_item(
        self,
        session_id: str,
        category: str,
        content: str,
        *,
        salience: float = 0.5,
        expires_at: float = 0.0,
        truth_score: float = 1.0,
        now: float | None = None,
    ) -> tuple[int, bool]:
        """写入一条记忆；若与已有条目高度相似则合并。

        Args:
            salience: 显著度。
            expires_at: 过期时间戳，0 表示永不过期。
            truth_score: 可信度 0..1，由抽取模型判断（用户第 32 轮要求
                「辨识真假」）。低分条目仍然保存，只是注入时会被降权/标注，
                因为「模型觉得可疑」不等于「这事是假的」。

        Returns:
            ``(item_id, created)``；``created`` 为 False 表示命中已有条目并做了合并。
        """

        current = time.time() if now is None else now
        text = " ".join(str(content or "").split())
        if not text:
            raise ValueError("content 不能为空")
        truth = min(1.0, max(0.0, float(truth_score)))

        best_row: dict[str, Any] | None = None
        best_score = 0.0
        # include_forgotten=True 是必须的：遗忘的条目也要参与去重比对，
        # 否则「忘了的事又被提到」会另起一条新记录，而不是把原来那条复活。
        for row in self.list_by_category(session_id, category, include_forgotten=True):
            score = similarity(text, str(row["content"]))
            if score > best_score:
                best_score = score
                best_row = row

        if best_row is not None and best_score >= DEDUPE_SIMILARITY:
            merged_salience = max(float(best_row["salience"]), float(salience))
            # expires_at 取更晚的一个；0 视为「永不过期」，优先级最高。
            old_expiry = float(best_row["expires_at"])
            if old_expiry == 0.0 or expires_at == 0.0:
                merged_expiry = 0.0
            else:
                merged_expiry = max(old_expiry, expires_at)
            # 合并时可信度取平均：同一条事被独立提到两次，说明它更可信；
            # 一次说得很确定、一次说得含糊，合起来就是中等。
            old_truth = float(best_row.get("truth_score") or 1.0)
            merged_truth = round((old_truth + truth) / 2.0, 4)
            # 被重新提到 = 又被想起一次，遗忘的条目要复活（但置顶与遗忘
            # 是两件事，这里只改状态，不碰 pinned）。
            revived = str(best_row.get("status") or "") == "forgotten"
            # 衰减基准一并清零：``last_seen_ts`` 已经推到 current，
            # 基准清零后 ``decay_salience`` 会回落到「从 last_seen_ts 起算」，
            # 也就是「这次被提到之前的闲置不再计息」。不清的话，一条被冷落
            # 很久又重新提起的记忆会立刻按旧闲置继续掉分。
            self.conn.execute(
                "UPDATE gap_items SET salience = ?, last_seen_ts = ?, expires_at = ?, "
                "truth_score = ?, source_count = source_count + 1, updated_at = ?, "
                "salience_decayed_at = 0"
                + (", status = 'active', forgotten_at = 0" if revived else "")
                + " WHERE id = ?",
                (
                    merged_salience,
                    current,
                    merged_expiry,
                    merged_truth,
                    current,
                    best_row["id"],
                ),
            )
            self.conn.commit()
            return int(best_row["id"]), False

        cursor = self.conn.execute(
            "INSERT INTO gap_items "
            "(session_id, category, content, salience, status, first_seen_ts, "
            " last_seen_ts, expires_at, source_count, created_at, updated_at, truth_score) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 1, ?, ?, ?)",
            (
                session_id,
                category,
                text,
                float(salience),
                current,
                current,
                expires_at,
                current,
                current,
                truth,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid), True

    def resolve_items(self, item_ids: Iterable[int], *, now: float | None = None) -> int:
        """把条目标记为已解决，返回受影响行数。"""

        ids = [int(i) for i in item_ids if i is not None]
        if not ids:
            return 0
        current = time.time() if now is None else now
        placeholders = ", ".join("?" for _ in ids)
        cursor = self.conn.execute(
            f"UPDATE gap_items SET status = 'resolved', updated_at = ? "
            f"WHERE id IN ({placeholders}) AND status = 'active'",
            [current, *ids],
        )
        self.conn.commit()
        return cursor.rowcount

    def expire_stale(self, *, now: float | None = None) -> int:
        """把已过期的条目状态改为 expired，返回受影响行数。

        置顶条目跳过：用户钉住它就是不希望它因为时间到了而消失。
        """

        current = time.time() if now is None else now
        cursor = self.conn.execute(
            "UPDATE gap_items SET status = 'expired', updated_at = ? "
            "WHERE status = 'active' AND pinned = 0 "
            "AND expires_at > 0 AND expires_at <= ?",
            (current, current),
        )
        self.conn.commit()
        return cursor.rowcount

    def trim_session(self, session_id: str, max_items: int, *, now: float | None = None) -> int:
        """超出上限时把最旧、最低分的活跃条目淘汰为 expired。

        置顶的条目不参与淘汰：用户把它钉住就是不想它被挤掉。
        """

        if max_items <= 0:
            return 0
        rows = self.conn.execute(
            "SELECT id FROM gap_items WHERE session_id = ? AND status = 'active' "
            "AND pinned = 0 "
            "ORDER BY salience DESC, last_seen_ts DESC LIMIT -1 OFFSET ?",
            (session_id, max_items),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return 0
        current = time.time() if now is None else now
        placeholders = ", ".join("?" for _ in ids)
        cursor = self.conn.execute(
            f"UPDATE gap_items SET status = 'expired', updated_at = ? WHERE id IN ({placeholders})",
            [current, *ids],
        )
        self.conn.commit()
        return cursor.rowcount

    def delete_session_items(self, session_id: str) -> int:
        cursor = self.conn.execute("DELETE FROM gap_items WHERE session_id = ?", (session_id,))
        self.conn.commit()
        return cursor.rowcount

    # ── 记忆生命周期：衰减 / 遗忘 / 想起 / 置顶 ──
    #
    # 用户第 32 轮的原话：「让记忆随时间衰减、遗忘（标注为遗忘，并非删除，
    # 不主动读取，只有他人明确强调才会想起）。增加记忆置顶功能，置顶的记忆
    # 不会被遗忘。」
    #
    # 所以这里有三个独立的动作，不要混在一起：
    #   decay_salience  —— 时间把显著度往下压（衰减），压到阈值以下转 forgotten
    #   recall_item     —— 被明确提起时把 forgotten 拉回 active（想起）
    #   set_pinned      —— 置顶，衰减与遗忘都跳过它
    #
    # 为什么不直接删：用户要的是「想不起来」而不是「没发生过」。删掉就再也
    # 捞不回来了，而遗忘态还能被一句话唤醒。

    def decay_salience(
        self,
        *,
        now: float | None = None,
        half_life_days: float = 45.0,
        forget_below: float = 0.12,
        forget_after_days: float = 30.0,
        min_age_days: float = 1.0,
    ) -> tuple[int, int]:
        """按时间衰减显著度，并把淡到阈值的条目标记为遗忘。

        衰减用半衰期：``salience *= 0.5 ** (本次流逝天数 / half_life_days)``。

        ## 为什么基准不是 ``last_seen_ts``

        这里修的是一个**真 bug**，而且它在真机上毁过数据。

        原来每一轮都拿 ``last_seen_ts`` 现算「这条闲置了多久」，乘完系数就把
        结果写回，**却没把基准往前推**。于是同一段闲置时间会被反复计息：
        ``decay_salience`` 被 ``cg_tracker.tick`` 每 ``refresh_tick_seconds``
        （默认 30 秒）调用一次，一条闲置 25 小时的记忆在 1 小时内会被连续
        衰减 120 次，从 0.7 掉到 0.118 并被标成遗忘 —— 而按设计它 25 小时
        只该掉 0.03%。

        修法是把「上次衰减到哪儿」单独记进 ``salience_decayed_at``，每次只对
        「上次衰减之后**新流逝的**时间」计息。数学上：

        ```
        ∏ 0.5 ** (Δt_i / T)  ==  0.5 ** (∑Δt_i / T)  ==  0.5 ** ((now - 基准) / T)
        ```

        所以**调多少次都不改变结果**，只和时间总跨度有关。这也是
        ``decay_person_axes`` 早就在用的做法（它用 ``axes_decayed_at``）——
        关系六维一直是对的，只有记忆这一侧漏了。

        ``last_seen_ts`` 仍然有用：它表示「最近一次被提到」，条目被重新提到时
        基准要跟着重置（见 :meth:`upsert_item` 与 :meth:`recall_item`），
        这样「常用的事记得牢」依然成立。

        Args:
            now: 当前时间。
            half_life_days: 多少天掉一半；``<= 0`` 表示关闭衰减。
            forget_below: 衰减后低于这个显著度就转 ``forgotten``。
            forget_after_days: 闲置超过这么多天也直接遗忘（兜底：显著度本来就
                低的条目可能衰减很久都还在阈值上）。
            min_age_days: 写入不足这么多天的条目不动，避免刚存进来就被压。

        Returns:
            ``(decayed, forgotten)`` 两个计数。
        """

        if half_life_days <= 0:
            return 0, 0
        current = time.time() if now is None else now
        day = 86400.0
        min_age = max(0.0, float(min_age_days)) * day
        life = float(half_life_days) * day

        rows = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM gap_items "
            "WHERE status = 'active' AND pinned = 0"
        ).fetchall()

        decayed = 0
        forgotten_ids: list[int] = []
        for raw in rows:
            row = dict(raw)
            item_id = int(row["id"])
            seen = float(row.get("last_seen_ts") or 0.0) or float(row.get("created_at") or 0.0)
            if seen <= 0.0:
                continue
            # 「上次衰减到哪儿」：从没衰减过就从 last_seen_ts 起算。
            # 基准不允许早于 last_seen_ts —— 条目被重新提到之后，之前那段
            # 闲置就不该再算衰减了。
            mark = float(row.get("salience_decayed_at") or 0.0)
            base = max(seen, mark) if mark > 0.0 else seen
            idle_total = current - seen
            step = current - base

            # 总闲置时长还不到一天：这条本来就还没到该衰减的时候。
            if idle_total < min_age:
                continue

            old = float(row.get("salience") or 0.0)
            if step <= 0.0:
                new = old
            else:
                # **不取整**。原来这里是 ``round(old * factor, 4)``，问题在于
                # 取整的残差会被永久丢弃：每写一次库，小数第 5 位以后就没了，
                # 而写库同时会把基准推到 current —— 于是被截掉的那部分再也
                # 算不回来。实测每 30 秒一次、跑满一天，0.7 会掉到 0.6684，
                # 而正确值 0.6582（偏差 1%，且随运行时长单调增大）。
                #
                # 保留完整浮点精度写库，只用一个「够不够明显」的阈值控制写盘
                # 频率：不足 EPSILON 就先不写、也**不推进基准**，让这段时间
                # 继续累加在 ``step`` 里，下一轮一并计入。这样精度无损，
                # 写库频率也和原来相当（约十几分钟一次）。
                new = old * (0.5 ** (step / life))
                if old - new >= _DECAY_EPSILON:
                    self.conn.execute(
                        "UPDATE gap_items SET salience = ?, updated_at = ?, "
                        "salience_decayed_at = ? WHERE id = ?",
                        (new, current, current, item_id),
                    )
                    decayed += 1
                else:
                    new = old

            if new < forget_below or idle_total > forget_after_days * day:
                forgotten_ids.append(item_id)

        if forgotten_ids:
            placeholders = ", ".join("?" for _ in forgotten_ids)
            self.conn.execute(
                f"UPDATE gap_items SET status = 'forgotten', forgotten_at = ?, updated_at = ?, "
                f"salience_decayed_at = ? WHERE id IN ({placeholders})",
                [current, current, current, *forgotten_ids],
            )
        if decayed or forgotten_ids:
            self.conn.commit()
        return decayed, len(forgotten_ids)

    def recall_item(self, item_id: int, *, now: float | None = None, boost: float = 0.15) -> bool:
        """把一条记忆「想起来」，返回是否真的改了状态。

        遗忘态会回到 active；显著度往上抬一点（``boost``），因为被重新提起
        说明它还有意义。已经 active 的条目只更新 ``last_recalled_ts``，这样
        它不会继续被衰减压下去。
        """

        current = time.time() if now is None else now
        row = self.get_item(item_id)
        if row is None:
            return False
        salience = min(1.0, float(row.get("salience") or 0.0) + max(0.0, float(boost)))
        status = "active" if str(row.get("status") or "") == "forgotten" else str(row.get("status") or "active")
        # 和 upsert_item 同理：想起来了就不再按旧闲置计息，基准清零。
        cursor = self.conn.execute(
            "UPDATE gap_items SET status = ?, salience = ?, last_recalled_ts = ?, "
            "last_seen_ts = ?, forgotten_at = 0, salience_decayed_at = 0, updated_at = ? "
            "WHERE id = ?",
            (status, salience, current, current, current, int(item_id)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_pinned(self, item_id: int, pinned: bool, *, now: float | None = None) -> bool:
        """置顶 / 取消置顶。置顶的条目不会被衰减与遗忘。"""

        current = time.time() if now is None else now
        fields = "pinned = ?, updated_at = ?"
        params: list[Any] = [1 if pinned else 0, current]
        # 取消置顶时顺手把遗忘时间清掉；置顶本身也说明它还有意义，
        # 所以置顶时把 forgotten 拉回 active。
        if pinned:
            fields += ", status = CASE WHEN status = 'forgotten' THEN 'active' ELSE status END"
            fields += ", forgotten_at = 0"
        cursor = self.conn.execute(
            f"UPDATE gap_items SET {fields} WHERE id = ?", [*params, int(item_id)]
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def list_forgotten(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """列出该会话里被遗忘的条目，供「被明确强调时想起来」的匹配用。"""

        rows = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM gap_items "
            "WHERE session_id = ? AND status = 'forgotten' "
            "ORDER BY last_seen_ts DESC LIMIT ?",
            (str(session_id), max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── 管理界面用的读写接口 ──
    #
    # 这一组方法是给 WebUI 管理页面用的：允许人工浏览、修改、新增、删除条目。
    # 它们和 upsert_item 的区别是**不做相似度去重**——人工操作要的是所见即所得，
    # 不该因为文本相似就被静默合并到别的条目里。

    @staticmethod
    def _like_pattern(text: str) -> str:
        """把用户输入转成 LIKE 模式，转义 ``%`` / ``_`` / ``\\``。"""

        escaped = str(text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def list_items(
        self,
        *,
        session_id: str = "",
        status: str = "",
        category: str = "",
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """按条件分页列出条目，返回 ``(rows, total)``。

        Args:
            session_id: 非空时只列该会话。
            status: 非空时只列该状态。
            category: 非空时只列该类别。
            search: 非空时按内容做大小写不敏感的子串匹配。
            limit: 单页条数，调用方负责夹紧范围。
            offset: 偏移量。

        Returns:
            ``(rows, total)``；``total`` 是不分页的总命中数。
        """

        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(str(session_id))
        if status:
            where.append("status = ?")
            params.append(str(status))
        if category:
            where.append("category = ?")
            params.append(str(category))
        if search:
            where.append("content LIKE ? ESCAPE '\\'")
            params.append(self._like_pattern(search))

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total_row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM gap_items{clause}", params
        ).fetchone()
        total = int(total_row["n"]) if total_row is not None else 0

        rows = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM gap_items{clause} "
            "ORDER BY salience DESC, last_seen_ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        ).fetchall()
        return [dict(row) for row in rows], total

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        """按主键读一条，不存在返回 None。"""

        row = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM gap_items WHERE id = ?", (int(item_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def update_item(
        self,
        item_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
        salience: float | None = None,
        status: str | None = None,
        pinned: bool | None = None,
        truth_score: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """按字段局部更新一条；只更新显式传入的字段。

        Returns:
            更新后的条目；条目不存在时返回 None。
        """

        fields: list[str] = []
        params: list[Any] = []
        # 先把 status 归一化出来，因为 pinned 那条分支要判断「调用方有没有
        # 显式指定状态」—— 指定了就以它为准，没指定才由 pinned 推导。
        status_value: str | None = None
        if status is not None:
            status_value = str(status).strip()
            if status_value not in STATUSES:
                raise ValueError(f"未知状态：{status_value}")
        if content is not None:
            text = " ".join(str(content).split())
            if not text:
                raise ValueError("内容不能为空")
            fields.append("content = ?")
            params.append(text)
        if category is not None:
            value = str(category).strip()
            if value not in CATEGORIES:
                raise ValueError(f"未知类别：{value}")
            fields.append("category = ?")
            params.append(value)
        if salience is not None:
            score = float(salience)
            if not 0.0 <= score <= 1.0:
                raise ValueError("显著度必须在 0 到 1 之间")
            fields.append("salience = ?")
            params.append(score)
        if truth_score is not None:
            score = float(truth_score)
            if not 0.0 <= score <= 1.0:
                raise ValueError("可信度必须在 0 到 1 之间")
            fields.append("truth_score = ?")
            params.append(score)
        if pinned is not None:
            fields.append("pinned = ?")
            params.append(1 if pinned else 0)
            if pinned:
                # 置顶 =「这条很重要」，和 :meth:`set_pinned` 保持**同一套语义**：
                # 把遗忘态**真正拉回 active**，而不是只清掉遗忘时间戳。
                #
                # 为什么这一行不能省：``list_active()`` 的 SQL 是
                # ``WHERE status = 'active'``，状态不改的话条目仍然注入不进去；
                # 而 ``decay_salience`` 的条件是 ``status = 'active' AND pinned = 0``，
                # 于是这条记忆既不会被读、也不会被衰减，卡在一个再也回不来的
                # 状态里 —— 用户看到的就是「点了置顶但没反应」。
                if status_value is None:
                    fields.append(
                        "status = CASE WHEN status = 'forgotten' THEN 'active' ELSE status END"
                    )
                fields.append("forgotten_at = 0")
        if status_value is not None:
            fields.append("status = ?")
            params.append(status_value)
            if status_value == "forgotten":
                fields.append("forgotten_at = ?")
                params.append(time.time() if now is None else now)
            elif status_value == "active":
                fields.append("forgotten_at = 0")
                # 被手动恢复成 active 的条目不该继续按「之前那段闲置」掉分，
                # 否则在页面点一下「想起来」，下一次 tick 又会被打回去。
                fields.append("salience_decayed_at = 0")

        if not fields:
            return self.get_item(item_id)

        current = time.time() if now is None else now
        fields.append("updated_at = ?")
        params.append(current)
        params.append(int(item_id))
        cursor = self.conn.execute(
            f"UPDATE gap_items SET {', '.join(fields)} WHERE id = ?", params
        )
        self.conn.commit()
        if cursor.rowcount <= 0:
            return None
        return self.get_item(item_id)

    def insert_item(
        self,
        session_id: str,
        category: str,
        content: str,
        *,
        salience: float = 0.6,
        status: str = "active",
        pinned: bool = False,
        truth_score: float = 1.0,
        now: float | None = None,
    ) -> int:
        """人工新增一条（不做相似度去重），返回新条目 id。"""

        text = " ".join(str(content or "").split())
        if not text:
            raise ValueError("内容不能为空")
        category_value = str(category or "").strip()
        if category_value not in CATEGORIES:
            raise ValueError(f"未知类别：{category_value}")
        status_value = str(status or "active").strip()
        if status_value not in STATUSES:
            raise ValueError(f"未知状态：{status_value}")
        score = float(salience)
        if not 0.0 <= score <= 1.0:
            raise ValueError("显著度必须在 0 到 1 之间")
        truth = min(1.0, max(0.0, float(truth_score)))

        current = time.time() if now is None else now
        cursor = self.conn.execute(
            "INSERT INTO gap_items "
            "(session_id, category, content, salience, status, first_seen_ts, "
            " last_seen_ts, expires_at, source_count, created_at, updated_at, "
            " pinned, truth_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?)",
            (
                str(session_id),
                category_value,
                text,
                score,
                status_value,
                current,
                current,
                current,
                current,
                1 if pinned else 0,
                truth,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def delete_item(self, item_id: int) -> bool:
        """删除单条，返回是否真的删掉了。"""

        cursor = self.conn.execute("DELETE FROM gap_items WHERE id = ?", (int(item_id),))
        self.conn.commit()
        return cursor.rowcount > 0

    def session_overview(self) -> list[dict[str, Any]]:
        """列出所有会话及其条目计数，供管理界面的会话下拉框使用。"""

        rows = self.conn.execute(
            "SELECT s.session_id, s.platform, s.chat_type, s.group_id, s.group_name, "
            "       s.user_id, s.user_nickname, s.last_seen_ts, s.last_extract_ts, "
            "       s.pending_count, "
            "       SUM(CASE WHEN i.status = 'active' THEN 1 ELSE 0 END) AS active_count, "
            "       SUM(CASE WHEN i.status = 'resolved' THEN 1 ELSE 0 END) AS resolved_count, "
            "       SUM(CASE WHEN i.status = 'expired' THEN 1 ELSE 0 END) AS expired_count, "
            "       SUM(CASE WHEN i.status = 'forgotten' THEN 1 ELSE 0 END) AS forgotten_count, "
            "       SUM(CASE WHEN i.pinned = 1 THEN 1 ELSE 0 END) AS pinned_count, "
            "       COUNT(i.id) AS total_count "
            "FROM gap_state s LEFT JOIN gap_items i ON i.session_id = s.session_id "
            "GROUP BY s.session_id "
            "ORDER BY s.last_seen_ts DESC"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in (
                "active_count",
                "resolved_count",
                "expired_count",
                "forgotten_count",
                "pinned_count",
                "total_count",
            ):
                item[key] = int(item.get(key) or 0)
            result.append(item)
        return result

    # ── 日志表 ──

    def record_extraction(
        self,
        session_id: str,
        *,
        model: str = "",
        ok: bool = False,
        item_count: int = 0,
        resolved_count: int = 0,
        prompt_chars: int = 0,
        response_chars: int = 0,
        error: str = "",
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else now
        self.conn.execute(
            "INSERT INTO gap_extractions "
            "(session_id, ts, model, ok, item_count, resolved_count, prompt_chars, response_chars, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                current,
                str(model or ""),
                1 if ok else 0,
                int(item_count),
                int(resolved_count),
                int(prompt_chars),
                int(response_chars),
                str(error or "")[:2000],
            ),
        )
        self.conn.commit()

    def record_injection(
        self,
        session_id: str,
        *,
        chars: int,
        item_count: int,
        categories: str = "",
        elapsed_ms: float = 0.0,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else now
        self.conn.execute(
            "INSERT INTO gap_injections (session_id, ts, chars, item_count, categories, elapsed_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, current, int(chars), int(item_count), str(categories or ""), float(elapsed_ms)),
        )
        self.conn.commit()

    def mark_injected(
        self,
        item_ids: Iterable[int],
        *,
        now: float | None = None,
        decay: float = 0.92,
    ) -> int:
        """把「这一轮真的注入出去的条目」记账。

        **必须只对真正写进提示词的条目调用**，不是「取出来考虑过的」。
        ``compose_block`` 会因为预算不足只收前几条，取 20 条只用 6 条是常态；
        如果对整批候选都记账，第 7 条以后就永远在「刚说过」的冷却里，
        轮换会彻底卡死。

        记账同时做两件互相配套的事：

        - ``last_injected_ts`` / ``inject_streak`` / ``inject_count`` 更新，
          让它下一轮排在后面（轮换的推力）。
        - 显著度按 ``decay`` 微降，让「反复被说」本身成为一种损耗。
          不降的话，高分条目靠显著度排序能一直压住新条目：轮换字段只能
          改变同一类别内的顺序，压不住跨类别的分数差。

        但降幅必须很小（默认 0.92）：注入不是遗忘，它只是「这一轮用掉了」。
        真正的遗忘由 ``decay_salience`` 按时间做，那里才是大刀子。

        Args:
            item_ids: 真实注入的条目 id。
            decay: 每注入一轮的显著度衰减系数，1.0 表示不衰减。

        Returns:
            实际记账的条目数。
        """

        ids = [int(item_id) for item_id in item_ids]
        if not ids:
            return 0
        current = time.time() if now is None else now
        factor = min(1.0, max(0.0, float(decay)))
        placeholders = ", ".join("?" for _ in ids)
        self.conn.execute(
            "UPDATE gap_items SET "
            "last_injected_ts = ?, "
            "inject_streak = inject_streak + 1, "
            "inject_count = inject_count + 1, "
            "salience = MAX(0.0, ROUND(salience * ?, 4)) "
            f"WHERE id IN ({placeholders})",
            (current, factor, *ids),
        )
        self.conn.commit()
        return len(ids)

    def reset_inject_streaks(
        self,
        *,
        now: float | None = None,
        gap_seconds: float = 21600.0,
    ) -> int:
        """把「很久没被注入」的条目的连击清零。

        连击要能自己断掉：一条记忆连着 5 轮出现，然后因为别的事情插进来
        安静了 6 小时，它再出现时应当算「重新提起」，而不是接着 +1 变成 6。
        否则 ``inject_streak`` 一路涨上去，这条记忆就被永久打入冷宫了。
        """

        current = time.time() if now is None else now
        cutoff = current - max(0.0, float(gap_seconds))
        cursor = self.conn.execute(
            "UPDATE gap_items SET inject_streak = 0 "
            "WHERE inject_streak > 0 AND last_injected_ts > 0 AND last_injected_ts < ?",
            (cutoff,),
        )
        if cursor.rowcount:
            self.conn.commit()
            return int(cursor.rowcount)
        return 0

    def session_stats(self, session_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT category, status, COUNT(*) AS n FROM gap_items "
            "WHERE session_id = ? GROUP BY category, status",
            (session_id,),
        ).fetchall()
        by_category: dict[str, dict[str, int]] = {}
        for item in row:
            by_category.setdefault(str(item["category"]), {})[str(item["status"])] = int(item["n"])
        last = self.conn.execute(
            "SELECT ts, ok, item_count, error FROM gap_extractions "
            "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return {
            "session_id": session_id,
            "by_category": by_category,
            "last_extraction": dict(last) if last is not None else None,
        }

    def global_stats(self) -> dict[str, Any]:
        items = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM gap_items GROUP BY status"
        ).fetchall()
        pinned = self.conn.execute(
            "SELECT COUNT(*) AS n FROM gap_items WHERE pinned = 1"
        ).fetchone()
        # 「存疑」的条目单独统计：抽取模型自己说不太确定（truth_score 低）。
        # 这些条目不是假的，只是不能当成事实到处讲，所以既不删也不藏，
        # 只是页面上标出来、注入时降权。
        doubted = self.conn.execute(
            "SELECT COUNT(*) AS n FROM gap_items WHERE truth_score < 0.5"
        ).fetchone()
        sessions = self.conn.execute("SELECT COUNT(*) AS n FROM gap_state").fetchone()
        extractions = self.conn.execute(
            "SELECT COUNT(*) AS n, SUM(ok) AS ok_n FROM gap_extractions"
        ).fetchone()
        return {
            "sessions": int(sessions["n"]) if sessions is not None else 0,
            "items_by_status": {str(row["status"]): int(row["n"]) for row in items},
            "items_pinned": int(pinned["n"]) if pinned is not None else 0,
            "items_doubted": int(doubted["n"]) if doubted is not None else 0,
            "extractions": int(extractions["n"]) if extractions is not None else 0,
            "extractions_ok": int(extractions["ok_n"] or 0) if extractions is not None else 0,
            "db_path": self.db_path,
        }

    # ── 情绪 ──

    def get_mood(self, scope: str = "global") -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM gap_mood WHERE scope = ?",
            (str(scope),),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_mood(
        self,
        *,
        valence: float,
        arousal: float,
        energy: float,
        label: str = "",
        reason: str = "",
        exhausted: bool = False,
        scope: str = "global",
        now: float | None = None,
        **primitives: float,
    ) -> None:
        """写入情绪状态（单行 upsert）。

        ``primitives`` 是八个基元（``joy`` / ``anger`` / …）；没给的就用默认
        基线。写成 ``**primitives`` 而不是显式参数，是为了让老的调用方
        （只传三维）仍然能跑通——升级过程中可能短暂不一致。
        """

        current = time.time() if now is None else now
        values: list[float] = []
        for name, default in _EMOTION_DEFAULTS:
            raw = primitives.get(name, default)
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = default
            if number != number:  # NaN
                number = default
            values.append(min(1.0, max(0.0, number)))

        columns = ", ".join(EMOTION_COLUMNS)
        placeholders = ", ".join("?" for _ in EMOTION_COLUMNS)
        updates = ", ".join(f"{column} = excluded.{column}" for column in EMOTION_COLUMNS)
        self.conn.execute(
            "INSERT INTO gap_mood "
            f"(scope, valence, arousal, energy, label, reason, updated_at, exhausted, {columns}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {placeholders}) "
            "ON CONFLICT(scope) DO UPDATE SET "
            "valence = excluded.valence, arousal = excluded.arousal, "
            "energy = excluded.energy, label = excluded.label, "
            "reason = excluded.reason, updated_at = excluded.updated_at, "
            f"exhausted = excluded.exhausted, {updates}",
            (
                str(scope),
                float(valence),
                float(arousal),
                float(energy),
                str(label or "")[:40],
                str(reason or "")[:200],
                current,
                1 if exhausted else 0,
                *values,
            ),
        )
        self.conn.commit()

    def record_mood(
        self,
        *,
        valence: float,
        arousal: float,
        energy: float,
        label: str = "",
        source: str = "",
        reason: str = "",
        exhausted: bool = False,
        now: float | None = None,
        **primitives: float,
    ) -> None:
        """追加一条情绪历史。"""

        current = time.time() if now is None else now
        values: list[float] = []
        for name, default in _EMOTION_DEFAULTS:
            raw = primitives.get(name, default)
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = default
            if number != number:  # NaN
                number = default
            values.append(min(1.0, max(0.0, number)))

        columns = ", ".join(EMOTION_COLUMNS)
        placeholders = ", ".join("?" for _ in EMOTION_COLUMNS)
        self.conn.execute(
            "INSERT INTO gap_mood_log "
            f"(ts, valence, arousal, energy, label, source, reason, exhausted, {columns}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {placeholders})",
            (
                current,
                float(valence),
                float(arousal),
                float(energy),
                str(label or "")[:40],
                str(source or "")[:40],
                str(reason or "")[:200],
                1 if exhausted else 0,
                *values,
            ),
        )
        self.conn.commit()

    def mood_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """按时间倒序返回情绪历史。"""

        rows = self.conn.execute(
            "SELECT * FROM gap_mood_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_mood_log(self, *, keep: int = 2000) -> int:
        """只保留最近 ``keep`` 条历史，返回删除行数。"""

        keep = max(1, int(keep))
        cursor = self.conn.execute(
            "DELETE FROM gap_mood_log WHERE id NOT IN "
            "(SELECT id FROM gap_mood_log ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    # ── 人物认知 ──

    @staticmethod
    def _person_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        raw_tags = data.get("tags")
        try:
            parsed = json.loads(raw_tags) if isinstance(raw_tags, str) and raw_tags else []
        except (json.JSONDecodeError, TypeError):
            parsed = []
        data["tags"] = [str(item) for item in parsed] if isinstance(parsed, list) else []
        # 六维存成 JSON 文本。解析失败就留空字符串，交给 ``axes_of`` 按旧
        # affection 反推 —— 宁可退化到旧行为，也不要抛异常让整个页面挂掉。
        raw_axes = data.get("axes")
        if isinstance(raw_axes, str) and raw_axes.strip():
            try:
                loaded = json.loads(raw_axes)
            except (json.JSONDecodeError, TypeError):
                loaded = None
            data["axes"] = loaded if isinstance(loaded, dict) else {}
        elif not isinstance(raw_axes, dict):
            data["axes"] = {}
        # 六维的来源（auto / manual）。老库没有这一列或值非法时按 auto 处理：
        # 宁可按「感受跟着聊天走」的默认行为，也不要凭空让一批人被锁死。
        source = str(data.get("axes_source") or "").strip().lower()
        data["axes_source"] = source if source in ("auto", "manual") else "auto"
        # 老的 notes 列还留在表里（SQLite 不能删列），读出来丢掉，
        # 免得上层拿到两份互相矛盾的备注。
        data.pop("notes", None)
        return data

    def get_person(self, person_key: str) -> dict[str, Any] | None:
        """按主键取一个人物，不存在返回 ``None``。"""

        key = str(person_key or "").strip()
        if not key:
            return None
        row = self.conn.execute(
            "SELECT * FROM gap_person WHERE person_key = ?", (key,)
        ).fetchone()
        return self._person_row(row)

    def upsert_person(self, person: dict[str, Any], *, now: float | None = None) -> None:
        """写入人物记录。

        ``tags`` 存成 JSON 文本 —— 用逗号分隔会在标签本身含逗号时出错。
        """

        key = str(person.get("person_key") or "").strip()
        if not key:
            return
        stamp = float(now if now is not None else time.time())
        tags = person.get("tags") or []
        tags_text = json.dumps(
            [str(item) for item in tags] if isinstance(tags, list) else [],
            ensure_ascii=False,
        )
        first_seen = float(person.get("first_seen_ts") or stamp)
        axes = person.get("axes") or {}
        axes_text = json.dumps(
            {str(k): float(v) for k, v in axes.items()} if isinstance(axes, dict) else {},
            ensure_ascii=False,
        )
        # 六维是谁写的。缺省 ``auto``：现有调用方（抽取、衰减、迁移）都是
        # 自动写的，只有页面/命令的手动入口会显式传 ``manual``。
        axes_source = str(person.get("axes_source") or "auto").strip().lower()
        if axes_source not in ("auto", "manual"):
            axes_source = "auto"
        self.conn.execute(
            "INSERT INTO gap_person ("
            "person_key, platform, user_id, display_name, relation_role, "
            "role_confidence, role_source, affection, emotional_impact, tags, "
            "axes, axes_source, first_seen_ts, last_seen_ts, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(person_key) DO UPDATE SET "
            "platform = excluded.platform, "
            "user_id = excluded.user_id, "
            "display_name = excluded.display_name, "
            "relation_role = excluded.relation_role, "
            "role_confidence = excluded.role_confidence, "
            "role_source = excluded.role_source, "
            "affection = excluded.affection, "
            "emotional_impact = excluded.emotional_impact, "
            "tags = excluded.tags, "
            "axes = excluded.axes, "
            "axes_source = excluded.axes_source, "
            "last_seen_ts = excluded.last_seen_ts, "
            "updated_at = excluded.updated_at",
            (
                key,
                str(person.get("platform") or ""),
                str(person.get("user_id") or ""),
                str(person.get("display_name") or "")[:32],
                str(person.get("relation_role") or "stranger"),
                float(person.get("role_confidence") or 0.0),
                str(person.get("role_source") or "auto"),
                float(person.get("affection") or 0.0),
                float(person.get("emotional_impact") or 0.0),
                tags_text,
                axes_text,
                axes_source,
                first_seen,
                float(person.get("last_seen_ts") or stamp),
                stamp,
            ),
        )
        self.conn.commit()

    def list_people(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """按「最近见过 + 好感度」排序返回人物列表。"""

        rows = self.conn.execute(
            "SELECT * FROM gap_person ORDER BY updated_at DESC, affection DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [item for item in (self._person_row(row) for row in rows) if item]

    def delete_person(self, person_key: str) -> bool:
        key = str(person_key or "").strip()
        if not key:
            return False
        # 关系边也要一起删：留着就是悬空引用，下次算第三方影响时
        # 会拿到一个已经不存在的人当来源。
        self.conn.execute(
            "DELETE FROM gap_person_links WHERE source_key = ? OR target_key = ?",
            (key, key),
        )
        cursor = self.conn.execute("DELETE FROM gap_person WHERE person_key = ?", (key,))
        self.conn.commit()
        return bool(cursor.rowcount)

    def decay_person_axes(self, *, half_life_days: float, now: float | None = None) -> int:
        """让久未联系的人六维往中性回落。

        为什么必须做：六维是「感受」，感受会随时间变淡。一个人半年没说话，
        你对他的亲近和信任本来就该回到中位，而不是停在半年前那个峰值上。
        熟悉度不衰减 —— 聊过的次数是历史事实，不会因为一段时间没说话归零。

        衰减基准用 ``axes_decayed_at``（上次衰减的时刻），**不动 ``updated_at``**：
        后者是「最近一次互动」，衰减不该把一个人往前排，否则越冷落的人越靠前。

        ``axes_source == 'manual'`` 的行整行跳过：用户手调过的六维不衰减。
        用户第 43 轮明确抱怨过「我调好的值自己变回去了」，手动值就该停住。

        Returns:
            实际改动的人数。
        """

        from .cg_person import AXIS_HALF_LIFE_DAYS, axes_of, decay_axes, affection_from_axes

        life = float(half_life_days or AXIS_HALF_LIFE_DAYS)
        if life <= 0:
            return 0
        stamp = float(now if now is not None else time.time())
        rows = self.conn.execute(
            "SELECT person_key, axes, axes_source, affection, axes_decayed_at, updated_at "
            "FROM gap_person"
        ).fetchall()
        changed = 0
        for row in rows:
            key = str(row["person_key"] or "")
            if not key:
                continue
            # 手动锁住的六维不参与衰减。用户拿着滑杆定下的关系，不该因为
            # 一段时间没聊天就被程序悄悄改掉 —— 那正是「我调的值又变了」。
            if str(row["axes_source"] or "").strip().lower() == "manual":
                continue
            # 上次衰减时刻；从没衰减过就用 updated_at 当起点。
            base_ts = float(row["axes_decayed_at"] or 0.0) or float(row["updated_at"] or 0.0)
            if base_ts <= 0:
                self.conn.execute(
                    "UPDATE gap_person SET axes_decayed_at = ? WHERE person_key = ?",
                    (stamp, key),
                )
                continue
            idle_days = (stamp - base_ts) / 86400.0
            if idle_days < 1.0:
                continue
            before = axes_of(self._person_row(row) or {})
            after = decay_axes(before, idle_days=idle_days)
            if all(abs(after[k] - before[k]) < 1e-4 for k in before):
                # 没实际变化也要推进基准，否则 idle 会越攒越大，下次一次性砸下来。
                self.conn.execute(
                    "UPDATE gap_person SET axes_decayed_at = ? WHERE person_key = ?",
                    (stamp, key),
                )
                continue
            self.conn.execute(
                "UPDATE gap_person SET axes = ?, affection = ?, axes_decayed_at = ? "
                "WHERE person_key = ?",
                (
                    json.dumps(after, ensure_ascii=False),
                    affection_from_axes(after),
                    stamp,
                    key,
                ),
            )
            changed += 1
        if changed:
            self.conn.commit()
        return changed

    # ── 关系网络（第三方影响）────────────────────────────────────────
    #
    # 用户第 34 轮说关系「还会受到其他人的影响」。这张表记的就是那条影响：
    # 「A 提到 B 的时候是什么态度」。同一个 (source, target) 只留一行，
    # 反复提到会把 ``attitude`` 往指数衰减滑动平均上叠（和 ``emotional_impact``
    # 同一套思路：最近的说法权重更大，人也可能改口）。

    def upsert_person_link(
        self,
        source_key: str,
        target_key: str,
        attitude: Any,
        *,
        evidence: str = "",
        decay: float = 0.75,
        now: float | None = None,
    ) -> None:
        """记录一条「谁对谁什么态度」。

        同一对 (source, target) 是**累积**的：反复说坏话会越来越负，但用
        衰减平均而不是直接相加 —— 说一百次也不会把数值顶到极限，且改口
        能拉回来。
        """

        src = str(source_key or "").strip()
        dst = str(target_key or "").strip()
        if not src or not dst or src == dst:
            # 自己说自己的不算第三方影响。
            return
        try:
            value = float(attitude)
        except (TypeError, ValueError):
            return
        if value != value:
            return
        value = max(-1.0, min(1.0, value))
        stamp = float(now if now is not None else time.time())
        key = f"{src}|{dst}"

        row = self.conn.execute(
            "SELECT attitude, seen_count FROM gap_person_links WHERE link_key = ?", (key,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO gap_person_links ("
                "link_key, source_key, target_key, attitude, evidence, "
                "seen_count, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (key, src, dst, value, str(evidence or "")[:80], stamp, stamp),
            )
        else:
            previous = float(row["attitude"] or 0.0)
            count = int(row["seen_count"] or 0) + 1
            merged = previous * float(decay) + value
            merged = max(-1.0, min(1.0, merged))
            self.conn.execute(
                "UPDATE gap_person_links SET attitude = ?, evidence = ?, "
                "seen_count = ?, updated_at = ? WHERE link_key = ?",
                (merged, str(evidence or "")[:80], count, stamp, key),
            )
        self.conn.commit()

    def list_person_links(self, target_key: str) -> list[dict[str, Any]]:
        """取「别人对某个人的态度」，按说话人与目标的熟悉度无关、按时间新到旧。"""

        rows = self.conn.execute(
            "SELECT * FROM gap_person_links WHERE target_key = ? "
            "ORDER BY updated_at DESC",
            (str(target_key or "").strip(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_outgoing_links(self, source_key: str) -> list[dict[str, Any]]:
        """取「某个人对别人的态度」。"""

        rows = self.conn.execute(
            "SELECT * FROM gap_person_links WHERE source_key = ? "
            "ORDER BY updated_at DESC",
            (str(source_key or "").strip(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_person_links(self, person_key: str) -> int:
        """删掉一个人相关的所有关系边（删人时一起清理，免得留悬空引用）。"""

        key = str(person_key or "").strip()
        if not key:
            return 0
        cursor = self.conn.execute(
            "DELETE FROM gap_person_links WHERE source_key = ? OR target_key = ?",
            (key, key),
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    # ── 喜好与厌恶 ──────────────────────────────────────────────────
    #
    # 和人物一样是「关于她自己的」数据，但少了关系维度、多了取向维度：
    # 同一条话题可能同时有「喜欢猫」和「讨厌猫毛」，所以主键是
    # ``kind:topic`` 而不是单纯 ``topic``。

    @staticmethod
    def _pref_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    def get_pref(self, pref_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM gap_prefs WHERE pref_key = ?", (str(pref_key or "").strip(),)
        ).fetchone()
        return self._pref_row(row)

    def upsert_pref(self, pref: dict[str, Any], *, now: float | None = None) -> None:
        """写入或更新一条喜好。

        ``created_at`` 在冲突时保持原值：它记录的是「这个口味是什么时候
        长出来的」，每次更新都刷新就没有意义了。
        """

        current = time.time() if now is None else float(now)
        key = str(pref.get("pref_key") or "").strip()
        if not key:
            raise ValueError("upsert_pref 需要 pref_key")
        created = float(pref.get("created_at") or current)
        self.conn.execute(
            "INSERT INTO gap_prefs "
            "(pref_key, kind, topic, strength, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pref_key) DO UPDATE SET "
            "kind = excluded.kind, topic = excluded.topic, "
            "strength = excluded.strength, source = excluded.source, "
            "updated_at = excluded.updated_at",
            (
                key,
                str(pref.get("kind") or "like"),
                str(pref.get("topic") or ""),
                float(pref.get("strength") or 0.5),
                str(pref.get("source") or "chat"),
                created,
                current,
            ),
        )
        self.conn.commit()

    def list_prefs(self, *, limit: int = 50, kind: str = "") -> list[dict[str, Any]]:
        """按强度降序返回喜好；``kind`` 非空时只返回那一种取向。"""

        sql = "SELECT * FROM gap_prefs"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(str(kind))
        sql += " ORDER BY strength DESC, updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [item for item in (self._pref_row(row) for row in rows) if item]

    def delete_pref(self, pref_key: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM gap_prefs WHERE pref_key = ?", (str(pref_key or "").strip(),)
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def trim_prefs(self, *, keep: int = 20) -> int:
        """只保留强度最高的若干条，多余的删掉。

        为什么要裁：喜好会随着聊天慢慢变多，但注入预算固定。留一堆
        强度 0.1 的口味既占地方又互相稀释，不如只留最鲜明的那些。
        """

        total = self.conn.execute("SELECT COUNT(*) FROM gap_prefs").fetchone()[0]
        excess = int(total) - max(0, int(keep))
        if excess <= 0:
            return 0
        cursor = self.conn.execute(
            "DELETE FROM gap_prefs WHERE pref_key IN ("
            "SELECT pref_key FROM gap_prefs "
            "ORDER BY strength ASC, updated_at ASC LIMIT ?)",
            (excess,),
        )
        self.conn.commit()
        return int(cursor.rowcount)


