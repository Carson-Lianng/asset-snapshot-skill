#!/usr/bin/env python3
"""vizspec.py —— 把服务端返回**翻译**成可视化规格（VizSpec）。

它不画图：图由宿主平台渲染。本文件的产出是一份「这张图该怎么画」的规格，
所以它必须是**可审计**的 —— 见下面三条纪律。

纪律（SKILL.md 硬规则 V1–V7）：
1. **只做字段映射**。输入是 read.py 已经拿到的服务端返回，输出是图规格；
   图上的每个数字都能在输入里指到同名字段。**不排序、不聚合、不补零**。
2. **唯一的算术是占比**（`share_of`）。服务端的 `BreakdownRowDTO` 只有
   `asset / liability / net / count`，**没有占比字段**，所以占比一定是派生量 ——
   它必须带 `caliber="derived"`、固定标注串、`formula_text` 与 `denominator`（写明分母是什么、值多少）。
   除此之外本文件不含任何公式。
   ⚠ **占比与「出不出图」解耦**：`MAX_ROWS` 保护的是**可读性**（条形会糊），
   而占比是**数据** —— 13 行做成表格完全读得下去，不该因为画不下就让人拿不到。
   所以 `share_block()` 独立于 `build()` 产出，图被否掉时它照样在；
   `share_note` / `viz_note` 会说明原因，绝不静默。
3. **不掩盖特征态**：收益不可用就是 `null`（不补 0）；沿用上次、折算参考值、
   本位币变更点都要带标记。条目超过 MAX_ROWS 不出图（降采样属于聚合，所以不做）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

#: 超过这个条目数不出图 —— 条形会糊，退回文本更诚实（**不做降采样**）
MAX_ROWS = 12

#: 折线至少要有这么多期，否则「走势」无从谈起（两期是「对比」，该用 compare）
MIN_LINE_POINTS = 3

#: 派生量的固定标注串（与 references/safety.md、设计文档 §5.1.7 保持逐字一致）
DERIVED_NOTICE = "非系统口径 · 未落库 · 不出现在系统界面"

#: 配色只给语义角色，由渲染层按主题落地；涨跌方向遵循中国市场口径
CONVENTION = {
    "up": "red",
    "down": "green",
    "flat": "neutral",
    "sign": "按行取值：row.dir 为 up → red / down → green / flat → neutral",
    "note": "涨红跌绿（中国市场口径，与系统界面一致）；不要用颜色以外的编码表达涨跌",
}

METRIC_LABELS = {
    "net_worth": "净资产",
    "total_assets": "总资产",
    "total_liabilities": "总负债",
}

#: 刻意不出图的视图 —— 都是词典式清单或账目明细，出图反而更难读。
#: 这里给的是**理由**：调用方要能说清「为什么没给图」，不能静默跳过。
NO_VIZ = {
    "accounts": "账户清单是词典式列表，文本更好读",
    "account": "单个账户的属性与账目是词典式信息，出图会丢细节",
    "snapshots": "快照列表是词典式列表；看走势请用 trend",
    "snapshot": "单张快照的明细是账目清单；看结构请用 breakdown",
    "catalog": "基础资料是词典式列表，不出图",
    "rates": "汇率表是词典式列表，不出图",
    "settings": "设置是键值信息，不出图",
    "import-preview": "导入预览是**待确认清单**，出图会让人误以为已经生效",
}


# --------------------------------------------------------------------------
# 基础小件
# --------------------------------------------------------------------------


def yuan(micro):
    """微元整数 → 元（数值）。`None` 原样返回 `None` —— **绝不补 0**。

    保留完整精度，格式化（几位小数）交给渲染层，技能不做显示取舍。
    """
    if micro is None:
        return None
    if isinstance(micro, str):
        try:
            micro = int(micro)
        except ValueError:
            return None
    try:
        return int(micro) / C.MICRO
    except (TypeError, ValueError):
        return None


def series(key, label, unit="CNY", decimals=2, caliber="server",
           color_role="neutral", **extra):
    """一个序列。`caliber` 与 `color_role` 是**必填**（V2、V6）。"""
    out = {
        "key": key,
        "label": label,
        "unit": unit,
        "decimals": decimals,
        "caliber": caliber,
        "color_role": color_role,
    }
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _snap_annotations(snap, extra=None):
    lines = []
    if snap:
        lines.append("快照 %s（%s）" % (snap.get("date"), snap.get("id")))
        if snap.get("base_currency"):
            lines.append("本位币 %s" % snap["base_currency"])
    if extra:
        lines.extend(extra)
    return lines


def _direction(value):
    """数值 → 语义方向。只做正负判读，不做量级判断。"""
    if value is None:
        return "flat"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _too_many(what, n):
    return ("%s 共 %d 项，超过 %d 项上限 —— 不做降采样（降采样属于聚合），"
            "请退回文本或缩小范围" % (what, n, MAX_ROWS))


# --------------------------------------------------------------------------
# 唯一的算术：占比（服务端不提供）
# --------------------------------------------------------------------------


def share_of(rows, denominator_micro, field="net"):
    """占比 = 该行 `field` ÷ 分母。**全技能唯一的算术**。

    返回 `[(label, ratio | None)]`；分母缺失或为 0 时给 `None`（**不补 0**）。
    调用方必须把结果当作派生量呈现：带标注、写明分母。
    """
    denom = yuan(denominator_micro)
    out = []
    for row in rows or []:
        value = yuan(row.get(field))
        label = row.get("name") or row.get("label")
        if not denom or value is None:
            out.append((label, None))
        else:
            out.append((label, value / denom))
    return out


#: 派生占比在各视图的形态：视图 → (取值字段, 公式文本, 分母名, 分母出处, 分母微元)。
#: ⚠ 分母一律取**服务端的汇总字段**，绝不在本地求和。
def _share_form(view, payload):
    if view == "breakdown":
        return ("net", "该行净值 ÷ 本期净资产", "本期净资产",
                "snapshot.net_worth（服务端汇总列）",
                (payload.get("snapshot") or {}).get("net_worth"))
    if view == "compare":
        return ("b", "该行晚期净值 ÷ 晚期净资产", "晚期净资产",
                "totals_late.net_worth（服务端汇总列）",
                (payload.get("totals_late") or {}).get("net_worth"))
    if view == "returns-breakdown":
        return ("cum", "该行累计收益 ÷ 本期市值", "本期市值",
                "reports.returns.market（服务端汇总列）", payload.get("market"))
    return None


def _denominator_of(form):
    return {"label": form[2], "unit": "CNY", "value": yuan(form[4]), "source": form[3]}


def share_series(form):
    """占比序列 —— 结构上就带着「这是派生量」的全部证据。"""
    return series(
        "share", "占比", unit="ratio", decimals=4, caliber="derived",
        color_role="derived", display_as="percent",
        formula_text=form[1],
        notice=DERIVED_NOTICE,
        denominator=_denominator_of(form),
    )


def require_share_view(view):
    """`--share` 只在这几个视图有意义；其它视图**显式报错，不静默忽略**。"""
    if view not in SHARE_VIEWS:
        raise C.FsError("--share 只支持这几个视图：%s" % "、".join(SHARE_VIEWS),
                        hint="其它视图的占比要么没有意义，要么分母不明确")


def share_block(payload):
    """派生占比 —— **与「出不出图」解耦**。

    图的条目上限保护的是**可读性**；占比是**数据**。使用者明确问了占比，
    就不该因为「画不下」而拿不到 —— 13 行做成表格完全读得下去。
    所以这里独立于 `build()` 产出，图被否掉时它照样在。

    返回 `{caliber, formula_text, denominator, notice, field, rows:[{label, value}]}`；
    视图不支持或没有行时返回 `None`（调用方用 `require_share_view` 区分「不支持」）。
    """
    payload = payload or {}
    form = _share_form(payload.get("view"), payload)
    if form is None:
        return None
    rows_in = payload.get("rows") or []
    if not rows_in:
        return None
    return {
        "caliber": "derived",
        "label": "占比",
        "unit": "ratio",
        "display_as": "percent",
        "decimals": 4,
        "field": form[0],
        "formula_text": form[1],
        "denominator": _denominator_of(form),
        "notice": DERIVED_NOTICE,
        "rows": [{"label": label, "value": value}
                 for label, value in share_of(rows_in, form[4], field=form[0])],
    }


def _share_into(payload, row_out, series_list, annotations):
    """把占比同时写进图的行与序列 —— 图的产物与 `share_block()` **同源**，不许分叉。"""
    form = _share_form(payload.get("view"), payload)
    block = share_block(payload)
    if form is None or block is None:
        return
    for row, item in zip(row_out, block["rows"]):
        row["share"] = item["value"]
    series_list.append(share_series(form))
    annotations.append("占比为派生量：%s" % DERIVED_NOTICE)


# --------------------------------------------------------------------------
# 各视图的映射
# --------------------------------------------------------------------------


def _overview(payload, want_share):
    snap = payload.get("snapshot") or {}
    counts = []
    if snap.get("item_count") is not None:
        note = "明细 %s 条" % snap.get("item_count")
        if snap.get("carried_over"):
            note += "（沿用上次 %s 条）" % snap.get("carried_over")
        counts.append(note)
    return {
        "kind": "kpi",
        "title": "资产概览",
        "source": {"view": "overview", "endpoint": "GET " + C.EP["snapshots"],
                   "snapshot_id": snap.get("id")},
        "annotations": _snap_annotations(snap, counts),
        "series": [
            series("net_worth", "净资产"),
            series("total_assets", "总资产"),
            series("total_liabilities", "总负债"),
        ],
        "rows": [{
            "label": snap.get("date") or "最新一期",
            "net_worth": yuan(snap.get("net_worth")),
            "total_assets": yuan(snap.get("total_assets")),
            "total_liabilities": yuan(snap.get("total_liabilities")),
        }],
    }, None


def _trend(payload, want_share):
    points = payload.get("points") or []
    if len(points) > MAX_ROWS:
        return None, _too_many("趋势点", len(points))
    if len(points) < MIN_LINE_POINTS:
        return None, ("只有 %d 期，画折线看不出走势 —— 两期属于「对比」，"
                      "用 compare 更合适" % len(points))
    metric = payload.get("metric") or "net_worth"
    label = METRIC_LABELS.get(metric, metric)
    rows = []
    breaks = []
    prev_ccy = None
    for p in points:
        row = {"label": p.get("date"), metric: yuan(p.get(metric))}
        if p.get("ref"):
            row["ref"] = True
        ccy = p.get("base_currency")
        row["base_currency"] = ccy
        if prev_ccy is not None and ccy != prev_ccy:
            breaks.append({"at_label": p.get("date"),
                           "because": "本位币 %s → %s" % (prev_ccy, ccy)})
        prev_ccy = ccy
        rows.append(row)

    annotations = ["共 %d 期" % len(points)]
    if payload.get("mode") == "current" or any(r.get("ref") for r in rows):
        annotations.append("含折算参考值")
    if breaks:
        annotations.append("本位币变更点折线断开（共 %d 处）" % len(breaks))

    spec = {
        "kind": "line",
        "title": "%s趋势（%s）" % (label, "原口径" if payload.get("mode") != "current" else "现口径"),
        "source": {"view": "trend", "endpoint": "GET " + C.EP["reports_trend"], "metric": metric},
        "annotations": annotations,
        "axis": {"start_at_zero": False,
                 "note": "纵轴不从 0 起：看变化幅度，不看绝对值"},
        "series": [series(metric, label)],
        "rows": rows,
    }
    if breaks:
        spec["breaks"] = breaks
    return spec, None


def _breakdown(payload, want_share):
    rows_in = payload.get("rows") or []
    if len(rows_in) > MAX_ROWS:
        return None, _too_many("结构行", len(rows_in))
    snap = payload.get("snapshot") or {}
    row_out = [{"label": r.get("name"), "net": yuan(r.get("net")),
                "asset": yuan(r.get("asset")), "liability": yuan(r.get("liability")),
                "count": r.get("count")} for r in rows_in]
    series_list = [
        series("net", "净值"),
        series("asset", "资产"),
        series("liability", "负债"),
    ]
    annotations = _snap_annotations(snap, ["%s 维度 · 服务端已按净值降序（不重排）" % payload.get("dim")])
    if payload.get("ref"):
        annotations.append("含折算参考值")

    if want_share:
        # 使用者明确要了占比 —— 这时才产出派生序列，且它必须自带标注与分母
        _share_into(payload, row_out, series_list, annotations)

    return {
        "kind": "bar",
        "title": "%s结构" % (payload.get("dim") or "维度"),
        "source": {"view": "breakdown", "endpoint": "GET " + C.EP["reports_breakdown"],
                   "snapshot_id": payload.get("snapshot_id"), "dim": payload.get("dim")},
        "annotations": annotations,
        "axis": {"start_at_zero": True, "note": "条形必须从 0 起，否则会放大差异"},
        "series": series_list,
        "rows": row_out,
    }, None


def _compare(payload, want_share):
    rows_in = payload.get("rows") or []
    if len(rows_in) > MAX_ROWS:
        return None, _too_many("对比行", len(rows_in))
    early = payload.get("early") or {}
    late = payload.get("late") or {}
    rows = []
    for r in rows_in:
        row = {"label": r.get("name"), "diff": yuan(r.get("diff")),
               "a": yuan(r.get("a")), "b": yuan(r.get("b")),
               "dir": _direction(yuan(r.get("diff")))}
        if r.get("is_new"):
            row["flag"] = "新增"
        elif r.get("is_gone"):
            row["flag"] = "已消失"
        rows.append(row)

    annotations = [
        "%s（%s）→ %s（%s）" % (early.get("date"), early.get("id"), late.get("date"), late.get("id")),
        "%s 维度 · 差值取自服务端 delta，不在本地相减" % payload.get("dim"),
    ]
    if payload.get("cross_currency"):
        annotations.append("跨币种折算参考值")

    series_list = [
        series("diff", "差额", color_role="sign", color_by="row.dir"),
        series("a", "早期", color_role="neutral"),
        series("b", "晚期", color_role="neutral"),
    ]
    if want_share:
        _share_into(payload, rows, series_list, annotations)

    spec = {
        "kind": "bar-diff",
        "title": "两期对比 · %s" % (payload.get("dim") or "维度"),
        "source": {"view": "compare", "endpoint": "GET " + C.EP["snapshots_compare"],
                   "a": early.get("id"), "b": late.get("id"), "dim": payload.get("dim")},
        "annotations": annotations,
        "axis": {"start_at_zero": True, "note": "双向条以 0 为轴"},
        "series": series_list,
        "totals": {
            "net_worth_early": yuan((payload.get("totals_early") or {}).get("net_worth")),
            "net_worth_late": yuan((payload.get("totals_late") or {}).get("net_worth")),
            "delta_net_worth": yuan(payload.get("delta_net_worth")),
            "rate_net_worth": payload.get("rate_net_worth"),
        },
        "rows": rows,
    }
    return spec, None


def _returns(payload, want_share):
    snap = payload.get("snapshot") or {}
    has_per = bool(payload.get("has_per"))
    rows = [{
        "label": snap.get("date") or "最新一期",
        "cum": yuan(payload.get("cum")),
        "per": yuan(payload.get("per")) if has_per else None,
        "cum_rate": payload.get("cum_rate"),
        "per_rate": payload.get("per_rate") if has_per else None,
        "per_status": "ok" if has_per else "unavailable",
    }]
    annotations = _snap_annotations(snap, ["服务端收益面板"])
    if not has_per:
        annotations.append("本期收益不可用：按服务端为准，**不画成 0**")
    states = []
    for k, cn in (("no_principal", "未跟踪本金"), ("first", "首期"), ("missing_base", "基准缺失")):
        if payload.get(k):
            states.append("%s %s" % (cn, payload[k]))
    if states:
        annotations.append("不计入的账户：" + " · ".join(states))
    return {
        "kind": "kpi",
        "title": "收益面板",
        "source": {"view": "returns", "endpoint": "GET " + C.EP["reports_returns"],
                   "snapshot_id": snap.get("id")},
        "annotations": annotations,
        "series": [
            series("cum", "累计收益"),
            series("per", "本期收益"),
        ],
        "rows": rows,
    }, None


def _returns_breakdown(payload, want_share):
    rows_in = payload.get("rows") or []
    if len(rows_in) > MAX_ROWS:
        return None, _too_many("收益行", len(rows_in))
    snap = payload.get("snapshot") or {}
    rows = []
    for r in rows_in:
        rows.append({
            "label": r.get("name"),
            "cum": yuan(r.get("cum")),
            "per": yuan(r.get("per")),
            "n": r.get("n"),
            "per_status": r.get("per_status"),
        })
    annotations = _snap_annotations(
        snap, ["%s 维度 · 服务端收益汇总" % payload.get("dim"),
               "本期收益不可用（首期 / 基准缺失）的行留空，**不画成 0**"])
    series_list = [series("cum", "累计收益"), series("per", "本期收益")]
    if want_share:
        _share_into(payload, rows, series_list, annotations)
    return {
        "kind": "bar",
        "title": "收益按%s" % (payload.get("dim") or "维度"),
        "source": {"view": "returns-breakdown",
                   "endpoint": "GET " + C.EP["reports_returns_breakdown"],
                   "snapshot_id": snap.get("id"), "dim": payload.get("dim")},
        "annotations": annotations,
        "axis": {"start_at_zero": True, "note": "双向条以 0 为轴"},
        "series": series_list,
        "rows": rows,
    }, None


def _returns_trend(payload, want_share):
    points = payload.get("points") or []
    if len(points) > MAX_ROWS:
        return None, _too_many("趋势点", len(points))
    if len(points) < MIN_LINE_POINTS:
        return None, ("只有 %d 期，画折线看不出走势 —— 两期属于「对比」，"
                      "用 returns-breakdown 看结构更合适" % len(points))
    rows = []
    for p in points:
        rows.append({
            "label": p.get("date"),
            "cum": yuan(p.get("cum")),
            "per": yuan(p.get("per")) if p.get("per_available") else None,
            "count": p.get("count"),
        })
    return {
        "kind": "line",
        "title": "累计收益 / 本期收益趋势",
        "source": {"view": "returns-trend", "endpoint": "GET " + C.EP["reports_returns_trend"]},
        "annotations": ["共 %d 期" % len(points),
                        "本期收益不可用的点留空，**不画成 0**"],
        "axis": {"start_at_zero": False, "note": "纵轴不从 0 起：看变化幅度"},
        "series": [series("cum", "累计收益"), series("per", "本期收益")],
        "rows": rows,
    }, None


BUILDERS = {
    "overview": _overview,
    "trend": _trend,
    "breakdown": _breakdown,
    "compare": _compare,
    "returns": _returns,
    "returns-breakdown": _returns_breakdown,
    "returns-trend": _returns_trend,
}

#: 明确支持派生占比的视图（其它视图传 --share 直接报错，**不静默忽略**）
SHARE_VIEWS = ("breakdown", "compare", "returns-breakdown")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build(payload, want_share=False):
    """返回 `(spec, note)`：两者必有其一。

    * 有图 → `(spec, None)`
    * 没图 → `(None, 理由)`，理由必须能直接说给使用者听（**不静默跳过**）
    """
    view = (payload or {}).get("view")
    if not view:
        return None, "返回体里没有视图标识，无法生成图规格"
    if want_share:
        require_share_view(view)
    if view in NO_VIZ:
        return None, NO_VIZ[view]
    builder = BUILDERS.get(view)
    if builder is None:
        return None, "视图 %s 目前没有对应的图型" % view
    if payload.get("empty"):
        return None, "这一期还没有数据：先盘一次，图才有东西可画"
    spec, note = builder(payload, want_share)
    if spec is None:
        if want_share and share_block(payload) is not None:
            note = (note or "") + "；占比与图无关，仍已单独给出（见 share 字段）"
        return None, note
    spec.setdefault("caliber_notice", DERIVED_NOTICE)
    spec.setdefault("color_convention", CONVENTION)
    spec.setdefault("render_hint", "由宿主平台渲染；无渲染能力时退回 Markdown 表，再退回文本")
    return spec, None


def main() -> int:
    """给自己留一个自检口：把「有图 / 无图」的视图清单一目了然地打出来。"""
    import argparse

    p = argparse.ArgumentParser(description="家底快照 · 可视化规格（自查用）")
    p.add_argument("--list", action="store_true", help="列出各视图的图型或不出图理由")
    p.add_argument("--view", help="对某个视图打印它为什么不出图")
    C.add_common_args(p)
    args = p.parse_args()

    if args.view:
        note = NO_VIZ.get(args.view)
        if note is None and args.view not in BUILDERS:
            note = "视图 %s 目前没有对应的图型" % args.view
        C.emit(args, [note or "「%s」可以出图（%s）" % (args.view, "bar/kpi/line"),
                      "派生占比（--share）可用视图：%s" % "、".join(SHARE_VIEWS)],
               {"ok": True, "view": args.view, "reason": note,
                "share_views": list(SHARE_VIEWS)})
        return C.EXIT_OK

    lines = ["可视化规格 · 映射表（唯一实现：scripts/vizspec.py）", ""]
    payload = {"with_viz": {}, "no_viz": dict(NO_VIZ)}
    for view in sorted(BUILDERS):
        lines.append("  ✓ %-18s %s" % (view, "支持出图" + ("（可加派生占比）" if view in SHARE_VIEWS else "")))
        payload["with_viz"][view] = "ok"
    lines.append("")
    for view in sorted(NO_VIZ):
        lines.append("  ✗ %-18s %s" % (view, NO_VIZ[view]))
    lines.append("")
    lines.append("  上限 %d 项；超过即退回文本（不做降采样）" % MAX_ROWS)
    lines.append("  占比是派生量：%s" % DERIVED_NOTICE)
    C.emit(args, lines, {"ok": True, "max_rows": MAX_ROWS, **payload})
    return C.EXIT_OK


if __name__ == "__main__":
    C.run(main)
