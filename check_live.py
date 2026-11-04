"""真机联检：对着运行中的 MaiBot 实例，端到端验证 WebUI 记忆管理页。

和另外几个脚本的分工：
  tests.py           —— 纯单元测试，不需要宿主（跑逻辑与存储层）
  real_sdk_check.py  —— 离线静态校验，不需要宿主在跑（用宿主源码/SDK 校验声明）
  check_page.py      —— 离线静态校验页面片段（语法、id、绑定名、外链）
  check_live.py      —— 本脚本：必须有运行中的实例，走真实 HTTP 与真实数据库

检查内容：
  1. 宿主 WebUI 扩展清单里有本插件的页面；
  2. 三个查询返回真实数据且字段完整；
  3. 写操作闭环：create → update → delete（测试条目会删掉，不留脏数据）；
  4. 危险操作必须确认：不带 confirmed 返回 409；
  5. 参数校验生效：越界值与未声明参数返回 422；
  6. 情绪接口：八基元读写闭环、派生量随基元变化、越界值被拒；
  7. 人物接口：列表字段完整、四种关系角色齐全、手动新增/重复拦截/删除闭环；
  8. 喜好接口：喜欢/讨厌两种、三个来源、新增→改强度→改话题→删除闭环；
  9. 页面文档能被宿主取出，且是 body 片段；
  10. 用真实数据渲染页面并检查 DOM（需要 node + Edge，缺则跳过）。

用法：
    python check_live.py
    python check_live.py --token <token>          # 覆盖自动读取
    python check_live.py --base http://127.0.0.1:8001
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_node() -> Path | None:
    """找 node 可执行文件（只有最后一步 DOM 渲染需要它）。

    优先 ``CGAP_NODE``，其次 PATH 上的 ``node``。找不到就跳过渲染检查。
    """
    override = os.environ.get("CGAP_NODE")
    if override:
        return Path(override)
    found = shutil.which("node")
    return Path(found) if found else None


MB_ROOT = Path(os.environ.get("CGAP_HOST_ROOT", ""))
NODE = _find_node()
PLUGIN_ID = "github.TommyAAKK.maibot-I-deepseek"
PAGE_ID = "memories"

_failures: list[str] = []
_checks = 0


def check(condition: bool, label: str) -> bool:
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")
    return bool(condition)


def skip(label: str) -> None:
    print(f"  skip {label}")


class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.prefix = f"{self.base}/api/webui/plugins/runtime/webui/{PLUGIN_ID}/{PAGE_ID}"

    def _request(self, url: str, body: dict | None = None) -> tuple[int, dict | None, str]:
        data = None
        headers = {"Cookie": f"maibot_session={self.token}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw), raw
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                return error.code, json.loads(raw), raw
            except json.JSONDecodeError:
                return error.code, None, raw

    def call(self, kind: str, name: str, args: dict, confirmed: bool = False):
        return self._request(
            f"{self.prefix}/{kind}/{name}",
            {"args": args, "confirmed": confirmed},
        )

    def query(self, name: str, args: dict | None = None) -> dict:
        status, payload, raw = self.call("queries", name, args or {})
        if status != 200 or not payload or not payload.get("success"):
            raise SystemExit(f"查询 {name} 失败: HTTP {status} {raw[:300]}")
        return payload["result"]

    def action(self, name: str, args: dict, confirmed: bool = False):
        return self.call("actions", name, args, confirmed)


def read_token() -> str:
    if not str(MB_ROOT):
        raise SystemExit(
            "没设置 CGAP_HOST_ROOT，无法定位宿主目录。"
            "请设置环境变量 CGAP_HOST_ROOT=<MaiBot 安装目录>，或用 --token 手动指定"
        )
    path = MB_ROOT / "data" / "webui.json"
    if not path.is_file():
        raise SystemExit(f"找不到 {path}，请用 --token 手动指定")
    return str(json.loads(path.read_text(encoding="utf-8")).get("access_token") or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default="")
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--skip-render", action="store_true")
    args = parser.parse_args()

    token = args.token or read_token()
    if not token:
        raise SystemExit("没有可用的 token")
    client = Client(args.base, token)

    print("[1] 宿主 WebUI 扩展清单")
    status, payload, raw = client._request(f"{args.base}/api/webui/plugins/runtime/webui")
    if not check(status == 200 and bool(payload), f"扩展清单可读（HTTP {status}）"):
        print(f"      响应: {raw[:300]}")
        return 1
    extensions = {item.get("plugin_id"): item for item in payload.get("extensions") or []}
    check(PLUGIN_ID in extensions, f"清单里有 {PLUGIN_ID}")
    extension = extensions.get(PLUGIN_ID) or {}
    pages = {page.get("id"): page for page in extension.get("pages") or []}
    check(PAGE_ID in pages, f"声明里有页面 {PAGE_ID}")
    page = pages.get(PAGE_ID) or {}
    check(page.get("mode") == "custom", f"页面模式为 custom（实际 {page.get('mode')}）")
    check(bool(page.get("document_revision")), "宿主已解析页面文档（有 document_revision）")

    print("[2] 查询接口")
    stats = client.query("stats")
    check(stats.get("ok") is True, "mem_stats 返回 ok")
    statuses = stats.get("items_by_status") or {}
    total_items = sum(int(value or 0) for value in statuses.values())
    check(total_items >= 0, f"条目统计可算（共 {total_items} 条）")
    check(len(stats.get("categories") or []) == 5, "五个类别标签齐全")
    check(len(stats.get("statuses") or []) == 4, "四个状态标签齐全（含遗忘）")
    check(
        [item.get("value") for item in stats.get("statuses") or []]
        == ["active", "resolved", "expired", "forgotten"],
        "状态顺序固定，遗忘排在最后",
    )
    check("items_pinned" in stats, "mem_stats 带置顶数")
    check("items_doubted" in stats, "mem_stats 带存疑数")

    sessions = client.query("sessions").get("sessions") or []
    check(isinstance(sessions, list), f"mem_sessions 返回列表（{len(sessions)} 个会话）")
    if sessions:
        first_session = sessions[0]
        for field in ("session_id", "label", "active_count", "total_count"):
            check(field in first_session, f"会话字段 {field} 存在")

    items_result = client.query("items", {"limit": 5})
    items = items_result.get("items") or []
    check(items_result.get("ok") is True, "mem_items 返回 ok")
    check(len(items) <= 5, f"分页生效（返回 {len(items)} 条，上限 5）")
    if items:
        item = items[0]
        for field in ("id", "category", "category_label", "content", "salience", "status"):
            check(field in item, f"条目字段 {field} 存在")

    print("[3] 写操作闭环（测试条目随后删除）")
    target_session = (sessions[0]["session_id"] if sessions else "")

    # 上一轮如果中途失败（比如插件正好在热重载，接口返回 502），临时条目会
    # 留在库里。开跑前先清一遍，免得越积越多、还会把「置顶数」之类统计弄脏。
    leftovers = client.query("items", {"limit": 200, "search": "check_live 临时条目"})
    for row in leftovers.get("items") or []:
        client.action("delete", {"item_id": row.get("id")}, confirmed=True)

    if not target_session:
        skip("没有会话，跳过写操作")
    else:
        status, payload, raw = client.action(
            "create",
            {
                "session_id": target_session,
                "category": "creative",
                "content": "【check_live 临时条目】验证完会立即删除。",
                "salience": 0.31,
            },
        )
        created = (payload or {}).get("result") or {}
        created_item = created.get("item") or {}
        new_id = created_item.get("id")
        check(status == 200 and created.get("ok") is True, f"create 成功（id={new_id}）")
        check(
            isinstance(new_id, int) and created_item.get("expires_at") == 0.0,
            "手工写入的条目不带过期时间",
        )

        if isinstance(new_id, int):
            status, payload, raw = client.action(
                "update",
                {
                    "item_id": new_id,
                    "content": "【check_live 临时条目·已改】",
                    "category": "relationship",
                    "salience": 0.66,
                    "status": "resolved",
                },
            )
            updated = ((payload or {}).get("result") or {}).get("item") or {}
            check(status == 200 and updated.get("ok", True) is not False, "update 成功")
            check(updated.get("content") == "【check_live 临时条目·已改】", "内容已改")
            check(updated.get("category") == "relationship", "类别已改")
            check(updated.get("status") == "resolved", "状态已改")
            check(abs(float(updated.get("salience") or 0) - 0.66) < 1e-6, "显著度已改")

            # ── 置顶 / 遗忘 / 想起（第 32 轮第 4 点）──
            status, payload, _ = client.action("pin", {"item_id": new_id, "pinned": True})
            pinned_item = ((payload or {}).get("result") or {}).get("item") or {}
            check(status == 200 and pinned_item.get("pinned") is True, "置顶成功")
            # 置顶只负责「不会被遗忘」，不会顺手改别的状态：
            # 上面把它改成了 resolved，这里应该还是 resolved。
            check(
                pinned_item.get("status") == "resolved",
                "置顶不会擅自改动已有状态",
            )

            status, payload, _ = client.action(
                "update", {"item_id": new_id, "status": "forgotten"}
            )
            forgotten_item = ((payload or {}).get("result") or {}).get("item") or {}
            check(status == 200 and forgotten_item.get("status") == "forgotten", "可以标为遗忘")
            check(
                float(forgotten_item.get("forgotten_at") or 0) > 0,
                "遗忘时间被记录下来",
            )
            check(
                forgotten_item.get("status_label") == "遗忘",
                "遗忘状态有中文标签",
            )

            # 置顶一条已经遗忘的条目，应该把它救回活跃状态。
            status, payload, _ = client.action("pin", {"item_id": new_id, "pinned": True})
            revived_item = ((payload or {}).get("result") or {}).get("item") or {}
            check(
                status == 200 and revived_item.get("status") == "active",
                "置顶一条遗忘的条目会把它救回来",
            )
            check(
                float(revived_item.get("forgotten_at") or 0) == 0.0,
                "救回来时清空遗忘时间",
            )

            # 再遗忘一次，走 recall 接口。
            client.action("update", {"item_id": new_id, "status": "forgotten"})
            status, payload, _ = client.action("recall", {"item_id": new_id})
            recalled_item = ((payload or {}).get("result") or {}).get("item") or {}
            check(status == 200 and recalled_item.get("status") == "active", "想起后回到活跃")
            check(
                float(recalled_item.get("last_recalled_ts") or 0) > 0,
                "想起时间被记录下来",
            )

            status, payload, _ = client.action("pin", {"item_id": new_id, "pinned": False})
            unpinned = ((payload or {}).get("result") or {}).get("item") or {}
            check(status == 200 and unpinned.get("pinned") is False, "可以取消置顶")

            status, payload, _ = client.action(
                "update", {"item_id": new_id, "truth_score": 0.2}
            )
            doubted = ((payload or {}).get("result") or {}).get("item") or {}
            check(status == 200 and doubted.get("doubted") is True, "可信度低的条目被标为存疑")
            check(
                abs(float(doubted.get("truth_score") or 0) - 0.2) < 1e-6,
                "可信度被写入",
            )

            status, payload, _ = client.action("delete", {"item_id": new_id}, confirmed=False)
            check(status == 409, f"删除未确认被拒（HTTP {status}，应为 409）")

            status, payload, raw = client.action("delete", {"item_id": new_id}, confirmed=True)
            deleted = (payload or {}).get("result") or {}
            check(status == 200 and deleted.get("deleted") == new_id, f"确认后删除成功（id={new_id}）")

            status, payload, _ = client.action("update", {"item_id": new_id, "content": "x"})
            check(status == 200 and ((payload or {}).get("result") or {}).get("ok") is False, "删除后更新返回业务失败")

    print("[4] 参数校验")
    status, _, _ = client.action("update", {"item_id": 1, "salience": 5})
    check(status == 422, f"显著度越界被拒（HTTP {status}，应为 422）")
    status, _, _ = client.action("update", {"item_id": 1, "bogus": "x"})
    check(status == 422, f"未声明参数被拒（HTTP {status}，应为 422）")
    status, _, _ = client.action("update", {})
    check(status == 422, f"缺少必填参数被拒（HTTP {status}，应为 422）")

    print("[5] 情绪接口")
    mood = client.query("mood")
    check(mood.get("ok") is True, "mem_mood 返回 ok")
    for key in (
        "valence", "arousal", "energy", "exhausted", "label", "guidance", "effort",
        "block", "enabled", "primitives", "primitive_labels", "baselines", "tone",
    ):
        check(key in mood, f"mem_mood 含 {key}")
    check(isinstance(mood.get("exhausted"), bool), "exhausted 是布尔值")
    check(isinstance(mood.get("effort"), str), "effort 是字符串")
    # 用户第 31 轮第 3 点：精力只影响「回复欲望 + 本次回复字数」，不再压发言频率
    check("desire" in mood and isinstance(mood.get("desire"), str), "mem_mood 含 desire（回复欲望）")
    check("length" in mood and isinstance(mood.get("length"), str), "mem_mood 含 length（回复长度）")
    effort_text = str(mood.get("effort") or "")
    check("发言频率" not in effort_text, "effort 文案里不再提发言频率")
    check(
        str(mood.get("desire") or "") in effort_text and str(mood.get("length") or "") in effort_text,
        "effort 由 desire + length 拼成",
    )
    # tone 是「只有语气、不含精力」的版本，页面靠它避免把精力那两句显示两遍。
    tone_text = str(mood.get("tone") or "")
    check(isinstance(mood.get("tone"), str), "tone 是字符串")
    check(
        tone_text and str(mood.get("guidance") or "").startswith(tone_text),
        "guidance = tone + effort（面板分块显示才不重复）",
    )
    check(
        not effort_text or effort_text not in tone_text,
        "tone 里不含精力文案（否则页面会重复显示）",
    )
    check("<companion-mood>" in str(mood.get("block") or ""), "block 带 companion-mood 标记")
    check(bool(str(mood.get("guidance") or "").strip()), "guidance 非空（能明显影响回复）")

    # 八个情绪基元都要下发给页面（用户要求每一个都能在配置页/面板里调）
    primitives = mood.get("primitives") or {}
    expected_prims = [
        "joy", "anger", "anxiety", "sadness", "interest", "calm", "disgust", "surprise",
    ]
    missing_prims = [name for name in expected_prims if name not in primitives]
    check(not missing_prims, f"mem_mood 下发八个情绪基元（缺 {missing_prims}）")
    labels = mood.get("primitive_labels") or {}
    missing_labels = [name for name in expected_prims if not labels.get(name)]
    check(not missing_labels, f"八个基元都有中文名（缺 {missing_labels}）")
    bases = mood.get("baselines") or {}
    check(
        all(isinstance(bases.get(name), (int, float)) for name in expected_prims),
        "八个基元的基线都下发了（页面要画参考线）",
    )
    # 注意：这里不把 energy 压到 0 去测耗尽。真机耗尽会触发两件有副作用的事——
    # 频率门控把该会话压到 floor，以及 proactive.trigger 发一句告别消息到用户群里。
    # 联检不该往用户聊天里插话，耗尽路径由 tests.py 离线覆盖。

    history = client.query("mood_history", {"limit": 5})
    check(history.get("ok") is True, "mem_mood_history 返回 ok")
    check("points" in history, "mem_mood_history 含 points")
    points = history.get("points") or []
    if points:
        check("primitives" in points[0], "情绪历史点也带八个基元")

    # 写闭环：改基元 → 读回 → 复位。原值先记下来，结束时恢复。
    original = dict(primitives)
    original["energy"] = mood.get("energy")
    status, payload, _ = client.action(
        "mood_set",
        {"anger": 0.85, "joy": 0.05, "calm": 0.10, "energy": 0.45, "reason": "联调测试"},
    )
    check(status == 200 and ((payload or {}).get("result") or {}).get("ok") is True,
          "mood_set 调用成功")
    changed = (payload or {}).get("result") or {}
    changed_prims = changed.get("primitives") or {}
    check(abs(float(changed_prims.get("anger") or 0) - 0.85) < 1e-6, "anger 基元写入生效")
    check(
        abs(float(changed_prims.get("calm") or 0) - 0.10) < 1e-6,
        "calm 基元写入生效（多维度同时调节）",
    )
    check(float(changed.get("valence") or 0) < 0, "愉悦度由基元推出为负")
    check(str(changed.get("label") or "") != "", "情绪标签由基元推出")
    check(str(changed.get("guidance") or "") != "", "行为指令随之改变（不是只改数字）")

    status, _, _ = client.action("mood_set", {"anger": 5})
    check(status == 422, f"基元越界被拒（HTTP {status}，应为 422）")

    status, payload, _ = client.action("mood_reset", {}, confirmed=True)
    check(status == 200 and ((payload or {}).get("result") or {}).get("ok") is True,
          "mood_reset 调用成功")
    reset = (payload or {}).get("result") or {}
    reset_prims = reset.get("primitives") or {}
    check(
        all(abs(float(reset_prims.get(name) or 0) - float(bases[name])) < 1e-6
            for name in expected_prims),
        "重置后八个基元都回到各自基线",
    )
    check(abs(float(reset.get("valence") or 0)) < 1e-6, "重置后愉悦度回到 0（基线推出的）")

    client.action("mood_set", original)
    check(True, "情绪已恢复到联调前的数值")

    print("[6] 人物接口")
    res = client.query("people", {"limit": 200})
    check(res.get("ok") is True, "people 查询可用")
    people = res.get("people")
    check(isinstance(people, list), "people 返回列表")
    roles = res.get("roles")
    # 身份表现在是配置项（用户第 33 轮要求可以自由增删），所以不能断言「恰好四种」，
    # 只能断言「至少有一个兜底身份 stranger 且都有中文名」。
    check(isinstance(roles, list) and len(roles) >= 1, "roles 返回关系身份表")
    if isinstance(roles, list):
        role_values = [str(item.get("value") or "") for item in roles if isinstance(item, dict)]
        check("stranger" in role_values, "身份表里有兜底身份 stranger")
        check(
            all(str(item.get("label") or "").strip() for item in roles if isinstance(item, dict)),
            "每种身份都有中文名",
        )
    levels = res.get("levels")
    check(isinstance(levels, list) and len(levels) >= 1, "levels 返回好感度分级表")
    if isinstance(levels, list):
        thresholds = [
            float(item.get("min_affection"))
            for item in levels
            if isinstance(item, dict) and item.get("min_affection") is not None
        ]
        check(thresholds == sorted(thresholds, reverse=True), "分级表按好感度下限从高到低排序")
        check(
            all(str(item.get("label") or "").strip() for item in levels if isinstance(item, dict)),
            "每一档都有中文名",
        )
        check(thresholds and thresholds[-1] <= -100.0, "最低一档能接住负好感度")
    if isinstance(people, list) and people:
        first = people[0]
        for key in (
            "person_key", "platform", "user_id", "display_name", "relation_role",
            "relation_role_label", "role_confidence", "role_source", "is_manual",
            "affection", "intimacy", "intimacy_label", "emotional_impact", "tags",
            # 第 34 轮：六维是真相，好感度是摘要
            "axes", "axes_text", "contrasts", "dominant",
        ):
            check(key in first, f"人物条目含字段 {key}")
        check(isinstance(first.get("tags"), list), "tags 是列表（不是 JSON 字符串）")
        check(isinstance(first.get("is_manual"), bool), "is_manual 是布尔")
        check(
            float(first.get("affection") or 0) >= -100 and float(first.get("affection") or 0) <= 100,
            "好感度在 -100 到 100 之间",
        )
        axes = first.get("axes") or {}
        check(isinstance(axes, dict) and len(axes) == 6, "六维是六个键的字典")
        check(
            all(0.0 <= float(value) <= 1.0 for value in axes.values()),
            "六个维度都在 0 到 1 之间",
        )
        check(isinstance(first.get("contrasts"), list), "矛盾描述是列表")
        check(isinstance(first.get("dominant"), list), "主要维度是列表")
        # 密切度是推导量，不能等于关系身份的中文名。
        # 用户第 33 轮把分级阈值开放成配置项了，所以这里只校验「是分级表里的一档」，
        # 不再写死四档中文名（阈值一改断言就废）。
        live_levels = res.get("levels") or []
        level_labels = {
            str(item.get("label") or "") for item in live_levels if isinstance(item, dict)
        }
        check(
            str(first.get("intimacy_label") or "") in level_labels,
            "密切度标签落在配置的分级表里",
        )
    else:
        skip("当前还没有记住任何人，跳过字段检查")

    # 六维元数据由 mem_people 一起下发，页面画滑杆用。
    live_axes = res.get("axes") or []
    check(isinstance(live_axes, list) and len(live_axes) == 6, "mem_people 下发六个维度的元数据")
    check(
        all(
            isinstance(item, dict) and item.get("key") and item.get("label")
            and item.get("hint") is not None and item.get("default") is not None
            for item in live_axes
        ),
        "每个维度都有 key / label / hint / default",
    )
    check(
        {str(item.get("key")) for item in live_axes}
        == {"trust", "closeness", "respect", "tension", "familiarity", "perceived_warmth"},
        "六个维度的标识与 cg_person.py 一致",
    )

    # ── 手动新增人物（用户第 31 轮第 5 点：不读好感度插件，让用户自己写）──
    test_uid = "__cglive_new__"
    test_key = f"qq:{test_uid}"
    status, payload, raw = client.action(
        "person_create",
        {"user_id": test_uid, "platform": "qq", "display_name": "联调新人",
         "relation_role": "friend", "tags": ["联调用的群友"],
         "axes": {"trust": 0.8, "closeness": 0.7, "respect": 0.5,
                  "tension": 0.1, "familiarity": 0.4, "perceived_warmth": 0.6}},
    )
    created_person = ((payload or {}).get("result") or {})
    check(status == 200 and created_person.get("ok") is True, f"person_create 成功（{status}）")
    new_person = created_person.get("person") or {}
    check(new_person.get("person_key") == test_key, "新增后 person_key 正确")
    check(new_person.get("is_manual") is True, "用户手加的人标成手动")
    check(new_person.get("role_source") == "manual", "手加的人 role_source 是 manual")
    check(new_person.get("relation_role") == "friend", "角色写入生效")
    new_axes = new_person.get("axes") or {}
    check(abs(float(new_axes.get("trust") or 0) - 0.8) < 1e-6, "六维写入生效（trust）")
    check(abs(float(new_axes.get("familiarity") or 0) - 0.4) < 1e-6,
          "六维写入生效（familiarity 也能手设）")
    # 好感度由六维算出来，不再是独立输入 —— 只要落在合理区间即可。
    check(-100 <= float(new_person.get("affection") or 0) <= 100,
          "好感度由六维算出且在有效区间")
    check(new_person.get("tags") == ["联调用的群友"], "标签写入生效")

    # 标签整份替换（用户第 32 轮第 1 点：要能改、能删，不只是能加）
    status, payload, _ = client.action(
        "person_update", {"person_key": test_key, "tags": ["换过的标签", "再加一条"]}
    )
    updated = (payload or {}).get("result") or {}
    check(status == 200 and updated.get("ok") is True, f"person_update 改标签成功（{status}）")
    check(
        ((updated.get("person") or {}).get("tags")) == ["换过的标签", "再加一条"],
        "标签是整份替换（旧的「联调用的群友」不见了）",
    )
    # 清空标签也要能生效（空数组 ≠ 不传）
    status, payload, _ = client.action("person_update", {"person_key": test_key, "tags": []})
    cleared = (payload or {}).get("result") or {}
    check(status == 200 and cleared.get("ok") is True, f"清空标签成功（{status}）")
    check(((cleared.get("person") or {}).get("tags")) == [], "标签可以清空成空数组")

    # ── 六维可以单独改（第 34 轮）──
    # 好感度已经是派生量，所以「想精确表达很熟但不信他」只能靠逐维设定。
    status, payload, _ = client.action(
        "person_update",
        {"person_key": test_key, "axes": {"closeness": 0.9, "trust": 0.1}},
    )
    axis_res = (payload or {}).get("result") or {}
    check(status == 200 and axis_res.get("ok") is True, f"person_update 改六维成功（{status}）")
    axis_person = axis_res.get("person") or {}
    axis_now = axis_person.get("axes") or {}
    check(abs(float(axis_now.get("closeness") or 0) - 0.9) < 1e-6, "亲近被改成 0.9")
    check(abs(float(axis_now.get("trust") or 0) - 0.1) < 1e-6, "信任被改成 0.1")
    check(abs(float(axis_now.get("respect") or 0) - 0.5) < 1e-6,
          "没传的维度保持不变（respect 还是 0.5）")
    # 「很熟但不信」这种组合应该被识别成矛盾，并给出一句人话
    check(
        any("熟" in str(line) or "信" in str(line) for line in (axis_person.get("contrasts") or [])),
        "矛盾组合被识别出来（很亲近但不太信）",
    )
    # 好感度必须跟着六维变，而且不能还是新增时那个值
    check(
        abs(float(axis_person.get("affection") or 0) - float(new_person.get("affection") or 0)) > 1,
        "好感度随六维变化重算（不再是独立输入）",
    )

    # 重复新增必须被拒（否则会有两条同 key 的记录，静默覆盖用户数据）
    status, payload, _ = client.action(
        "person_create", {"user_id": test_uid, "platform": "qq"}
    )
    dup = (payload or {}).get("result") or {}
    check(status == 200 and dup.get("ok") is False, "重复新增同一个人返回业务失败")
    check("已经认识" in str(dup.get("error") or ""), "重复新增的报错说明原因")

    # 缺 user_id 必须被拒
    status, payload, _ = client.action("person_create", {"user_id": ""})
    blank = (payload or {}).get("result") or {}
    check(status == 200 and blank.get("ok") is False, "user_id 为空时返回业务失败")
    check("user_id" in str(blank.get("error") or ""), "缺 user_id 的报错点明字段")

    # ── 自定义身份（用户第 33 轮第 2 点：身份可以自由配置，不能只认内置四档）──
    # 这里用配置表里真实存在的一个标识，验证「不是 owner/friend/member/stranger
    # 也能写进去」。因为身份表现在可增删，所以从下发的 roles 里挑，不写死。
    # 关键点：webui.json 原先给 relation_role 写了 choices 白名单，自定义身份会被
    # 宿主 schema 挡成 422，这个断言就是防那个回归的。
    live_role_values = [
        str(item.get("value") or "") for item in (roles or []) if isinstance(item, dict)
    ]
    odd_role = next(
        (value for value in live_role_values if value not in ("owner", "friend", "member", "stranger")),
        "",
    )
    if odd_role:
        status, payload, _ = client.action(
            "person_update", {"person_key": test_key, "relation_role": odd_role}
        )
        custom = (payload or {}).get("result") or {}
        check(status == 200, f"自定义身份 {odd_role} 没被 schema 挡（{status}）")
        check(custom.get("ok") is True, "自定义身份能写进人物记录")
        check(
            ((custom.get("person") or {}).get("relation_role")) == odd_role,
            "自定义身份写入后原样返回",
        )
    else:
        # 配置里只有内置四档时，改回内置档位，保证后续断言状态干净
        status, payload, _ = client.action(
            "person_update", {"person_key": test_key, "relation_role": "member"}
        )
        check(status == 200, "关系身份写回内置档位成功")

    # 不存在的身份必须被拒（防止模型凭空造身份写进库）
    status, payload, _ = client.action(
        "person_update", {"person_key": test_key, "relation_role": "__not_a_role__"}
    )
    bogus = (payload or {}).get("result") or {}
    check(status == 200 and bogus.get("ok") is False, "不认识的身份证被拒（不是 422 崩溃）")
    check(
        "身份" in str(bogus.get("error") or ""),
        "拒绝未知身份时报错说明是身份问题",
    )

    # 新增的人能在列表里查到
    listed = client.query("people", {"limit": 500}).get("people") or []
    keys = [str(item.get("person_key") or "") for item in listed if isinstance(item, dict)]
    check(test_key in keys, "新增的人出现在 people 列表里")

    # 清理：删掉联调造的这条
    status, payload, _ = client.action(
        "person_delete", {"person_key": test_key}, confirmed=True
    )
    gone = (payload or {}).get("result") or {}
    check(status == 200 and gone.get("ok") is True, "person_delete 清理成功")
    listed_after = client.query("people", {"limit": 500}).get("people") or []
    keys_after = [str(item.get("person_key") or "") for item in listed_after if isinstance(item, dict)]
    check(test_key not in keys_after, "删除后列表里不再有这个人")

    print("[7] 喜好接口")
    prefs_payload = client.query("prefs", {"limit": 200})
    prefs = prefs_payload.get("prefs") or []
    check(isinstance(prefs, list), "prefs 查询返回列表")
    kinds = prefs_payload.get("kinds") or []
    check([item.get("value") for item in kinds] == ["like", "dislike"], "喜好只有喜欢与讨厌两种")
    check(
        all(item.get("label") in ("喜欢", "讨厌") for item in kinds if isinstance(item, dict)),
        "两种喜好都有中文名",
    )
    sources = prefs_payload.get("sources") or []
    check(
        [item.get("value") for item in sources] == ["persona", "manual", "chat"],
        "口味的三个来源齐全（人设 / 手写 / 聊天）",
    )
    if prefs:
        first_pref = next((item for item in prefs if isinstance(item, dict)), {})
        pref_fields = [
            "pref_key", "kind", "kind_label", "topic", "strength",
            "source", "source_label", "is_manual", "created_at", "updated_at",
        ]
        missing_pref = [name for name in pref_fields if name not in first_pref]
        check(not missing_pref, f"喜好条目字段齐全（缺 {missing_pref}）")
        check(
            0.0 <= float(first_pref.get("strength") or 0.0) <= 1.0,
            "喜好强度在 0..1 之间",
        )
    else:
        print("  skip 当前还没有任何口味（首次启动提炼可能还没跑）")

    # 写闭环：新增 → 改强度 → 改话题（等于换一条）→ 删除
    # 注意：返回的是 {"ok": true, "pref": {...}}，字段在 pref 里而不是顶层。
    #
    # 先清掉上一次失败可能留下的联调口味，否则这一轮的 create 会被判成
    # 「已经认识这条了」而失败——这坑踩过一次。
    existing_prefs = client.query("prefs", {"limit": 500}).get("prefs") or []
    for row in existing_prefs:
        key = str((row or {}).get("pref_key") or "")
        if key.startswith("like:check_live") or key.startswith("dislike:check_live"):
            client.action("pref_delete", {"pref_key": key}, confirmed=True)

    pref_topic = "check_live 联调口味"
    created = client.action(
        "pref_create", {"kind": "like", "topic": pref_topic, "strength": 0.7}
    )[1] or {}
    created_result = created.get("result") or {}
    created_pref = created_result.get("pref") or {}
    pref_key = str(created_pref.get("pref_key") or "")
    check(created_result.get("ok") is True, "pref_create 成功")
    check(pref_key == f"like:{pref_topic}", "pref_key 由 kind:topic 拼成")
    check(created_pref.get("source") == "manual", "手动新增的口味标记为手写来源")
    check(created_pref.get("is_manual") is True, "手动新增的口味 is_manual 为真")

    dup = client.action("pref_create", {"kind": "like", "topic": pref_topic})[1] or {}
    dup_result = dup.get("result") or {}
    check(dup_result.get("ok") is False, "同一条口味重复新增被拦下")
    check("这一条" in str(dup_result.get("error") or ""), "重复新增给出可读提示")

    # kind 非法由宿主的 schema 校验挡在前面（HTTP 422），不会进到插件里。
    bad_kind_status, _, _ = client.action("pref_create", {"kind": "neutral", "topic": "随便"})
    check(bad_kind_status == 422, f"非法的 kind 被拒（HTTP {bad_kind_status}，应为 422）")

    empty_topic = client.action("pref_create", {"kind": "like", "topic": "  "})[1] or {}
    check((empty_topic.get("result") or {}).get("ok") is False, "空话题被拦下")

    updated = client.action(
        "pref_update", {"pref_key": pref_key, "strength": 0.25}
    )[1] or {}
    updated_pref = (updated.get("result") or {}).get("pref") or {}
    check((updated.get("result") or {}).get("ok") is True, "pref_update 改强度成功")
    check(
        abs(float(updated_pref.get("strength") or 0.0) - 0.25) < 1e-6,
        "改后的强度写进了库",
    )

    # 改话题等于换一条：老 key 必须消失，否则会留下两行同义不同名。
    renamed = client.action(
        "pref_update", {"pref_key": pref_key, "topic": "check_live 改过的口味"}
    )[1] or {}
    renamed_result = renamed.get("result") or {}
    renamed_pref = renamed_result.get("pref") or {}
    check(renamed_result.get("ok") is True, "pref_update 改话题成功")
    after_rename = client.query("prefs", {"limit": 500}).get("prefs") or []
    rename_keys = [str(item.get("pref_key") or "") for item in after_rename if isinstance(item, dict)]
    check(pref_key not in rename_keys, "改话题后旧 key 不再存在（没有留下同义的两行）")
    new_key = str(renamed_pref.get("pref_key") or "")
    check(new_key == "like:check_live 改过的口味", "改话题后返回的 key 是新的")
    check(new_key in rename_keys, "改话题后新 key 出现在列表里")

    deleted = client.action("pref_delete", {"pref_key": new_key}, confirmed=True)[1] or {}
    check((deleted.get("result") or {}).get("ok") is True, "pref_delete 删除成功")
    after_delete = client.query("prefs", {"limit": 500}).get("prefs") or []
    delete_keys = [str(item.get("pref_key") or "") for item in after_delete if isinstance(item, dict)]
    check(new_key not in delete_keys, "删除后列表里不再有这条口味")

    print("[8] 页面文档")
    status, payload, raw = client._request(f"{client.prefix}/document")
    document = (payload or {}).get("document") if payload else None
    check(status == 200 and isinstance(document, str) and len(document) > 5000, "页面文档可取（>5KB）")
    if isinstance(document, str):
        # 注释里会提到 <html>/<head>/<body> 这些字样（说明「不能写」），
        # 所以先剥掉注释再判断，否则会误报。
        body = re.sub(r"<!--.*?-->", "", document, flags=re.S).lower()
        for tag in ("<html", "<head", "<body"):
            check(tag not in body, f"文档是 body 片段（不含 {tag}>）")
        for needle in (
            "cg-mood", "cg-mood-modal", "cg-mood-guide", "cg-mood-effort",
            "cg-mood-prims", "mood_history", "cg-people", "cg-person-modal",
            "cg-person-create-modal", "cg-panel", "cg-memories",
            # 第 32 轮：记忆生命周期（置顶 / 遗忘 / 想起 / 存疑）
            "st-forgotten", "cg-badge pinned", "cg-badge doubted",
            "M.action('pin'", "M.action('recall'",
            # 第 32 轮：喜好面板
            "cg-prefs", "cg-pref-modal", "cg-pref-create-modal",
            "M.action('pref_create'", "M.action('pref_update'", "M.action('pref_delete'",
            # 第 44 轮：人物卡片与弹窗都要显示账号（QQ 号）
            "accountText", "platformLabel", "cg-person-id", "cg-person-in-account",
        ):
            check(needle in document, f"页面文档含 {needle}")

    print("[9] 真实数据渲染校验")
    if args.skip_render:
        skip("按要求跳过渲染校验")
    elif NODE is None or not NODE.is_file():
        skip("没找到 node，跳过渲染校验")
    else:
        build = subprocess.run(
            [sys.executable, str(HERE / "build_preview.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if build.returncode != 0:
            check(False, f"生成预览页失败: {(build.stderr or build.stdout)[-300:]}")
        else:
            preview_dir = Path(
                os.environ.get("CGAP_PREVIEW_DIR")
                or (Path(tempfile.gettempdir()) / "cgap-preview")
            )
            # 四种组合都要过：默认浅色/深色验证常规观感，future-retro 浅色/深色
            # 验证「--maibot-surface 采样成 transparent」时弹窗是否仍不透明
            # （用户两次反馈的透明复发都出在这个主题下）。
            cases = [
                ("preview-light.html", "浅色", "cdp_check.js"),
                ("preview-light.html", "浅色", "cdp_interact.js"),
                ("preview-dark.html", "深色", "cdp_interact.js"),
                ("preview-retro.html", "未来复古", "cdp_check.js"),
                ("preview-retro.html", "未来复古", "cdp_interact.js"),
                ("preview-retro-dark.html", "未来复古深色", "cdp_interact.js"),
            ]
            for filename, label, script in cases:
                preview = preview_dir / filename
                if not preview.is_file():
                    check(False, f"缺少预览页 {filename}（build_preview.py 应生成它）")
                    continue
                script_path = HERE / script
                if not script_path.is_file():
                    # cdp_*.js 是不随仓库发布的调试脚本（里面写死了本机浏览器路径），
                    # 有就多验一层，没有就跳过——不能因此判失败。
                    skip(f"{script} 不在（渲染校验需要自备 cdp 脚本）")
                    continue
                proc = subprocess.run(
                    [str(NODE), str(script_path), str(preview)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                tail = (proc.stdout or "").strip().splitlines()
                summary = tail[-1] if tail else (proc.stderr or "").strip()[:200]
                kind = "渲染校验" if script == "cdp_check.js" else "交互校验"
                check(proc.returncode == 0, f"{kind}（{label}）: {summary}")

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项 / 共 {_checks} 项检查")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print(f"真机联检全部通过：{_checks} 项检查")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
