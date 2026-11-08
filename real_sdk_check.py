"""用真实的 maibot_sdk 校验装饰器与配置模型（不是 tests.py 里的 stub）。

运行：python real_sdk_check.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: 开发时用克隆的 SDK；在真实 MaiBot 环境里跑时 site-packages 里已有 SDK，优先用它。
SDK_ROOT = Path(os.environ.get("CGAP_SDK_ROOT") or (HERE.parent / ".tmp" / "maibot-sdk"))

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
try:  # 真实环境里直接可用
    import maibot_sdk as _sdk_probe  # noqa: F401
except ModuleNotFoundError:  # 开发环境回退到克隆
    if str(SDK_ROOT) not in sys.path:
        sys.path.insert(0, str(SDK_ROOT))
    import maibot_sdk as _sdk_probe  # noqa: F401

#: 下面解析能力代理要用它的真实路径，所以要留一个引用。
_sdk_module = _sdk_probe


def _manifest_id() -> str:
    """读出 ``_manifest.json`` 里的插件 ID。

    不写死字符串：ID 会随仓库地址变，写死就要在三个地方同步改，
    而漏改一处的后果是插件去一个新目录建空库——数据看起来「丢了」。
    """
    manifest_path = HERE / "_manifest.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        return str(json.load(handle).get("id") or "")

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def _check_manifest_with_host(expected_version: str, expected_id: str) -> None:
    """用宿主真实的 ManifestValidator 校验 manifest。

    需要 `.tmp/maibot-src` 可导入（额外依赖 packaging / structlog / tomlkit）；
    缺依赖时跳过而不是失败，因为插件本身并不依赖它们。

    Args:
        expected_version: 配置模型里的 ``CONFIG_VERSION``，用来确认 manifest 与之同步。
        expected_id: ``plugin.py`` 里的 ``PLUGIN_ID``。宿主按 manifest 的 id 建数据目录，
            两边不一致会让插件在换 ID 后读到空库。
    """

    source_root = Path(os.environ.get("CGAP_HOST_ROOT") or (HERE.parent / ".tmp" / "maibot-src"))
    if not source_root.is_dir():
        print("  跳过：没找到宿主源码目录")
        return
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    try:
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过：宿主校验器导入失败（{type(exc).__name__}: {exc}）")
        return

    host_version = os.environ.get("CGAP_HOST_VERSION", "1.3.18")
    validator = ManifestValidator(
        host_version=host_version,
        sdk_version="2.11.0",
        project_root=source_root,
        validate_python_package_dependencies=False,
        log_errors=False,
    )
    manifest = validator.load_from_plugin_path(HERE, require_entrypoint=True)
    check(manifest is not None, "宿主校验器接受 manifest")
    check(not validator.errors, f"宿主校验器无错误（{validator.errors}）")
    if manifest is not None:
        check(manifest.id == expected_id, f"宿主读到正确 id（{manifest.id}）")
        check(manifest.version == expected_version, f"宿主读到正确版本（{manifest.version}）")


def _check_capability_authorization() -> None:
    """用宿主真实的 AuthorizationManager 校验：代码里调到的能力是否都被授权。

    这一节是补一个真机上「静默失效」的教训：宿主按 manifest 的 ``capabilities``
    签发能力令牌，没声明的那项在 ``capability_service.handle_capability_request``
    里会被直接回 ``E_CAPABILITY_DENIED``。而 SDK 的代理层把 RPC 异常吞进
    ``except Exception``，插件侧只看到 ``None``，看起来像「宿主没实现」而不是
    「我没申请」。精力门控和告别语就这样躺了整整一周没人发现。
    """

    source_root = Path(os.environ.get("CGAP_HOST_ROOT") or (HERE.parent / ".tmp" / "maibot-src"))
    if not source_root.is_dir():
        print("  跳过：没找到宿主源码目录")
        return
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    try:
        from src.plugin_runtime.host.authorization import AuthorizationManager
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过：宿主授权器导入失败（{type(exc).__name__}: {exc}）")
        return

    manifest = json.loads((HERE / "_manifest.json").read_text(encoding="utf-8"))
    plugin_id = manifest["id"]
    auth = AuthorizationManager()
    auth.register_plugin(plugin_id, list(manifest.get("capabilities") or []))

    # 每一项都是代码里真的会调到的（代理方法 → 底层能力名）。
    used = {
        "ctx.config.get": "config.get",
        "ctx.llm.generate": "llm.generate",
        "call_capability('message.get_recent')": "message.get_recent",
        "ctx.frequency.get_adjust": "frequency.get_adjust",
        "ctx.frequency.set_adjust": "frequency.set_adjust",
        "ctx.maisaka.proactive.trigger": "maisaka.proactive.trigger",
        "call_capability('database.query')": "database.query",
        "ctx.api.call": "api.call",
    }
    for where, capability in used.items():
        allowed, reason = auth.check_capability(plugin_id, capability)
        check(allowed, f"{where} → {capability} 已授权" + ("" if allowed else f"（{reason}）"))


def _capability_proxy_map() -> dict[str, dict[str, str]]:
    """从真实 SDK 的 capabilities/*.py 里解析出「代理名 → {方法 → 能力名}」。

    用 AST 而不是 import：这些代理模块只是把 ``call_capability`` 包一层，
    静态读字面量足够，也不需要在没装 SDK 的环境里跑起来。
    """

    import ast

    package = Path(_sdk_module.__file__).resolve().parent / "capabilities"
    if not package.is_dir():
        return {}
    out: dict[str, dict[str, str]] = {}
    for path in sorted(package.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        methods: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            capability = None
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if not isinstance(func, ast.Attribute) or func.attr != "call_capability":
                    continue
                if (
                    sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)
                ):
                    capability = sub.args[0].value
            if capability:
                methods[node.name] = capability
        if methods:
            out[path.stem] = methods
    return out


def _check_capability_usage_declared() -> None:
    """扫插件源码，确认每一个用到的能力都写进了 manifest。

    ``_check_capability_authorization`` 只验证「manifest 里声明的都放行」，
    它天然发现不了**漏声明**——因为漏的那项压根没进检查清单。这一节补上
    另一半：从源码反推实际用到的能力集合，再和 manifest 对差。

    能识别三种写法：

    - 代理属性直调 ``ctx.config.get(...)`` / ``self._ctx.llm.generate(...)``
    - 裸 RPC ``call_capability("message.get_recent", ...)``
    - 先取出来再调 ``cap = getattr(self._ctx, "frequency", None); cap.get_adjust(x)``
      —— 精力门控就是这么写的，光靠「属性直调」和「字符串字面量」都会漏掉它，
      而这正好是当初漏声明的那两项，所以必须跟别名。
    """

    import ast

    proxies = _capability_proxy_map()
    if not proxies:
        print("  跳过：解析不到 SDK 的能力代理")
        return

    manifest = json.loads((HERE / "_manifest.json").read_text(encoding="utf-8"))
    declared = set(manifest.get("capabilities") or [])

    # 一次性调试脚本不参与发布，不算进「插件用到了什么」。
    skip_files = {
        "tests.py",
        "check_page.py",
        "check_live.py",
        "real_sdk_check.py",
        "bench.py",
        "fix_decay_bug.py",
    }

    class _Visitor(ast.NodeVisitor):
        """按源码顺序走一遍，维护「局部变量 → 代理链」的别名表。"""

        def __init__(self) -> None:
            self.found: dict[str, set[str]] = {}
            self.scopes: list[dict[str, list[str]]] = [{}]
            self.filename = ""

        # ── 作用域 ──
        def _enter(self, node: Any) -> None:
            self.scopes.append({})
            self.generic_visit(node)
            self.scopes.pop()

        visit_FunctionDef = _enter  # type: ignore[assignment]
        visit_AsyncFunctionDef = _enter  # type: ignore[assignment]

        def _note(self, capability: str) -> None:
            self.found.setdefault(capability, set()).add(self.filename)

        # ── 解析一条表达式指向哪个代理 ──
        def _chain(self, node: Any) -> list[str] | None:
            if isinstance(node, ast.Attribute):
                if node.attr in ("ctx", "_ctx"):
                    return []
                base = self._chain(node.value)
                return None if base is None else base + [node.attr]
            if isinstance(node, ast.Name):
                for scope in reversed(self.scopes):
                    if node.id in scope:
                        return scope[node.id]
                if node.id == "ctx":
                    return []
                return None
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
                    attr = node.args[1]
                    if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                        base = self._chain(node.args[0])
                        return None if base is None else base + [attr.value]
            return None

        def visit_Assign(self, node: ast.Assign) -> None:
            chain = self._chain(node.value)
            if chain and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.scopes[-1][node.targets[0].id] = chain
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            # 写法：call_capability("xxx.yyy", ...)
            if isinstance(func, ast.Attribute) and func.attr == "call_capability":
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    self._note(node.args[0].value)
            elif isinstance(func, ast.Attribute):
                chain = self._chain(func.value)
                if chain:
                    capability = proxies.get(chain[0], {}).get(func.attr)
                    if capability:
                        self._note(capability)
            self.generic_visit(node)

    used: dict[str, set[str]] = {}
    for path in sorted(HERE.glob("*.py")):
        if path.name in skip_files:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        visitor = _Visitor()
        visitor.filename = path.name
        visitor.visit(tree)
        for capability, where in visitor.found.items():
            used.setdefault(capability, set()).update(where)

    check(bool(used), f"源码里扫到 {len(used)} 项能力调用")
    missing = {cap: where for cap, where in used.items() if cap not in declared}
    if missing:
        for cap, where in sorted(missing.items()):
            print(f"      漏声明 {cap}（出现在 {', '.join(sorted(where))}）")
    check(not missing, "源码用到的能力全部写进了 manifest")
    # 反向：声明了却没用，说明清单在膨胀，也该清掉。
    unused = sorted(declared - set(used))
    check(not unused, f"manifest 没有声明用不到的能力（多出来的：{unused}）")


def _check_api_call_style() -> None:
    """扫源码里 ``ctx.api.call(...)`` 的调用方式。

    防的是这一类：适配器 API 的签名是
    ``async def api_action_xxx(self, params: Dict = None)`` ——**只接一个
    ``params``**。而 ``ctx.api.call(name, **kwargs)`` 会把每个键当独立关键字
    递给那个方法，于是 ``TypeError: got an unexpected keyword argument
    'user_id'``。

    这个错误为什么特别难发现：
    1. 异常在**另一个插件的 Runner 里**抛出，只写进宿主日志；
    2. 宿主 ``_cap_api_call`` 把它包成 ``{"success": False, "error": ...}``
       正常返回，我们这边看不到「抛了」；
    3. 如果下游的解析再认不出这个失败壳（我们之前就是），就会得出
       「0 条消息、没有错误」，于是日志写「补抓 0 条，已到缓存尽头」——
       一个越错越像正常的组合。
    从 2026-11-06 上线到 11-07 才发现，就是这么攒出来的。

    判据：**传参只能用 ``params=``，不许用 ``**`` 展开**。
    ``**`` 展开在这套 API 下必错，不管展开的是不是 OneBot 参数。
    """

    import ast

    offenders: list[str] = []
    scanned = 0
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith(("tests", "real_sdk", "check_", "bench", "build_", "fix_")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # 认 `xxx.api.call(...)` 与 `xxx.call(...)` 两种写法里带 api 的那种
            if not isinstance(func, ast.Attribute) or func.attr != "call":
                continue
            owner = func.value
            is_api = False
            if isinstance(owner, ast.Attribute) and owner.attr == "api":
                is_api = True
            elif isinstance(owner, ast.Name) and "api" in owner.id.lower():
                is_api = True
            if not is_api:
                continue
            scanned += 1
            for keyword in node.keywords:
                if keyword.arg is None:
                    offenders.append(f"{path.name}:{node.lineno} 用了 ** 展开")

    check(scanned > 0, f"源码里扫到 {scanned} 处 api.call 调用")
    check(
        not offenders,
        "**api.call 只用 params= 传参，不许 ** 展开**"
        f"（适配器签名只接一个 params；问题项：{offenders}）",
    )


#: 反序列化的 `item_type` 白名单在这几个文件里都可能出现——宿主 1.3.19
#: 把它从 ``src/llm_models/request_snapshot.py`` 挪到了
#: ``src/llm_models/payload_content/context_snapshot.py``。**旧路径找不到时
#: 这一节会静默跳过并打印「通过」**，等于白写，所以这里按候选列表挨个试，
#: 一个都找不到才算真的「宿主版本不认识」。
_SNAPSHOT_SOURCES: tuple[str, ...] = (
    "src/llm_models/payload_content/context_snapshot.py",
    "src/llm_models/request_snapshot.py",
)


def _host_supported_item_types(source_root: Path) -> set[str]:
    """从宿主源码里抽出「快照 item_type 认哪些字符串」。

    走静态解析而不是 ``import``：宿主那几个模块一导入就会连带启动
    配置加载、数据库迁移、表情包管理器，在插件自检里做这种事既慢又有副作用
    （实测还会因为真机库版本比源码新而直接抛
    ``DatabaseMigrationExecutionError``）。而我们要的信息只是几条
    ``if item_type == "XXX":`` 字面量，``ast`` 足够。
    """

    import ast

    found: set[str] = set()
    for relative in _SNAPSHOT_SOURCES:
        target = source_root / relative
        if not target.is_file():
            continue
        try:
            tree = ast.parse(target.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not isinstance(node.left, ast.Name) or node.left.id != "item_type":
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                    if isinstance(comparator.value, str) and comparator.value:
                        found.add(comparator.value)
        if found:
            break
    return found


def _check_planner_items_with_host(instance) -> None:
    """用宿主源码里的真实类型约束校验 planner 注入的 items。

    这条防的是**又一处「写了但从没生效」**。宿主
    ``serialize_context_item_snapshot()`` 用 ``item.__class__.__name__`` 当
    ``item_type``（``"SystemMessageItem"``），反序列化时按类名精确匹配；
    认不出就抛 ``不支持的 Context Item 快照类型: xxx``，
    ``chat_loop_service`` 捕获后**整批 items 丢弃**，只留一行
    ``Hook maisaka.planner.before_request 返回的 items 无效，已忽略``。

    我们一直写 ``"system"``，于是 planner 侧的状态提示从写下那天起就没进过
    一次请求——单元测试全过，因为它只检查自己拼的字典。

    这里不 ``import`` 宿主（会连带起数据库/配置/表情包管理器），而是静态
    抽出宿主认的 item_type 白名单，再把插件改写后的 items 逐个字段按宿主
    ``_deserialize_item_meta`` 的规则验一遍。
    """

    source_root = Path(os.environ.get("CGAP_HOST_ROOT") or (HERE.parent / ".tmp" / "maibot-src"))
    if not source_root.is_dir():
        print("  跳过：没找到宿主源码目录")
        return

    supported = _host_supported_item_types(source_root)
    if not supported:
        print(
            "  跳过：宿主这几个文件里都没解析出 item_type 白名单（"
            + " / ".join(_SNAPSHOT_SOURCES)
            + "）"
        )
        return
    check(
        "SystemMessageItem" in supported,
        f"宿主认 SystemMessageItem 这个类型名（白名单 {len(supported)} 项）",
    )

    import asyncio
    from datetime import datetime as _dt

    # 按宿主的快照格式造载荷（字段名与 serialize_context_item_snapshot 一致）。
    serialized = [
        {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": "real-sdk-system",
                "logical_turn_id": None,
                "timestamp": _dt.now().astimezone().isoformat(),
            },
            "parts": [{"type": "text", "text": "原有系统提示"}],
        },
        {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": "real-sdk-user",
                "logical_turn_id": None,
                "timestamp": _dt.now().astimezone().isoformat(),
            },
            "parts": [{"type": "text", "text": "今天天气怎么样"}],
        },
    ]

    kwargs = {
        "session_id": "qq:group:real-sdk-check",
        "items": serialized,
        "item_schema_version": 1,
        "tool_definitions": [],
        "reply_reason": "被使唤来使唤去",
    }

    # 打开边界感注入，逼出「往 items 里写东西」那条路径。
    try:
        base = instance.config
        instance._config_fallback = base.model_copy(
            update={
                "boundary": base.boundary.model_copy(
                    update={"enabled": True, "note_enabled": True, "note_to_planner": True}
                )
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过：调整配置失败（{type(exc).__name__}: {exc}）")
        return

    # 边界感渲染依赖 ``_store`` / ``_mood``，它们只有 ``on_load`` 之后才有值。
    # 自检里不启动插件（会真连数据库、真起后台任务），所以给两个最小替身：
    # 注入路径要读的东西不多，桩够用就行，重点是让「往 items 里写东西」
    # 这条分支真的被执行到。
    class _StubStore:
        def get_state(self, session_id: str) -> dict:
            return {"platform": "qq", "user_id": "10000001", "chat_type": "group"}

        def get_person(self, key: str):
            return None

        def list_prefs(self, **kwargs: Any):
            return []

    class _StubSnapshot:
        valence = 0.2
        anger = 0.0
        label = "平静"

    class _StubMood:
        def snapshot(self, **kwargs: Any):
            return _StubSnapshot()

        def can_initiate(self, **kwargs: Any) -> bool:
            return True

    class _StubLogger:
        def debug(self, *args: Any, **kwargs: Any) -> None:
            return None

        def info(self, *args: Any, **kwargs: Any) -> None:
            return None

        def warning(self, *args: Any, **kwargs: Any) -> None:
            return None

        def error(self, *args: Any, **kwargs: Any) -> None:
            return None

    class _StubCtx:
        logger = _StubLogger()

    instance._store = _StubStore()
    instance._mood = _StubMood()
    # ``_debug`` / ``_warn`` 走 ``self.ctx.logger``，没有 ctx 会抛
    # 「插件上下文尚未初始化」——注入路径里没有 try 包住日志，所以桩必须给上。
    instance._ctx = _StubCtx()

    try:
        result = asyncio.run(instance.inject_planner_state(**kwargs))
    except Exception as exc:  # noqa: BLE001
        check(False, f"inject_planner_state 可调用（{type(exc).__name__}: {exc}）")
        return

    modified = (result.get("modified_kwargs") or {}).get("items")
    check(isinstance(modified, list) and bool(modified), "inject_planner_state 返回了 items")
    if not isinstance(modified, list) or not modified:
        return
    # 逐个 item 按宿主规则验：类型名在白名单里、meta 三件套合法。
    bad_types: list[str] = []
    bad_meta: list[str] = []
    for index, item in enumerate(modified):
        if not isinstance(item, dict):
            bad_types.append(f"#{index} 不是字典")
            continue
        item_type = item.get("item_type")
        if not isinstance(item_type, str) or item_type not in supported:
            bad_types.append(f"#{index} {item_type!r}")
            continue
        meta = item.get("meta")
        if not isinstance(meta, dict):
            bad_meta.append(f"#{index} meta 不是字典")
            continue
        item_id = meta.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            bad_meta.append(f"#{index} item_id 非法")
        if "logical_turn_id" not in meta:
            bad_meta.append(f"#{index} 缺 logical_turn_id 键")
        stamp = meta.get("timestamp")
        if not isinstance(stamp, str) or not stamp.strip():
            bad_meta.append(f"#{index} timestamp 非法")
        else:
            try:
                _dt.fromisoformat(stamp)
            except ValueError:
                bad_meta.append(f"#{index} timestamp 不是合法 ISO")
    check(not bad_types, f"插件写回的 item_type 都在宿主白名单里（问题项 {bad_types}）")
    check(not bad_meta, f"插件写回的 meta 满足宿主协议（问题项 {bad_meta}）")

    # item_id 必须唯一，否则宿主 validate_context_items 会抛「存在重复 item_id」。
    ids = [
        (item.get("meta") or {}).get("item_id")
        for item in modified
        if isinstance(item, dict) and isinstance(item.get("meta"), dict)
    ]
    check(len(ids) == len(set(ids)), f"item_id 互不重复（{len(ids)} 条）")

    # 注入的文字必须真的落在某条 system 消息里。
    joined = ""
    for item in modified:
        if not isinstance(item, dict):
            continue
        for part in item.get("parts") or ():
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    joined += text + "\n"
    check("原有系统提示" in joined, "原有系统提示仍在（没有被覆盖掉）")
    check(
        "此刻的你" in joined,
        "边界感状态提示真的进了 items（宿主不会再整批丢弃）",
    )


def _check_webui_declaration(instance) -> None:
    """用宿主真实的 load_webui_extension 校验 webui.json，并交叉核对 API。

    这一步能抓住三类只有真机才会暴露的问题：

    1. 声明文件本身不合规（schema_version、capabilities、entry 路径、大小）；
    2. ``api`` 短名在插件里没有对应的 ``@API`` 方法（宿主会 404）；
    3. ``parameters`` 与处理函数签名对不上（宿主按声明校验后 ``**args`` 直调，
       多余参数直接 TypeError、漏掉必填参数则永远走默认值）。
    """

    source_root = Path(os.environ.get("CGAP_HOST_ROOT") or (HERE.parent / ".tmp" / "maibot-src"))
    if not source_root.is_dir():
        print("  跳过：没找到宿主源码目录")
        return
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    try:
        from src.plugin_runtime.webui_schema import load_webui_extension
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过：webui_schema 导入失败（{type(exc).__name__}: {exc}）")
        return

    try:
        extension = load_webui_extension(str(HERE))
    except Exception as exc:  # noqa: BLE001
        check(False, f"宿主接受 webui.json（{type(exc).__name__}: {exc}）")
        return

    check(extension is not None, "宿主读到 webui.json")
    if extension is None:
        return
    check(extension.schema_version == 2, "schema_version 为 2")
    check("custom_pages" in extension.required_capabilities, "声明 custom_pages 能力")
    check(len(extension.pages) == 1, "只有 1 个页面")

    page = extension.pages[0]
    check(getattr(page, "mode", "") == "custom", "页面是 custom 模式")
    check(getattr(page, "layout", "") == "document", "布局 document（按内容高度自适应）")
    document = getattr(page, "document", "")
    check(bool(document.strip()), "页面 HTML 非空")
    print(f"  页面 {page.id!r} / entry={getattr(page, 'entry', '?')} / {len(document)} 字符")

    # 片段会被宿主塞进 <body>，出现整文档标签说明写错了层级。
    # 先剥掉注释，否则文档里"不要写 <body> 标签"这类说明文字会被误判。
    import re

    stripped = re.sub(r"<!--.*?-->", "", document, flags=re.S).lower()
    for tag in ("<html", "<head", "<body"):
        check(tag not in stripped, f"页面片段不含 {tag}>")
    # CSP 是 default-src 'none'，任何外链都会静默失败。
    check("http://" not in document and "https://" not in document, "页面无外链资源")
    check("window.maibot" in document, "页面通过 window.maibot 通信")
    check("4000" in document, "页面与服务端的 4000 字上限一致")

    # 收集插件实例上的 @API 方法名。
    import inspect
    import typing

    from maibot_sdk.components import collect_components

    api_names = {
        str(info["name"]) for info in collect_components(instance) if str(info["type"]) == "API"
    }
    print(f"  插件 API: {sorted(api_names)}")

    # boolean 允许写成 ``bool | None``：像 mem_update.pinned 这种「不传表示不改」
    # 的开关必须能表达「没给」这个状态，注解天然是 Optional[bool]，而宿主按
    # 声明类型 boolean 校验后传进来的仍是 True/False。
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": (bool, bool | None),
    }

    for kind in ("queries", "actions"):
        bindings = getattr(page, kind)
        check(bool(bindings), f"{kind} 非空")
        for name, binding in bindings.items():
            check(binding.api in api_names, f"{kind}.{name} 的 api={binding.api!r} 有对应 @API 方法")
            handler = getattr(instance, binding.api, None)
            if handler is None:
                continue
            try:
                signature = inspect.signature(handler)
            except (TypeError, ValueError):
                continue
            # 模块开了 from __future__ import annotations，注解是字符串，需要求值。
            try:
                hints = typing.get_type_hints(handler)
            except Exception:  # noqa: BLE001
                hints = {}
            params = signature.parameters
            accepts_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())

            # 声明的参数必须能被方法接收
            for param_name in binding.parameters:
                check(
                    accepts_kwargs or param_name in params,
                    f"{binding.api} 能接收参数 {param_name}",
                )

            # 方法的必填参数必须在声明里也标 required，否则调用永远缺参
            for param_name, param in params.items():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                if param.default is not inspect.Parameter.empty:
                    continue
                declared = binding.parameters.get(param_name)
                check(
                    declared is not None and declared.required,
                    f"{binding.api} 的必填参数 {param_name} 在 webui.json 里声明为 required",
                )

            # 类型要和注解对得上（宿主按声明的类型校验后才传进来）
            for param_name, declared in binding.parameters.items():
                expected = type_map.get(declared.type)
                if expected is None or param_name not in params:
                    continue
                annotation = hints.get(param_name, params[param_name].annotation)
                if annotation is inspect.Parameter.empty:
                    continue
                if isinstance(expected, tuple):
                    matched = annotation in expected
                else:
                    matched = annotation is expected
                check(
                    matched,
                    f"{binding.api}.{param_name} 注解 {annotation!r} 与声明类型 {declared.type} 一致",
                )


def main() -> int:
    print("[1] 真实 SDK 可用性")
    import maibot_sdk

    check(hasattr(maibot_sdk, "MaiBotPlugin"), "maibot_sdk 导出 MaiBotPlugin")
    from maibot_sdk import HookHandler, MaiBotPlugin, PluginConfigBase  # noqa: F401
    from maibot_sdk.components import collect_components
    from maibot_sdk.types import ComponentType, ErrorPolicy, HookMode, HookOrder

    print(f"  sdk path = {maibot_sdk.__file__}")
    print(f"  sdk version = {getattr(maibot_sdk, '__version__', '?')}")

    print("[2] 插件可实例化并收集组件")
    # 宿主 Runner 用 spec_from_file_location(..., submodule_search_locations=[插件目录])
    # 把 plugin.py 当「包」加载，插件内部一律相对导入；这里必须用同样方式加载。
    import importlib
    import importlib.util

    pkg_name = "_cgap_real_sdk_check"
    pkg_spec = importlib.util.spec_from_file_location(
        pkg_name,
        os.path.join(str(HERE), "plugin.py"),
        submodule_search_locations=[str(HERE)],
    )
    if pkg_spec is None or pkg_spec.loader is None:
        raise RuntimeError("无法为 plugin.py 构造模块 spec")
    pkg_module = importlib.util.module_from_spec(pkg_spec)
    sys.modules[pkg_name] = pkg_module
    pkg_spec.loader.exec_module(pkg_module)
    plugin_mod = pkg_module

    instance = plugin_mod.create_plugin()
    check(isinstance(instance, MaiBotPlugin), "create_plugin 返回 MaiBotPlugin")
    check(plugin_mod.PLUGIN_ID == _manifest_id(), f"PLUGIN_ID 与 manifest 一致（{plugin_mod.PLUGIN_ID}）")

    infos = collect_components(instance)
    by_type: dict[str, list[str]] = {}
    for info in infos:
        by_type.setdefault(str(info["type"]), []).append(str(info["name"]))
    print(f"  组件: {json.dumps(by_type, ensure_ascii=False)}")

    check(len(by_type.get("HOOK_HANDLER", [])) == 4, "注册了 4 个 HookHandler")
    check(len(by_type.get("COMMAND", [])) == 1, "注册了 1 个 Command")

    hooks = {str(i["name"]): i["metadata"] for i in infos if i["type"] == "HOOK_HANDLER"}
    check("capture_message" in hooks, "capture_message 存在")
    check("inject_context" in hooks, "inject_context 存在")
    check("observe_reply" in hooks, "observe_reply 存在")
    check("inject_planner_state" in hooks, "inject_planner_state 存在")
    check(
        hooks["inject_planner_state"]["hook"] == "maisaka.planner.before_request",
        "planner 状态注入挂在 planner.before_request（决定要不要回之前）",
    )
    check(
        hooks["inject_planner_state"]["mode"] == HookMode.BLOCKING.value,
        "planner 状态注入是 BLOCKING（要能改 items）",
    )
    check(hooks["capture_message"]["hook"] == "chat.receive.after_process", "capture hook 名正确")
    check(hooks["inject_context"]["hook"] == "maisaka.replyer.before_request", "inject hook 名正确")
    check(
        hooks["observe_reply"]["hook"] == "maisaka.replyer.after_response",
        "observe_reply 挂在 replyer.after_response（观察 Bot 自己说了什么）",
    )
    # after_response 从 OBSERVE 改成 BLOCKING 是「冷场兜底」的前提：
    # 观察型处理器返回的 modified_kwargs 会被宿主直接丢掉
    # （hook_dispatcher.py:695-696），而 retry 只可能通过回传生效。
    check(
        hooks["observe_reply"]["mode"] == HookMode.BLOCKING.value,
        "observe_reply 是 BLOCKING（重生成只能靠回传 kwargs）",
    )
    check(hooks["inject_context"]["mode"] == HookMode.BLOCKING.value, "inject 是 BLOCKING")
    check(hooks["inject_context"]["order"] == HookOrder.LATE.value, "inject 是 LATE")
    check(hooks["inject_context"]["error_policy"] == ErrorPolicy.SKIP.value, "inject 是 SKIP")
    # 4000ms 而不是宿主的 6000ms：本机 8 个插件串行挂在同一个 planner hook
    # 上，2500ms 曾是整条链最紧的一环，连续 3 次超时会被宿主熔断 60~300 秒
    # （circuit_breaker._FAILURE_THRESHOLD = 3），熔断期间整个插件在这个 hook
    # 上静默跳过——表现和「闸门把她永久静音」一模一样，但根因完全不同。
    check(
        hooks["inject_context"]["timeout_ms"] == plugin_mod.INJECT_TIMEOUT_MS,
        "inject 超时与 INJECT_TIMEOUT_MS 一致",
    )
    check(
        plugin_mod.INJECT_TIMEOUT_MS == 4000,
        "inject 超时 4000ms（留出串行链余量，别撞熔断）",
    )
    check(
        hooks["inject_planner_state"]["timeout_ms"] == plugin_mod.INJECT_TIMEOUT_MS,
        "planner 注入与 inject 共用同一个超时预算",
    )
    check(hooks["capture_message"]["mode"] == HookMode.OBSERVE.value, "capture 是 OBSERVE")

    cmd = next(i for i in infos if i["type"] == "COMMAND")
    cmd_meta = cmd["metadata"]
    check(str(cmd_meta["command_pattern"]).startswith("(?i)"), "命令带正则")
    check(cmd_meta.get("permission") == "operator", "命令声明 operator 权限")

    print("[3] 配置模型与 schema")
    CompanionGapConfig = plugin_mod.CompanionGapConfig

    default_config = CompanionGapConfig()
    check(
        default_config.plugin.config_version == plugin_mod.CONFIG_VERSION,
        f"默认配置版本 {plugin_mod.CONFIG_VERSION}",
    )
    check(default_config.extract.task_name == "utils", "默认任务名 utils")
    check(default_config.inject.header == "<companion-state>", "默认注入 header")
    check(len(default_config.extract.categories) == 5, "默认五类")

    rebuilt = CompanionGapConfig(**default_config.model_dump())
    check(rebuilt.model_dump() == default_config.model_dump(), "配置可往返序列化")

    schema = plugin_mod.CompanionGapPlugin.build_config_schema(
        plugin_id=plugin_mod.PLUGIN_ID,
        plugin_name="我，deepseek",
        plugin_version=plugin_mod.CONFIG_VERSION,
        plugin_description="补 A_Memorix 没有的情境记忆",
        plugin_author="local",
    )
    check(isinstance(schema, dict), "生成配置 schema")
    sections = schema.get("sections") or {}
    print(f"  schema sections = {list(sections.keys())}")
    for expected in (
        "plugin",
        "capture",
        "extract",
        "inject",
        "retention",
        "mood",
        "people",
        "prefs",
        "absence",
        "boundary",
        "presence",
        "backfill",
        "initiative",
        "history",
        "thought",
        "keepsake",
        "proactive",
        "debug",
    ):
        check(expected in sections, f"schema 含 {expected} 节")

    # 配置页显示的是 json_schema_extra 里的 label / hint，不是 description。
    # 这里用宿主真实 SDK 生成的 schema 反查，确保页面上不会出现英文裸键名。
    def _has_cjk(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text)

    plain_labels: list[str] = []
    plain_hints: list[str] = []
    field_total = 0
    for section_name, section_schema in sections.items():
        for field_name, field_schema in (section_schema.get("fields") or {}).items():
            field_total += 1
            label = str(field_schema.get("label") or "")
            hint = str(field_schema.get("hint") or "")
            if label == field_name or not _has_cjk(label):
                plain_labels.append(f"[{section_name}].{field_name}={label!r}")
            if not hint or not _has_cjk(hint):
                plain_hints.append(f"[{section_name}].{field_name}={hint!r}")
    check(field_total == 192, f"schema 共 192 个配置项（实际 {field_total}）")
    check(not plain_labels, f"每个配置项的 label 都是中文（问题项 {plain_labels}）")
    check(not plain_hints, f"每个配置项的 hint 都是中文（问题项 {plain_hints}）")

    # 抽一个字段看实际下发值，方便肉眼确认
    sample = sections["inject"]["fields"]["max_chars"]
    print(f"  样例 [inject].max_chars → label={sample['label']!r} hint={sample['hint']!r}")
    check(sample["label"] == "注入字数上限", "样例 label 符合预期")
    mood_sample = sections["mood"]["fields"]["llm_blend_weight"]
    print(
        f"  样例 [mood].llm_blend_weight → label={mood_sample['label']!r} "
        f"hint={mood_sample['hint']!r}"
    )
    check(mood_sample["label"] == "模型判断权重", "情绪节也下发了中文 label")

    # 用户第 33 轮第 2 点要求「身份可以自由配置、每档提示词在配置页可见可编辑」。
    # 靠的是 list[PluginConfigBase] → item_type=object + item_fields，配置页会把它
    # 渲染成可增删的表格（前端 ListFieldEditor 按 item_fields 逐列渲染）。
    for field_name, wanted in (
        ("roles", {"key", "label", "tone"}),
        ("intimacy_levels", {"key", "label", "min_affection", "tone"}),
    ):
        spec = sections["people"]["fields"][field_name]
        print(
            f"  样例 [people].{field_name} → type={spec.get('type')!r} "
            f"item_type={spec.get('item_type')!r}"
        )
        check(spec.get("type") == "array", f"[people].{field_name} 是数组")
        check(spec.get("ui_type") == "list", f"[people].{field_name} 用列表编辑器渲染")
        check(
            spec.get("item_type") == "object",
            f"[people].{field_name} 的每行是结构化对象（配置页才会出多个输入框）",
        )
        item_fields = spec.get("item_fields") or {}
        check(
            wanted <= set(item_fields),
            f"[people].{field_name} 每行含 {sorted(wanted)}",
        )
        check(
            all(str(item.get("label") or "").strip() for item in item_fields.values()),
            f"[people].{field_name} 每个子字段都有中文名（不让用户看英文键）",
        )
    check(
        len(sections["people"]["fields"]["roles"].get("item_fields") or {}) == 3,
        "身份表每行就是「标识 / 称呼 / 提示词」三格",
    )

    print("[4] manifest 与代码一致性")
    manifest = json.loads((HERE / "_manifest.json").read_text(encoding="utf-8"))
    check(manifest["id"] == plugin_mod.PLUGIN_ID, "manifest id 与 PLUGIN_ID 一致")
    check(
        manifest["version"] == plugin_mod.CONFIG_VERSION,
        f"manifest 版本与配置版本一致（{manifest['version']}）",
    )
    check(manifest["manifest_version"] == 2, "manifest_version 2")
    caps = set(manifest.get("capabilities") or [])
    # 这八项是代码里真的会调到的，一项都不能少：宿主按 manifest 签发能力令牌，
    # 没声明的那项会被 capability_service 直接回 E_CAPABILITY_DENIED。
    # 早期只声明了前三项，精力门控和告别语因此是「写了但调不通」。
    # 后来加「在场感知」用了 database.query、加「历史补抓」用了 api.call，
    # 两处都是先写代码后补声明，于是同样的坑又踩了一次（见 README 已知限制）。
    for need in (
        "config.get",
        "message.get_recent",
        "llm.generate",
        "frequency.get_adjust",
        "frequency.set_adjust",
        "maisaka.proactive.trigger",
        "database.query",
        "api.call",
    ):
        check(need in caps, f"capabilities 声明 {need}")
    check("host_application" in manifest and "sdk" in manifest, "manifest 含双版本区间")

    print("[5] 命令正则实际匹配")
    import re

    pattern = re.compile(str(cmd_meta["command_pattern"]))
    for text, expect_action in (
        ("mcompgap", None),
        ("/mcompgap status", "status"),
        ("！mcompgap list 2", "list"),
        ("mcompgap help", "help"),
    ):
        match = pattern.search(text)
        check(match is not None, f"正则匹配 {text!r}")
        if match is not None:
            check(match.groupdict().get("action") == expect_action, f"{text!r} 解析 action")

    print("[6] 用宿主真实 ManifestValidator 校验（可选）")
    _check_manifest_with_host(plugin_mod.CONFIG_VERSION, plugin_mod.PLUGIN_ID)

    print("[7] 用宿主真实 WebUI 声明校验（可选）")
    _check_webui_declaration(instance)

    print("[8] 用宿主真实授权器校验能力令牌（可选）")
    _check_capability_authorization()

    print("[9] 源码用到的能力是否都写进了 manifest")
    _check_capability_usage_declared()

    print("[10] 用宿主真实反序列化器校验 planner 注入（可选）")
    _check_planner_items_with_host(instance)

    print("[11] 适配器 API 的调用方式")
    _check_api_call_style()

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("真实 SDK 校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
