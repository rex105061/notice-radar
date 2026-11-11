#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D8：导出人工标注表。

从「已分析」的公告里按类别分层抽样，生成一份 Markdown 标注表。
作者在这张表上做三个判断，用于算真实准确率。

分层逻辑：每个类别按自动分排序后取「最高 / 中间 / 最低」，保证样本
覆盖不同质量水平——只抽高分的那叫自证，不叫评测。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DB_PATH  # noqa: E402

OUT_PATH = Path(__file__).resolve().parent.parent / "eval" / "标注表_D8_20261023.md"

# 类别 → 取几条。优先选「公司公告」，不选会议/制度类。
CATEGORIES = [
    ("利润分配", 4),
    ("重大合同与订单", 4),
    ("股权变动", 4),
    ("并购重组", 3),
    ("风险事项", 3),
    ("业绩与定期报告", 3),
]


def clean(text: str, limit: int = 700) -> str:
    text = re.sub(r"[ \t\u3000]+", " ", (text or "").strip())
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:limit]


def bullets(items, max_n=3) -> str:
    items = items or []
    if not items:
        return "（无）"
    return "；".join(f"{i}.{x}" for i, x in enumerate(items[:max_n], 1))


def pick_rows(conn) -> list:
    picked, seen = [], set()
    for cat, k in CATEGORIES:
        rows = conn.execute(
            "SELECT n.*, a.extracted_json, a.interpreted_json, a.score_total "
            "FROM notices n JOIN analysis a ON a.announcement_id = n.announcement_id "
            "WHERE n.category = ? AND n.content_text IS NOT NULL "
            "AND n.content_text != '' ORDER BY a.score_total DESC", (cat,)
        ).fetchall()
        if not rows:
            continue
        n = len(rows)
        idx = [0, n // 2, n - 1] if n >= 3 else list(range(n))
        idx += [i for i in range(n) if i not in idx]
        for i in idx[:k]:
            row = dict(rows[i])
            if row["announcement_id"] in seen:
                continue
            seen.add(row["announcement_id"])
            picked.append(row)
    return picked


def render(rows: list) -> str:
    out = [
        "# D8 人工标注表",
        "",
        "> 共 %d 条。" % len(rows),
        "> **操作**：每条先读「原文节选」，自己想 30 秒这条公告意味着什么；",
        "> 再看「AI 输出」；然后把「你的判断」里对应的 `[ ]` 改成 `[x]`。",
        "> 三条都标完，保存文件，告诉我一声。",
        "",
        "---",
        "",
    ]
    for i, r in enumerate(rows, 1):
        interp = json.loads(r["interpreted_json"] or "{}")
        ex = json.loads(r["extracted_json"] or "{}")
        flags = interp.get("compliance_flags") or []
        if flags:
            flag_txt = "；".join(
                f"[{f.get('risk_level','?')}]「{f.get('quote','')}」{f.get('rule','')}"
                for f in flags[:3])
        else:
            flag_txt = "无"

        out += [
            f"## {i:02d} · {r['sec_name']}（{r['sec_code']}）· {r['category']}",
            "",
            f"**公告标题**：{r['title']}",
            "",
            f"**披露日期**：{r['notice_date']}　**自动分**：{r['score_total']}",
            "",
            "**原文节选**",
            "",
            "> " + clean(r.get("content_text")).replace("\n", " "),
            "",
            "**AI 输出**",
            "",
            f"- 一句话解读：{interp.get('one_line', '（缺失）')}",
            f"- 买方会问：{bullets(interp.get('buy_side_questions'))}",
            f"- 卖方会问：{bullets(interp.get('sell_side_questions'))}",
            f"- 建议口径：{interp.get('suggested_statement', '（缺失）')}",
            f"- 合规标记：{flag_txt}",
            "",
            "**你的判断**",
            "",
            "- 解读准确性：`[ ]` 准确　`[ ]` 部分准确　`[ ]` 错误",
            "- 口径可用性：`[ ]` 可直接用　`[ ]` 改后可用　`[ ]` 不能用",
            "- 合规漏报：`[ ]` 无　`[ ]` 有 → 漏了什么：________",
            "- 备注：________",
            "",
            "---",
            "",
        ]
    return "\n".join(out)


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = pick_rows(conn)
    finally:
        conn.close()

    if not rows:
        print("没有可抽样的已分析公告。")
        return 1

    OUT_PATH.write_text(render(rows), encoding="utf-8")
    print(f"已生成：{OUT_PATH}")
    print(f"共 {len(rows)} 条，类别分布：")
    from collections import Counter
    for cat, n in Counter(r["category"] for r in rows).most_common():
        print(f"  {cat:<12}{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
