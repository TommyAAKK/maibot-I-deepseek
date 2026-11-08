"""静态校验 webui_page.html：语法、DOM id、绑定名、外链资源。

宿主把本文件整体塞进 <body>，CSP 为 default-src 'none'，所以任何外部资源
或语法错误都会让页面直接白屏。这个脚本在没有浏览器的情况下尽量把这类
问题挡住：
  1. 提取全部内联 <script>，用 node --check 逐个做语法检查；
  2. 校验 JS 里 getElementById('x') 用到的每个 id 都在 HTML 中存在；
  3. 校验 maibot.query('name') / maibot.action('name') 的 name 都在 webui.json 里声明；
  4. 校验没有任何外部资源引用（src=/href=http/url(http 等）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_node() -> Path | None:
    """找 node 可执行文件。

    优先 ``CGAP_NODE`` 环境变量，其次 PATH 上的 ``node``。都找不到时返回
    ``None``——调用方据此跳过需要 node 的那几项语法检查，而不是直接失败。
    """
    override = os.environ.get("CGAP_NODE")
    if override:
        return Path(override)
    found = shutil.which("node")
    return Path(found) if found else None


NODE = _find_node()

_failures: list[str] = []
_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        _failures.append(message)
        print(f"  FAIL {message}")
    else:
        print(f"  ok   {message}")


def extract_scripts(html: str) -> list[str]:
    return re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S | re.I)


def _function_source(html: str, name: str) -> str:
    """从页面源码里揪出一个 ``function name(...) { ... }`` 的完整源码。

    用来在 node 里实跑纯函数的分支逻辑 —— 字符串存在性检查测不出
    「昵称本身就是号」那种边界，而那恰恰是最容易渲染成重复数字的地方。
    按花括号配平找结尾，够用：页面里这些函数体内没有字符串模板里带花括号。
    """

    match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", html)
    if not match:
        return f"/* missing {name} */\n"
    depth = 0
    for index in range(match.end() - 1, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[match.start(): index + 1]
    return f"/* unterminated {name} */\n"


def _has_panel_class(html: str, element_id: str) -> bool:
    """某个 id 的元素是否带 ``cg-panel`` 类。"""
    pattern = rf'<div[^>]*class="[^"]*\bcg-panel\b[^"]*"[^>]*id="{re.escape(element_id)}"'
    pattern2 = rf'<div[^>]*id="{re.escape(element_id)}"[^>]*class="[^"]*\bcg-panel\b'
    return bool(re.search(pattern, html) or re.search(pattern2, html))


def _webui_title() -> str:
    """读 webui.json 里第一个页面的标题。"""
    path = HERE / "webui.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["pages"][0]["title"])
    except Exception:
        return ""


def _root_span(html: str) -> tuple[int, int] | None:
    """返回 `.cg-root` 那个 div 在源码里的 [开始, 结束) 区间。

    只数 div 的开闭标签，够用：页面里 .cg-root 的子树全是 div 结构，
    而且嵌套的 div 都成对出现。
    """
    match = re.search(r'<div[^>]*class="[^"]*\bcg-root\b[^"]*"[^>]*>', html)
    if not match:
        return None
    start = match.start()
    depth = 0
    for tag in re.finditer(r"<div\b[^>]*>|</div\s*>", html[start:], flags=re.I):
        if tag.group(0).lower().startswith("</"):
            depth -= 1
            if depth == 0:
                return start, start + tag.end()
        else:
            depth += 1
    return None


def _css_rule(html: str, pattern: str) -> str | None:
    """按正则抽出某条 CSS 规则的声明体（不含花括号）。"""
    match = re.search(pattern, html, flags=re.S | re.I)
    if not match:
        return None
    return match.group(1)


def is_inside_root(html: str, needle: str) -> bool:
    """判断 needle 出现的位置是否落在 `.cg-root` 的子树里。"""
    span = _root_span(html)
    if span is None:
        return False
    position = html.find(needle)
    if position < 0:
        return False
    return span[0] <= position < span[1]


def _div_ancestors(html: str) -> dict[str, list[str]]:
    """解析 div 嵌套，返回「带 id 的 div → 它的祖先 id 链」。

    只数 div 的开闭标签。弹窗曾经被嵌套错过一次（人物弹窗套进了情绪弹窗内部，
    父级 hidden 把子级一起藏了，点「编辑」看不到弹窗、点「调整」两个一起弹），
    光断言 id 存在抓不到这种结构错误，必须看祖先链。
    """
    body = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    stack: list[str] = []
    ancestors: dict[str, list[str]] = {}
    for tag in re.finditer(r"<div\b([^>]*)>|</div\s*>", body, flags=re.I):
        if tag.group(0).lower().startswith("</"):
            if stack:
                stack.pop()
            continue
        found = re.search(r'id="([^"]+)"', tag.group(1) or "")
        if found:
            ancestors[found.group(1)] = list(stack)
        stack.append(found.group(1) if found else "")
    return ancestors


def _is_descendant(html: str, child_id: str, ancestor_id: str) -> bool:
    """child_id 是否在 ancestor_id 内部。"""
    return ancestor_id in _div_ancestors(html).get(child_id, [])


def main() -> int:
    html_path = HERE / "webui_page.html"
    json_path = HERE / "webui.json"
    if not html_path.is_file():
        print(f"缺少 {html_path}")
        return 2
    html = html_path.read_text(encoding="utf-8")
    declaration = json.loads(json_path.read_text(encoding="utf-8"))

    print("[1] 内联脚本语法检查")
    scripts = extract_scripts(html)
    check(len(scripts) >= 1, f"页面含 {len(scripts)} 个内联 script 块")
    if NODE is None:
        print("  跳过：没找到 node（设置 CGAP_NODE 或把 node 放进 PATH）")
    for index, script in enumerate(scripts):
        if NODE is None:
            break
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(script)
            temp_name = handle.name
        try:
            proc = subprocess.run(
                [str(NODE), "--check", temp_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            check(
                proc.returncode == 0,
                f"script[{index}] 语法正确"
                + ("" if proc.returncode == 0 else f" -> {proc.stderr.strip()[:400]}"),
            )
        finally:
            Path(temp_name).unlink(missing_ok=True)

    print("[2] DOM id 引用检查")
    declared_ids = set(re.findall(r'\bid\s*=\s*"([^"]+)"', html))
    # 页面用 $('id') 简写包装了 getElementById，两种写法都要识别。
    used_ids = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", html))
    used_ids |= set(re.findall(r"(?<![\w$.])\$\(\s*['\"]([^'\"]+)['\"]\s*\)", html))
    missing = sorted(used_ids - declared_ids)
    check(not missing, f"JS 引用的 {len(used_ids)} 个 id 都存在（缺失: {missing}）")

    print("[3] 宿主绑定名检查")
    pages = declaration.get("pages") or []
    check(len(pages) == 1, f"webui.json 声明了 {len(pages)} 个页面")
    page = pages[0]
    declared_queries = set((page.get("queries") or {}).keys())
    declared_actions = set((page.get("actions") or {}).keys())
    # 页面把 window.maibot 存进局部变量 M，所以按「变量名.方法名」匹配。
    call_pattern = r"(?:window\.maibot|maibot|\bM)\.%s\(\s*['\"]([^'\"]+)['\"]"
    used_queries = set(re.findall(call_pattern % "query", html))
    used_actions = set(re.findall(call_pattern % "action", html))
    bad_queries = sorted(used_queries - declared_queries)
    bad_actions = sorted(used_actions - declared_actions)
    check(
        not bad_queries,
        f"query 调用 {sorted(used_queries)} 均已声明（多余: {bad_queries}）",
    )
    check(
        not bad_actions,
        f"action 调用 {sorted(used_actions)} 均已声明（多余: {bad_actions}）",
    )
    unused_actions = sorted(declared_actions - used_actions)
    check(not unused_actions, f"声明的 action 都被页面使用（未使用: {unused_actions}）")

    print("[4] 外链资源检查")
    check(not re.search(r"<script[^>]+\bsrc\s*=", html, re.I), "没有外部 <script src>")
    check(not re.search(r"<link\b", html, re.I), "没有 <link> 标签")
    check(
        not re.search(r"\b(?:src|href)\s*=\s*['\"]https?://", html, re.I),
        "没有 http(s) 绝对资源引用",
    )
    check(
        not re.search(r"url\(\s*['\"]?https?://", html, re.I),
        "CSS 里没有远程 url()",
    )
    check(not re.search(r"@import", html, re.I), "CSS 里没有 @import")
    check(
        not re.search(r"\bfetch\(|XMLHttpRequest|new WebSocket", html),
        "没有绕过 window.maibot 的直接网络调用",
    )

    print("[5] 页面片段约束检查")
    stripped = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    for tag in ("<html", "<head", "<body", "<!doctype"):
        check(tag not in stripped.lower(), f"片段中不含 {tag}>")

    print("[6] 危险 DOM 写入检查")
    check(
        not re.search(r"\.innerHTML\s*=", html),
        "没有使用 innerHTML 赋值（避免记忆内容里的 HTML 被解释）",
    )

    print("[7] 主题作用域检查")
    # .cg-root 上定义了 --cg-bg / --cg-fg 等主题变量，控件样式也全是
    # `.cg-root select` 这种后代选择器。任何用到这些变量的节点只要跑到
    # .cg-root 外面，var() 就会解析成未定义值、background 声明失效，
    # 于是弹窗背景透明、底下的卡片透出来。这类问题纯静态就能查出来。
    markup = stripped.split("<script>")[0]
    opens = len(re.findall(r"<div\b", markup, re.I))
    closes = len(re.findall(r"</div\s*>", markup, re.I))
    check(opens == closes, f"div 标签配平（{opens} 开 / {closes} 关）")
    check(is_inside_root(stripped, 'class="cg-modal"'), "弹窗在 .cg-root 内部（主题变量可见）")
    check(is_inside_root(stripped, 'class="cg-toast"'), "toast 在 .cg-root 内部（主题变量可见）")
    check(
        not re.search(r"\.cg-root\s+\.cg-modal", html),
        "弹窗样式没有被误写成 .cg-root 后代选择器",
    )

    print("[8] 弹窗定位检查（避免把宿主页面滚走）")
    # layout=document 时宿主把 iframe 高度撑成整页内容高度，iframe 内的
    # position:fixed 相对这个超高 iframe 定位。用 inset:0 会把弹窗居中到
    # iframe 垂直中点（在用户视野下方很远），focus() 再触发外层滚动。
    # 必须改用宿主下发的 --maibot-viewport-top / --maibot-viewport-height。
    modal_rule = _css_rule(html, r"\.cg-modal\s*\{([^}]*)\}")
    check(modal_rule is not None, "找到 .cg-modal 样式规则")
    if modal_rule is not None:
        check(
            "inset" not in modal_rule.replace(" ", ""),
            ".cg-modal 没有使用 inset（那会相对超高 iframe 定位）",
        )
        check(
            "--maibot-viewport-top" in modal_rule,
            ".cg-modal 用 --maibot-viewport-top 钉在可见区域顶部",
        )
        check(
            "--maibot-viewport-height" in modal_rule,
            ".cg-modal 用 --maibot-viewport-height 控制高度",
        )
    toast_rule = _css_rule(html, r"\.cg-toast\s*\{([^}]*)\}")
    check(toast_rule is not None, "找到 .cg-toast 样式规则")
    if toast_rule is not None:
        check(
            "--maibot-viewport-height" in toast_rule,
            ".cg-toast 按可见区域定位（不用 bottom，那会落到 iframe 底部）",
        )
    focus_calls = re.findall(r"\.focus\(([^)]*)\)", html)
    check(len(focus_calls) >= 2, f"找到 {len(focus_calls)} 处 focus() 调用")
    check(
        all("preventScroll" in call for call in focus_calls),
        "所有 focus() 都带 preventScroll（避免滚动宿主页面）",
    )

    print("[9] 宿主主题变量使用检查")
    # 页面颜色必须来自宿主注入的 --maibot-* 变量，否则用户换主题/换深色模式
    # 时页面还停在写死的配色上，观感与宿主不一致。
    check("--maibot-background" in html, "使用 --maibot-background")
    check("--maibot-surface" in html, "使用 --maibot-surface")
    check("--maibot-foreground" in html, "使用 --maibot-foreground")
    check("--maibot-primary" in html, "使用 --maibot-primary")
    check("--maibot-border" in html, "使用 --maibot-border")
    check("--maibot-muted" in html, "使用 --maibot-muted")
    github_blues = re.findall(r"#0969da|#0d1117|#1f2328|#f6f8fa|#d0d7de|#e6edf3|#30363d", html, re.I)
    check(not github_blues, f"没有残留 GitHub 配色写死值（命中: {sorted(set(github_blues))}）")
    # 兜底值必须仍然是 MaiBot 的橙色系，不能是别的产品的蓝。
    fallback = re.search(r"--cg-fb-primary:\s*([^;]+);", html)
    check(fallback is not None, "深色/浅色兜底主色已定义")
    if fallback is not None:
        value = fallback.group(1).strip().lower()
        check(
            value.startswith("#e0") or value.startswith("#d9") or value.startswith("#f"),
            f"兜底主色是 MaiBot 橙色系（{value}）",
        )

    print("[10] 面板不透明度检查")
    # 宿主的 --maibot-surface 采样自卡片背景色。future-retro 主题把卡片改成
    # `background-color: transparent !important`（dashboard/src/index.css:1008-1018），
    # 于是 --maibot-surface 就是 transparent。任何「只写 background: var(--cg-surface)」
    # 的元素在那种主题下会整块透明、底下内容透出来 —— 这就是弹窗透明两次复发的根因。
    # 正确写法是「先铺不透明的 --cg-bg，再用 --cg-surface 当渐变盖上去」，
    # 这样 surface 是实色时覆盖底色、是 transparent 时什么也不画。
    bare_surface = re.findall(
        r"(?<![-a-z])background(?:-color)?\s*:\s*var\(--cg-surface\)",
        re.sub(r"/\*.*?\*/", "", html, flags=re.S),
    )
    check(
        not bare_surface,
        f"没有裸用 background: var(--cg-surface)（命中 {len(bare_surface)} 处，"
        "该值在 future-retro 下是 transparent）",
    )
    check(
        "--cg-panel-image" in html and "linear-gradient(var(--cg-surface)" in html,
        "面板用 --cg-panel-image 两层画法（底色不透明 + surface 叠加）",
    )
    modal_box_rule = _css_rule(html, r"\.cg-modal-box\s*\{([^}]*)\}")
    check(modal_box_rule is not None, "找到 .cg-modal-box 样式规则")
    if modal_box_rule is not None:
        check(
            "background-color: var(--cg-bg)" in modal_box_rule.replace("\n", " ")
            or "--cg-panel-image" in modal_box_rule,
            "弹窗盒有确定的不透明底色（不是只靠 --cg-surface）",
        )

    print("[11] 宿主 dashboardStyle 适配检查")
    # 宿主把 context.dashboardStyle 写到根元素 data-dashboard-style
    # （dashboard/src/lib/plugin-custom-page.ts:75）。future-retro 是「无圆角 +
    # 墨线描边」的语言，页面必须按这个属性切换，否则观感与宿主明显割裂。
    check(
        "data-dashboard-style='future-retro'" in html,
        "按 data-dashboard-style 适配 future-retro 主题",
    )
    retro_rule = _css_rule(html, r"data-dashboard-style='future-retro'\]\s*\.cg-root\s*\{([^}]*)\}")
    check(retro_rule is not None, "找到 future-retro 的 .cg-root 变量覆盖规则")
    if retro_rule is not None:
        check("--cg-radius: 0px" in retro_rule, "future-retro 下圆角归零（与宿主一致）")
        check("--cg-stroke-width: 2px" in retro_rule, "future-retro 下描边加粗到 2px")

    print("[12] 纸面亮度对齐检查")
    # 宿主外壳和对话框都不是直接铺 --retro-paper，而是在它上面再叠一层
    # --retro-paper-brightness-overlay（rgb(255 252 242 / 0.24)，见
    # dashboard/src/index.css:594-597、:646-653、:1394-1406）。少叠这一层，
    # 页面就比宿主外壳暗一档 —— 用户反馈的「颜色深一点点」正是这个。
    check(
        "--cg-bg-shell" in html,
        "定义了 --cg-bg-shell（叠过提亮层的纸面底色）",
    )
    shell_fallback = re.search(r"--cg-bg-shell:\s*([^;]+);", html)
    check(shell_fallback is not None, "找到 --cg-bg-shell 的默认值")
    if shell_fallback is not None:
        check(
            "var(--cg-bg)" in shell_fallback.group(1),
            "默认 --cg-bg-shell 等于 --cg-bg（非 retro 主题不改变观感）",
        )
    retro_shell = _css_rule(
        html, r"data-dashboard-style='future-retro'\]\s*\.cg-root\s*\{([^}]*)\}"
    )
    check(retro_shell is not None, "找到 future-retro 的 .cg-root 规则")
    if retro_shell is not None:
        check(
            "color-mix" in retro_shell and "255 252 242" in retro_shell,
            "future-retro 下用 color-mix 复现宿主的 24% 提亮层",
        )
    # 深色 retro 的 --retro-paper-brightness-overlay 是 none（index.css:619），
    # 没有提亮层可叠，必须还原成原始纸色，否则会凭空发亮。
    check(
        re.search(
            r"html\.dark\[data-dashboard-style='future-retro'\]\s*\.cg-root\s*\{[^}]*"
            r"--cg-bg-shell:\s*var\(--cg-bg\)",
            html,
            re.S,
        )
        is not None,
        "深色 future-retro 还原原始纸色（该主题没有提亮层）",
    )
    # 根节点同时带 .maibot-page，而宿主的 context.styles 是 append 到 body 末尾的，
    # `.maibot-page{background-color:var(--maibot-background)}` 与 `.cg-root` 同特异性
    # 却排在后面，会把我们算好的提亮值盖掉。必须提一级特异性。
    check(
        re.search(r"\.cg-root\.maibot-page\s*\{[^}]*background-color:\s*var\(--cg-bg-shell\)", html, re.S)
        is not None,
        "用 .cg-root.maibot-page 提特异性，避免被宿主 .maibot-page 盖掉",
    )

    print("[13] 事件回调参数转发检查")
    # 来历：debounce 曾写成 `setTimeout(fn, delay)`，参数全丢，回调里的 event
    # 变成 undefined，`event.target.value` 抛 TypeError 被静默吞掉 ——
    # 表现为「搜索栏完全没反应」。这类 bug 静态也能抓一部分。
    debounce_body = _css_rule(html, r"function debounce\(fn, delay\)\s*\{([\s\S]*?)\n  \}")
    check(debounce_body is not None, "找到 debounce 实现")
    if debounce_body is not None:
        check(
            "arguments" in debounce_body and "apply" in debounce_body,
            "debounce 转发 this 与参数（否则回调收到 undefined）",
        )
        check(
            "setTimeout(fn," not in debounce_body.replace(" ", ""),
            "debounce 没有直接 setTimeout(fn, delay)（那会丢掉全部参数）",
        )
    # 延迟执行的回调里不能再读 event.target —— 那时事件对象已失效。
    search_handler = re.search(
        r"cg-f-search'\)\.addEventListener\('input',([\s\S]*?)\}, 320\)\);", html
    )
    check(search_handler is not None, "找到搜索框的 input 监听")
    if search_handler is not None:
        handler = search_handler.group(1)
        check(
            "event.target" not in handler,
            "搜索回调不依赖 event.target（改为直接读输入框的值）",
        )
        check(
            "cg-f-search').value" in handler or 'cg-f-search").value' in handler,
            "搜索回调直接读输入框当前值",
        )
        check(
            "state.filters.search" in handler,
            "搜索回调会更新 state.filters.search",
        )
        check(
            "loadItems()" in handler,
            "搜索回调会重新拉取列表",
        )
    # ── 记忆列表默认筛选「活跃」──
    #
    # 列表按显著度降序排，resolved / expired 里躺着大量高分历史条目。
    # 默认「全部状态」时第一屏全是已结束的话题，用户会以为「显著度没变」。
    check(
        "var DEFAULT_STATUS_FILTER = 'active'" in html,
        "定义了默认状态筛选常量且值为 active",
    )
    check(
        "status: DEFAULT_STATUS_FILTER" in html,
        "state.filters.status 初始值用该常量（不是空串）",
    )
    check(
        "fillSelect($('cg-f-status'), statusOptions, DEFAULT_STATUS_FILTER)" in html,
        "状态下拉框回显默认值（否则下拉显示与实际筛选不一致）",
    )
    check(
        "state.filters.status = DEFAULT_STATUS_FILTER" in html,
        "初始化时把 filters.status 一并设成默认值（只改 UI 会让后端仍收到空筛选）",
    )
    check(
        "{ value: '', label: '全部状态' }" in html,
        "「全部状态」仍保留在下拉框里（可选回看历史）",
    )

    # 所有 debounce 包裹的回调都不该再用 event 参数（延迟后事件对象不可靠）。
    check(
        not re.search(r"debounce\(function\s*\(event\)", html),
        "没有 debounce(function (event) ...) 这种写法",
    )

    print("[14] 情绪面板检查")
    primitives = ["joy", "anger", "anxiety", "sadness", "interest", "calm", "disgust", "surprise"]
    mood_ids = [
        "cg-mood", "cg-mood-empty", "cg-mood-body", "cg-mood-label", "cg-mood-reason",
        "cg-mood-adjust", "cg-mood-reset", "cg-mood-guide", "cg-mood-effort",
        "cg-mood-prims", "cg-mood-prims-title",
        "cg-mood-val-bar", "cg-mood-aro-bar", "cg-mood-eng-bar",
        "cg-mood-val-txt", "cg-mood-aro-txt", "cg-mood-eng-txt",
        "cg-mood-hist", "cg-mood-hist-cap", "cg-mood-modal",
        "cg-mood-in-eng", "cg-mood-in-eng-val",
        "cg-mood-save", "cg-mood-cancel",
    ]
    mood_ids += [f"cg-mood-in-{name}" for name in primitives]
    mood_ids += [f"cg-mood-in-{name}-val" for name in primitives]
    missing_mood = [i for i in mood_ids if f'id="{i}"' not in html]
    check(not missing_mood, f"情绪面板的 {len(mood_ids)} 个 id 都在（缺失: {missing_mood}）")
    check("M.query('mood'" in html, "页面会查询 mood")
    check("M.query('mood_history'" in html, "页面会查询 mood_history")
    check("M.action('mood_set'" in html, "页面会调用 mood_set")
    check("M.action('mood_reset'" in html, "页面会调用 mood_reset")
    # 情绪条的宽度只能靠 style.width，不能拼字符串进 innerHTML
    check(
        "cg-mood-' + name + '-bar').style.width" in html,
        "情绪条宽度走 style.width（不用 innerHTML）",
    )
    # 手动调整弹窗同样受 preventScroll 保护
    check(
        "$('cg-mood-in-joy').focus({ preventScroll: true })" in html,
        "调整弹窗的 focus() 也带 preventScroll",
    )
    check(
        "isInsideRoot" not in html and is_inside_root(stripped, 'id="cg-mood-modal"'),
        "情绪弹窗在 .cg-root 内部（主题变量可见）",
    )
    # 情绪面板必须与记忆块分开，不共用 companion-state 的预算
    check(
        "cg-mood-guide" in html and "include_guidance" in html,
        "情绪面板展示行为指令，并说明 include_guidance 开关",
    )
    # 精力耗尽状态：面板要显示「不再主动发言」而不是只把进度条压到 0
    check(
        "res.exhausted" in html and "cg-mood-effort exhausted" in html,
        "精力耗尽时面板显示 exhausted 状态",
    )
    check(
        "不再主动发言" in html,
        "耗尽状态下说明「不再主动发言、被 @ 才回」",
    )
    check(
        "res.desire" in html and "res.length" in html,
        "面板把精力拆成「回复欲望 + 回复长度」两块显示",
    )
    # 语气块必须读 res.tone 而不是 res.guidance：guidance 里已经拼了精力两句，
    # 再和下面的精力块并排显示，用户会看到同一段话出现两遍（真实出现过的缺陷）。
    check(
        "res.tone" in html,
        "语气块用 res.tone（只有语气，不含精力，避免与精力块重复）",
    )
    check(
        "cg-mood-effort-line" in html,
        "「回复欲望」「回复长度」各占一行（并排会连成一长串）",
    )
    # 用户明确要求精力不再影响发言频率，页面文案不能再说「发言精力 / 发言频率」。
    # 只看用户能看到的文案（带冒号的标签），代码注释里解释「为什么不这么做」不算。
    check(
        "'发言精力：'" not in html and "发言频率：" not in html,
        "页面不再把精力描述成发言频率",
    )
    # --cg-cat-avoid 从未定义过，写成它等于样式静默失效
    check(
        "--cg-cat-avoid" not in html,
        "不引用未定义的 --cg-cat-avoid 变量",
    )
    check(
        ".cg-mood-effort.exhausted" in html and "var(--cg-danger)" in html,
        "耗尽样式使用已定义的 --cg-danger",
    )

    # ── 八基元面板 ──
    # 用户要求八个维度每一个都能在配置页调整，页面也必须能看到每一个。
    missing_bar = [name for name in primitives if f"cg-mood-prim-' + name + '-bar" not in html]
    check(not missing_bar, f"八个情绪基元都渲染进度条（缺 {missing_bar}）")
    missing_edit = [name for name in primitives if f'id="cg-mood-in-{name}"' not in html]
    check(not missing_edit, f"调整弹窗里八个基元都有滑杆（缺 {missing_edit}）")
    check(
        "PRIMITIVE_ORDER = ['joy', 'anger', 'anxiety', 'sadness', 'interest', 'calm', 'disgust', 'surprise']"
        in html,
        "页面里的基元顺序与 cg_mood.EMOTIONS 一致",
    )
    check(
        "res.primitives" in html and "res.primitive_labels" in html and "res.baselines" in html,
        "页面用后端下发的基元值、中文名与基线，不自己硬编码",
    )
    # 基线参考线：只显示数值看不出「比平时强烈还是平淡」
    check(
        "mark.title = '基线 '" in html and "track.appendChild(mark)" in html,
        "进度条上画出基线参考线",
    )
    # 派生量不能再提交回去：mem_mood_set 明确忽略 valence/arousal
    check(
        "args[name] = Number($('cg-mood-in-' + name + '.value" not in html
        and "PRIMITIVE_ORDER.forEach(function (name) {\n      args[name]" in html,
        "保存时提交八个基元而不是 valence/arousal",
    )
    check(
        "valence: Number($('cg-mood-in-val')" not in html,
        "页面不再提交已废弃的 valence 滑杆",
    )

    # ── 人物面板 ──
    print("[15] 人物面板检查")
    person_ids = [
        "cg-people",
        "cg-people-list",
        "cg-people-empty",
        "cg-people-cap",
        "cg-person-modal",
        "cg-person-in-name",
        "cg-person-in-role",
        "cg-person-in-axes",
        "cg-person-in-affection",
        "cg-person-in-tags",
        "cg-person-in-tag-new",
        "cg-person-in-tag-add",
        "cg-person-save",
        "cg-person-del",
        "cg-person-cancel",
    ]
    missing_person = [name for name in person_ids if f'id="{name}"' not in html]
    check(not missing_person, f"人物面板的 {len(person_ids)} 个 id 都存在（缺 {missing_person}）")
    check(
        "M.query('people'" in html,
        "页面用 people 查询读取人物列表",
    )
    check(
        "M.action('person_update'" in html and "M.action('person_delete'" in html,
        "页面通过 person_update / person_delete 改动人物",
    )
    check(
        all(name in html for name in ("cg-people", "cg-people-list", "cg-person-modal")),
        "人物面板与弹窗都在页面内",
    )
    check(
        is_inside_root(html, 'id="cg-people"'),
        "人物面板在 .cg-root 内（否则继承不到主题变量）",
    )
    # 用户报过：点「认识的人」的编辑不弹窗，点情绪的「调整」反而两个弹窗一起出来。
    # 根因是 #cg-person-modal 被套进了 #cg-mood-modal 内部，父级 hidden 连子级一起藏。
    check(
        not _is_descendant(html, "cg-person-modal", "cg-mood-modal"),
        "人物弹窗没有被嵌进情绪弹窗（否则点编辑看不到、点调整两个一起弹）",
    )
    check(
        not _is_descendant(html, "cg-mood-modal", "cg-person-modal"),
        "情绪弹窗也没有被嵌进人物弹窗",
    )
    check(
        not _is_descendant(html, "cg-person-create-modal", "cg-mood-modal"),
        "新增人物弹窗同样不能嵌在情绪弹窗里",
    )
    # 手动设定的关系必须能看出来，否则用户不知道自动判断还能不能改它
    check(
        "cg-badge.manual" in html and "person.is_manual" in html,
        "手动设定的关系有单独的样式标记",
    )
    # 身份表由后端下发（配置页可增删身份），页面不能再写死四档；
    # 但后端没给过数据时（首次加载失败）也要能显示出一个像样的名字。
    check(
        "var ROLE_TABLE = [];" in html and "function roleLabelOf(role)" in html,
        "关系中文名由后端下发的身份表决定（配置页可自定义身份）",
    )
    check(
        "return key || '陌生人';" in html,
        "身份表还没到时有兜底显示，不会露出英文标识",
    )
    check(
        "fillRoleOptions(" in html and "<option value=\"owner\">" not in html,
        "关系下拉按配置动态生成，没有写死的内置身份",
    )
    check(
        "include_role" not in html and "inject_role_label" not in html,
        "页面不假设称呼一定会注入（称呼默认是关的）",
    )
    check(
        "cg-person-in-affection-val" in html and "toFixed(0)" in html,
        "好感度滑杆有数值回显",
    )
    check(
        "function levelHint(affection)" in html and "min_affection" in html,
        "好感度滑杆实时提示「现在算哪一档」（阈值可在配置页调）",
    )
    check(
        "cg-person-level-hint" in html and "cg-person-new-level-hint" in html,
        "两个人物弹窗都有分级提示",
    )
    check(
        "fillRoleOptions(\n      $('cg-person-in-role')," in html
        or "fillRoleOptions(" in html,
        "打开编辑弹窗时会重建身份下拉",
    )
    check(
        "配置里已没有这个身份" in html,
        "配置页删掉某个身份后，库里还有该身份的人不会被静默改成「不设定」",
    )
    check(
        "person.is_manual ? (person.relation_role || '') : ''" in html,
        "非手动设定的人打开弹窗时关系显示为「不设定」",
    )
    check(
        "toast('没有改动', 'err')" in html,
        "没改任何字段时不发多余的请求",
    )
    # 角色和密切度常常算出同一个词（好感度一般的人既是「朋友」又是「朋友」），
    # 那时再挂一枚一模一样的徽章只是噪音，看着像重复渲染。
    check(
        "person.intimacy_label !== roleLabel" in html,
        "密切度徽章与角色同名时不重复显示",
    )

    # ── 关系六维（用户第 34 轮）──
    # 「衡量人与人之间的情感与关系，并不能简单地依靠一个好感度的数值」。
    # 页面必须能编辑六个维度，且定义表由后端下发（不能前端再抄一份）。
    check(
        'id="cg-person-in-axes"' in html and 'id="cg-person-new-axes"' in html,
        "两个人物弹窗都有六维滑杆容器",
    )
    check(
        "function renderAxisSliders(container, values)" in html,
        "六维滑杆由统一函数渲染（不是内联一堆重复代码）",
    )
    check(
        "AXIS_TABLE" in html and "res.axes" in html,
        "六维定义表由后端下发（标签/说明/基线与 cg_person.py 同一份真相）",
    )
    check(
        "function collectAxisValues(container)" in html,
        "保存时从滑杆收集六维数值",
    )
    check(
        "args.axes = axes" in html,
        "六维改动会随 person_update / person_create 一起提交",
    )
    check(
        "function renderAxesMini(person)" in html and "cg-axes-mini" in html,
        "人物卡片上有六维小条（一眼看出哪一维偏离最多）",
    )
    check(
        "person.contrasts" in html and "cg-person-voice" in html,
        "卡片上显示矛盾描述（「很熟但有些话不跟你说」这种人话）",
    )
    # 好感度已经是派生量，页面不能再让用户直接拖它 —— 拖了会与六维自相矛盾。
    check(
        html.count('id="cg-person-in-affection" type="range"') >= 0
        and 'id="cg-person-in-affection" type="range" min="-100" max="100" step="5" value="0" disabled' in html,
        "编辑弹窗里的好感度滑杆是只读的（由六维算出来）",
    )
    check(
        'id="cg-person-new-affection" type="range" min="-100" max="100" step="5" value="0" disabled' in html,
        "新增弹窗里的好感度滑杆同样是只读的",
    )

    # ── 新增人物 ──
    # 用户要求「只需要给『认识的人』增加一个『新增』功能，让用户自行写入即可」，
    # 所以入口和弹窗都必须存在，且不能去读别的好感度插件。
    create_ids = [
        "cg-person-create",
        "cg-person-create-modal",
        "cg-person-new-id",
        "cg-person-new-name",
        "cg-person-new-platform",
        "cg-person-new-role",
        "cg-person-new-axes",
        "cg-person-new-affection",
        "cg-person-new-affection-val",
        "cg-person-new-tags",
        "cg-person-new-tag-new",
        "cg-person-new-tag-add",
        "cg-person-new-save",
        "cg-person-new-cancel",
    ]
    missing_create = [name for name in create_ids if f'id="{name}"' not in html]
    check(not missing_create, f"新增人物的 {len(create_ids)} 个 id 都存在（缺 {missing_create}）")
    check(
        "M.action('person_create'" in html,
        "页面通过 person_create 新增人物",
    )
    check(
        is_inside_root(html, 'id="cg-person-create-modal"'),
        "新增人物弹窗在 .cg-root 内（否则主题变量失效背景透明）",
    )
    check(
        "affection_from_plugin" not in html and "feyumy" not in html,
        "页面不读取任何好感度插件的数据（用户要求手填）",
    )
    check(
        "user_id" in html and "请填写对方的账号" in html,
        "新增时账号（user_id）是必填项",
    )
    # ── 人物卡片必须显示账号（用户第 44 轮：「QQ 号也应该显示出来」）──
    # 昵称能改、能重名，出问题时第一个要问的就是「这是哪个人」，所以账号
    # 得一直看得见；但昵称本身就是号的时候不能再重复一遍同样的数字。
    check(
        "platformLabel" in html and "accountText" in html,
        "人物卡片把平台名与账号翻成可读文字（QQ 100000001）",
    )
    check(
        "cg-person-id" in html and "accountText(person)" in html,
        "人物卡片标题行显示账号",
    )
    check(
        "id=\"cg-person-in-account\"" in html and "cg-person-in-account" in html,
        "编辑弹窗只读展示账号（改名改不动它）",
    )
    check(
        "改称呼不会改它" in html,
        "页面说明了账号与称呼的关系",
    )
    check(
        "没有它就没办法把「这个人」和聊天里出现的人对上" in html
        or "插件以后认人只看它" in html,
        "新增弹窗说明账号是认人的唯一依据",
    )
    # 这两个函数的分支逻辑只用字符串存在性测不出来 —— 真正容易出错的是
    # 「群名片本身就是账号」那条（真机上 100000004 就是把群名片设成了自己的
    # QQ 号，这是正常数据）。第一版为了少写一个重复数字，在这种情况只返回
    # 平台名，结果那一行变成光秃秃的「QQ」，账号反而丢了。所以现在断言的是
    # 「任何情况都带号码」，顺便把去重逻辑也钉住。
    js = "\n".join(
        _function_source(html, name) for name in ("platformLabel", "accountText")
    )
    js += """
