"""注入热路径延迟基准。

``maisaka.replyer.before_request`` 的 Host 侧超时是 6000ms，插件自己声明 2500ms。
这个脚本量化真实耗时，确认注入路径没有意外阻塞。

运行：python bench.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

import tests as T  # noqa: E402  复用 stub、包加载器与 FakeCtx

plugin_mod = T.plugin_mod
store_mod = T.store_mod
AvoidanceProbe = T.avoidance_mod.AvoidanceProbe

SESSION_ID = "qq:group:bench"
ITERATIONS = 400


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cgap_bench_")
    db = str(Path(tmp) / "bench.db")
    store = store_mod.GapStore(db)
    store.open()

    try:
        # 铺满 5 类、每类 6 条，逼近 max_items_per_category 上限
        samples = {
            "open_loop": [
                "说好下次一起去看那部纪录片",
                "她让我帮她挑一份生日礼物还没挑",
                "约了周末把上次没打完的游戏通关",
                "答应给她整理一份书单",
                "她说等忙完这阵要一起吃火锅",
                "还没回她关于旅行路线的问题",
            ],
            "emotion": [
                "她最近加班很多，情绪偏低",
                "这两天她在准备答辩，比较紧张",
                "她今天心情不错，主动分享了好消息",
                "她对工作变动有点焦虑",
                "她睡眠不太好，白天容易累",
                "她刚和家人通完电话，情绪有点沉",
            ],
            "relationship": [
                "她不喜欢被连续追问",
                "她更习惯用语音而不是长文字",
                "她记得我提过的那首歌",
                "她在我状态差的时候会主动岔开话题",
                "她喜欢被具体地夸奖而不是泛泛而谈",
                "她对玩笑的接受度比我高",
            ],
            "self": [
                "我一直用偏简洁的方式回她",
                "我不主动追问她的私事",
                "我习惯先接住情绪再给建议",
                "我不会假装记得没发生的事",
                "我说话带一点克制",
                "我不在她忙的时候刷屏",
            ],
            "creative": [
                "我们一起编过一个关于灯塔的故事",
                "约好给那只流浪猫写观察日记",
                "一起想了个乐队的名字",
                "共同设定了一个只在夜里出现的角色",
                "计划合写一份旅行清单",
                "一起给常去的店起了外号",
            ],
        }
        for category, contents in samples.items():
            for content in contents:
                store.upsert_item(SESSION_ID, category, content, salience=0.8)

        plugin = plugin_mod.create_plugin()
        plugin._ctx = T.FakeCtx(tmp)
        plugin._store = store
        plugin._probe = AvoidanceProbe(plugin._ctx)
        await plugin._probe.probe(force=True)

        active = store.list_active(SESSION_ID)
        print(f"活跃条目 {len(active)} 条")

        kwargs = {
            "session_id": SESSION_ID,
            "request_type": "normal",
            "task_name": "replyer",
            "model_name": "test-model",
            "extra_prompt": "保持简短。",
            "attempt": 1,
            "retry_count": 0,
            "max_retries": 2,
            "reply_message_id": "m1",
            "reply_reason": "普通回复",
            "selected_expression_ids": [],
            "reply_tool_args": {},
        }

        # 预热
        for _ in range(20):
            await plugin.inject_context(**kwargs)

        samples_ms: list[float] = []
        for _ in range(ITERATIONS):
            started = time.perf_counter()
            result = await plugin.inject_context(**kwargs)
            samples_ms.append((time.perf_counter() - started) * 1000.0)

        block = str(result["modified_kwargs"].get("extra_prompt") or "")
        print(f"注入块长度 {len(block)} 字符")
        print(f"注入块预览:\n{block[:400]}")
        print()
        print(f"迭代 {ITERATIONS} 次")
        print(f"  平均 {statistics.fmean(samples_ms):.3f} ms")
        print(f"  中位 {statistics.median(samples_ms):.3f} ms")
        print(f"  p95  {_percentile(samples_ms, 0.95):.3f} ms")
        print(f"  p99  {_percentile(samples_ms, 0.99):.3f} ms")
        print(f"  最大 {max(samples_ms):.3f} ms")

        budget_ms = 2500.0
        worst = max(samples_ms)
        print()
        if worst > budget_ms * 0.2:
            print(f"警告：最慢 {worst:.1f}ms 超过自声明超时 {budget_ms}ms 的 20%")
            return 1
        print(f"结论：最慢 {worst:.2f}ms，远低于自声明超时 {budget_ms:.0f}ms（占比 {worst / budget_ms:.3%}）")
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
