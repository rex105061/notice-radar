#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量分析入口：从库里挑出有正文的公告，跑「抽取 → 解读 → 评分」，结果落库并打印。

用法：
    python src/pipeline.py --list                       # 看库里有哪些可分析的公告
    python src/pipeline.py --limit 1                    # 分析最新 1 条
    python src/pipeline.py --id 1225608844              # 分析指定公告
    python src/pipeline.py --code 300750 --limit 5      # 分析某公司 5 条
    python src/pipeline.py --limit 20 --skip-done       # 跳过已分析过的
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import DDL_ANALYSIS, analyze_notice, save_analysis  # noqa: E402
from config import DB_PATH  # noqa: E402


def _ensure_tables() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(DDL_ANALYSIS)
    finally:
        conn.close()


def fetch_candidates(code=None, aid=None, limit=None, skip_done=False) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT n.* FROM notices n "
           "WHERE n.content_text IS NOT NULL AND n.content_text != ''")
    params: list = []
    if aid:
        sql += " AND n.announcement_id = ?"
        params.append(aid)
    if code:
        sql += " AND n.sec_code = ?"
        params.append(code)
    if skip_done:
        sql += (" AND n.announcement_id NOT IN "
                "(SELECT announcement_id FROM analysis)")
    sql += " ORDER BY n.notice_date DESC, n.is_high_value DESC, n.adjunct_size ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def list_available() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT sec_code, sec_name, COUNT(*) AS n FROM notices "
        "WHERE content_text IS NOT NULL AND content_text != '' "
        "GROUP BY sec_code, sec_name ORDER BY n DESC LIMIT 30"
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM notices WHERE content_text IS NOT NULL "
        "AND content_text != ''").fetchone()[0]
    conn.close()
    print(f"库中已有正文的公告：{total} 条")
    if not rows:
        print("⚠️ 还没有正文。先执行："
              "python src/fetch_notices.py --days 7 --with-pdf --high-value-only")
        return
    print(f"{'代码':<8}{'简称':<10}{'正文条数'}")
    for code, name, n in rows:
        print(f"{code:<8}{name:<10}{n}")


def print_result(row: dict, result: dict) -> None:
    ex, it, sc = result["extracted"], result["interpreted"], result["score"]
    line = "─" * 62
    print(f"\n{line}")
    print(f"【公司】{row['sec_name']}（{row['sec_code']}）   【日期】{row.get('notice_date','')}")
    print(f"【公告】{row['title']}")
    print(line)

    print(f"\n▍一句话解读\n  {it.get('one_line', '（缺失）')}")

    print("\n▍机构可能关注点")
    print("  买方（基金经理）：")
    for i, q in enumerate(it.get("buy_side_questions") or [], 1):
        print(f"    {i}. {q}")
    print("  卖方（分析师）：")
    for i, q in enumerate(it.get("sell_side_questions") or [], 1):
        print(f"    {i}. {q}")

    print(f"\n▍建议口径\n  {it.get('suggested_statement', '（缺失）')}")

    flags = it.get("compliance_flags") or []
    print("\n▍建议口径的合规风险")
    if flags:
        for f in flags:
            print(f"  [{f.get('risk_level','?')}] 「{f.get('quote','')}」"
                  f" — {f.get('rule','')} — {f.get('reason','')}")
    else:
        print("  建议口径未命中合规红线")
    if it.get("question_caution"):
        print("\n▍不宜原样对外提问的地方")
        for c in it["question_caution"]:
            print(f"  - {c}")
    if it.get("compliance_note"):
        print(f"  提示：{it['compliance_note']}")

    d = sc["dimensions"]
    print(f"\n▍系统质量分  {sc['total']}/100")
    print(f"   事实落地 {d['事实落地']}/25 | 数字一致 {d['数字一致']}/25 | "
          f"合规安全 {d['合规安全']}/25 | 输出完整 {d['输出完整']}/25")
    for item in sc["detail"]:
        print(f"   · {item}")

    print(f"\n▍结构化抽取\n  {json.dumps(ex, ensure_ascii=False, indent=2)[:800]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="公告雷达批量分析")
    ap.add_argument("--list", action="store_true", help="列出库中有正文的公告")
    ap.add_argument("--id", help="只分析指定 announcement_id")
    ap.add_argument("--code", help="只分析指定股票代码")
    ap.add_argument("--limit", type=int, help="最多分析多少条")
    ap.add_argument("--skip-done", action="store_true", help="跳过已分析过的")
    args = ap.parse_args()

    _ensure_tables()

    if args.list:
        list_available()
        return 0

    rows = fetch_candidates(args.code, args.id, args.limit, args.skip_done)
    if not rows:
        print("没有可分析的公告（库里没有正文，或都分析过了）。")
        print("先抓正文：python src/fetch_notices.py --days 7 --with-pdf --high-value-only")
        return 1

    print(f"待分析 {len(rows)} 条，开始……")
    ok = fail = 0
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row['sec_name']} {row['title'][:32]}")
        t0 = time.time()
        try:
            result = analyze_notice(row)
            save_analysis(result)
            print_result(row, result)
            ok += 1
            print(f"（本条耗时 {time.time() - t0:.1f}s）")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  ✗ 失败：{type(exc).__name__}: {exc}")
        time.sleep(0.5)

    print(f"\n=== 完成：成功 {ok} 条，失败 {fail} 条 ===")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
