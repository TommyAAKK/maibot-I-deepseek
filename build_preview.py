"""生成一个离线预览页，用于在无浏览器自动化的情况下肉眼校验 webui_page.html 的渲染。

做法：
  1. 用真实 token 调用宿主 HTTP 接口，抓取 stats / sessions / items 三个查询的真实返回；
  2. 把 webui_page.html 的**原始片段**（一字不改）包进一个完整 HTML 文档；
  3. 在 <head> 里注入一个桩 window.maibot，query() 直接返回第 1 步抓到的真实数据，
     action() 返回成功但不落库（预览只读，不会动真实记忆）；
  4. 用 Edge headless 的 --screenshot 截图。

这样校验的是「页面自身的 HTML/CSS/JS 是否能把真实数据正确渲染出来」，
宿主侧的 window.maibot 桥接链路已由 HTTP 端到端测试单独验证。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: 宿主安装目录。必须显式给，否则不知道去哪读 data/webui.json 里的 token。
MB = Path(os.environ.get("CGAP_HOST_ROOT", ""))
OUT_DIR = Path(os.environ.get("CGAP_PREVIEW_DIR") or (Path(tempfile.gettempdir()) / "cgap-preview"))
PLUGIN_ID = "github.TommyAAKK.maibot-I-deepseek"
BASE = f"http://127.0.0.1:8001/api/webui/plugins/runtime/webui/{PLUGIN_ID}/memories"
EDGE = os.environ.get(
    "CGAP_BROWSER",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def read_token() -> str:
    if not str(MB):
        raise SystemExit(
            "没设置 CGAP_HOST_ROOT，无法定位宿主目录。"
            "请设置环境变量 CGAP_HOST_ROOT=<MaiBot 安装目录>"
        )
    data = json.loads((MB / "data" / "webui.json").read_text(encoding="utf-8"))
    token = str(data.get("access_token") or "")
    if not token:
        raise SystemExit("data/webui.json 里没有 access_token")
    return token


def call(token: str, kind: str, name: str, args: dict) -> dict:
    body = json.dumps({"args": args, "confirmed": False}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/{kind}/{name}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cookie": f"maibot_session={token}",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        raise SystemExit(f"{name} 调用失败: {payload}")
    return payload["result"]


STUB = """
<script>
(function () {
  var DATA = __DATA__;
  // 复刻 cg_webui.mem_items 的过滤 + 分页语义（含 _clamp_page_size/offset 的夹紧），
  // 让预览里的搜索、筛选、翻页都是真实行为，而不是每次都返回同一批数据。
  function clampSize(v) {
    var n = parseInt(v, 10);
    if (!isFinite(n) || n <= 0) return 50;
    return Math.min(n, 200);
  }
  function clampOffset(v) {
    var n = parseInt(v, 10);
    if (!isFinite(n) || n < 0) return 0;
    return n;
  }
  function filterItems(args) {
    var all = (DATA.items && DATA.items.items) || [];
    var total = (DATA.items && DATA.items.total) || all.length;
    var sessionId = args.session_id || '';
    var status = args.status || '';
    var category = args.category || '';
    var search = (args.search || '').trim().toLowerCase();
    var limit = clampSize(args.limit);
    var offset = clampOffset(args.offset);

    var rows = all.filter(function (it) {
      if (sessionId && it.session_id !== sessionId) return false;
      if (status && it.status !== status) return false;
      if (category && it.category !== category) return false;
      if (search && String(it.content || '').toLowerCase().indexOf(search) < 0) return false;
      return true;
    });
    // 预览只抓了首页 25 条，所以过滤后 total 用「过滤命中数」而不是后端的全库计数，
    // 否则会出现「共 469 条但只渲染 3 条」的假象，掩盖真实问题。
    var matched = rows.length;
    if (matched === all.length) matched = total;
    return {
      ok: true,
      items: rows.slice(offset, offset + limit),
      total: matched,
      limit: limit,
      offset: offset,
      has_more: offset + limit < matched
    };
  }
  window.maibot = {
    ready: Promise.resolve(),
    context: {
      theme: '__THEME__',
      language: 'zh-CN',
      dashboardStyle: '__STYLE__',
      styles: ''
    },
    page: { id: 'memories', layout: 'document', state: null },
    query: function (name, args) {
      if (!Object.prototype.hasOwnProperty.call(DATA, name)) {
        return Promise.reject(new Error('未声明的查询: ' + name));
      }
      // items 查询要按参数真的过滤，否则预览里点搜索/翻页都看不出变化，
      // 「搜索栏没反应」这类 bug 在预览里就永远发现不了。
      if (name === 'items') return Promise.resolve(filterItems(args || {}));
      return Promise.resolve(DATA[name]);
    },
    action: function (name) {
      return Promise.resolve({ ok: true, preview: true, action: name });
    },
    upload: function () { return Promise.reject(new Error('预览不支持上传')); },
    navigate: function () {}
  };
  // 模拟宿主下发的可见区域变量。真实宿主里 iframe 自身不滚动，外层页面滚到哪、
  // iframe 露出哪一段，就由宿主算出 top/height 后 postMessage 给页面
  // （dashboard/src/components/plugin-custom-page.tsx:84-110 与
  //   dashboard/src/lib/plugin-custom-page.ts:134-138）。
  // 预览用 URL 参数 vtop / vheight 复现同样的输入，方便验证弹窗定位。
  var params = new URLSearchParams(location.search);
  var simTop = Number(params.get('vtop') || 0);
  var simHeight = Number(params.get('vheight') || 0) || window.innerHeight;
  function syncViewport() {
    var root = document.documentElement;
    root.style.setProperty('--maibot-viewport-top', simTop + 'px');
    root.style.setProperty('--maibot-viewport-height', simHeight + 'px');
  }
  syncViewport();
  window.addEventListener('resize', function () {
    if (!params.get('vheight')) {
      simHeight = window.innerHeight;
      syncViewport();
    }
  });
})();
</script>
"""

# 宿主的主题变量。真实值由 dashboard/src/lib/plugin-page-theme.ts 采样宿主组件后
# 以 context.styles 注入 iframe；这里按 dashboard/src/index.css 的 token 换算成
# 实际颜色，只为离线预览能看出「与 MaiBot 统一」的效果。
THEME_LIGHT = """
:root {
  --maibot-background: hsl(0 0% 100%);
  --maibot-surface: hsl(188.5 14% 98.6%);
  --maibot-soft: hsl(188.5 12% 96%);
  --maibot-foreground: hsl(222.2 84% 4.9%);
  --maibot-muted: hsl(188.5 20% 46.9%);
  --maibot-border: hsl(188.5 20% 91.4%);
  --maibot-primary: hsl(28.9 94.8% 45.1%);
  --maibot-primary-foreground: hsl(210 40% 98%);
  --maibot-danger: hsl(0 84.2% 45%);
  --maibot-radius: 8px;
  --maibot-font-body: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
  color-scheme: light;
}
"""

THEME_DARK = """
:root {
  --maibot-background: hsl(222.2 84% 4.9%);
  --maibot-surface: hsl(188.5 18% 8.8%);
  --maibot-soft: hsl(188.5 12% 17.5%);
  --maibot-foreground: hsl(210 40% 98%);
  --maibot-muted: hsl(188.5 20% 65.1%);
  --maibot-border: hsl(188.5 20% 17.5%);
  --maibot-primary: hsl(28.9 94.8% 45.1%);
  --maibot-primary-foreground: hsl(210 40% 98%);
  --maibot-danger: hsl(0 62.8% 30.6%);
  --maibot-radius: 8px;
  --maibot-font-body: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
  color-scheme: dark;
}
"""


def _retro_texture(style: str, intensity: int, dark: bool) -> str:
    """复刻 dashboard/src/lib/theme/future-retro.ts 的 buildFutureRetroTexture。

    宿主在 theme-provider 里用它生成纸纹 SVG data URL，再经 context.styles 的
    .maibot-page 规则注入 iframe。预览必须用同一套算法，否则看不出真实质感。
    """
    if style == "none" or intensity <= 0:
        return "none"
    strength = max(0, min(100, intensity)) / 100
    ink = "#d2c0a9" if dark else "#6b3b1c"
    accent = "#c04d27" if dark else "#c24d24"
    if style == "dot-grid":
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
            f'<circle cx="2" cy="2" r="0.8" fill="{ink}" '
            f'opacity="{0.34 * strength:.3f}"/></svg>'
        )
    elif style == "ruled":
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="28">'
            f'<path d="M0 27.5H40" stroke="{ink}" stroke-width="0.7" '
            f'opacity="{0.28 * strength:.3f}"/></svg>'
        )
    else:
        coarse = style == "coarse"
        size = 260 if coarse else 220
        frequency = "0.32" if coarse else "0.6"
        noise_opacity = f"{strength * (0.28 if coarse else 0.29):.3f}"
        fleck_opacity = f"{strength * (0.2 if coarse else 0.18):.3f}"
        r1, r2, r3 = (1.5, 1.1, 1.35) if coarse else (1.1, 0.8, 1)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {size} {size}">'
            f'<filter id="n"><feTurbulence type="fractalNoise" baseFrequency="{frequency}" '
            f'numOctaves="3" seed="11"/></filter>'
            f'<rect width="100%" height="100%" filter="url(#n)" opacity="{noise_opacity}"/>'
            f'<g fill="{accent}" opacity="{fleck_opacity}">'
            f'<circle cx="12" cy="21" r="{r1}"/><circle cx="67" cy="43" r="{r2}"/>'
            f'<circle cx="139" cy="16" r="{r3}"/></g></svg>'
        )
    return f'url("data:image/svg+xml,{urllib.parse.quote(svg, safe="")}")'


# 未来复古（future-retro）主题下的 --maibot-* 实际取值。
# 这些不是猜的：按 dashboard/src/lib/plugin-page-theme.ts:95-115 的采样逻辑，
# 对着 dashboard/src/index.css 的 future-retro 规则与 tokens.ts 的
# futureRetroLightTokens/futureRetroDarkTokens 逐个推出来 ——
#   surface  = card.backgroundColor   → 卡片被改成 transparent !important，故为 transparent
#   soft     = input.backgroundColor  → var(--retro-recessed) = rgb(73 48 22 / .055)
#   foreground / border = card.color / card.borderTopColor = var(--retro-ink)
#   radius   = card.borderRadius      → 0px
#   background = var(--retro-paper)   → 米黄纸色
# 把 surface 取成 transparent 正是为了复现「弹窗透明」那个 bug 的现场。
def _retro_theme(dark: bool) -> str:
    if dark:
        paper = "color-mix(in srgb, #090909, #110906 100%)"
        paper_deep = "color-mix(in srgb, #111, #19110e 100%)"
        ink = "#d2c0a9"
        recessed = "rgb(210 192 169 / 0.04)"
        primary = "hsl(19.2 44.7% 42.5%)"
        primary_fg = "hsl(34 40% 86%)"
        danger = "hsl(0 62.8% 30.6%)"
        muted = "hsl(34 19% 58%)"
        overlay = "none"
        stroke = "1.25px"
    else:
        paper = "color-mix(in srgb, #fff, #f3e3cc 100%)"
        paper_deep = "color-mix(in srgb, #f4f4f2, #dec5a5 100%)"
        ink = "#0a4550"
        recessed = "rgb(73 48 22 / 0.055)"
        primary = "hsl(15.6 68.7% 45.1%)"
        primary_fg = "hsl(39.5 100% 92%)"
        danger = "hsl(0 84.2% 60.2%)"
        muted = "hsl(39.1 11.6% 39%)"
        overlay = "linear-gradient(rgb(255 252 242 / 0.24), rgb(255 252 242 / 0.24))"
        stroke = "2px"
    texture = _retro_texture("fine", 55, dark)
    return f"""