const cases = [
  ['qq', '100000001', '群友A', 'QQ 100000001'],
  ['qq', '100000004', '100000004', 'QQ 100000004'],
  ['qq', '', '', ''],
  ['qq', '', '有名字没号', ''],
  ['telegram', '42', '小林', 'TG 42'],
  ['', '8888', '无名', '8888'],
];
let bad = [];
for (const [platform, user_id, display_name, want] of cases) {
  const got = accountText({platform, user_id, display_name});
  if (got !== want) bad.push(`${platform}/${user_id}/${display_name} -> ${JSON.stringify(got)} != ${JSON.stringify(want)}`);
}
if (bad.length) { console.error(bad.join('\\n')); process.exit(1); }
"""
    if NODE is None:
        print("  跳过：没找到 node（设置 CGAP_NODE 或把 node 放进 PATH）")
    else:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(js)
            temp_name = handle.name
        try:
            proc = subprocess.run(
                [str(NODE), temp_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            check(
                proc.returncode == 0,
                "账号文案任何情况都带号码（含群名片=账号、没号、非 QQ 平台）"
                + ("" if proc.returncode == 0 else f" -> {proc.stderr.strip()[:300]}"),
            )
        finally:
            Path(temp_name).unlink(missing_ok=True)
    # 名字与账号重复时只留账号一份 —— 这是渲染层的去重，不是把数字丢掉。
    check(
        "display && display !== uid" in html,
        "群名片等于账号时不在名字位置重复渲染",
    )
    check(
        "(未命名)" in html,
        "既没名字也没账号时给出占位文案",
    )
    # 「新增记忆」按钮按用户要求移到下方记忆模块里，不再留在页头
    check(
        "cg-memories" in html and 'id="cg-create"' in html
        and html.index('id="cg-memories"') < html.index('id="cg-create"'),
        "「新增记忆」按钮在记忆模块区域内（不在页头）",
    )

    # ── 人物标签可改可删（用户第 32 轮第 1 点）──
    # 原来只有一个「加一条备注」的输入框，加错了删不掉、也改不了。
    # 现在标签是一份可增删的列表，保存时整份替换。
    check(
        "editingTags" in html and "tags.splice(index, 1)" in html,
        "编辑人物时标签可以逐条删掉",
    )
    check(
        "args.tags = editingTags.slice()" in html,
        "保存时把整份标签发过去（覆盖而不是追加）",
    )
    check(
        "sameTags(editingTags, editingPerson.tags || [])" in html,
        "标签没改动时不提交（避免无意义的写入）",
    )
    check(
        "args.tags = newTags.slice()" in html,
        "新增人物时也会提交标签",
    )
    check(
        "abstract" not in html and "抽象的群友" in html,
        "标签输入框给出「XX的XX」写法的例子",
    )
    check(
        "person.tags" in html and "person.notes" not in html,
        "页面读的是 tags 而不是旧的 notes",
    )
    check(
        "'你打的标签'" in html,
        "卡片上的标签栏标成「你打的标签」",
    )

    # ── 三模块统一风格 ──
    # 用户指出「情绪、认识的人、记忆 这三个模块应该采取统一的模块风格，
    # 你现在做的界面把前两个单独框出来了」。三个模块必须共用同一个 .cg-panel 外壳，
    # 而不是各自画自己的边框。
    panels = ["cg-mood", "cg-people", "cg-memories"]
    check(
        all(f'class="cg-panel' in html and f'id="{name}"' in html for name in panels)
        or all(
            _has_panel_class(html, name) for name in panels
        ),
        "情绪 / 认识的人 / 记忆 三块都用 .cg-panel 统一外壳",
    )
    check(
        ".cg-panel {" in html and ".cg-panel-title" in html and ".cg-panel-cap" in html,
        "统一外壳的样式与标题类已定义",
    )
    # 旧的独立面板样式留着会让人以为还有两套外观
    check(
        ".cg-people {" not in html and ".cg-people-title" not in html,
        "不再保留「认识的人」的独立面板样式",
    )
    # 记忆模块的筛选条以前自带边框，套进 .cg-panel 后会变双框
    check(
        ".cg-toolbar {" in html and "padding: 0; margin-top: 0;" in html,
        "记忆模块的筛选条不再自绘边框（避免双框）",
    )
    # 用户第 32 轮第 3 点：统计行描述的是记忆，应该搬进「记忆」面板内部，
    # 不能再单独浮在「认识的人」和「记忆」中间。
    check(
        _is_descendant(html, "cg-stats", "cg-memories"),
        "统计行在「记忆」面板内部（不再单独浮在两个模块之间）",
    )
    check(
        (_div_ancestors(html).get("cg-stats") or [""])[-1] == "cg-memories",
        "统计行是「记忆」面板的直接子元素",
    )
    check(
        not _is_descendant(html, "cg-stats", "cg-people"),
        "统计行不在「认识的人」面板里",
    )

    # ── 八基元每行四个 ──
    # 用户指出「第一行有 6 个，第二行只有 2 个」，要求均分成每行 4 个。
    check(
        ".cg-mood-bars.prims" in html and "repeat(4, minmax(0, 1fr))" in html,
        "八个情绪基元按 grid 固定每行 4 个",
    )
    check(
        "@media (max-width: 720px)" in html and "repeat(2, minmax(0, 1fr))" in html,
        "窄屏下基元条退成每行 2 个",
    )
    check(
        'class="cg-mood-bars prims"' in html,
        "基元容器确实带 prims 类（否则 grid 规则不生效）",
    )

    # ── 页面名就是插件名 ──
    #
    # 宿主的侧边栏是「插件扩展 → <page.title>」两级。`workspace_title` 只有
    # 在 `placement == "workspace"` 时才会被当作分组标题使用，我们这个页面
    # 是 sidebar，所以那个字段不生效——想让名字显示出来，必须写在 page.title。
    check(
        _webui_title() == "我，deepseek",
        "webui.json 里页面标题是「我，deepseek」（sidebar 模式下只有这个会显示）",
    )

    # ── 记忆生命周期（第 32 轮第 4 点）──
    # 用户要求：记忆要能衰减、遗忘（标注为「遗忘」而非删除）、
    # 只有被明确强调才「想起」，并且能置顶（置顶不被遗忘）。
    check(
        "cg-badge st-forgotten" in html or ".cg-badge.st-forgotten" in html,
        "页面给「遗忘」状态单独一套样式",
    )
    check(
        ".cg-badge.st-forgotten" in html and "border-style: dashed" in html,
        "遗忘用虚线边框，和「已过期」的实线警告区分开",
    )
    check(
        "item.pinned" in html and "cg-badge pinned" in html,
        "置顶的条目在卡片上标出来",
    )
    check(
        "item.doubted" in html and "cg-badge doubted" in html,
        "存疑的条目在卡片上标出来",
    )
    check(
        "'置顶'" in html and "'取消置顶'" in html,
        "每条记忆都有置顶 / 取消置顶按钮",
    )
    check(
        "M.action('pin'" in html,
        "置顶按钮调用 pin action",
    )
    check(
        "item.status === 'forgotten'" in html and "'想起'" in html,
        "遗忘的条目有「想起」按钮",
    )
    check(
        "M.action('recall'" in html,
        "「想起」按钮调用 recall action",
    )
    check(
        "togglePin" in html and "recallItem" in html,
        "置顶与想起各有一个独立函数（不是内联一堆逻辑）",
    )
    check(
        "statusMap.forgotten" in html,
        "统计行把「遗忘」单独计数",
    )
    check(
        "['遗忘', statusMap.forgotten" in html,
        "统计行有「遗忘」这一项",
    )
    check(
        "items_pinned" in html and "items_doubted" in html,
        "统计行显示置顶数与存疑数",
    )
    check(
        "遗忘于 " in html and "想起于 " in html,
        "卡片上能看到遗忘时间和想起时间",
    )

    # ── 喜好面板（第 32 轮第 5 点）──
    prefs_ids = [
        "cg-prefs",
        "cg-prefs-list",
        "cg-prefs-empty",
        "cg-prefs-cap",
        "cg-prefs-tip",
        "cg-pref-create",
        "cg-pref-modal",
        "cg-pref-create-modal",
        "cg-pref-in-kind",
        "cg-pref-in-topic",
        "cg-pref-in-strength",
        "cg-pref-save",
        "cg-pref-cancel",
        "cg-pref-new-kind",
        "cg-pref-new-topic",
        "cg-pref-new-strength",
        "cg-pref-new-save",
        "cg-pref-new-cancel",
    ]
    missing_prefs = [item for item in prefs_ids if f'id="{item}"' not in html]
    check(not missing_prefs, f"喜好面板的 {len(prefs_ids)} 个元素都在（缺 {missing_prefs}）")
    check(_has_panel_class(html, "cg-prefs"), "喜好面板也套用统一外壳 .cg-panel")
    check(
        html.index('id="cg-people"') < html.index('id="cg-prefs"') < html.index('id="cg-memories"'),
        "喜好面板夹在「认识的人」和「记忆」中间（用户指定的位置）",
    )
    check(
        "M.query('prefs'" in html,
        "页面从 prefs 查询读喜好",
    )
    check(
        "M.action('pref_create'" in html
        and "M.action('pref_update'" in html
        and "M.action('pref_delete'" in html,
        "喜好的新增/修改/删除三个 action 都接了",
    )
    check(
        is_inside_root(html, 'id="cg-pref-modal"')
        and is_inside_root(html, 'id="cg-pref-create-modal"'),
        "两个喜好弹窗都在页面根节点内",
    )
    check(
        not _is_descendant(html, "cg-pref-modal", "cg-person-modal")
        and not _is_descendant(html, "cg-pref-create-modal", "cg-mood-modal"),
        "喜好弹窗没有被嵌进别的弹窗（上次人物弹窗就栽在这里）",
    )
    check(
        "editingPref" in html and "function renderPref" in html and "function loadPrefs" in html,
        "喜好有独立的渲染/加载/编辑状态",
    )
    check(
        "'喜欢' : '讨厌'" in html,
        "喜好卡片用中文区分喜欢与讨厌",
    )
    check(
        "pref.is_manual" in html and "自动观察不会改它" in html,
        "手写的口味在页面上标出「不会被自动改掉」",
    )
    check(
        "pref.kind === 'like'" in html and "cg-pref-bar" in html,
        "喜好卡片画出强度条",
    )
    check(
        "modalClosers" in html,
        "遮罩关闭用查表实现（否则新弹窗会被最后一个分支误关成情绪弹窗）",
    )

    print("[16] 折叠 / 分页 / 分段切换")
    # 用户第 43 轮：「给认识的人也用同样的模式折叠一下，每页五个」
    # 「喜好模块我觉得也可以折叠一下」。
    # 折叠的意义不是省空间，是让「记忆」不用滚很久才看得到 —— 记忆才是
    # 这个页面最常看的东西，而人物卡片和喜好行都比记忆条目高得多。
    for panel_id, fold_id, body_id in (
        ("cg-people", "cg-people-fold", "cg-people-body"),
        ("cg-prefs", "cg-prefs-fold", "cg-prefs-body"),
    ):
        check(
            f'id="{fold_id}"' in html and f'id="{body_id}"' in html,
            f"{panel_id} 有折叠按钮与可折叠的内容容器",
        )
        check(
            f'aria-controls="{body_id}"' in html and "aria-expanded" in html,
            f"{panel_id} 的折叠按钮带 aria 关联与展开状态",
        )
    # 默认折叠：两块都是「看一眼知道大概就够」的信息，展开才有内容。
    check(
        'class="cg-panel cg-people folded"' in html,
        "「认识的人」默认折叠",
    )
    check(
        'class="cg-panel cg-prefs folded"' in html,
        "「喜好」默认折叠",
    )
    check(
        ".cg-panel.folded .cg-panel-body" in html and "display: none" in html,
        "折叠用 CSS 藏内容而不是重排 DOM",
    )
    # 折叠时「新增」按钮一起藏起来：看不见内容却还能点新增，会让人以为是
    # 新建到了别的地方。
    check(
        ".cg-panel.folded .cg-panel-act" in html,
        "折叠时标题行右侧的操作按钮一起收起",
    )
    # 折叠状态不落 localStorage：每次打开都该是整洁的默认态。
    # 注意只查真实调用（setItem/getItem/removeItem），不查关键字 ——
    # 上面那条 CSS 注释里就写着「不写 localStorage」，按关键字查会把自己绊倒。
    check(
        "localStorage.setItem" not in html
        and "localStorage.getItem" not in html
        and "localStorage.removeItem" not in html,
        "折叠状态不写 localStorage（每次打开都是默认态）",
    )
    check(
        "function toggleFold" in html,
        "折叠逻辑抽成了一个函数（两块面板共用）",
    )

    # 人物分页：每页 5 个。一张人物卡片比一条记忆高得多，
    # 10 张叠起来一屏放不下，翻页就失去意义。
    check(
        "PEOPLE_PAGE_SIZE = 5" in html,
        "人物每页 5 个",
    )
    check(
        "function renderPeoplePage" in html and "peopleState.all" in html,
        "人物分页是客户端切片（查一次全量再切）",
    )
    check(
        "Math.ceil(all.length / peopleState.limit)" in html,
        "人物页数由总数算出，不写死",
    )
    # 删掉最后一个之后 offset 会越界，不钳回去就渲染出空白页。
    check(
        "page >= pages" in html and "peopleState.offset = page * peopleState.limit" in html,
        "删空最后一页后把 offset 钳回有效页（否则空白页）",
    )
    check(
        'id="cg-people-pager"' in html and "cg-people-prev" in html and "cg-people-next" in html,
        "人物面板有上一页 / 下一页",
    )
    check(
        "all.length <= peopleState.limit" in html,
        "一页就能装下时隐藏分页器",
    )

    # 喜好分段按钮，不做左右分栏。
    check(
        'id="cg-prefs-seg"' in html and 'role="tablist"' in html,
        "喜好用分段按钮切换，不是左右分栏",
    )
    check(
        'id="cg-prefs-tab-like"' in html and 'id="cg-prefs-tab-dislike"' in html,
        "喜欢 / 讨厌两个页签都在",
    )
    # 按钮上带条数：不点开也知道对面有几条，这是选择分段而非分栏后
    # 唯一丢掉的信息，必须补回来。
    check(
        'id="cg-prefs-n-like"' in html and 'id="cg-prefs-n-dislike"' in html,
        "两个页签上各带一个条数",
    )
    check(
        "function setPrefTab" in html and 'aria-selected' in html,
        "切换页签同步更新 aria-selected",
    )
    check(
        "var prefTab = 'like'" in html,
        "默认停在「喜欢」（实测 16 喜欢 / 4 讨厌，打开就落在信息多的一边）",
    )
    # 从「讨厌」页点新增，默认就该是讨厌 —— 刚在看的类别就是要加的类别。
    check(
        "$('cg-pref-new-kind').value = prefTab" in html,
        "新增喜好时默认选当前页签的类别",
    )
    check(
        ".cg-seg-btn.is-on" in html and "var(--cg-panel-image)" in html,
        "选中态用不透明底色 + surface 叠加（future-retro 下不会透明）",
    )
    # 折叠后标题行的摘要必须自己说清楚两边的量 —— 折起来只剩这一句。
    check(
        "'喜欢 ' + likes + ' / 讨厌 ' + dislikes" in html,
        "折叠时标题行摘要仍写全两边的数量",
    )

    print("[17] 念头 / 留念 / 主动面板")
    # 用户第 57 轮：「我们的 webui 中还没有做念头的模块」。
    # 三块都是「她自己想出来的东西」，性质上和上面那些「查记忆」的面板不同：
    # 记忆是记录，这些是倾向。所以三块一律默认折叠、都排在记忆下面。
    for panel_id, fold_id, body_id in (
        ("cg-thoughts", "cg-thoughts-fold", "cg-thoughts-body"),
        ("cg-keepsakes", "cg-keepsakes-fold", "cg-keepsakes-body"),
        ("cg-sparks", "cg-sparks-fold", "cg-sparks-body"),
    ):
        check(
            f'class="cg-panel {panel_id} folded" id="{panel_id}"' in html
            or f'class="cg-panel cg-{panel_id.split("-")[1]} folded" id="{panel_id}"' in html
            or (
                f'id="{panel_id}"' in html
                and f'id="{fold_id}"' in html
                and f'id="{body_id}"' in html
            ),
            f"{panel_id} 存在且有折叠按钮与内容容器",
        )
        check(
            f'aria-controls="{body_id}"' in html,
            f"{panel_id} 的折叠按钮带 aria 关联",
        )
    # 三块必须排在记忆面板**后面**：用户明确说了「放在记忆模块下面」。
    # 用「面板开始位置」的相对顺序判断，避免依赖具体行号。
    _order = {
        name: html.find(f'id="{name}"')
        for name in ("cg-memories", "cg-thoughts", "cg-keepsakes", "cg-sparks")
    }
    check(
        all(_order[name] > _order["cg-memories"] for name in ("cg-thoughts", "cg-keepsakes", "cg-sparks")),
        "念头 / 留念 / 主动三块都排在记忆面板后面",
    )
    check(
        _order["cg-thoughts"] < _order["cg-keepsakes"] < _order["cg-sparks"],
        "三块的顺序是念头 → 留念 → 主动（从「她在想什么」到「她做过什么」）",
    )
    # 折叠状态必须和标题行摘要一起工作：折起来只剩「还惦记着 N 件」这类字数。
    check(
        "cg-thoughts-cap" in html and "cg-keepsakes-cap" in html and "cg-sparks-cap" in html,
        "三块折叠后标题行都有摘要位置",
    )

    # 念头的来源必须显示。没有来源的念头就只是猜测——这一列是「她不是在背后
    # 议论人」的唯一可见证据，不能省。
    check(
        "cg-thought-source" in html and "'来自：' + item.source_text" in html,
        "念头卡片显示它出自哪一句话",
    )
    check(
        "if (item.source_text)" in html,
        "没有来源时不渲染空的「来自：」（旧数据可能没有）",
    )

    # 留念的「放掉」和「删除」必须是两个按钮两种语义。
    # 放掉是记录（还看得见），删除是抹掉（连「曾经留过」都没了）。
    check(
        "keepsake_release" in html and "keepsake_delete" in html,
        "留念同时有「放掉」和「删除」，两者分开",
    )
    check(
        "item.status !== 'released'" in html,
        "已经放掉的留念不再显示「放掉」按钮",
    )
    check(
        "'放掉于 ' + fmtTime(item.released_at)" in html,
        "放掉的留念标出放掉时间（记录而不是消失）",
    )
    # 摘要不能写成「已用 3/12」：那会读成待办进度条，而这一块刻意不是待办。
    check(
        "/ ' + capNum" not in html and "capNum" not in html.split("cg-keepsakes-cap")[1][:600],
        "留念摘要不显示「已用 x/12」这种暗示要清理的进度",
    )

    # 主动的「没开口」也要显示，并且带原因。
    check(
        "if (item.reason)" in html and "item.status_label" in html,
        "主动记录同时显示「开口了 / 没开口」和原因",
    )
    check(
        "'开口 ' + triggered + ' 次 / 没开口 ' + skipped + ' 次'" in html,
        "主动摘要分开写开口与没开口的次数（只写成功会让排查无从下手）",
    )

    # 三块都没有编辑入口：能改的只有删掉 / 放掉。
    # 改内容等于替她想，而「她想什么」正是这个功能本身。
    for modal_id in ("cg-thought-modal", "cg-keepsake-modal", "cg-spark-modal"):
        check(
            f'id="{modal_id}"' not in html,
            f"没有 {modal_id} —— 这三块不提供编辑（改内容等于替她想）",
        )

    # 空状态不能写成出错的样子：大部分时间它们本来就该是空的。
    for empty_text in ("没有搁着的事", "还没留下什么", "还没有自己想起来"):
        check(
            empty_text in html,
            f"空状态文案是中性描述而不是报错：{empty_text}",
        )

    # 加载器要挂在 boot 链上，否则页面打开时这几块永远是「正在读取…」。
    check(
        "loadThoughts()" in html and "loadKeepsakes()" in html and "loadSparks()" in html,
        "三个加载器都接上了",
    )
    _boot_chain = html.split("function boot()")[-1]
    check(
        "loadThoughts" in _boot_chain and "loadKeepsakes" in _boot_chain and "loadSparks" in _boot_chain,
        "boot 的加载链调用了这三个（否则永远停在「正在读取…」）",
    )
    # 刷新按钮要真的刷新整页，不能只刷记忆那三样。
    _reload_body = html.split("function reload()")[-1].split("function ")[0]
    for loader in ("loadMood", "loadPeople", "loadPrefs", "loadThoughts", "loadKeepsakes", "loadSparks"):
        check(
            loader in _reload_body,
            f"刷新按钮会重载 {loader}",
        )

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项 / 共 {_checks} 项检查")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{_checks} 项检查")
    return 0

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
