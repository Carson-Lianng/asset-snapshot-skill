#!/usr/bin/env python3
"""write.py —— 全部写操作。**无确认参数一律拒绝**；高风险动作要两级确认并先自动备份。

分级与禁止清单的唯一实现是 `scripts/safety.py`（说明见 `references/safety.md`）。
本脚本只负责：组装请求体 → 过分级门禁 → 需要时先备份 → 调接口 → 把服务端的原话回传。

两条刻意的约束：
  · **保存快照只接受 `--plan`**（由 plan.py 产出的计划文件），不接受随手拼的请求体 ——
    这样「先出将写入清单并展示给使用者」就是流程上绕不过去的一步。
  · 服务端的错误一律**原样透出**（按 `code` 分派，不改写规则、不自动重试）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402
import safety as S  # noqa: E402

CATALOG_EP = {
    "currency": ("currencies", "currency", "code"),
    "platform": ("platforms", "platform", "id"),
    "category": ("categories", "category", "id"),
    "tag": ("tags", "tag", "id"),
}

ACTIONS = sorted(S.ACTIONS)


# --------------------------------------------------------------------------
# 请求体组装
# --------------------------------------------------------------------------


def _coerce(value):
    try:
        return json.loads(value)
    except ValueError:
        return value


def load_body(args, *, allow_data=True):
    body = {}
    if getattr(args, "data", None) is not None and allow_data:
        raw = args.data
        if raw == "-":
            raw = sys.stdin.read()
        elif raw.startswith("@"):
            try:
                with open(raw[1:], "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as e:
                raise C.FsError("读不到 --data 指定的文件：%s（%s）" % (args.data[1:], e))
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise C.FsError("--data 不是合法 JSON：%s" % e)
        if not isinstance(parsed, dict):
            raise C.FsError("--data 必须是一个 JSON 对象")
        body.update(parsed)
    for pair in getattr(args, "set", None) or []:
        if "=" not in pair:
            raise C.FsError("--set 的写法是 键=值，例如 --set name=招行活期")
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "tags" and value and not value.startswith("["):
            body[key] = [t.strip() for t in value.split(",") if t.strip()]
        else:
            body[key] = _coerce(value)
    return body


def load_plan(path):
    if not path:
        raise C.FsError("保存快照只接受 --plan <计划文件>：先跑 plan.py 产出将写入清单并给使用者确认",
                        hint="plan.py --date YYYY-MM-DD --items 录入项.tsv --out 计划.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
    except OSError as e:
        raise C.FsError("读不到计划文件：%s（%s）" % (path, e))
    except ValueError as e:
        raise C.FsError("计划文件不是合法 JSON：%s（%s）" % (path, e))
    if not isinstance(body, dict) or not body.get("entries"):
        raise C.FsError("计划文件里没有任何录入项：%s" % path)
    # 只把服务端认识的字段送出去（计划文件可能带着人读用的附注）
    return {k: body[k] for k in ("date", "note", "entries", "rates") if k in body}


def catalog_ref(kind, args):
    """字典项的指认方式：币种用 --code，其余用 --id；也可以把 ref 放进 --data。"""
    _, _, ref_field = CATALOG_EP[kind]
    ref = args.code if ref_field == "code" else args.id
    if not ref and args.data:
        try:
            ref = (json.loads(args.data) or {}).get(ref_field)
        except (ValueError, AttributeError):
            ref = None
    if not ref:
        raise C.FsError("这个动作需要指定目标：%s"
                        % ("--code <币种代码>" if ref_field == "code" else "--id <字典项 id>"))
    return ref


def norm_kind(raw):
    key = C.CATALOG_ALIASES.get((raw or "").strip().lower())
    if key not in CATALOG_EP:
        raise C.FsError("--kind 只能是 currency / platform / category / tag（币种 / 平台 / 分类 / 标签）")
    return key


# --------------------------------------------------------------------------
# 各动作
# --------------------------------------------------------------------------


def act_account_create(ledger, args):
    body = load_body(args)
    for field in ("name", "category_id", "type", "currency"):
        if field not in body:
            raise C.FsError("新建账户缺少必填字段 %s" % field,
                            hint="必填：name / category_id / type(asset|liability) / currency；"
                                 "可选：platform_id / tags（标签 id 列表）/ note / include_in_net_worth / track_principal / archived。"
                                 "分类 id 与币种代码先看 read.py catalog all")
    return ledger.send("POST", "accounts", body=body), "已新建账户：%s（%s）" % (body.get("name"), body.get("category_id"))


def act_account_update(ledger, args):
    if not args.id:
        raise C.FsError("account-update 需要 --id <账户 id>")
    body = load_body(args)
    if not body:
        raise C.FsError("没有要改的字段：用 --set 键=值 或 --data 给一个要改的字段")
    return ledger.send("PATCH", "account", body=body, id=args.id), "已更新账户 %s" % args.id


def act_account_archive(ledger, args):
    if not args.id:
        raise C.FsError("account-archive 需要 --id <账户 id>")
    archived = not args.unarchive
    return (ledger.send("PATCH", "account", body={"archived": archived}, id=args.id),
            "%s账户 %s" % ("已归档" if archived else "已恢复", args.id))


def act_account_delete(ledger, args):
    if not args.id:
        raise C.FsError("account-delete 需要 --id <账户 id>")
    r = ledger.send("DELETE", "account", id=args.id)
    return r, "已删除账户 %s（历史快照明细保留 %s 条）" % (r.get("deleted"), r.get("snapshot_items_kept"))


def act_catalog_create(ledger, args):
    kind = norm_kind(args.kind)
    body = load_body(args)
    if "name" not in body:
        raise C.FsError("新建%s需要 name" % kind, hint="币种还需要 symbol；分类还需要 type")
    return ledger.send("POST", CATALOG_EP[kind][0], body=body), "已新建%s：%s" % (kind, body.get("name"))


def act_catalog_update(ledger, args):
    kind = norm_kind(args.kind)
    body = load_body(args)
    if not body:
        raise C.FsError("没有要改的字段")
    ref = catalog_ref(kind, args)
    return ledger.send("PATCH", CATALOG_EP[kind][1], body=body, id=ref, code=ref), "已更新%s %s" % (kind, ref)


def act_catalog_delete(ledger, args):
    kind = norm_kind(args.kind)
    ref = catalog_ref(kind, args)
    return ledger.send("DELETE", CATALOG_EP[kind][1], id=ref, code=ref), "已删除%s %s" % (kind, ref)


def act_draft_save(ledger, args):
    body = load_plan(args.plan) if args.plan else load_body(args)
    current = ledger.get("inventory_draft") or {}
    version = (current.get("draft") or {}).get("version")
    if version is None:
        version = 0
    payload = dict(body)
    payload["version"] = version
    try:
        r = ledger.send("PUT", "inventory_draft", body=payload)
    except C.ApiError as e:
        # 草稿冲突：对齐版本后重试一次（服务端用 version 做乐观锁）
        if e.code != "CONFLICT":
            raise
        fresh = (ledger.get("inventory_draft") or {}).get("draft") or {}
        payload["version"] = fresh.get("version", 0)
        r = ledger.send("PUT", "inventory_draft", body=payload)
        return r, "草稿已保存（首次写入遇到冲突，已对齐到版本 %s 后重试成功）" % payload["version"]
    return r, "草稿已保存（版本 %s）" % r.get("version")


def act_draft_discard(ledger, args):
    return ledger.send("DELETE", "inventory_draft"), "草稿已丢弃"


def act_snapshot_save(ledger, args):
    body = load_plan(args.plan)
    if args.confirm_unfilled:
        body["confirm_unfilled"] = True
    try:
        r = ledger.send("POST", "inventory_snapshots", body=body)
    except C.ApiError as e:
        details = e.details or {}
        if isinstance(details, dict) and details.get("confirm_required"):
            names = "、".join(x.get("account_name", "") for x in (details.get("unfilled") or []))
            raise S.ConfirmRequired(
                "服务端拦下了这次保存：有 %s 个账户本次「未填写」(%s)。它们不会写进这一期，"
                "**不等于余额为 0**。确认要这样保存，请加 --confirm-unfilled" % (
                    len(details.get("unfilled") or []), names),
                hint="确认后重跑：%s --confirm-unfilled" % _cmd_hint(),
            )
        raise
    snap = r.get("snapshot") or {}
    return r, ("已保存快照 %s（%s）：写入 %s 条 · 沿用上次 %s 条 · 未填写 %s 个 · 未填本金 %s 个\n"
               "  总资产 %s ／ 总负债 %s ／ 净资产 %s\n"
               "  下一步可核对：read.py snapshot %s"
               % (snap.get("date"), snap.get("id"), r.get("written"), r.get("carried_over"),
                  r.get("unfilled"), r.get("no_principal"), C.money(snap.get("total_assets")),
                  C.money(snap.get("total_liabilities")), C.money(snap.get("net_worth")), snap.get("id")))


def act_backup_now(ledger, args):
    r = ledger.send("POST", "backup")
    return r, "已备份：%s（%s 字节 · 本次清理旧备份 %s 份）" % (r.get("name"), r.get("size"), r.get("pruned"))


def act_snapshot_delete(ledger, args):
    if not args.id:
        raise C.FsError("snapshot-delete 需要 --id <快照 id>")
    r = ledger.send("DELETE", "snapshot", id=args.id)
    return r, "已删除快照 %s（含明细与冻结汇率，不可撤销）" % r.get("deleted")


def act_base_currency_change(ledger, args):
    if not args.to:
        raise C.FsError("base-currency-change 需要 --to <新本位币代码>")
    if args.conversion_rate is None:
        raise C.FsError("base-currency-change 需要 --conversion-rate（1 旧本位币 = X 新本位币）",
                        hint="这个比价是整表反算汇率的基准，不能由脚本猜")
    body = {"to": args.to.strip().upper(), "conversion_rate": C.yuan_to_micro(args.conversion_rate)}
    override = parse_code_values(args.rate_override or [])
    if override:
        body["rates"] = override
    r = ledger.send("POST", "base_currency", body=body)
    return r, ("已变更本位币 %s → %s（换算比价 %s）· 重算 %s 个币种的汇率 · 变更历史 %s"
               % (r.get("from"), r.get("to"), C.rate(r.get("conversion_rate")), r.get("recalculated"), r.get("history_id")))


def act_rates_set(ledger, args):
    body = load_body(args)
    if not body.get("rates"):
        pairs = parse_code_values(args.set_rate or [])
        if not pairs:
            raise C.FsError("rates-set 需要 --set-rate 币种=汇率（可重复），或用 --data 给 {\"rates\": {...}}",
                            hint="方向：1 单位原币 = X 本位币；本位币自身恒为 1，改它会被服务端拒绝")
        body = {"rates": pairs}
    r = ledger.send("PUT", "rates", body=body)
    return r, "已更新 %s 个币种的汇率（本位币 %s）" % (r.get("updated"), r.get("base_currency"))


def act_import_apply(ledger, args):
    if not args.file:
        raise C.FsError("import-apply 需要 --file <csv 文件>",
                        hint="先用 read.py import-preview --file <csv> 看清差异四态，再决定是否应用")
    try:
        with open(args.file, "r", encoding="utf-8-sig", errors="replace") as fh:
            content = fh.read()
    except OSError as e:
        raise C.FsError("读不到文件：%s（%s）" % (args.file, e))
    r = ledger.send("POST", "import_apply",
                    body={"filename": os.path.basename(args.file), "content": content})
    return r, "导入完成：新增 %s · 更新 %s · 跳过 %s（余额列不落库）" % (
        r.get("inserted"), r.get("updated"), r.get("skipped"))


def act_restore(ledger, args):
    if args.backup_name:
        body = {"backup_name": args.backup_name}
    elif args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except OSError as e:
            raise C.FsError("读不到备份文件：%s（%s）" % (args.file, e))
        except ValueError as e:
            raise C.FsError("备份文件不是合法 JSON：%s" % e)
        body = {"document": doc}
    else:
        raise C.FsError("restore 需要 --file <导出的 JSON 备份> 或 --backup-name <账本备份文件名>")
    r = ledger.send("POST", "restore", body=body)
    restored = r.get("restored") or {}
    return r, ("已从备份恢复（数据格式 v%s）：账户 %s · 快照 %s · 明细 %s\n"
               "  恢复前自动留下的备份：%s（null 表示那一步没成功，恢复仍已执行）"
               % (r.get("schema_version"), restored.get("accounts"), restored.get("snapshots"),
                  restored.get("snapshot_items"), r.get("pre_restore_backup")))


HANDLERS = {
    "account-create": act_account_create,
    "account-update": act_account_update,
    "account-archive": act_account_archive,
    "account-delete": act_account_delete,
    "catalog-create": act_catalog_create,
    "catalog-update": act_catalog_update,
    "catalog-delete": act_catalog_delete,
    "draft-save": act_draft_save,
    "draft-discard": act_draft_discard,
    "snapshot-save": act_snapshot_save,
    "snapshot-delete": act_snapshot_delete,
    "backup-now": act_backup_now,
    "base-currency-change": act_base_currency_change,
    "rates-set": act_rates_set,
    "import-apply": act_import_apply,
    "restore": act_restore,
}


def parse_code_values(pairs):
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise C.FsError("写法是 币种=数值，例如 HKD=0.93")
        code, _, value = pair.partition("=")
        out[code.strip().upper()] = C.yuan_to_micro(value.strip())
    return out


def _cmd_hint():
    return "python3 %s %s" % (os.path.basename(sys.argv[0]), " ".join(sys.argv[1:]))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(description="家底快照 · 写操作（无确认一律拒绝）")
    p.add_argument("action", choices=ACTIONS, help="动作名；分级见 safety.py list")
    p.add_argument("--id", help="目标 id（账户 / 快照 / 字典项）")
    p.add_argument("--kind", help="字典类别：currency / platform / category / tag")
    p.add_argument("--code", help="币种代码（字典项是币种时用它指认）")
    p.add_argument("--data", help="请求体 JSON（可直接给，或用 @文件.json，或 - 从标准输入读）")
    p.add_argument("--set", action="append", help="字段赋值，可重复：--set name=招行活期")
    p.add_argument("--plan", help="plan.py 产出的计划文件（保存快照/草稿用它）")
    p.add_argument("--file", help="CSV（导入）或 JSON 备份（恢复）文件")
    p.add_argument("--backup-name", dest="backup_name", help="账本 backups 目录里的备份文件名")
    p.add_argument("--to", help="变更本位币的目标币种代码")
    p.add_argument("--conversion-rate", dest="conversion_rate", help="1 旧本位币 = X 新本位币")
    p.add_argument("--rate-override", action="append", help="变更本位币时逐条修正的汇率，可重复：--rate-override HKD=0.93")
    p.add_argument("--set-rate", action="append", help="整表更新汇率，可重复：--set-rate USD=7.28")
    p.add_argument("--unarchive", action="store_true", help="account-archive 反过来：恢复账户")
    p.add_argument("--confirm", action="store_true", help="一次确认（可逆写必需）")
    p.add_argument("--confirm-2", dest="confirm_2", action="store_true", help="二级确认（不可逆写必需）")
    p.add_argument("--confirm-unfilled", dest="confirm_unfilled", action="store_true",
                   help="确认「本次未填写」的账户清单（服务端拦下时才会用到）")
    C.add_common_args(p)
    return p


def main() -> int:
    missing = [a for a in ACTIONS if a not in HANDLERS]
    if missing:
        raise C.FsError("safety.py 登记了动作但没有实现：%s" % "、".join(missing))
    args = build_parser().parse_args()
    cmd_hint = _cmd_hint()

    # 分级门禁（不满足就在这里终止，**连账本都不去连**）
    spec = S.gate(args.action, args.confirm, args.confirm_2, cmd_hint)

    ledger = C.resolve_ledger(args.ledger, args.base_url)

    lines = ["动作：%s（%s）· 接口 %s" % (spec["label"], spec["grade_label"], spec["api"])]
    pre_backup = None
    if S.needs_prebackup(args.action):
        try:
            pre_backup = ledger.send("POST", "backup")
        except C.ApiError as e:
            raise C.FsError(
                "动作为不可逆档，但动作前的自动备份失败了，已拒绝执行（把不可逆变可回退是这一档的前提）：%s" % e.render())
        lines.append("  动作前自动备份：%s" % pre_backup.get("name"))

    result, message = HANDLERS[args.action](ledger, args)
    lines.append("  " + message)
    lines.append("  目标账本：%s" % ledger.root)

    payload = {
        "ok": True,
        "action": args.action,
        "grade": spec["grade"],
        "ledger": ledger.root,
        "base_url": ledger.base_url,
        "pre_backup": pre_backup.get("name") if pre_backup else None,
        "result": result,
    }
    C.emit(args, lines, payload)
    return C.EXIT_OK


if __name__ == "__main__":
    C.run(main)