:root {{
  --maibot-background: {paper};
  --maibot-surface: transparent;      /* ← future-retro 把卡片设成 transparent !important */
  --maibot-soft: {recessed};
  --maibot-foreground: {ink};
  --maibot-muted: {muted};
  --maibot-border: {ink};
  --maibot-primary: {primary};
  --maibot-primary-foreground: {primary_fg};
  --maibot-danger: {danger};
  --maibot-radius: 0px;               /* ← 卡片 border-radius: 0 !important */
  --maibot-font-body: 'MaiRetroText', 'Microsoft YaHei UI', system-ui, sans-serif;
  --retro-paper: {paper};
  --retro-paper-deep: {paper_deep};
  --retro-ink: {ink};
  --retro-line: {'rgb(210 192 169 / 0.38)' if dark else 'rgb(13 70 80 / 0.82)'};
  --retro-rust: {'#c04d27' if dark else '#c24d24'};
  --retro-recessed: {recessed};
  --retro-stroke: {stroke};
  --retro-paper-texture: {texture};
  --retro-paper-texture-size: 180px 180px;
  --retro-paper-brightness-overlay: {overlay};
  color-scheme: {'dark' if dark else 'light'};
}}
/* 宿主的 .maibot-page 规则（plugin-page-theme.ts:132），页面根节点带这个类，
   靠它继承纸纹与字体。这里照抄一份，预览才和真机一致。 */
