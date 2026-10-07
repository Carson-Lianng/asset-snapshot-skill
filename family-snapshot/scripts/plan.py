#!/usr/bin/env python3
"""plan.py —— 把「录入项」整理成盘点计划，交服务端权威试算，产出**将写入清单**。

职责边界（重要）：
  · 本脚本负责：解析录入项（JSON 或制表符文本）→ 匹配账户 → 组织请求体 → 调服务端试算 → 排版。
  · 服务端负责：折算、汇总、未填写清单、缺汇率等门禁判定（这些一律**不由技能计算**）。

因此清单里给的是**原币金额**与**服务端试算合计**，不列逐账户折算值 ——
试算返回里没有它，而本地折算会另起一份口径（折算比率的标度与口径系统不同，连乘会差出分位）。
逐账户折算值可在保存后从快照明细读取（`read.py snapshot <id>`）。

用法：
  plan.py --date 2026-09-30 --items 录入项.tsv [--note 备注] [--out 计划.json]
  plan.py --today --items - < 录入项.tsv

录入项格式（两种，自动识别）：

  JSON  —— {"招行活期": 12345.67, "华泰证券": {"金额": 80000, "本金": 75000}}
           或 [{"账户":"招行活期","金额":12345.67,"本金":null}, …]

  文本  —— 每行一条，制表符或逗号分隔：账户名 <TAB> 金额 [<TAB> 本金]
           金额写「沿用」（或 - / carry）表示沿用上次，交由服务端按历史回填。
           以 # 开头的行是注释。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

NAME_KEYS = ("account", "account_name", "name", "账户", "账户名")
AMOUNT_KEYS = ("amount", "金额", "余额")
PRINCIPAL_KEYS = ("principal", "本金")
CARRY_TOKENS = ("沿用", "沿用上次", "carry", "-", "—", "same")

HEADER_TOKENS = ("账户名", "账户", "account", "name")


# --------------------------------------------------------------------------
# 解析录入项
# --------------------------------------------------------------------------


def extract(obj, keys):
    for k in keys:
        if k in obj:
            return obj[k]
    return None


def parse_items(raw: str, micro_in: bool):
    """→ ([{raw_name, amount_micro, principal_micro, carry}], 备注说明)"""
    text = raw.strip()
    if not text:
        raise C.FsError("录入项是空的：没东西可盘")
    if text[0] in "{[":
        return parse_json_items(text, micro_in)
    return parse_text_items(text, micro_in)


def _amount(value, micro_in, where):
    if value is None or (isinstance(value, str) and value.strip() in CARRY_TOKENS):
        return None
    if micro_in:
        try:
            return int(str(value).strip())
        except ValueError:
            raise C.FsError("%s 的金额不是整数（--micro 模式要求微元整数）：%r" % (where, value))
    return C.yuan_to_micro(value)


def parse_json_items(text: str, micro_in: bool):
    try:
        data = json.loads(text)
    except ValueError as e:
        raise C.FsError("录入项的 JSON 解析失败：%s" % e)
    items = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    if isinstance(data, dict):
        for name, value in data.items():
            if isinstance(value, dict):
                items.append((name, _amount(extract(value, AMOUNT_KEYS), micro_in, name),
                              _amount(extract(value, PRINCIPAL_KEYS), micro_in, name)))
            else:
                items.append((name, _amount(value, micro_in, name), None))
    elif isinstance(data, list):
        for i, row in enumerate(data, 1):
            if not isinstance(row, dict):
                raise C.FsError("录入项第 %d 项不是对象：%r" % (i, row))
            name = extract(row, NAME_KEYS)
            if not name:
                raise C.FsError("录入项第 %d 项缺少账户名" % i)
            items.append((str(name), _amount(extract(row, AMOUNT_KEYS), micro_in, name),
                          _amount(extract(row, PRINCIPAL_KEYS), micro_in, name)))
    else:
        raise C.FsError("录入项的 JSON 既不是对象也不是数组")
    return _finalize(items)


def parse_text_items(text: str, micro_in: bool):
    items = []
    for lineno, line in enumerate(text.splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if "\t" in raw:
            parts = [p.strip() for p in raw.split("\t")]
        elif "," in raw:
            parts = [p.strip() for p in raw.split(",", 2)]
        else:
            head, _, tail = raw.rpartition(" ")
            parts = [head.strip(), tail.strip()] if head.strip() else [raw]
        if len(parts) < 2:
            raise C.FsError("第 %d 行读不出「账户名 + 金额」：%r（用制表符或逗号分隔）" % (lineno, raw))
        name = parts[0]
        if lineno == 1 and name.lower() in [t.lower() for t in HEADER_TOKENS]:
            continue  # 从表格里粘来的首行表头
        amount = _amount(parts[1], micro_in, "第 %d 行" % lineno)
        principal = _amount(parts[2], micro_in, "第 %d 行" % lineno) if len(parts) > 2 else None
        items.append((name, amount, principal))
    return _finalize(items)


def _finalize(items):
    seen = {}
    for name, amount, principal in items:
        if not name or not str(name).strip():
            raise C.FsError("有录入项没写账户名")
        key = str(name).strip()
        if key in seen:
            raise C.FsError("录入项里「%s」出现了两次 —— 金额只能有一个值，请合并后再来" % key)
        if amount is None and principal is not None:
            raise C.FsError("「%s」写了本金但没写金额：本金要有对应的市值才有意义" % key)
        seen[key] = (amount, principal)
    if not seen:
        raise C.FsError("录入项里没有任何有效行")
    return seen


# --------------------------------------------------------------------------
# 匹配账户
# --------------------------------------------------------------------------


def match_accounts(listed, wanted):
    by_name = {}
    for a in listed:
        by_name.setdefault((a.get("name") or "").strip(), []).append(a)
    resolved = {}
    missing = []
    for raw_name in wanted:
        if raw_name in by_name:
            cands = by_name[raw_name]
            if len(cands) > 1:
                raise C.FsError("账户名「%s」在账本里有 %d 个同名账户，无法自动指认：%s"
                                % (raw_name, len(cands), "、".join(a["id"] for a in cands)),
                                hint="把其中一个改名，或告诉我该用哪个 id（之后可用 write.py account-update 改名）")
            resolved[raw_name] = cands[0]
            continue
        loose = [a for a in listed if raw_name in (a.get("name") or "")]
        if len(loose) == 1:
            resolved[raw_name] = loose[0]
            continue
        missing.append((raw_name, loose))
    if missing:
        lines = []
        for raw_name, loose in missing:
            if loose:
                lines.append("「%s」不唯一，可能是：%s" % (raw_name, "、".join(a["name"] for a in loose)))
            else:
                lines.append("账本里没有「%s」" % raw_name)
        known = "、".join(a.get("name") for a in listed) or "（一个账户都没有）"
        raise C.FsError("有 %d 项对不上账户：\n  " % len(missing) + "\n  ".join(lines),
                        hint="现有账户：%s。要新建账户请先走 write.py account-create（可逆写，需确认）"
                             % known)
    return resolved


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def read_items(path):
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except OSError as e:
        raise C.FsError("读不到录入项文件：%s（%s）" % (path, e))


def parse_rate_args(pairs):
    """`--rate HKD=0.93` → {"HKD": 930000}（微元）"""
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise C.FsError("--rate 的写法是 币种=数值，例如 --rate HKD=0.93")
        code, _, value = pair.partition("=")
        out[code.strip().upper()] = C.yuan_to_micro(value.strip())
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="盘点计划 → 服务端试算 → 将写入清单")
    p.add_argument("--date", help="盘点日期 YYYY-MM-DD")
    p.add_argument("--today", action="store_true", help="用服务端的「今天」作为盘点日期（避免前后端时区不一致）")
    p.add_argument("--items", required=True, help="录入项文件，或 - 表示从标准输入读")
    p.add_argument("--note", default="", help="快照备注")
    p.add_argument("--rate", action="append", help="本次要改的汇率，可重复：--rate HKD=0.93（1 单位该币种 = X 本位币）")
    p.add_argument("--micro", action="store_true", help="录入项里的金额已经是微元整数（×10^6）")
    p.add_argument("--out", help="把可直接交给 write.py 的计划写成 JSON 文件")
    C.add_common_args(p)
    args = p.parse_args()

    if not args.date and not args.today:
        raise C.FsError("必须给定盘点日期：--date YYYY-MM-DD（或 --today 用服务端的今天）",
                        hint="盘点日期决定这一期在趋势图上的位置，不能由脚本替你猜")

    ledger = C.resolve_ledger(args.ledger, args.base_url)

    session_date = args.date
    if args.today:
        session = ledger.get("inventory_session") or {}
        session_date = session.get("today")
        if not session_date:
            raise C.FsError("服务端没给出「今天」，请改用 --date YYYY-MM-DD")
    if len(session_date) != 10 or session_date[4] != "-":
        raise C.FsError("盘点日期要写成 YYYY-MM-DD，收到：%s" % session_date)

    _, listed = C.accounts_index(ledger)
    if not listed:
        raise C.FsError("账本里还没有任何账户 —— 没有账户就无法盘点",
                        hint="先建账户：write.py account-create --confirm --set name=… --set category_id=… "
                             "--set type=asset --set currency=CNY")

    wanted = parse_items(read_items(args.items), args.micro)
    resolved = match_accounts(listed, wanted)

    entries = {}
    warnings = []
    for raw_name, acc in resolved.items():
        amount, principal = wanted[raw_name]
        entries[acc["id"]] = {
            "amount": amount,
            "principal": principal,
            "state": "carry" if amount is None else "filled",
        }
        if acc.get("archived"):
            warnings.append("「%s」是已归档账户：本次录入会写进快照（服务端对明确定名的归档账户是计入的）" % acc["name"])
        if amount is not None and amount < 0:
            warnings.append("「%s」的金额是负数（%s）：账本按「正数金额 + 资产/负债类型」表达方向，"
                            "负债请填正数；负数只有在确实要冲减时才用" % (acc["name"], C.money(amount)))
        if principal is not None and not acc.get("track_principal"):
            warnings.append("「%s」没有开启跟踪本金，填的本金不会被用于收益计算" % acc["name"])
        if not acc.get("include_in_net_worth"):
            warnings.append("「%s」不计入净资产：它会进快照明细，但不参与净资产合计" % acc["name"])

    rates = parse_rate_args(args.rate)
    body = {"date": session_date, "entries": entries}
    if rates:
        body["rates"] = rates

    preview = ledger.send("POST", "inventory_preview", body=body)
    counts = preview.get("counts") or {}
    totals = preview.get("totals") or {}

    lines = ["将写入清单 · 盘点日期 %s · 本位币 %s" % (preview.get("date"), preview.get("base_currency"))]
    lines.append("  本次点名录入 %d 个账户（下表）。没点名的账户由服务端按规则处理："
                 "有历史则「沿用上次」，没有则记入「本次未填写」。" % len(entries))
    lines.append("")
    lines.append("  账户                      类型   币种   原币金额            本金（原币）")
    for raw_name, acc in resolved.items():
        amount, principal = wanted[raw_name]
        lines.append("  %-24s %-6s %-6s %18s %18s" % (
            acc["name"], "资产" if acc.get("type") == "asset" else "负债", acc.get("currency"),
            ("沿用上次" if amount is None else C.money(amount)),
            (C.money(principal) if principal is not None else "—")))
    lines.append("")
    lines.append("  服务端试算合计（唯一口径来源）：")
    lines.append("    总资产 %s ／ 总负债 %s ／ 净资产 %s" % (
        C.money(totals.get("total_assets")), C.money(totals.get("total_liabilities")), C.money(totals.get("net_worth"))))
    lines.append("    将写入 %s 条（本次填写 %s · 沿用上次 %s）· 本次未填写 %s · 已跟踪未填本金 %s · 不计入净资产 %s 个账户"
                 % (counts.get("written"), counts.get("filled"), counts.get("carry"),
                    counts.get("unfilled"), counts.get("no_principal"), counts.get("excluded_from_net_worth")))
    lines.append("    （逐账户的折算值由服务端在保存时冻结，试算返回里不含逐账户折算值 —— 技能不做本地折算）")

    if preview.get("unfilled"):
        lines.append("")
        lines.append("  ⚠ 本次未填写的账户（不会写进这一期，**不等于余额为 0**）：")
        for ref in preview["unfilled"]:
            lines.append("    · %s（%s）" % (ref.get("account_name"), ref.get("platform_name")))
        lines.append("    确认要这样保存时，保存命令要额外加 --confirm-unfilled。")
    if preview.get("no_principal"):
        lines.append("  ! 已跟踪本金但本期没填本金（仍会写入，只是这一期不产出收益指标）：")
        for ref in preview["no_principal"]:
            lines.append("    · %s（%s）" % (ref.get("account_name"), ref.get("platform_name")))
    if preview.get("issues"):
        lines.append("")
        lines.append("  ✗ 保存前的门禁未通过（%d 项）—— 服务端会拒绝保存，请先改：" % len(preview["issues"]))
        for it in preview["issues"]:
            lines.append("    · [%s] %s：%s" % (it.get("code"), it.get("account_name"), it.get("message")))
    if preview.get("duplicate_date"):
        lines.append("  ! %s 这一天已经有快照了：趋势图按日期取当天**最后一张**。" % preview.get("date"))
    if rates:
        lines.append("  ! 本次带上了手工汇率（会随保存一并冻结进快照）：%s"
                     % "、".join("%s=%s" % (k, C.rate(v)) for k, v in rates.items()))
    if warnings:
        lines.append("")
        lines.append("  ⚠ 需要使用者确认的疑点：")
        for w in warnings:
            lines.append("    · %s" % w)

    plan = {"date": session_date, "note": args.note, "entries": entries}
    if rates:
        plan["rates"] = rates

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        lines.append("")
        lines.append("  计划已写出：%s（保存时：write.py snapshot-save --plan %s --confirm）" % (args.out, args.out))

    payload = {
        "ok": True,
        "view": "plan",
        "date": session_date,
        "base_currency": preview.get("base_currency"),
        "ledger": ledger.root,
        "entries": entries,
        "resolved": {k: {"id": v["id"], "name": v["name"], "currency": v.get("currency"), "type": v.get("type")}
                     for k, v in resolved.items()},
        "preview": preview,
        "warnings": warnings,
        "plan_body": plan,
    }
    C.emit(args, lines + ["", "→ 把这份清单完整念给使用者听，得到确认后再保存。"], payload)
    return C.EXIT_OK


if __name__ == "__main__":
    C.run(main)
