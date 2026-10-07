#!/usr/bin/env python3
"""help.py —— 使用答疑：从账本包内的使用手册抽取正文。

## 知识源只有一个

系统使用手册（界面 `#help`）的正文就是**唯一**知识源，本技能**不另写 FAQ** ——
否则立刻出现两份口径，手册改版后那份就成了「过期却看似权威」的文本。

## 抽取只依赖结构契约

按手册的稳定结构定位取文，不做正则猜格式：

1. `outline.ts` 的 `HELP_OUTLINE` 给出章节 id / 序号 / 标题 / 一句话（数据字面量，形状固定）；
2. `content.tsx` 的 `BODIES` 给出「章节 id → 正文常量」的绑定；
3. 每个正文常量是一段固定写法的 JSX（`const X: ReactNode = ( … );`），列表型章节由
   `Xxx.map(...)` 渲染一个固定形状的数据数组（元组数组 或 含 q/a 的对象数组）。

抽取失败时**显式降级**：说清原因并给出路（升级包版本 / 打开界面手册对应章节），
不猜、不编、不拿别处的文本顶替。

用法：
  help.py list                  列出章节索引（id / 序号 / 标题 / 一句话）
  help.py <章节id>              打印该章正文
  help.py <关键词>              在全部章节里搜关键词，给命中的章节与所在行
  help.py --source              只报告手册来源与可用性（doctor 也用它）
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402


# --------------------------------------------------------------------------
# 词法小工具（只在字符串外计数，避免中文正文里的括号干扰）
# --------------------------------------------------------------------------

QUOTES = "'\"`"


def _skip_string(text: str, i: int) -> int:
    quote = text[i]
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return i


def _scan_balanced(text: str, start: int) -> int:
    """从 text[start]（`(` / `[` / `{`）扫到配对的结束位置，返回其下标；找不到返回 -1。"""
    pairs = {"(": ")", "[": "]", "{": "}"}
    open_ch = text[start]
    close_ch = pairs[open_ch]
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in QUOTES:
            i = _skip_string(text, i)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _string_at(text: str, i: int):
    if i >= len(text) or text[i] not in QUOTES:
        return None
    end = _skip_string(text, i)
    return text[i + 1:end - 1]


def const_value(text: str, name: str):
    """取 `const NAME … = <值>;` 的值文本。

    值以 `(` 包起来时返回括号**内部**；以 `[` 开头时连括号一起返回；都找不到返回 None。
    """
    m = re.search(r"\bconst\s+%s\b" % re.escape(name), text)
    if not m:
        return None
    eq = text.find("=", m.end())
    if eq < 0:
        return None
    i = eq + 1
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        return None
    if text[i] == "(":
        end = _scan_balanced(text, i)
        return None if end < 0 else text[i + 1:end]
    if text[i] == "[":
        end = _scan_balanced(text, i)
        return None if end < 0 else text[i:end + 1]
    return None


# --------------------------------------------------------------------------
# 解析手册
# --------------------------------------------------------------------------


OUTLINE_ITEM_RE = re.compile(
    r"\{\s*id:\s*'([^']+)'\s*,\s*no:\s*'([^']*)'\s*,\s*title:\s*'([^']*)'\s*,\s*summary:\s*'([^']*)'\s*\}"
)


def parse_outline(text: str):
    """`HELP_OUTLINE` 是一个形状固定的数据字面量数组，按这个形状读它。"""
    m = re.search(r"HELP_OUTLINE[^=]*=\s*\[", text)
    if not m:
        return None
    start = m.end() - 1  # 正则末尾就是那个 '[' —— 别用 index() 往前找，类型注解里也有 []
    end = _scan_balanced(text, start)
    if end < 0:
        return None
    body = text[start:end + 1]
    chapters = [
        {"id": g[0], "no": g[1], "title": g[2], "summary": g[3]}
        for g in OUTLINE_ITEM_RE.findall(body)
    ]
    return chapters or None


def parse_bodies(text: str):
    """`BODIES` 给出「章节 id → 正文常量名」的绑定 —— 这是正文定位的结构契约。"""
    m = re.search(r"BODIES\s*:\s*Record<string,\s*ReactNode>\s*=\s*\{", text)
    if not m:
        return None
    start = m.end() - 1  # 正则末尾就是那个 '{'
    end = _scan_balanced(text, start)
    if end < 0:
        return None
    body = text[start + 1:end]
    pairs = {}
    # 最后一项可能没有尾逗号（实测踩过：`glossary: APPENDIX` 后面直接是 `}`）
    for key, value in re.findall(r"([A-Za-z_$][\w$]*)\s*:\s*([A-Za-z_$][\w$]*)\s*,?", body):
        pairs[key] = value
    return pairs or None


BLOCK_RULES = [
    (r"<h3[^>]*>", "\n### "),
    (r"</h3>", "\n"),
    (r"<li[^>]*>", "\n- "),
    (r"</li>", ""),
    (r"<dt[^>]*>", "\n"),
    (r"</dt>", "："),
    (r"<dd[^>]*>", ""),
    (r"<summary[^>]*>", "\n■ "),
    (r"</summary>", "\n"),
    (r"<br\s*/?>", "\n"),
    (r"<p[^>]*>", "\n"),
    (r"</p>", "\n"),
    (r"<td[^>]*>", "| "),
    (r"</td>", " "),
    (r"</tr>", "\n"),
]

INLINE_TAGS = ("table", "tbody", "thead", "tr", "div", "section", "details", "dl", "ul", "ol",
               "span", "b", "i", "em", "strong", "code", "pre", "a", "small", "Note")


def strip_jsx(raw: str) -> str:
    text = re.sub(r"\{/\*.*?\*/\}", "", raw, flags=re.S)
    # JSX 片段：`<>` / `</>` 没有标签名，上面的通用标签规则（要求标签名至少一个字符）碰不到它们
    text = text.replace("<>", "").replace("</>", "")
    text = re.sub(r"\{\s*'([^']*)'\s*\}", r"\1", text)
    text = re.sub(r'\{\s*"([^"]*)"\s*\}', r"\1", text)
    text = re.sub(r"\{[^{}]*\}", " ", text)  # 其余表达式（含占位符）先保留，占位符不含花括号
    for pattern, repl in BLOCK_RULES:
        text = re.sub(pattern, repl, text)
    text = re.sub(r"</?(?:%s)[^>]*>" % "|".join(INLINE_TAGS), "", text)
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "),
                 ("&quot;", '"'), ("&#39;", "'"), ("&hellip;", "…")):
        text = text.replace(a, b)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _split_top_items(body: str):
    """把一个 `[ … ]` 字面量拆成顶层元素文本（字符串与嵌套结构都跳过）。"""
    inner = body[1:-1]
    items = []
    depth = 0
    buf = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch in QUOTES:
            end = _skip_string(inner, i)
            buf.append(inner[i:end])
            i = end
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        items.append("".join(buf))
    return [x.strip() for x in items if x.strip()]


def _all_strings(text: str):
    out = []
    i = 0
    while i < len(text):
        if text[i] in QUOTES:
            s = _string_at(text, i)
            if s is not None:
                out.append(s)
            i = _skip_string(text, i)
            continue
        i += 1
    return out


def render_array(content_text: str, name: str):
    """渲染一个数据数组：元组数组 → `- 名：说明`；对象数组（含 q/a）→ `■ 问` + 答。"""
    value = const_value(content_text, name)
    if not value or not value.startswith("["):
        return None
    lines = []
    for item in _split_top_items(value):
        q = re.search(r"(?<![\w$])q\s*:\s*(['\"])", item)
        if q:
            question = _string_at(item, q.end() - 1) or ""
            a_m = re.search(r"(?<![\w$])a\s*:", item)
            answer = ""
            if a_m:
                answer = item[a_m.end():].strip()
                # 去掉包着答案的 ( <> … </> ) } 这层壳，再去标签
                answer = answer.lstrip("({[ ").rstrip(")}], \n\t")
            answer = strip_jsx(answer)
            lines.append("■ %s" % question)
            if answer:
                lines.append("  " + answer.replace("\n", "\n  "))
            continue
        strings = _all_strings(item)
        if len(strings) >= 2:
            lines.append("- %s：%s" % (strings[0], strings[1]))
        elif strings:
            lines.append("- %s" % strings[0])
    return "\n".join(lines) if lines else None


LIST_CALL_RE = re.compile(r"\{([A-Za-z_$][\w$]*)\.map\(")


def jsx_to_text(raw: str, content_text: str):
    """JSX 正文 → 纯文本。列表型段落由 `.map` 的数据源渲染，其余按标签规则取文。"""
    placeholders = []
    out = []
    i = 0
    while True:
        m = LIST_CALL_RE.search(raw, i)
        if not m:
            out.append(raw[i:])
            break
        out.append(raw[i:m.start()])
        paren = raw.index("(", m.end() - 1)
        end = _scan_balanced(raw, paren)
        if end < 0:
            out.append(raw[m.start():])
            break
        token = "@@LIST_%d@@" % len(placeholders)
        placeholders.append((token, m.group(1)))
        out.append("\n%s\n" % token)
        i = end + 1
        while i < len(raw) and raw[i] in " )\n\t}":
            if raw[i] == "}":
                i += 1
                break
            i += 1
    text = strip_jsx("".join(out))
    for token, name in placeholders:
        rendered = render_array(content_text, name)
        if rendered is None:
            rendered = "（这一段是列表，但抽取器没在手册里找到它的数据源 %s —— 请打开界面手册核对这一节）" % name
        text = text.replace(token, rendered)
    return text.strip()


# --------------------------------------------------------------------------
# 载入
# --------------------------------------------------------------------------


def load_manual(ledger):
    src = C.find_help_source(ledger.root)
    if not src:
        raise C.ManualUnavailable(
            "这个账本包里没有使用手册源码（%s）—— 答疑需要它，**不猜也不编**。" % C.HELP_SOURCE_REL,
            hint="升级到含手册的账本包版本（打包流程应保证手册在包内）；"
                 "或让使用者直接打开界面「帮助」页对应章节（#help）看",
        )
    outline_text = src["outline"].read_text(encoding="utf-8", errors="replace")
    content_text = src["content"].read_text(encoding="utf-8", errors="replace")
    chapters = parse_outline(outline_text)
    if not chapters:
        raise C.ManualUnavailable(
            "手册骨架读不出来（%s 里的 HELP_OUTLINE 形状变了）" % src["outline"].name,
            hint="这是抽取器的结构契约失效，需要同步更新 help.py；当前请改用界面手册",
        )
    bodies = parse_bodies(content_text)
    if not bodies:
        raise C.ManualUnavailable(
            "手册正文绑定读不出来（%s 里的 BODIES 形状变了）" % src["content"].name,
            hint="这是抽取器的结构契约失效，需要同步更新 help.py；当前请改用界面手册",
        )
    texts = {}
    for ch in chapters:
        const_name = bodies.get(ch["id"])
        if not const_name:
            texts[ch["id"]] = None
            continue
        value = const_value(content_text, const_name)
        if value is None:
            texts[ch["id"]] = None
            continue
        if value.startswith("["):
            texts[ch["id"]] = render_array(content_text, const_name)
        else:
            texts[ch["id"]] = jsx_to_text(value, content_text)
    return src, chapters, texts


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="使用答疑：抽取账本包内的使用手册正文")
    p.add_argument("query", nargs="?", help="章节 id，或要在手册里搜的关键词")
    p.add_argument("--source", action="store_true", help="只报告手册来源与可用性")
    C.add_common_args(p)
    args = p.parse_args()

    if args.source:
        ledger = C.resolve_ledger(args.ledger, args.base_url, need_service=False)
        src = C.find_help_source(ledger.root)
        if src:
            chapters = parse_outline(src["outline"].read_text(encoding="utf-8", errors="replace")) or []
            lines = ["手册可用（%s）· 共 %d 章" % (src["source"], len(chapters))]
            payload = {"ok": True, "available": True, "dir": str(src["dir"]),
                       "source": src["source"], "chapters": len(chapters)}
        else:
            lines = ["手册**不可用**：账本包内没有 %s" % C.HELP_SOURCE_REL]
            payload = {"ok": False, "available": False, "dir": None}
        C.emit(args, lines, payload)
        return C.EXIT_OK

    if not args.query:
        raise C.FsError("要给一个章节 id 或关键词；看目录用 help.py list")

    ledger = C.resolve_ledger(args.ledger, args.base_url, need_service=False)
    src, chapters, texts = load_manual(ledger)

    if args.query == "list":
        lines = ["使用手册 · %d 章（来源：%s）" % (len(chapters), src["source"])]
        for ch in chapters:
            ok = "✓" if texts.get(ch["id"]) else "✗"
            lines.append("  %s %-10s %-8s %s —— %s" % (ok, ch["id"], ch["no"], ch["title"], ch["summary"]))
        lines.append("")
        lines.append("  ✗ 表示这一章没抽到正文（结构变了的那种），要人工核对 help.py")
        C.emit(args, lines, {
            "ok": True,
            "source": src["source"],
            "chapters": [{**ch, "extracted": bool(texts.get(ch["id"]))} for ch in chapters],
        })
        return C.EXIT_OK

    hit = next((ch for ch in chapters if ch["id"] == args.query), None)
    if hit:
        text = texts.get(hit["id"])
        if not text:
            raise C.ManualUnavailable(
                "第 %s 章「%s」没抽到正文（手册结构变了）" % (hit["no"], hit["title"]),
                hint="需要同步更新 help.py 的抽取规则；当前请打开界面 #help?sec=%s 看这一章" % hit["id"],
            )
        lines = ["## %s. %s —— %s" % (hit["no"], hit["title"], hit["summary"]), "", text]
        lines.append("")
        lines.append("（来源：账本包内使用手册 %s；正文是对当前界面的承诺，界面改了会与手册同批改）" % src["source"])
        C.emit(args, lines, {"ok": True, "chapter": hit, "text": text, "source": src["source"]})
        return C.EXIT_OK

    needle = args.query.lower()
    matched = []
    for ch in chapters:
        text = texts.get(ch["id"]) or ""
        if needle in text.lower():
            hits = [ln.strip() for ln in text.splitlines() if needle in ln.lower()][:4]
            matched.append({"chapter": ch, "lines": hits})

    if not matched:
        C.emit(args, [
            "手册里没有提到「%s」。" % args.query,
            "  这是实话：手册就是唯一知识源，技能不另外编答案。可以再看这几章：" +
            "、".join("%s(%s)" % (c["title"], c["id"]) for c in chapters),
        ], {"ok": True, "found": False, "query": args.query, "chapters": [c["id"] for c in chapters]})
        return C.EXIT_OK

    lines = ["手册里「%s」出现在 %d 章：" % (args.query, len(matched))]
    for m in matched:
        lines.append("  【%s】%s —— %s" % (m["chapter"]["id"], m["chapter"]["title"], m["chapter"]["summary"]))
        for ln in m["lines"]:
            lines.append("      … %s" % ln)
    lines.append("")
    lines.append("看整章：help.py %s" % matched[0]["chapter"]["id"])
    C.emit(args, lines, {"ok": True, "found": True, "query": args.query, "matches": matched})
    return C.EXIT_OK


if __name__ == "__main__":
    C.run(main)
