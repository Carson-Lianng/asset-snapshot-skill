#!/usr/bin/env python3
"""_common.py —— 公共层：账本定位、令牌读取、HTTP 调用、错误解包、输出格式化。

唯一持有**接口路径常量**的地方（可维护性约定：接口变了只改这里，其余脚本与文档不动）。

三条纪律（改这个文件时别破坏它们）：

1. 这里只做「传输」与「展示格式化」，**不做任何口径计算**。折算、汇总、收益、两期对比
   一律由服务端算。金额单位换算（元 ↔ 微元）属于接口契约的一部分，因此放在这里单点完成，
   与口径计算严格区分。
2. **绝不把令牌写进任何输出**（不打印、不进 JSON、不进日志）。令牌只从数据目录的令牌文件读。
3. 只依赖 Python 标准库。不引入第三方包，不调用外部命令。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# --------------------------------------------------------------------------
# 退出码（SKILL.md 有对应表；改这里要同批改 SKILL.md）
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_API = 2
EXIT_MANUAL = 3
EXIT_NEED_CONFIRM = 4
EXIT_NO_SERVICE = 10
EXIT_NO_LEDGER = 11
EXIT_NO_TOKEN = 12

MICRO = 1_000_000

# --------------------------------------------------------------------------
# 接口路径常量 —— **唯一来源**
# --------------------------------------------------------------------------

EP = {
    # 系统
    "health": "/api/health",
    "ready": "/api/ready",
    # 设置与基础资料
    "settings": "/api/settings",
    "base_currency": "/api/settings/base-currency",
    "base_currency_history": "/api/base-currency-history",
    "rates": "/api/rates",
    "currencies": "/api/currencies",
    "currency": "/api/currencies/{code}",
    "platforms": "/api/platforms",
    "platform": "/api/platforms/{id}",
    "categories": "/api/categories",
    "category": "/api/categories/{id}",
    "tags": "/api/tags",
    "tag": "/api/tags/{id}",
    # 账户
    "accounts": "/api/accounts",
    "account": "/api/accounts/{id}",
    # 盘点
    "inventory_session": "/api/inventory/session",
    "inventory_draft": "/api/inventory/draft",
    "inventory_preview": "/api/inventory/preview",
    "inventory_snapshots": "/api/inventory/snapshots",
    # 快照
    "snapshots": "/api/snapshots",
    "snapshots_compare": "/api/snapshots/compare",
    "snapshot": "/api/snapshots/{id}",
    "snapshot_returns": "/api/snapshots/{id}/returns",
    # 报表（只读）
    "reports_trend": "/api/reports/trend",
    "reports_breakdown": "/api/reports/breakdown",
    "reports_platform_currency": "/api/reports/platform-currency",
    "reports_currency": "/api/reports/currency",
    "reports_returns": "/api/reports/returns",
    "reports_returns_breakdown": "/api/reports/returns-breakdown",
    "reports_returns_trend": "/api/reports/returns-trend",
    # 数据管理
    "backup": "/api/backup",
    "restore": "/api/restore",
    "import_preview": "/api/import/preview",
    "import_apply": "/api/import/apply",
}

DIM_ALIASES = {
    "account": "account", "账户": "account",
    "platform": "platform", "平台": "platform",
    "category": "category", "分类": "category",
    "currency": "currency", "币种": "currency",
    "tag": "tag", "标签": "tag",
}

CATALOG_ALIASES = {
    "currency": "currencies", "币种": "currencies",
    "platform": "platforms", "平台": "platforms",
    "category": "categories", "分类": "categories",
    "tag": "tags", "标签": "tags",
    "all": "all", "全部": "all",
}

DEFAULT_PORT = 5188
"""账本缺省端口（`.env` 里的约定值）。PORT=0 时地址每次都会变，只能靠 `--base-url`。"""


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------


class FsError(Exception):
    """技能自己抛出的错误：带退出码与可读说明。"""

    exit_code = EXIT_USAGE

    def __init__(self, message: str, *, details=None, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details
        self.hint = hint

    def payload(self) -> dict:
        out = {"ok": False, "error": {"message": self.message, "details": self.details}}
        if self.hint:
            out["error"]["hint"] = self.hint
        return out

    def render(self) -> str:
        text = self.message
        if self.hint:
            text += "\n  → " + self.hint
        return text


class ServiceUnreachable(FsError):
    exit_code = EXIT_NO_SERVICE


class LedgerNotFound(FsError):
    exit_code = EXIT_NO_LEDGER


class TokenMissing(FsError):
    exit_code = EXIT_NO_TOKEN


class ManualUnavailable(FsError):
    exit_code = EXIT_MANUAL


class ApiError(FsError):
    """服务端按契约返回的结构化错误（按 `code` 分派，不看状态码）。"""

    exit_code = EXIT_API

    def __init__(self, status: int, body, path: str):
        err = {}
        if isinstance(body, dict):
            err = body.get("error") or {}
        message = err.get("message") or "服务端返回了无法解析的错误"
        # ⚠ 必须把 details 交给 super()：FsError.__init__ 里有一句 `self.details = details`，
        #   若先自己赋值再调 super()，会被 None 覆盖掉 ⇒ 所有按 details 分派的分支静默失效。
        super().__init__(message, details=err.get("details"))
        self.status = status
        self.code = err.get("code") or "HTTP_%d" % status
        self.request_id = err.get("request_id")
        self.path = path

    def payload(self) -> dict:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "status": self.status,
                "request_id": self.request_id,
            },
        }

    def render(self) -> str:
        text = "[%s] %s" % (self.code, self.message)
        if self.details is not None:
            text += "\n  细节：" + json.dumps(self.details, ensure_ascii=False)
        if self.request_id:
            text += "\n  request_id：%s" % self.request_id
        return text


# --------------------------------------------------------------------------
# 配置与账本定位
# --------------------------------------------------------------------------


def config_path() -> Path:
    """记住的账本配置。**不写在技能目录里**（技能目录只放 SKILL.md/scripts/references）。"""
    env = os.environ.get("FAMILY_SNAPSHOT_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".family-snapshot" / "config.json"


def load_config() -> dict:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def read_dotenv(path: Path) -> dict:
    """读 `KEY=VALUE` 形式的最小 `.env`（忽略注释与空行）。**只读，不改写。**"""
    out = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            out[key.strip()] = value
    return out


def find_ledger_upward(start: Path) -> Path:
    """从 `start` 向上找「账本根」——判据是目录里存在账本数据目录（含令牌文件）。"""
    cur = start.resolve()
    for cand in [cur, *cur.parents]:
        if (cand / ".data" / ".token").is_file():
            return cand
    raise LedgerNotFound(
        "在当前目录及各级父目录里都没找到账本（判据：存在数据目录与其下的令牌文件）：%s" % cur,
        hint="用 --ledger <账本根目录> 显式指定；或先在该账本目录里跑一次 doctor.py --remember 记住它",
    )


def health_of(base_url: str, timeout: float = 2.0):
    """探一个地址是否是本账本服务在跑。能返回 dict 就说明是。"""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + EP["health"], timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 —— 探活失败的原因不重要，重要的是「没探到」
        return None


def pick_base_url(candidates, timeout: float = 2.0):
    """在候选地址里挑第一个能应答的。返回 (base_url, health)；都不通返回 (None, None)。"""
    seen = []
    for url in candidates:
        if not url or url in seen:
            continue
        seen.append(url)
        info = health_of(url, timeout=timeout)
        if info:
            return url, info
    return None, None


class Ledger(object):
    """一个账本实例的连接：根目录、数据目录、服务地址、令牌。"""

    def __init__(self, root: Path, data_dir: Path, base_url: str, token: str, sources=None):
        self.root = str(root)
        self.data_dir = str(data_dir)
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.sources = sources or {}

    # ---- 展示用 ----
    def describe(self) -> str:
        return "账本 %s（服务 %s）" % (self.root, self.base_url)

    # ---- 请求 ----
    def request(self, method: str, path: str, params=None, body=None, timeout: float = 20.0):
        """发一次请求。path 用 EP 里的常量填好占位符后再传入。

        **不带 Origin 头**：本机非浏览器客户端由令牌兜底放行（服务端的既有行为），
        带了反而要维护一份来源白名单。
        """
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {
            "X-App-Token": self._token,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return _parse_json(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise ApiError(e.code, _parse_json(raw), path)
        except urllib.error.URLError as e:
            raise ServiceUnreachable(
                "连不上账本服务：%s（%s）" % (self.base_url, e.reason),
                hint="服务可能没在跑。在账本目录里执行 npm start 后重试；本技能不代起服务",
            )
        except TimeoutError:
            raise ServiceUnreachable(
                "请求 %s 超时（%s）" % (path, self.base_url),
                hint="确认服务仍在运行；大账本的首个报表请求可能较慢，可重试一次",
            )

    def get(self, key: str, params=None, **fmt):
        return self.request("GET", _path(key, fmt), params=params)

    def send(self, method: str, key: str, body=None, **fmt):
        return self.request(method, _path(key, fmt), body=body)


def _path(key: str, fmt: dict) -> str:
    if key not in EP:
        raise FsError("未定义的接口键：%s（接口常量只在 scripts/_common.py 的 EP 里）" % key)
    path = EP[key]
    if fmt:
        path = path.format(**fmt)
    return path


def _parse_json(raw: str):
    if raw == "":
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw}


def resolve_ledger(cli_ledger: str = None, cli_base_url: str = None, remember: bool = False,
                   need_service: bool = True) -> Ledger:
    """三级定位：显式参数 → 记住的配置 → 从当前目录向上搜索。

    `need_service=False` 时**不探活**（答疑只需要账本目录，服务在不在跑与它无关）。
    """
    cfg = load_config()
    env_ledger = os.environ.get("FAMILY_SNAPSHOT_LEDGER")
    env_base = os.environ.get("FAMILY_SNAPSHOT_BASE_URL")

    root = None
    source = ""
    if cli_ledger:
        root, source = Path(cli_ledger).expanduser(), "--ledger"
    elif env_ledger:
        root, source = Path(env_ledger).expanduser(), "环境变量 FAMILY_SNAPSHOT_LEDGER"
    elif cfg.get("ledger"):
        root, source = Path(cfg["ledger"]).expanduser(), "记住的配置 %s" % config_path()

    if root is not None and not root.is_dir():
        raise LedgerNotFound("指定的账本目录不存在：%s（来自 %s）" % (root, source))
    if root is None:
        root = find_ledger_upward(Path.cwd())
        source = "从当前目录向上搜索"

    root = root.resolve()
    dotenv = read_dotenv(root / ".env")

    data_dir = Path(os.environ.get("FAMILY_SNAPSHOT_DATA_DIR") or cfg.get("data_dir") or (dotenv.get("DATA_DIR") or "") or (root / ".data"))
    if not data_dir.is_absolute():
        data_dir = (root / data_dir).resolve()

    token_file = data_dir / ".token"
    if not token_file.is_file():
        if not need_service:
            # 答疑只需要账本目录：服务还没起、令牌还没生成，都不该挡住「手册里怎么写的」这类问题
            token = ""
        else:
            raise TokenMissing(
                "读不到访问令牌：%s" % token_file,
                hint="确认账本已至少启动过一次（服务首次启动会生成令牌）；"
                     "数据目录不在默认位置时用 --ledger 指向账本根目录（或设 FAMILY_SNAPSHOT_DATA_DIR）",
            )
    else:
        token = token_file.read_text(encoding="utf-8", errors="replace").strip()

    # 服务地址：显式指定就用它一家；没显式指定才去猜（账本 .env 的 PORT → 缺省端口）。
    #
    # ⚠ 显式指定时**绝不回落到别的候选**：使用者说「这个账本的地址是 X」，X 连不上就是错，
    #   不能悄悄改去连另一个端口 —— 那可能正连着**另一本真实账本**（本机可以同时跑多个实例），
    #   而技能会照着使用者的意图往那本账上写。宁可失败，也不能连错账本。
    explicit = cli_base_url or env_base or cfg.get("base_url")
    env_port = (dotenv.get("PORT") or "").strip()
    if explicit:
        candidates = [explicit]
    else:
        candidates = []
        for port in (env_port, str(DEFAULT_PORT)):
            if port and port != "0":
                candidates.append("http://127.0.0.1:%s" % port)

    if not need_service:
        return Ledger(root, data_dir, (candidates[0] if candidates else "http://127.0.0.1:%s" % DEFAULT_PORT),
                      token, sources={"ledger": source, "health": None, "explicit": bool(explicit)})

    base_url, health = pick_base_url(candidates)
    if not base_url:
        if explicit:
            hint = ("地址是显式指定的（%s），连不上就是错 —— **不会**自动改连别的端口，"
                    "因为那可能是另一本真实账本。请在账本目录（%s）执行 npm start 启动服务后重试"
                    "（本技能不代起服务）" % (explicit, root))
        else:
            hint = ("在账本目录（%s）执行 npm start 启动服务（本技能不代起服务）；"
                    "地址不是上面这些时用 --base-url 指定，例如 --base-url http://127.0.0.1:5200" % root)
        raise ServiceUnreachable(
            "账本服务没有应答。已试过：%s" % ("、".join(candidates) or "（没有可用的候选地址）"),
            hint=hint,
        )

    if remember:
        save_config({"ledger": str(root), "base_url": base_url})

    return Ledger(root, data_dir, base_url, token, sources={"ledger": source, "health": health, "explicit": bool(explicit)})


# --------------------------------------------------------------------------
# 展示格式化（只做单位换算与排版，不做口径计算）
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 使用手册的定位（doctor 与 help 同源，**只有一个定义**）
# --------------------------------------------------------------------------

#: 手册在账本包内的相对位置（源码形态）。改动这里要同批改 references/manual-index.md。
HELP_SOURCE_REL = "app/web/src/features/help"
HELP_OUTLINE_NAME = "outline.ts"
HELP_CONTENT_NAME = "content.tsx"


def find_help_source(ledger_root) -> dict:
    """定位账本包内的使用手册源码。

    返回 `{"dir": Path, "outline": Path, "content": Path, "source": 说明}`；
    找不到返回 `None`（**不猜、不编**，由调用方显式降级）。

    ⚠ 显式给了 `FAMILY_SNAPSHOT_HELP_DIR` 就**只认它**，不完整即视为找不到 ——
      与 `--base-url` / `--ledger` 同一条原则：显式指定时静默回落到别处，
      会让人以为自己在看 A、其实技能读的是 B。
    """
    env = os.environ.get("FAMILY_SNAPSHOT_HELP_DIR")
    if env:
        roots = [(Path(env).expanduser(), "环境变量 FAMILY_SNAPSHOT_HELP_DIR")]
    else:
        roots = [(Path(ledger_root) / HELP_SOURCE_REL, "账本包内 %s" % HELP_SOURCE_REL)]
    for base, desc in roots:
        outline = base / HELP_OUTLINE_NAME
        content = base / HELP_CONTENT_NAME
        if outline.is_file() and content.is_file():
            return {"dir": base, "outline": outline, "content": content, "source": desc}
    return None


def claim(values, *keys, default=None):
    """按顺序取第一个存在的键（服务端字段偶尔有历史差异时用，**不做兜底计算**）。"""
    for k in keys:
        if isinstance(values, dict) and k in values and values[k] is not None:
            return values[k]
    return default


def money(micro, dash="—") -> str:
    """微元整数 → 元（2 位小数，千分位）。None → 破折号。"""
    if micro is None:
        return dash
    if isinstance(micro, str):
        try:
            micro = int(micro)
        except ValueError:
            return dash
    q = (Decimal(int(micro)) / MICRO).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "{:,.2f}".format(q)


def money_brief(micro, dash="—") -> str:
    """微元整数 → 元（0 位小数，千分位）。用于列表里的数字列（界面也是这个口径）。"""
    if micro is None:
        return dash
    q = (Decimal(int(micro)) / MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return "{:,.0f}".format(q)


def rate(micro, dash="—") -> str:
    """微元整数 → 汇率（最多 6 位小数，去尾零）。"""
    if micro is None:
        return dash
    q = Decimal(int(micro)) / MICRO
    s = "{:f}".format(q)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def pct(value, dash="—") -> str:
    """小数比率（0.0512）→ 百分数文本。None → 破折号。"""
    if value is None:
        return dash
    return "%.2f%%" % (float(value) * 100)


def yuan_to_micro(text) -> int:
    """「12,345.67 元」「¥12345.67」→ 微元整数。解析不了抛错，**不猜**。"""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(Decimal(str(text)).scaleb(6).to_integral_value(rounding=ROUND_HALF_UP))
    raw = str(text).strip()
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
    if cleaned in ("", "-", ".", "-."):
        raise FsError("金额无法解析：%r（请写数字，如 12345.67；千分位与货币符号可保留）" % raw)
    try:
        d = Decimal(cleaned)
    except Exception:
        raise FsError("金额无法解析：%r（请写数字，如 12345.67）" % raw)
    return int(d.scaleb(6).to_integral_value(rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# 参数与输出
# --------------------------------------------------------------------------


def add_common_args(parser):
    parser.add_argument("--ledger", help="账本根目录（压过配置与环境变量）")
    parser.add_argument("--base-url", dest="base_url", help="账本服务地址，如 http://127.0.0.1:5188")
    parser.add_argument("--json", action="store_true", help="输出紧凑 JSON（缺省输出人读摘要）")
    parser.add_argument("--viz", action="store_true",
                        help="随返回体附上可视化规格（图由宿主平台渲染；不出图时给出理由）")
    parser.add_argument("--share", action="store_true",
                        help="追加**派生**占比序列（服务端不提供占比；会带固定标注与分母，仅部分视图支持）")


def _share_attach(payload):
    """把**派生占比**挂到返回体上 —— **与 `--viz` 解耦**。

    图会因条目过多被否掉，但占比是数据不是装饰：使用者问了就必须给得出来。
    视图不支持时**显式报错**（`require_share_view`），不静默忽略。
    """
    try:
        import vizspec
    except ImportError as e:  # 规格器丢了就直说，不假装没有这个能力
        return {"share_note": "占比不可用：可视化规格器缺失（%s）" % e}
    vizspec.require_share_view(payload.get("view"))
    block = vizspec.share_block(payload)
    if block is None:
        return {"share_note": "这一期没有可算占比的行"}
    return {"share": block}


def _share_lines(block):
    """占比的人读形态。**标注与分母必须跟着数字一起出现。**"""
    d = block.get("denominator") or {}
    # ⚠ 分母的 value 已经是**元**（vizspec 里 `yuan()` 换过），不能再走 money()（那要的是微元）
    dv = d.get("value")
    dtext = "—" if dv is None else "{:,.2f}".format(Decimal(str(dv)))
    out = ["", "  占比（派生量 · %s）" % block.get("notice")]
    out.append("    分母：%s ¥%s（%s）"
               % (d.get("label"), dtext, d.get("source") or "服务端汇总列"))
    out.append("    公式：%s" % block.get("formula_text"))
    for row in block.get("rows") or []:
        out.append("    · %-16s %s" % (row.get("label"), pct(row.get("value"))))
    return out


def _viz_attach(args, payload):
    """把可视化规格挂到返回体上（**唯一挂载点**，各视图不用各自实现）。

    取不到就挂 `viz_note`（理由是给人看的），绝不静默跳过。
    """
    try:
        import vizspec
    except ImportError as e:  # 规格器丢了就直说，不假装没有这个能力
        return {"viz_note": "可视化规格器不可用（%s）" % e}
    spec, note = vizspec.build(payload, want_share=bool(getattr(args, "share", False)))
    if spec is not None:
        return {"viz": spec}
    return {"viz_note": note}


def emit(args, human_lines, payload):
    """人读摘要或紧凑 JSON 二选一。**绝不包含令牌。**"""
    if isinstance(payload, dict) and (getattr(args, "viz", False) or getattr(args, "share", False)):
        extra = {}
        if getattr(args, "viz", False):
            extra.update(_viz_attach(args, payload))
        if getattr(args, "share", False):
            extra.update(_share_attach(payload))
        payload = dict(payload, **extra)
        if not getattr(args, "json", False):
            human_lines = list(human_lines)
            if "viz" in extra:
                human_lines.append("  ［可视化规格已生成%s：加 --json 取用，由宿主平台渲染］"
                                   % ("（含派生占比）" if getattr(args, "share", False) else ""))
            elif "viz_note" in extra:
                human_lines.append("  ［未出图：%s］" % extra["viz_note"])
            if "share" in extra:
                human_lines.extend(_share_lines(extra["share"]))
            elif "share_note" in extra and getattr(args, "share", False):
                human_lines.append("  ［未给占比：%s］" % extra["share_note"])
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write("\n".join(human_lines).rstrip() + "\n")


def run(main_fn):
    """统一入口：把异常翻译成退出码与可读输出。"""
    try:
        code = main_fn()
        sys.exit(int(code or 0))
    except FsError as e:
        if "--json" in sys.argv[1:]:
            sys.stdout.write(json.dumps(e.payload(), ensure_ascii=False, separators=(",", ":")) + "\n")
        else:
            sys.stderr.write(e.render() + "\n")
        sys.exit(e.exit_code)
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        sys.exit(130)


# --------------------------------------------------------------------------
# 基础资料名称映射（运行时从接口拉，**不在技能里写死任何 id 与名称**）
# --------------------------------------------------------------------------


def name_maps(ledger: Ledger) -> dict:
    """平台 / 分类 / 标签 / 币种的 id 或 code → 名称。只用于展示。"""
    out = {"platform": {}, "category": {}, "tag": {}, "currency": {}}
    for key, ep_key, id_field in (
        ("platform", "platforms", "id"),
        ("category", "categories", "id"),
        ("tag", "tags", "id"),
        ("currency", "currencies", "code"),
    ):
        data = ledger.get(ep_key, params={"includeDisabled": "true"}) or {}
        for row in data.get("items", []) if isinstance(data, dict) else []:
            out[key][row.get(id_field)] = row.get("name") or row.get(id_field)
    return out


def accounts_index(ledger: Ledger):
    """(按 id 的账户表, 账户列表)。带一次展示用的平台/分类名称。"""
    data = ledger.get("accounts") or {}
    items = data.get("items", []) if isinstance(data, dict) else []
    maps = name_maps(ledger)
    by_id = {}
    for a in items:
        a["_platform"] = maps["platform"].get(a.get("platform_id"), "未指定平台")
        a["_category"] = maps["category"].get(a.get("category_id"), a.get("category_id"))
        a["_tags"] = [maps["tag"].get(t, t) for t in (a.get("tags") or [])]
        by_id[a.get("id")] = a
    listed = sorted(items, key=lambda a: (a.get("sort") or 0, a.get("id") or ""))
    return by_id, listed


def latest_snapshot(snapshots):
    """最新一张：全序 = 日期 → 创建时间 → id（与趋势图的排序同源）。"""
    if not snapshots:
        return None
    return sorted(
        snapshots,
        key=lambda s: (s.get("date") or "", s.get("created_at") or "", s.get("id") or ""),
    )[-1]


def pick_snapshot(snapshots, ref):
    """把「id 或日期」解析成一张快照。日期命中多张时取当天最后一张（与界面一致）。"""
    if ref is None:
        return latest_snapshot(snapshots)
    same_id = [s for s in snapshots if s.get("id") == ref]
    if same_id:
        return same_id[0]
    same_date = [s for s in snapshots if s.get("date") == ref]
    if same_date:
        return latest_snapshot(same_date)
    return None


def require_frontmatter_line(text: str, label: str) -> str:
    """占位小工具：给报错信息加统一前缀。"""
    return "%s：%s" % (label, text)


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
