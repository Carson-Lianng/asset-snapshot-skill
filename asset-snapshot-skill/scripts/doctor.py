#!/usr/bin/env python3
"""doctor.py —— 账本定位与环境自检（首次使用与故障排查的唯一入口）。

自检项：账本根目录 · 数据目录 · 令牌 · 服务可达 · 服务版本 · 数据格式版本 ·
数据模式（演示 / 真实）· 基础资料规模 · 使用手册可用性 · 本地解释器版本。

**门槛**：`writable` 为假（服务不可达 / 令牌读不到）时不得进入任何写流程。
服务没跑只提示、不代起 —— 本技能不产生长驻进程。
"""

from __future__ import annotations

import argparse
import os
import platform
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

MIN_PY = (3, 8)


def collect(ledger):
    """跑完所有自检，返回 (checks, facts)。check = (级别, 名称, 说明)。"""
    checks = []
    facts = {}

    py_ok = sys.version_info >= MIN_PY
    checks.append(("ok" if py_ok else "fail", "解释器",
                   "Python %d.%d.%d" % sys.version_info[:3]))
    checks.append(("ok" if platform.system() else "warn", "平台",
                   "%s / %s" % (platform.system(), platform.machine())))
    checks.append(("ok", "账本根目录", ledger.root))
    checks.append(("ok", "数据目录", ledger.data_dir))
    checks.append(("ok", "服务地址", ledger.base_url))

    health = ledger.sources.get("health") or {}
    facts["service_version"] = health.get("version")
    checks.append(("ok", "服务可达", "GET %s 正常（服务版本 %s，已运行 %ss）"
                   % (C.EP["health"], health.get("version", "?"), health.get("uptime_s", "?"))))

    # 令牌：只报长度，**绝不回显内容**
    token_len = len(ledger._token)  # noqa: SLF001 —— 同目录内的私有字段，够用且不额外暴露
    checks.append(("ok" if token_len >= 32 else "warn", "访问令牌",
                   "已从数据目录读取（%d 字符，不显示内容）" % token_len))

    # 数据格式版本与规模
    ready = ledger.get("ready")
    facts["ready"] = ready
    checks.append(("ok", "数据格式版本",
                   "v%s · 表规模 %s" % (ready.get("schema_version"), ready.get("counts"))))

    # 设置：本位币 / 版本 / 数据模式
    settings = ledger.get("settings")
    facts["settings"] = settings
    mode = settings.get("data_mode")
    mode_note = "真实账本" if mode == "live" else "**演示数据**（写操作会写进演示库；要开始记自己的账请先在终端做一次重置）"
    checks.append(("warn" if mode != "live" else "ok", "数据模式", "%s —— %s" % (mode, mode_note)))
    checks.append(("ok", "本位币 / 界面版本",
                   "%s · %s" % (settings.get("base_currency"), settings.get("app_version"))))

    # 使用手册（答疑的知识源）
    help_src = C.find_help_source(ledger.root)
    facts["manual"] = bool(help_src)
    if help_src:
        checks.append(("ok", "使用手册", "可用（%s）" % help_src["source"]))
    else:
        checks.append(("warn", "使用手册",
                       "**不可用**：账本包内没有手册源码（%s）。答疑会失败并提示升级包版本" % C.HELP_SOURCE_REL))

    # 基础资料：确认字典是运行时可读的（技能不写死字典）
    _, accounts = C.accounts_index(ledger)
    checks.append(("ok", "账户", "%d 个" % len(accounts)))

    facts["writable"] = all(lv != "fail" for lv, _, _ in checks)
    return checks, facts


def main() -> int:
    p = argparse.ArgumentParser(description="账本定位与环境自检")
    C.add_common_args(p)
    p.add_argument("--remember", action="store_true", help="把本次定位到的账本与服务地址记住")
    args = p.parse_args()

    try:
        ledger = C.resolve_ledger(args.ledger, args.base_url, remember=args.remember)
    except C.FsError as e:
        # 定位类失败也要给结构化输出，便于 Agent 直接引用
        payload = e.payload()
        payload["checks"] = [{"level": "fail", "name": "定位账本", "detail": e.message}]
        payload["writable"] = False
        emit_lines = ["✗ " + e.render()]
        C.emit(args, emit_lines, payload)
        return e.exit_code

    checks, facts = collect(ledger)
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = ["家底快照助手 · 环境自检", ""]
    for level, name, detail in checks:
        lines.append("  %s %-12s %s" % (icon[level], name, detail))
    lines.append("")
    if facts["writable"]:
        lines.append("结论：可以进入写流程（只读动作随时可用）。")
    else:
        lines.append("结论：**不满足写流程的前置条件**，请先解决上面标 ✗ 的项。")
    if args.remember:
        lines.append("已记住账本：%s" % C.config_path())

    payload = {
        "ok": True,
        "writable": facts["writable"],
        "ledger": ledger.root,
        "data_dir": ledger.data_dir,
        "base_url": ledger.base_url,
        "service_version": facts.get("service_version"),
        "schema_version": (facts.get("ready") or {}).get("schema_version"),
        "counts": (facts.get("ready") or {}).get("counts"),
        "settings": facts.get("settings"),
        "manual_available": facts.get("manual"),
        "checks": [{"level": lv, "name": n, "detail": d} for lv, n, d in checks],
    }
    C.emit(args, lines, payload)
    return C.EXIT_OK


if __name__ == "__main__":
    C.run(main)
