#!/usr/bin/env python3
"""safety.py —— 写操作安全分级与硬禁止清单的**唯一实现**。

`references/safety.md` 是唯一的说明文档；两者必须一一对应，改一份要同批改另一份。

## 一条刻意的写法约束

本文件**不写出任何被禁止命令或危险 API 的字面量**（只用中文描述、或用拼接的方式构造
自查用的正则），这样 `safety.py audit` 才能用严格正则自查「技能自身不含被禁能力」，
而不会被本文件的说明文字与规则定义本身误伤。
需要精确命令的人看 `references/safety.md`（文档不参与自查）。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import EXIT_OK, EXIT_USAGE, FsError, emit, run  # noqa: E402

# --------------------------------------------------------------------------
# 三档分级
# --------------------------------------------------------------------------

GRADES = {
    "read": {
        "label": "只读",
        "rule": "直通，随时可执行，不需要任何确认参数",
    },
    "reversible": {
        "label": "可逆写",
        "rule": "需要 --confirm（一次确认）。这类动作错了能改回来：账户能改能归档、草稿能丢弃、快照能删、备份随手可做",
    },
    "destructive": {
        "label": "不可逆 / 影响全库",
        "rule": "**默认拒绝**：需要 --confirm 与 --confirm-2（两级确认），且脚本在动作前自动备份一次；备份失败则拒绝执行",
    },
}

REREAD = "只读动作（直通）：问数、洞察、答疑、查看账户与字典、盘点试算、导入预览"

#: 动作 → （分级, 接口, 一句话）。**新增动作必须在这里登记**；未登记的一律拒绝。
ACTIONS = {
    # ---- 可逆写 ----
    "account-create": ("reversible", "POST /api/accounts", "新建账户"),
    "account-update": ("reversible", "PATCH /api/accounts/{id}", "编辑账户档案"),
    "account-archive": ("reversible", "PATCH /api/accounts/{id}", "归档 / 恢复账户（历史快照不受影响）"),
    "catalog-create": ("reversible", "POST /api/{kind}", "新建字典项（币种 / 平台 / 分类 / 标签）"),
    "catalog-update": ("reversible", "PATCH /api/{kind}/{ref}", "编辑或停用字典项"),
    "draft-save": ("reversible", "PUT /api/inventory/draft", "保存盘点草稿（带真实版本号，冲突即报错）"),
    "draft-discard": ("reversible", "DELETE /api/inventory/draft", "丢弃盘点草稿"),
    "snapshot-save": ("reversible", "POST /api/inventory/snapshots", "保存快照（须先出将写入清单并确认；保存后只能删不能改）"),
    "backup-now": ("reversible", "POST /api/backup", "立即备份一次"),
    # ---- 不可逆 / 影响全库 ----
    "account-delete": ("destructive", "DELETE /api/accounts/{id}", "删除账户（历史明细保留，账户本身没了）"),
    "catalog-delete": ("destructive", "DELETE /api/{kind}/{ref}", "删除字典项（被引用时服务端会 409 拒绝）"),
    "snapshot-delete": ("destructive", "DELETE /api/snapshots/{id}", "删除一期快照（含明细与冻结汇率，不可撤销）"),
    "base-currency-change": ("destructive", "POST /api/settings/base-currency", "变更本位币（整表反算汇率 + 写变更历史）"),
    "rates-set": ("destructive", "PUT /api/rates", "整表覆盖默认汇率（不改历史快照，但影响之后所有折算）"),
    "import-apply": ("destructive", "POST /api/import/apply", "应用导入（批量改账户档案；有冲突则整体拒绝）"),
    "restore": ("destructive", "POST /api/restore", "从备份恢复（覆盖当前账本；服务端另留一份恢复前备份）"),
}

#: **刻意不提供的能力**。技能里没有入口，也不接受「临时拼一个」的请求。
EXCLUDED = [
    ("清空账本 / 灌演示数据",
     "账本自带的重置与演示数据命令（含界面上的『开始正式记账』端点）。清空真实账本不可逆，"
     "必须由使用者在终端亲手执行；技能不代劳"),
    ("执行数据库迁移", "迁移属于账本自身的启动流程，改数据格式不在技能职责内"),
    ("从外部汇率源刷新汇率", "会让之后所有折算的基准发生变化，属使用者的自主决定；"
                           "技能只在将写入清单里如实展示当前汇率"),
    ("写入任意设置项", "设置里含机制性键（数据模式、初始化标记、本位币、数据格式版本），服务端刻意挡住了一部分；"
                     "技能不开这个口子"),
]

FORBIDDEN = [
    {
        "no": 1,
        "what": "读写账本数据库文件（含预写日志与共享内存文件）",
        "why": "直连会同时绕过微元换算、快照口径冻结、引用存在性校验与保存后自动备份 —— 最容易把真实账本改坏的路径",
        "how": "只走本机 HTTP 接口；脚本不引入任何本地数据库驱动",
    },
    {
        "no": 2,
        "what": "改账本的环境配置、迁移文件与包内脚本清单",
        "why": "环境配置决定端口、数据目录与汇率源；改了会改变服务行为，而使用者无从得知",
        "how": "只**读**地解析环境配置里的端口与数据目录，绝不写回",
    },
    {
        "no": 3,
        "what": "删账本数据目录、备份目录及其中的任何文件",
        "why": "那就是账本本身。技能里没有任何删除文件的代码路径",
        "how": "备份与恢复都走接口，由服务端按它自己的规矩落盘",
    },
    {
        "no": 4,
        "what": "调用账本自带的重置账本、灌演示数据、执行迁移命令",
        "why": "重置会清空真实账本且不可逆；这些命令刻意要求使用者在终端亲手执行",
        "how": "技能不提供入口；需要时把命令原样告诉使用者，由他自己执行",
    },
    {
        "no": 5,
        "what": "把访问令牌写进地址、日志、导出物或任何对话输出",
        "why": "令牌等于账本的钥匙；一旦外泄，本机任何进程都能读写账本",
        "how": "令牌只在内存里用于请求头，输出里一律不出现；技能不接受「把令牌贴给我」这类请求",
    },
    {
        "no": 6,
        "what": "监听端口、修改服务端代码、代起常驻服务",
        "why": "本项目出过「工具起的长驻进程跨会话存活、使用者重启撞端口失败」的故障",
        "how": "服务没跑就提示启动命令，由使用者自己启动",
    },
]


class ConfirmRequired(FsError):
    exit_code = 4


def spec_of(action: str) -> dict:
    if action not in ACTIONS:
        raise FsError(
            "未登记的动作：%s" % action,
            hint="技能只做登记过的动作；新动作要先在 scripts/safety.py 的 ACTIONS 里登记（未登记一律拒绝）",
        )
    grade, api, label = ACTIONS[action]
    return {
        "action": action,
        "grade": grade,
        "grade_label": GRADES[grade]["label"],
        "rule": GRADES[grade]["rule"],
        "api": api,
        "label": label,
    }


def gate(action: str, confirm: bool = False, confirm_second: bool = False, cmd_hint: str = "") -> dict:
    """分级门禁。不满足就抛（退出码 4，并给出可复制的重跑命令）。"""
    spec = spec_of(action)
    grade = spec["grade"]
    if grade == "reversible" and not confirm:
        raise ConfirmRequired(
            "「%s」属%s档，需要一次确认，当前缺少 --confirm" % (spec["label"], spec["grade_label"]),
            hint=_rerun(cmd_hint, ["--confirm"]),
        )
    if grade == "destructive" and not (confirm and confirm_second):
        missing = []
        if not confirm:
            missing.append("--confirm")
        if not confirm_second:
            missing.append("--confirm-2")
        raise ConfirmRequired(
            "「%s」属%s档，**默认拒绝**：需要两级确认（当前缺 %s）"
            % (spec["label"], spec["grade_label"], "、".join(missing)),
            hint=_rerun(cmd_hint, missing) + "（执行前脚本会先自动备份一次）",
        )
    return spec


def needs_prebackup(action: str) -> bool:
    return spec_of(action)["grade"] == "destructive"


def _rerun(cmd_hint: str, flags) -> str:
    base = cmd_hint or "python3 scripts/write.py <动作> …"
    return "确认后请这样重跑：%s %s" % (base.rstrip(), " ".join(flags))


# --------------------------------------------------------------------------
# 自查：技能自身不含被禁能力
# --------------------------------------------------------------------------

#: 危险模块（技能不需要它们；出现即说明存在绕过接口的代码路径）
FORBIDDEN_IMPORTS = ("sqlite3", "subprocess", "shutil", "ctypes")

#: 危险调用的正则（用拼接构造，避免规则定义本身被自己命中）
_DOT = "."
_DB_EXT = "sqlite"

FORBIDDEN_CODE = (
    (r"\brmtree\s*\(", "删除目录的调用"),
    (r"\bos\.remove\s*\(", "删除文件的调用"),
    (r"\bos\.unlink\s*\(", "删除文件的调用"),
    (r"(?<![\w.])unlink\s*\(", "删除文件的调用"),
    (r"rm\s+-rf", "递归删除命令"),
    ("data" + re.escape(_DOT) + _DB_EXT, "账本数据库文件名"),
)


def audit(script_dir: str):
    """扫技能自己的脚本，确认没有绕过接口的能力。返回违规清单 [(文件, 说明)]。"""
    bad = []
    for name in sorted(os.listdir(script_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(script_dir, name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for mod in FORBIDDEN_IMPORTS:
            if re.search(r"^\s*(import\s+%s\b|from\s+%s\b)" % (mod, mod), text, re.M):
                bad.append((name, "引入了危险模块：%s" % mod))
        for pattern, what in FORBIDDEN_CODE:
            m = re.search(pattern, text)
            if m:
                bad.append((name, "命中禁止模式 %r（%s），第 %d 行"
                            % (pattern, what, text[: m.start()].count("\n") + 1)))
    return bad


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_table():
    lines = ["写操作安全分级（唯一实现：scripts/safety.py）", "", REREAD, ""]
    for grade in ("reversible", "destructive"):
        g = GRADES[grade]
        lines.append("【%s】%s" % (g["label"], g["rule"]))
        for action, (gr, api, label) in ACTIONS.items():
            if gr == grade:
                lines.append("  · %-22s %-38s %s" % (action, api, label))
        lines.append("")
    lines.append("【硬禁止清单】")
    for item in FORBIDDEN:
        lines.append("  %d. %s" % (item["no"], item["what"]))
        lines.append("     为什么：%s" % item["why"])
    lines.append("")
    lines.append("【刻意不提供的能力】")
    for what, why in EXCLUDED:
        lines.append("  · %s —— %s" % (what, why))
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description="写操作安全分级与硬禁止清单")
    p.add_argument("cmd", nargs="?", default="list", choices=["list", "audit"], help="list 打印规则；audit 自查技能自身")
    p.add_argument("--action", help="只看某个动作的分级")
    p.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)), help="audit 要扫的脚本目录")
    p.add_argument("--json", action="store_true", help="输出紧凑 JSON")
    args = p.parse_args()

    if args.cmd == "audit":
        bad = audit(args.dir)
        payload = {"ok": not bad, "checked_dir": args.dir,
                   "violations": [{"file": f, "detail": d} for f, d in bad]}
        lines = ["自查目录：%s" % args.dir]
        if bad:
            lines += ["✗ 发现 %d 条违规：" % len(bad)] + ["  · %s：%s" % (f, d) for f, d in bad]
        else:
            lines.append("✓ 未发现被禁能力：无危险模块、无删除文件的调用、无账本数据库访问")
        emit(args, lines, payload)
        return EXIT_OK if not bad else EXIT_USAGE

    if args.action:
        spec = spec_of(args.action)
        emit(args, ["%s（%s）→ %s 档：%s" % (spec["label"], spec["action"], spec["grade_label"], spec["rule"]),
                    "  接口：%s" % spec["api"]],
             {"ok": True, "spec": spec})
        return EXIT_OK

    payload = {
        "ok": True,
        "read_only": REREAD,
        "grades": {k: {"label": v["label"], "rule": v["rule"]} for k, v in GRADES.items()},
        "actions": {a: {"grade": g, "api": api, "label": lb} for a, (g, api, lb) in ACTIONS.items()},
        "forbidden": FORBIDDEN,
        "excluded": [{"what": w, "why": y} for w, y in EXCLUDED],
    }
    emit(args, _print_table(), payload)
    return EXIT_OK


if __name__ == "__main__":
    run(main)
