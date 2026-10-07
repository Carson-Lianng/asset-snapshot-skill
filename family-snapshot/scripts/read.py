#!/usr/bin/env python3
"""read.py —— 全部只读取数：问数与洞察一律由服务端出数。

技能内**不做任何折算、汇总、收益或对比计算**：每个视图都直接取服务端某个端点的结果，
只做「字段挑出来 + 单位换算成元 + 排版」。派生指标若服务端没给，就在这里说「服务端未提供」，
绝不在本地补算（本地算出来的必须由调用方标注「非系统口径」）。

用法：read.py <视图> [参数] [--ledger 路径] [--json]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

METRICS = {
    "net_worth": "净资产", "networth": "净资产", "净资产": "net_worth",
    "total_assets": "总资产", "总资产": "total_assets",
    "total_liabilities": "总负债", "总负债": "total_liabilities",
}

RETURN_STATUS = {
    "ok": "可计算",
    "not-tracked": "未跟踪本金",
    "no-principal": "本期未填本金",
    "first": "首期（无上期可比）",
    "base-missing": "基准缺失（上期没记本金）",
}

VIEWS = [
    "overview", "trend", "breakdown", "compare", "returns", "returns-breakdown",
    "returns-trend", "accounts", "account", "snapshots", "snapshot", "catalog",
    "rates", "settings", "import-preview",
]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def norm_dim(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key not in C.DIM_ALIASES:
        raise C.FsError("不认识的维度：%s" % raw,
                        hint="可用维度：account 账户 / platform 平台 / category 分类 / currency 币种 / tag 标签")
    return C.DIM_ALIASES[key]


def norm_metric(raw: str) -> str:
    key = (raw or "net_worth").strip().lower()
    if key not in METRICS:
        raise C.FsError("不认识的指标：%s" % raw, hint="可用：net_worth 净资产 / total_assets 总资产 / total_liabilities 总负债")
    value = METRICS[key]
    # 中文键映射到自身时会拿到中文，这里统一回英文枚举
    return {"净资产": "net_worth", "总资产": "total_assets", "总负债": "total_liabilities"}.get(value, value)


def all_snapshots(ledger):
    data = ledger.get("snapshots") or {}
    return data.get("items", []) if isinstance(data, dict) else []


def resolve_snapshot_or_fail(ledger, ref):
    """把 id/日期 解析成一张快照；库里一张都没有时给出「还没盘过」而不是报错。"""
    snaps = all_snapshots(ledger)
    if not snaps:
        return None, "还是空的——账本里一张快照都没有。先盘一次（plan.py → write.py snapshot-save）才会有数。"
    snap = C.pick_snapshot(snaps, ref)
    if snap is None:
        recent = "、".join("%s(%s)" % (s.get("date"), s.get("id")) for s in snaps[-5:])
        return None, "找不到快照：%s。最近几期：%s" % (ref, recent)
    return snap, ""


def empty_view(args, message, view):
    C.emit(args, [message], {"ok": True, "empty": True, "view": view, "message": message})
    return C.EXIT_OK


def days_since(iso_date: str):
    try:
        y, m, d = (int(x) for x in iso_date.split("-"))
        return (date.today() - date(y, m, d)).days
    except Exception:  # noqa: BLE001 —— 日期不合法只影响这一行展示
        return None


# --------------------------------------------------------------------------
# 视图实现
# --------------------------------------------------------------------------


def view_overview(ledger, args):
    snap, msg = resolve_snapshot_or_fail(ledger, None)
    if snap is None:
        return empty_view(args, msg, "overview")
    sid = snap["id"]

    panel = None
    try:
        panel = ledger.get("reports_returns", params={"snapshot_id": sid})
    except C.ApiError as e:
        panel = {"_error": e.render()}

    breakdown = None
    try:
        breakdown = ledger.get("reports_breakdown", params={"snapshot_id": sid, "dim": "platform"})
    except C.ApiError as e:
        breakdown = {"_error": e.render()}

    lines = ["最新一期 %s（%s）%s" % (snap.get("date"), sid, ("　备注：" + snap["note"]) if snap.get("note") else "")]
    d = days_since(snap.get("date"))
    if d is not None:
        lines.append("  距今 %d 天 · 本位币 %s" % (d, snap.get("base_currency")))
    lines.append("  总资产 %s ／ 总负债 %s ／ 净资产 %s" % (
        C.money(snap.get("total_assets")), C.money(snap.get("total_liabilities")), C.money(snap.get("net_worth"))))
    lines.append("  明细 %s 条（沿用上次 %s 条）· 已跟踪但未填本金 %s 条"
                 % (snap.get("item_count"), snap.get("carried_over"), snap.get("no_principal")))

    if panel and "_error" not in panel:
        lines.append("理财收益（服务端口径）：")
        lines.append("  纳入 %s 个账户 · 本金 %s → 市值 %s" % (panel.get("count"), C.money(panel.get("principal")), C.money(panel.get("market"))))
        lines.append("  累计收益 %s（%s） · 本期收益 %s（%s）" % (
            C.money(panel.get("cum")), C.pct(panel.get("cum_rate")),
            C.money(panel.get("per")) if panel.get("has_per") else "本期不可用", C.pct(panel.get("per_rate"))))
        lines.append("  未跟踪本金 %s · 首期 %s · 基准缺失 %s"
                     % (panel.get("no_principal"), panel.get("first"), panel.get("missing_base")))
    elif panel:
        lines.append("理财收益：取不到（%s）" % panel["_error"])

    if breakdown and "_error" not in breakdown:
        rows = breakdown.get("rows") or []
        tail = "（只列前 8 项，共 %d 项 —— 看全用 breakdown）" % len(rows) if len(rows) > 8 else ""
        lines.append("按平台看（服务端口径，净值降序）%s：" % tail)
        for row in rows[:8]:
            lines.append("  %-14s 资产 %s ／ 负债 %s ／ 净值 %s" % (
                row.get("name"), C.money(row.get("asset")), C.money(row.get("liability")), C.money(row.get("net"))))
    elif breakdown:
        lines.append("按平台看：取不到（%s）" % breakdown["_error"])

    payload = {"ok": True, "view": "overview", "snapshot": snap, "returns": panel, "breakdown_by_platform": breakdown}
    C.emit(args, lines, payload)
    return C.EXIT_OK


def view_trend(ledger, args):
    metric = norm_metric(args.metric)
    mode = args.mode or "origin"
    data = ledger.get("reports_trend", params={"metric": metric, "mode": mode})
    points = data.get("points") or []
    lines = ["%s趋势（服务端口径 · %s口径）· 共 %d 个点" % (METRICS.get(metric, metric), "原口径" if mode == "origin" else "现口径", len(points))]
    for p in points[-12:]:
        flag = "（折算参考值）" if p.get("ref") else ""
        lines.append("  %s  %14s  %s" % (p.get("date"), C.money(p.get(metric)), flag))
    if data.get("base_changes"):
        lines.append("本位币变更（折算链的原料）：")
        for ch in data["base_changes"]:
            lines.append("  %s → %s  @ %s" % (ch.get("from_currency"), ch.get("to_currency"), ch.get("changed_at")))
    C.emit(args, lines or ["这一口径下还没有可用的点。"], {"ok": True, "view": "trend", **data})
    return C.EXIT_OK


def view_breakdown(ledger, args):
    dim = norm_dim(args.dim)
    snap, msg = resolve_snapshot_or_fail(ledger, args.snapshot)
    if snap is None:
        return empty_view(args, msg, "breakdown")
    data = ledger.get("reports_breakdown",
                      params={"snapshot_id": snap["id"], "dim": dim, "mode": args.mode or "origin"})
    lines = ["%s结构（快照 %s · %s）%s" % (dim, snap["date"], snap["id"], "　含折算参考值" if data.get("ref") else "")]
    for row in data.get("rows") or []:
        lines.append("  %-16s 资产 %14s ／ 负债 %14s ／ 净值 %14s ／ %s 个账户"
                     % (row.get("name"), C.money(row.get("asset")), C.money(row.get("liability")),
                        C.money(row.get("net")), row.get("count")))
    C.emit(args, lines, {"ok": True, "view": "breakdown", "snapshot": snap,
                         "snapshot_id": snap["id"], **data})
    return C.EXIT_OK


def view_compare(ledger, args):
    early, msg_a = resolve_snapshot_or_fail(ledger, args.a)
    if early is None:
        return empty_view(args, msg_a, "compare")
    late, msg_b = resolve_snapshot_or_fail(ledger, args.b)
    if late is None:
        return empty_view(args, msg_b, "compare")
    if early["id"] == late["id"]:
        raise C.FsError("两期是同一张快照（%s），没法比" % early["id"])
    dim = norm_dim(args.dim or "platform")
    data = ledger.get("snapshots_compare",
                      params={"a": early["id"], "b": late["id"], "dim": dim, "mode": args.mode or "origin"})
    lines = ["两期对比 %s(%s) → %s(%s) · 维度 %s%s" % (
        early["date"], early["id"], late["date"], late["id"], dim, "　跨币种折算参考值" if data.get("cross_currency") else "")]
    lines.append("  净资产 %s → %s（差 %s，%s）" % (
        C.money((data.get("totals_early") or {}).get("net_worth")),
        C.money((data.get("totals_late") or {}).get("net_worth")),
        C.money(data.get("delta_net_worth")), C.pct(data.get("rate_net_worth"))))
    for row in (data.get("rows") or [])[:15]:
        mark = "新增" if row.get("is_new") else ("已消失" if row.get("is_gone") else "")
        lines.append("  %-16s %14s → %14s ／ 差 %14s %s" % (
            row.get("name"), C.money(row.get("a")), C.money(row.get("b")), C.money(row.get("diff")), mark))
    C.emit(args, lines, {"ok": True, "view": "compare", **data})
    return C.EXIT_OK


def view_returns(ledger, args):
    snap, msg = resolve_snapshot_or_fail(ledger, args.snapshot)
    if snap is None:
        return empty_view(args, msg, "returns")
    data = ledger.get("reports_returns", params={"snapshot_id": snap["id"]})
    lines = ["收益面板（快照 %s · 服务端口径）" % snap["date"]]
    lines.append("  纳入 %s 个账户 · 本金 %s → 市值 %s" % (data.get("count"), C.money(data.get("principal")), C.money(data.get("market"))))
    lines.append("  累计收益 %s（%s）" % (C.money(data.get("cum")), C.pct(data.get("cum_rate"))))
    lines.append("  本期收益 %s（%s）" % (
        C.money(data.get("per")) if data.get("has_per") else "本期不可用", C.pct(data.get("per_rate"))))
    lines.append("  未跟踪本金 %s 个 · 首期 %s 个 · 基准缺失 %s 个"
                 % (data.get("no_principal"), data.get("first"), data.get("missing_base")))
    C.emit(args, lines, {"ok": True, "view": "returns", "snapshot": snap, **data})
    return C.EXIT_OK


def view_returns_breakdown(ledger, args):
    dim = norm_dim(args.dim)
    if dim == "tag":
        raise C.FsError("收益维度不支持标签（服务端只提供账户 / 平台 / 分类 / 币种）")
    snap, msg = resolve_snapshot_or_fail(ledger, args.snapshot)
    if snap is None:
        return empty_view(args, msg, "returns-breakdown")
    data = ledger.get("reports_returns_breakdown", params={"snapshot_id": snap["id"], "dim": dim})
    lines = ["收益按%s汇总（快照 %s · 服务端口径）" % (dim, snap["date"])]
    for row in data.get("rows") or []:
        per = C.money(row.get("per")) if row.get("per") is not None else "—（%s）" % {"first": "首期", "base-missing": "基准缺失", "ok": "本期不可用"}.get(row.get("per_status"), row.get("per_status"))
        lines.append("  %-16s %s 个 · 累计 %14s（%s） · 本期 %s" % (
            row.get("name"), row.get("n"), C.money(row.get("cum")), C.pct(row.get("cum_rate")), per))
    C.emit(args, lines, {"ok": True, "view": "returns-breakdown", "snapshot": snap, **data})
    return C.EXIT_OK


def view_returns_trend(ledger, args):
    data = ledger.get("reports_returns_trend")
    lines = ["累计收益 / 本期收益趋势（服务端口径）· 共 %d 个点" % len(data.get("points") or [])]
    for p in (data.get("points") or [])[-12:]:
        per = C.money(p.get("per")) if p.get("per_available") else "本期不可用"
        lines.append("  %s  累计 %14s ／ 本期 %s ／ 纳入 %s" % (p.get("date"), C.money(p.get("cum")), per, p.get("count")))
    C.emit(args, lines, {"ok": True, "view": "returns-trend", **data})
    return C.EXIT_OK


def view_accounts(ledger, args):
    _, listed = C.accounts_index(ledger)
    want_archived = args.archived
    if want_archived == "false":
        listed = [a for a in listed if not a.get("archived")]
    elif want_archived == "true":
        listed = [a for a in listed if a.get("archived")]
    if args.q:
        needle = args.q.lower()
        listed = [a for a in listed if needle in (a.get("name") or "").lower()]
    lines = ["账户 %d 个%s" % (len(listed), "（含已归档）" if want_archived != "false" else "")]
    for a in listed:
        lines.append("  %-16s %-8s %-10s %-10s %s%s%s" % (
            a.get("name"), a.get("id"), a.get("_platform"), a.get("_category"), a.get("currency"),
            "／资产" if a.get("type") == "asset" else "／负债",
            "／已归档" if a.get("archived") else "",
        ))
    C.emit(args, lines, {"ok": True, "view": "accounts", "total": len(listed), "items": listed})
    return C.EXIT_OK


def resolve_account(accounts, ref):
    for a in accounts:
        if a.get("id") == ref:
            return a
    exact = [a for a in accounts if (a.get("name") or "") == ref]
    if len(exact) == 1:
        return exact[0]
    loose = [a for a in accounts if ref in (a.get("name") or "")]
    if len(loose) == 1:
        return loose[0]
    if len(loose) > 1:
        raise C.FsError("「%s」匹配到多个账户：%s" % (ref, "、".join("%s(%s)" % (a["name"], a["id"]) for a in loose)),
                        hint="用账户 id 指认，或把名字写完整")
    raise C.FsError("找不到账户：%s" % ref, hint="用 read.py accounts 看现有账户的 id 与名称")


def view_account(ledger, args):
    accounts, listed = C.accounts_index(ledger)
    acc = resolve_account(listed, args.ref)
    lines = ["账户 %s（%s）" % (acc.get("name"), acc.get("id"))]
    lines.append("  平台 %s · 分类 %s · 类型 %s · 币种 %s"
                 % (acc.get("_platform"), acc.get("_category"), "资产" if acc.get("type") == "asset" else "负债", acc.get("currency")))
    lines.append("  标签 %s · 计入净资产 %s · 跟踪本金 %s · 状态 %s"
                 % ("、".join(acc.get("_tags") or []) or "—", "是" if acc.get("include_in_net_worth") else "否",
                    "是" if acc.get("track_principal") else "否", "已归档" if acc.get("archived") else "在用"))
    if acc.get("note"):
        lines.append("  备注 %s" % acc["note"])

    snap, msg = resolve_snapshot_or_fail(ledger, None)
    payload = {"ok": True, "view": "account", "account": acc}
    if snap is None:
        lines.append("  " + msg)
        C.emit(args, lines, payload)
        return C.EXIT_OK

    detail = ledger.get("snapshot", id=snap["id"])
    item = next((i for i in (detail.get("items") or []) if i.get("account_id") == acc["id"]), None)
    if item:
        lines.append("最近一期 %s（%s）" % (snap["date"], snap["id"]))
        lines.append("  原币 %s %s · 汇率 %s · 折本位币 %s · %s"
                     % (C.money(item.get("original_amount")), item.get("currency"),
                        C.rate(item.get("exchange_rate")), C.money(item.get("amount_in_base")),
                        "沿用上次" if item.get("is_carried_over") else "本次填写"))
        if item.get("tracks_principal"):
            lines.append("  本金 %s %s · 折本位币 %s"
                         % (C.money(item.get("principal")), item.get("currency"), C.money(item.get("principal_in_base"))))
        payload["item"] = item
    else:
        lines.append("最近一期 %s 里没有这个账户（该期未写入或已归档）" % snap["date"])

    try:
        rets = ledger.get("snapshot_returns", id=snap["id"])
        r = next((x for x in (rets.get("items") or []) if x.get("account_id") == acc["id"]), None)
        if r:
            lines.append("  收益（服务端口径）：%s" % RETURN_STATUS.get(r.get("status"), r.get("status")))
            if r.get("cum") is not None:
                lines.append("    累计收益 %s（%s）· 本期收益 %s（%s）"
                             % (C.money(r.get("cum")), C.pct(r.get("cum_rate")),
                                C.money(r.get("per")) if r.get("per") is not None else "不可用", C.pct(r.get("per_rate"))))
            else:
                lines.append("    累计收益不可用（原因见上）")
            if r.get("prev_amount") is not None:
                # 刻意不写出基准日期：基准是「更早、且包含该账户的最新一期」，不保证是紧邻的那一期
                lines.append("    较上一期 %s → %s（基准期为更早且包含该账户的最新一期）"
                             % (C.money(r.get("prev_amount")), C.money(r.get("original_amount"))))
            elif r.get("status") == "ok":
                lines.append("    较上一期：无可用对比基准")
            payload["returns"] = r
    except C.ApiError as e:
        lines.append("  收益：取不到（%s）" % e.render())

    C.emit(args, lines, payload)
    return C.EXIT_OK


def view_snapshots(ledger, args):
    snaps = all_snapshots(ledger)
    if args.frm:
        snaps = [s for s in snaps if (s.get("date") or "") >= args.frm]
    if args.to:
        snaps = [s for s in snaps if (s.get("date") or "") <= args.to]
    lines = ["快照 %d 期（时间升序）" % len(snaps)]
    for s in snaps:
        lines.append("  %s  %s  净资产 %14s  明细 %s 条%s"
                     % (s.get("date"), s.get("id"), C.money(s.get("net_worth")), s.get("item_count"),
                        ("　备注：" + s["note"]) if s.get("note") else ""))
    C.emit(args, lines, {"ok": True, "view": "snapshots", "total": len(snaps), "items": snaps})
    return C.EXIT_OK


def view_snapshot(ledger, args):
    snap, msg = resolve_snapshot_or_fail(ledger, args.ref)
    if snap is None:
        return empty_view(args, msg, "snapshot")
    detail = ledger.get("snapshot", id=snap["id"], params={"view": args.detail_view or "origin"})
    lines = ["快照 %s（%s）%s%s" % (
        detail.get("date"), detail.get("id"), "　备注：" + detail["note"] if detail.get("note") else "",
        "　口径：%s" % ("原口径（保存时冻结）" if (args.detail_view or "origin") == "origin" else "现口径（按本位币变更折算的参考值）"))]
    lines.append("  总资产 %s ／ 总负债 %s ／ 净资产 %s · 本位币 %s"
                 % (C.money(detail.get("total_assets")), C.money(detail.get("total_liabilities")),
                    C.money(detail.get("net_worth")), detail.get("base_currency")))
    lines.append("  明细 %s 条（沿用上次 %s 条 · 跟踪本金 %s 条 · 未填本金 %s 条）"
                 % (detail.get("item_count"), detail.get("carried_over"), detail.get("tracked"), detail.get("no_principal")))
    for it in detail.get("items") or []:
        lines.append("  %-16s %-14s %10s %-8s → %14s%s" % (
            it.get("account_name_snapshot"), it.get("platform_name_snapshot"), C.money(it.get("original_amount")),
            it.get("currency"), C.money(it.get("amount_in_base")),
            "　沿用" if it.get("is_carried_over") else ""))
    C.emit(args, lines, {"ok": True, "view": "snapshot", **detail})
    return C.EXIT_OK


def view_catalog(ledger, args):
    kind = C.CATALOG_ALIASES.get((args.kind or "").strip().lower())
    if kind is None:
        raise C.FsError("不认识的基础资料类别：%s" % args.kind, hint="可用：币种 / 平台 / 分类 / 标签 / all")
    payload = {"ok": True, "view": "catalog"}
    lines = []
    titles = {"currencies": "币种", "platforms": "平台", "categories": "分类", "tags": "标签"}
    keys = ["currencies", "platforms", "categories", "tags"] if kind == "all" else [kind]
    for key in keys:
        data = ledger.get(key, params={"includeDisabled": "true"})
        rows = data.get("items", []) if isinstance(data, dict) else []
        lines.append("【%s】%d 项" % (titles.get(key, key), len(rows)))
        for r in rows:
            if key == "currencies":
                lines.append("  %-8s %-14s 汇率 %-12s %s" % (
                    r.get("code"), r.get("name"), C.rate(r.get("rate_to_base")),
                    "启用" if r.get("enabled") else "已停用"))
            else:
                extra = ""
                if key == "categories":
                    extra = "／默认跟踪本金" if r.get("default_track_principal") else ""
                    extra += "／资产" if r.get("type") == "asset" else "／负债"
                lines.append("  %-14s %-16s %-12s %s%s" % (
                    r.get("id"), r.get("name"), "启用" if r.get("enabled") else "已停用",
                    "被 %s 个账户引用" % r.get("account_count"), extra))
        payload[key] = rows
    C.emit(args, lines, payload)
    return C.EXIT_OK


def view_rates(ledger, args):
    data = ledger.get("rates")
    lines = ["默认汇率表（本位币 %s）· 方向：1 单位原币 = X 本位币" % data.get("base_currency")]
    for code, micro in (data.get("rates") or {}).items():
        lines.append("  %-8s %s" % (code, C.rate(micro)))
    C.emit(args, lines, {"ok": True, "view": "rates", **data})
    return C.EXIT_OK


def view_settings(ledger, args):
    data = ledger.get("settings")
    lines = ["设置：本位币 %s · 界面版本 %s · 数据格式版本 %s · 数据模式 %s"
             % (data.get("base_currency"), data.get("app_version"), data.get("schema_version"), data.get("data_mode"))]
    C.emit(args, lines, {"ok": True, "view": "settings", **data})
    return C.EXIT_OK


def view_import_preview(ledger, args):
    if not args.file:
        raise C.FsError("import-preview 需要 --file <csv 文件>")
    try:
        with open(args.file, "r", encoding="utf-8-sig", errors="replace") as fh:
            content = fh.read()
    except OSError as e:
        raise C.FsError("读不到文件：%s（%s）" % (args.file, e))
    data = ledger.send("POST", "import_preview",
                       body={"filename": os.path.basename(args.file), "content": content})
    lines = ["导入预览（**只算不写**）：新增 %s 行 · 更新 %s 行 · 冲突 %s 行 · 跳过 %s 行"
             % (data.get("inserted"), data.get("updated"), data.get("conflicts"), data.get("skipped"))]
    for row in data.get("rows") or []:
        if row.get("action") in ("conflict", "skip"):
            lines.append("  第 %s 行 [%s] %s —— %s" % (row.get("line"), row.get("action"), row.get("account_name"), row.get("reason")))
    if data.get("conflicts"):
        lines.append("  ⚠ 有冲突时服务端会整体拒绝应用（不会导入一半），请先修正再 apply")
    lines.append("  注意：CSV 的余额列**不会**落库，余额只能通过盘点录入。")
    C.emit(args, lines, {"ok": True, "view": "import-preview", **data})
    return C.EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(description="家底快照 · 只读取数")
    p.add_argument("view", choices=VIEWS, help="视图名")
    p.add_argument("ref", nargs="?", help="account / snapshot 视图的目标（id 或名称/日期）")
    p.add_argument("b", nargs="?", help="compare 视图的第二期（id 或日期）")
    p.add_argument("--dim", help="维度：account / platform / category / currency / tag")
    p.add_argument("--metric", help="趋势指标：net_worth / total_assets / total_liabilities")
    p.add_argument("--mode", choices=["origin", "current"], help="口径：origin 原口径 / current 现口径")
    p.add_argument("--snapshot", help="指定快照（id 或日期），缺省取最新一期")
    p.add_argument("--view", dest="detail_view", choices=["origin", "current"], help="snapshot 视图的口径")
    p.add_argument("--from", dest="frm", help="snapshots 视图起始日期 YYYY-MM-DD")
    p.add_argument("--to", help="snapshots 视图结束日期 YYYY-MM-DD")
    p.add_argument("--archived", choices=["true", "false"], default="false",
                   help="accounts 视图是否只看已归档（缺省只看在用的）")
    p.add_argument("--q", help="accounts 视图按名称过滤")
    p.add_argument("--file", help="import-preview 的 CSV 文件")
    C.add_common_args(p)
    return p


def main() -> int:
    args = build_parser().parse_args()
    # compare 视图的两个位置参数（argparse 里只有两个通用位置槽位，这里显式命名）
    if args.view == "compare":
        args.a, args.b = args.ref, args.b
        if not args.a or not args.b:
            raise C.FsError("compare 需要两期：read.py compare <早期> <晚期>（id 或日期）")
    ledger = C.resolve_ledger(args.ledger, args.base_url)

    dispatch = {
        "overview": view_overview, "trend": view_trend, "breakdown": view_breakdown,
        "compare": view_compare, "returns": view_returns, "returns-breakdown": view_returns_breakdown,
        "returns-trend": view_returns_trend, "accounts": view_accounts, "account": view_account,
        "snapshots": view_snapshots, "snapshot": view_snapshot, "catalog": view_catalog,
        "rates": view_rates, "settings": view_settings, "import-preview": view_import_preview,
    }
    view = args.view
    if view in ("account", "snapshot") and not args.ref:
        raise C.FsError("%s 视图需要一个目标（id 或名称/日期）" % view)
    if view == "breakdown":
        args.dim = args.dim or args.ref or "platform"
    if view == "returns-breakdown":
        args.dim = args.dim or args.ref or "platform"
    if view == "catalog":
        args.kind = args.ref
    return dispatch[view](ledger, args)


if __name__ == "__main__":
    C.run(main)
