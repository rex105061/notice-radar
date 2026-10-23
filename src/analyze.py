# -*- coding: utf-8 -*-
"""单条公告的完整分析链：抽取 → 解读三件套 → 规则化质量评分 → 落库。

为什么要「规则化评分」而不是让 LLM 给自己打分：
面试官一定会问「这个 82 分是怎么来的」。规则分能逐项拆开解释，
LLM 自评则答不出依据——那是最容易被问穿的地方。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from config import DB_PATH
from llm import DEFAULT_MODEL, chat_json
from prompts import (
    COMPLIANCE_RULES,
    EXTRACT_SYSTEM,
    EXTRACT_USER,
    INTERPRET_SYSTEM,
    INTERPRET_USER,
    MAX_CHARS_FOR_LLM,
    _RULE_TEXT,
)

DDL_ANALYSIS = """
CREATE TABLE IF NOT EXISTS analysis (
    announcement_id  TEXT PRIMARY KEY,
    extracted_json   TEXT,
    interpreted_json TEXT,
    score_total      INTEGER,
    score_detail     TEXT,
    model            TEXT,
    created_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_score_total ON analysis(score_total);
"""


# ------------------------------------------------------------------ 工具
def _fill(template: str, **kw) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _clip(text: str, limit: int = MAX_CHARS_FOR_LLM) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[正文过长，已截断]"


def _jaccard(a: str, b: str) -> float:
    """字符集合相似度，用来判断一句话解读是不是在复述标题。"""
    sa, sb = set(re.sub(r"\s", "", a or "")), set(re.sub(r"\s", "", b or ""))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _has_long_copy(text: str, source: str, n: int = 40) -> bool:
    """检查 text 中是否存在长度 >= n 的连续片段直接抄自 source。

    用于识别「建议口径只是把公告原文抄了一遍」——那没有完成对外转述这一步。
    """
    if not text or not source or len(text) < n:
        return False
    for i in range(len(text) - n + 1):
        if text[i:i + n] in source:
            return True
    return False


def _amount_in_source(amount: dict | None, content: str) -> bool | None:
    """抽取出的金额能否在原文找到。None 表示无法判断（本来就没金额）。"""
    if not amount:
        return None
    raw = str(amount.get("raw") or "").replace(",", "").replace("，", "").strip()
    if raw and raw in content:
        return True
    val = amount.get("value")
    if val is None:
        return None
    num = str(val).replace(",", "").rstrip("0").rstrip(".")
    return num in content.replace(",", "").replace("，", "") if num else None


def _hit_rules(text: str) -> list:
    """检查一段文字命中了哪些合规红线词。"""
    hits = []
    for name, words in COMPLIANCE_RULES:
        for w in words:
            if w in (text or ""):
                hits.append((name, w))
                break
    return hits


# ------------------------------------------------------------------ 评分
def score_result(extracted: dict, interpreted: dict, content: str, title: str) -> dict:
    """四维规则评分，满分 100。每个扣分项都记录原因，保证可解释。"""
    detail = []

    # ① 事实落地 25 分：关键字段是否抽出来了
    s1 = 0
    for field, pts in (("notice_type", 5), ("event_summary", 5),
                       ("direction", 5), ("amount", 5), ("key_terms", 5)):
        v = extracted.get(field)
        if v and (not isinstance(v, list) or len(v) >= 2):
            s1 += pts
        else:
            detail.append(f"[-{pts}] 字段 {field} 缺失或过于简略")
    detail.append(f"[+{s1}] 事实抽取完整度 25 分制")

    # ② 数字一致性 25 分
    in_src = _amount_in_source(extracted.get("amount"), content)
    if in_src is True:
        s2 = 25
        detail.append("[+25] 抽取金额可在原文找到")
    elif in_src is None:
        s2 = 15
        detail.append("[+15] 公告本身未涉及金额，不扣满")
    else:
        s2 = 0
        detail.append("[-25] 抽取金额在原文中找不到，疑似数字错误")

    # ③ 合规安全 25 分
    stmt = interpreted.get("suggested_statement") or ""
    bad = _hit_rules(stmt)
    s3 = max(0, 25 - 8 * len(bad))
    if bad:
        for rule, word in bad:
            detail.append(f"[-8] 建议口径命中红线「{rule}」：{word}")
    else:
        detail.append("[+25] 建议口径未命中合规红线")

    # ④ 输出完整度 25 分
    s4 = 0
    one = interpreted.get("one_line") or ""
    if one and _jaccard(one, title) < 0.6:
        s4 += 7
        detail.append("[+7] 一句话解读存在且未复述标题")
    else:
        detail.append("[-7] 一句话解读缺失，或在复述标题")
    if len(interpreted.get("buy_side_questions") or []) >= 3:
        s4 += 6
        detail.append("[+6] 买方视角问题 ≥3 条")
    else:
        detail.append("[-6] 买方视角问题不足 3 条")
    if len(interpreted.get("sell_side_questions") or []) >= 3:
        s4 += 6
        detail.append("[+6] 卖方视角问题 ≥3 条")
    else:
        detail.append("[-6] 卖方视角问题不足 3 条")
    if len(stmt) >= 20:
        s4 += 6
        detail.append("[+6] 建议口径长度合格")
    else:
        detail.append("[-6] 建议口径过短")

    # ⑤ 区分度扣分项：没有这一层，所有「格式齐全」的输出都会拿满分，评分就失去意义
    penalty = 0
    amount_val = (extracted.get("amount") or {}).get("value")
    if amount_val and not re.search(r"\d", one):
        penalty += 4
        detail.append("[-4] 公告含明确金额，但一句话解读未引用任何数字")

    buy = interpreted.get("buy_side_questions") or []
    sell = interpreted.get("sell_side_questions") or []
    if buy and sell:
        max_sim = max((_jaccard(b, s) for b in buy for s in sell), default=0.0)
        if max_sim > 0.6:
            penalty += 5
            detail.append(f"[-5] 买方与卖方问题高度重合（相似度 {max_sim:.2f}），视角区分度不足")

    if len(content) > MAX_CHARS_FOR_LLM:
        penalty += 3
        detail.append("[-3] 正文超长已截断，后文条款可能遗漏")

    if stmt and _has_long_copy(stmt, content, 40):
        penalty += 6
        detail.append("[-6] 建议口径含 40 字以上原文照搬，未完成对外转述")

    total = max(0, s1 + s2 + s3 + s4 - penalty)
    return {
        "total": total,
        "dimensions": {
            "事实落地": s1,
            "数字一致": s2,
            "合规安全": s3,
            "输出完整": s4,
        },
        "detail": detail,
    }


# ------------------------------------------------------------------ 主流程
def analyze_notice(row: dict) -> dict:
    """row 需含 announcement_id / sec_code / sec_name / title / content_text。"""
    content = (row.get("content_text") or "").strip()
    if not content:
        raise ValueError(
            f"公告 {row.get('announcement_id')} 没有正文，无法分析。"
            "请先用 fetch_notices.py --with-pdf 抓取正文。"
        )

    clipped = _clip(content)

    extracted = chat_json(
        EXTRACT_SYSTEM,
        _fill(EXTRACT_USER, SEC_NAME=row.get("sec_name", ""),
              SEC_CODE=row.get("sec_code", ""), TITLE=row.get("title", ""),
              CONTENT=clipped),
        temperature=0.1,
        max_tokens=1500,
    )

    interpreted = chat_json(
        INTERPRET_SYSTEM,
        _fill(INTERPRET_USER, SEC_NAME=row.get("sec_name", ""),
              SEC_CODE=row.get("sec_code", ""), TITLE=row.get("title", ""),
              EXTRACTED=json.dumps(extracted, ensure_ascii=False, indent=2),
              CONTENT=clipped, RULES=_RULE_TEXT),
        temperature=0.3,
        max_tokens=3000,
    )

    score = score_result(extracted, interpreted, content, row.get("title", ""))

    return {
        "announcement_id": row.get("announcement_id"),
        "extracted": extracted,
        "interpreted": interpreted,
        "score": score,
    }


def save_analysis(result: dict, model: str = DEFAULT_MODEL) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(DDL_ANALYSIS)
        conn.execute(
            "INSERT OR REPLACE INTO analysis "
            "(announcement_id, extracted_json, interpreted_json, "
            " score_total, score_detail, model, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                result["announcement_id"],
                json.dumps(result["extracted"], ensure_ascii=False),
                json.dumps(result["interpreted"], ensure_ascii=False),
                result["score"]["total"],
                json.dumps(result["score"], ensure_ascii=False),
                model,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