.maibot-page {{ margin: 0; color: var(--maibot-foreground);
  background-color: var(--maibot-background);
  background-image: {texture};
  background-size: var(--retro-paper-texture-size);
  font-family: var(--maibot-font-body); }}
/* 宿主外壳的真实画法（index.css:646-653）：纸色上再叠一层提亮层。
   预览必须照做，否则「页面比外壳暗一档」在预览里根本看不出来。 */
html[data-dashboard-style='future-retro'], body {{
  background-color: var(--retro-paper);
  background-image: var(--retro-paper-brightness-overlay), var(--retro-paper-texture);
  background-size: auto, var(--retro-paper-texture-size);
}}
"""


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    token = read_token()
    print(f"token 长度 {len(token)}")

    data = {
        "stats": call(token, "queries", "stats", {}),
        "sessions": call(token, "queries", "sessions", {}),
        "items": call(token, "queries", "items", {"limit": 25}),
        "mood": call(token, "queries", "mood", {}),
        "mood_history": call(token, "queries", "mood_history", {"limit": 40}),
        "people": call(token, "queries", "people", {"limit": 200}),
        "prefs": call(token, "queries", "prefs", {"limit": 200}),
    }
    for name, value in data.items():
        (OUT_DIR / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  {name}: {len(json.dumps(value, ensure_ascii=False))} 字节")

    fragment = (HERE / "webui_page.html").read_text(encoding="utf-8")
    data_json = json.dumps(data, ensure_ascii=False)
    # 三套组合：默认浅色、默认深色、未来复古浅色、未来复古深色。
    # 未来复古那两套是这轮「透明复发 + 风格不符」的验收现场。
    combos = [
        ("light", THEME_LIGHT, "modern", False),
        ("dark", THEME_DARK, "modern", True),
        ("retro", _retro_theme(False), "future-retro", False),
        ("retro-dark", _retro_theme(True), "future-retro", True),
    ]
    outputs: list[Path] = []
    for suffix, tokens, style, dark in combos:
        stub = (
            STUB.replace("__DATA__", data_json)
            .replace("__THEME__", "dark" if dark else "light")
            .replace("__STYLE__", style)
        )
        # 宿主 SDK 是给 <html> 加/去 dark class 并把 dashboardStyle 写到根元素
        # （plugin-custom-page.ts:73-75），页面里的 `html.dark .cg-root` 与
        # `html[data-dashboard-style='future-retro']` 都依赖这一点，预览必须照做。
        attributes = f' lang="zh-CN" data-dashboard-style="{style}"'
        if dark:
            attributes += ' class="dark"'
        # 关键：宿主的 context.styles 是 append 到 body 末尾的
        # （plugin-custom-page.ts:79 `(document.body || document.head).append(themeStyle)`），
        # 不是放进 head。层叠顺序直接决定 `.maibot-page` 会不会盖掉 `.cg-root`，
        # 预览必须复现同样的顺序，否则「页面比外壳暗一档」这类问题在预览里看不出来。
        theme_style = (
            f"<style>{tokens}"
            f"html,body{{margin:0;padding:0;background:var(--maibot-background);"
            f"color:var(--maibot-foreground);}}"
            f"body{{padding:16px 16px 24px;font-family:var(--maibot-font-body);}}"
            "</style>"
        )
        document = (
            "<!doctype html><html"
            + attributes
            + "><head><meta charset=\"utf-8\">"
            f"<title>记忆管理 · 离线预览（{suffix}）</title>"
            + stub
            + "</head><body>"
            + fragment
            + theme_style
            + "</body></html>"
        )
        preview = OUT_DIR / f"preview-{suffix}.html"
        preview.write_text(document, encoding="utf-8")
        outputs.append(preview)
        print(f"预览页（{suffix}）: {preview} ({len(document)} 字符)")

    for preview in outputs:
        shot = OUT_DIR / (preview.stem + ".png")
        command = [
            EDGE,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=980,1600",
            f"--screenshot={shot}",
            preview.as_uri(),
        ]
        print("截图命令:", " ".join(command[:4]), "...")
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if proc.stderr.strip():
            print("stderr:", proc.stderr.strip()[:800])
        if not shot.is_file():
            print(f"截图失败: {shot}")
            return 1
        print(f"截图: {shot} ({shot.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
