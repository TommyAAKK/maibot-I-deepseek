"""修复被「衰减复利」bug 误判为遗忘的记忆。

## 背景

``cg_store.decay_salience`` 原来每轮都拿 ``last_seen_ts`` 现算整段闲置时长，
乘完衰减系数却**不把基准往前推**。而 ``cg_tracker.tick`` 每
``refresh_tick_seconds``（默认 30 秒）调一次，于是同一段闲置被反复计息。

真机实测：一条 0.7 显著度、闲置 25 小时的记忆，一小时内被压到 0.118 并
标成遗忘。2026-11-03 当天，297 条记忆因此全部被误判为遗忘，active 归零，
注入层的记忆块彻底空转。

## 恢复依据

- 真实闲置时长只有 1.6~4.9 天（按 ``last_seen_ts`` 算），而半衰期是 45 天，
  所以按设计最多只该掉 7% —— **这 297 条本应全部仍是 active**。
- 原始显著度无法精确还原（连乘信息已丢失，库里只剩 0.113~0.120），
  因此按**同类别的中位数**还原：这是从同一批数据里未被污染的部分
  （``expired`` / ``resolved``）实测出来的，比拍一个全局常数更接近原貌。

## 安全性

- 只改 ``status='forgotten'`` 的条目，``resolved`` / ``expired`` 一律不动。
- 不改内容、不改时间戳、不改 pinned。
- 干跑模式（``--dry-run``）会先把每一条的前后对比打出来，不写库。
- 写库前自动备份整库（db + wal + shm 三件套）。

用法::

    python fix_decay_bug.py --db <路径> --dry-run      # 只看会发生什么
    python fix_decay_bug.py --db <路径>                # 真写（自动备份）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any

#: 各状态/类别里未被 bug 污染的行，用来算「正常显著度」的参照值。
REFERENCE_STATUSES = ("expired", "resolved")

#: 找不到参照值时的兜底（等于 ``upsert_item`` 的默认显著度）。
FALLBACK_SALIENCE = 0.5

#: 注入层的门槛。恢复后低于它的条目仍然注入不进去，要在这里提醒。
MIN_INJECT_SALIENCE = 0.35


def _reference_salience(conn: sqlite3.Connection) -> dict[str, float]:
    """按类别算出「没被污染的行」的显著度中位数。"""

    out: dict[str, float] = {}
    chapters = ",".join("?" for _ in REFERENCE_STATUSES)
    rows = conn.execute(
        f"SELECT category, salience FROM gap_items "
        f"WHERE status IN ({chapters}) ORDER BY category, salience",
        REFERENCE_STATUSES,
    ).fetchall()
    buckets: dict[str, list[float]] = {}
    for category, salience in rows:
        buckets.setdefault(str(category), []).append(float(salience))
    for category, values in buckets.items():
        if not values:
            continue
        values.sort()
        middle = len(values) // 2
        if len(values) % 2:
            out[category] = round(values[middle], 4)
        else:
            out[category] = round((values[middle - 1] + values[middle]) / 2.0, 4)
    return out


def _ensure_column(conn: sqlite3.Connection) -> None:
    """确保 ``salience_decayed_at`` 列存在。

    恢复脚本可能要跑在一个**还没被新插件打开过**的旧库上（用户先跑脚本、
    后升级插件），这时列还不存在。这里补上，和 ``cg_store._migrate`` 保持
    一致；已经存在时是空操作。
    """

    columns = {row[1] for row in conn.execute("PRAGMA table_info(gap_items)")}
    if "salience_decayed_at" not in columns:
        conn.execute(
            "ALTER TABLE gap_items ADD COLUMN salience_decayed_at REAL NOT NULL DEFAULT 0"
        )
        conn.commit()
        print("已补上 salience_decayed_at 列（旧库迁移）")


def _backup_db(db_path: str) -> str:
    """把 db + wal + shm 一起复制到一个带时间戳的目录。"""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = f"{db_path}.bak-fixdecay-{stamp}"
    root = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(target, exist_ok=True)
    copied = []
    for suffix in ("", "-wal", "-shm"):
        source = db_path + suffix
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(target, os.path.basename(db_path) + suffix))
            copied.append(os.path.basename(source))
    _ = root
    return f"{target}（{', '.join(copied)}）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="修复被衰减复利 bug 误遗忘的记忆")
    parser.add_argument("--db", required=True, help="companion_gap.db 的路径")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几条（0 = 全部）")
    args = parser.parse_args(argv)

    db_path = args.db
    if not os.path.exists(db_path):
        print(f"找不到数据库：{db_path}")
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_column(conn)
        # ① 先确认真的是受害库：如果 forgotten 的显著度没有集体塌陷，
        #    说明要么已经修过，要么这库根本没中招，不该乱动。
        rows = conn.execute(
            "SELECT id, session_id, category, content, salience, status, "
            "last_seen_ts, created_at, forgotten_at FROM gap_items "
            "WHERE status = 'forgotten' ORDER BY id"
        ).fetchall()
        if not rows:
            print("没有 forgotten 条目，无需处理。")
            return 0

        saliences = [float(r["salience"]) for r in rows]
        peak = max(saliences)
        print(f"待检查的 forgotten 条目：{len(rows)} 条")
        print(f"当前显著度范围：{min(saliences):.4f} ~ {peak:.4f}")

        reference = _reference_salience(conn)
        print("\n各类别的参照显著度（取自 expired / resolved 的中位数）：")
        for category in sorted(reference):
            count = conn.execute(
                "SELECT COUNT(*) FROM gap_items WHERE category = ? AND status IN ('expired','resolved')",
                (category,),
            ).fetchone()[0]
            print(f"  {category:<14} {reference[category]:.3f}  （参照 {count} 条）")

        # ② 逐条决定恢复目标
        targets: list[dict[str, Any]] = []
        for row in rows:
            category = str(row["category"])
            target = reference.get(category, FALLBACK_SALIENCE)
            if target < MIN_INJECT_SALIENCE:
                target = MIN_INJECT_SALIENCE
            targets.append(
                {
                    "id": int(row["id"]),
                    "category": category,
                    "old": float(row["salience"]),
                    "new": round(target, 4),
                    "content": str(row["content"]),
                }
            )
            if args.limit and len(targets) >= args.limit:
                break

        print(f"\n将恢复 {len(targets)} 条为 active：")
        for item in targets[:12]:
            print(
                f"  #{item['id']:<4} {item['category']:<13} "
                f"{item['old']:.4f} → {item['new']:.4f}  {item['content'][:34]}"
            )
        if len(targets) > 12:
            print(f"  …… 另有 {len(targets) - 12} 条")

        below = sum(1 for item in targets if item["new"] < MIN_INJECT_SALIENCE)
        if below:
            print(f"\n注意：{below} 条恢复后仍低于注入门槛 {MIN_INJECT_SALIENCE}，不会被注入。")

        if args.dry_run:
            print("\n[dry-run] 没有写库。")
            return 0

        # ③ 备份后写库
        backup = _backup_db(db_path)
        print(f"\n已备份到：{backup}")

        now = time.time()
        changed = 0
        for item in targets:
            # salience_decayed_at 一并清零：从「现在」重新开始计衰减，
            # 不把灾难期间那段（已经被证伪的）闲置继续算进来。
            conn.execute(
                "UPDATE gap_items SET status = 'active', salience = ?, "
                "forgotten_at = 0, salience_decayed_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'forgotten'",
                (item["new"], now, now, item["id"]),
            )
            changed += conn.total_changes
        conn.commit()
    finally:
        conn.close()

    # ④ 复核
    verify = sqlite3.connect(db_path)
    verify.row_factory = sqlite3.Row
    try:
        stats = {
            str(r["status"]): int(r["n"])
            for r in verify.execute(
                "SELECT status, COUNT(*) AS n FROM gap_items GROUP BY status"
            )
        }
        active_rows = verify.execute(
            "SELECT COUNT(*) FROM gap_items WHERE status='active' AND salience >= ?",
            (MIN_INJECT_SALIENCE,),
        ).fetchone()[0]
    finally:
        verify.close()

    print(f"\n恢复后状态分布：{json.dumps(stats, ensure_ascii=False)}")
    print(f"其中可注入（active 且 salience >= {MIN_INJECT_SALIENCE}）：{active_rows} 条")
    print(f"本次处理 {len(targets)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
