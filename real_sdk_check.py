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
    check(hooks["observe_reply"]["mode"] == HookMode.OBSERVE.value, "observe_reply 是 OBSERVE（不改写回复）")
    check(hooks["inject_context"]["mode"] == HookMode.BLOCKING.value, "inject 是 BLOCKING")
    check(hooks["inject_context"]["order"] == HookOrder.LATE.value, "inject 是 LATE")
    check(hooks["inject_context"]["error_policy"] == ErrorPolicy.SKIP.value, "inject 是 SKIP")
    check(hooks["inject_context"]["timeout_ms"] == 2500, "inject 超时 2500ms")
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
    check(field_total == 145, f"schema 共 145 个配置项（实际 {field_total}）")
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
    # 这六项是代码里真的会调到的，一项都不能少：宿主按 manifest 签发能力令牌，
    # 没声明的那项会被 capability_service 直接回 E_CAPABILITY_DENIED。
    # 早期只声明了前三项，精力门控和告别语因此是「写了但调不通」。
    for need in (
        "config.get",
        "message.get_recent",
        "llm.generate",
        "frequency.get_adjust",
        "frequency.set_adjust",
        "maisaka.proactive.trigger",
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

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("真实 SDK 校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
