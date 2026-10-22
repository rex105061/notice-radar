# -*- coding: utf-8 -*-
"""公告雷达 · 网页版（Streamlit）

启动：
    streamlit run src/app.py

设计取舍：
- 列表从 SQLite 读，秒开；AI 解读按需触发（点按钮），避免一进页面就烧 token
- 已分析过的结果直接读库，不重复调用模型
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import DDL_ANALYSIS, analyze_notice, save_analysis  # noqa: E402
from config import DB_PATH  # noqa: E402

DISCLAIMER = (
    "**免责声明**：本页内容由 AI 生成，仅供参考，**不构成任何投资建议**。"
    "所有结论使用前须经人工复核。数据来源为巨潮资讯网公开披露信息。"
)

st.set_page_config(page_title="公告雷达", layout="wide")


# ------------------------------------------------------------------ 数据
@st.cache_data(ttl=30)
def query_notices(keyword: str, days: int, high_only: bool) -> list:
    sql = ("SELECT * FROM notices WHERE 1=1")
    params: list = []
    if keyword:
        sql += " AND (sec_code = ? OR sec_code LIKE ? OR sec_name LIKE ?)"
        params += [keyword, f"{keyword}%", f"%{keyword}%"]
    if days:
        since = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        sql += " AND notice_date >= ?"
        params.append(since)
    if high_only:
        sql += " AND is_high_value = 1"
    sql += " ORDER BY notice_date DESC, is_high_value DESC, adjunct_size ASC LIMIT 60"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def load_analysis(aid: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(DDL_ANALYSIS)
        row = conn.execute(
            "SELECT extracted_json, interpreted_json, score_detail, score_total "
            "FROM analysis WHERE announcement_id = ?", (aid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "extracted": json.loads(row[0] or "{}"),
        "interpreted": json.loads(row[1] or "{}"),
        "score": json.loads(row[2] or "{}"),
        "total": row[3],
    }


@st.cache_data(ttl=300)
def db_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
        d = conn.execute(
            "SELECT MIN(notice_date), MAX(notice_date) FROM notices").fetchone()
        with_text = conn.execute(
            "SELECT COUNT(*) FROM notices WHERE content_text IS NOT NULL "
            "AND content_text != ''").fetchone()[0]
        analyzed = conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]
    except sqlite3.Error:
        n, d, with_text, analyzed = 0, (None, None), 0, 0
    finally:
        conn.close()
    return {"total": n, "range": d, "with_text": with_text, "analyzed": analyzed}


# ------------------------------------------------------------------ 渲染
def render_analysis(res: dict) -> None:
    it = res["interpreted"]
    ex = res["extracted"]
    sc = res.get("score") or {}

    st.markdown("**一句话解读**")
    st.info(it.get("one_line") or "（缺失）")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**买方会问什么**")
        for i, q in enumerate(it.get("buy_side_questions") or [], 1):
            st.markdown(f"{i}. {q}")
    with c2:
        st.markdown("**卖方会问什么**")
        for i, q in enumerate(it.get("sell_side_questions") or [], 1):
            st.markdown(f"{i}. {q}")

    st.markdown("**建议对外口径**")
    st.success(it.get("suggested_statement") or "（缺失）")

    flags = it.get("compliance_flags") or []
    st.markdown("**建议口径的合规风险**")
    if flags:
        for f in flags:
            lvl = f.get("risk_level", "?")
            icon = {"高": "red", "中": "orange", "低": "blue"}.get(lvl, "gray")
            st.markdown(
                f":{icon}[**[{lvl}]**] 「{f.get('quote','')}」 — "
                f"{f.get('rule','')}：{f.get('reason','')}")
    else:
        st.markdown(":green[建议口径未命中合规红线]")

    caution = it.get("question_caution") or []
    if caution:
        with st.expander("哪些问题不能原样抛给公司"):
            for c in caution:
                st.markdown(f"- {c}")

    if it.get("compliance_note"):
        st.caption(it["compliance_note"])

    if sc:
        dims = sc.get("dimensions", {})
        st.markdown(f"**系统质量分　{sc.get('total', res.get('total'))}/100**")
        cols = st.columns(4)
        for col, (k, v) in zip(cols, dims.items()):
            col.metric(k, f"{v}/25")
        with st.expander("分数怎么来的"):
            for item in sc.get("detail", []):
                st.markdown(f"- {item}")

    with st.expander("结构化抽取结果"):
        st.json(ex)


def main() -> None:
    st.title("公告雷达")
    st.caption("输入股票代码 → 看公告 → 读出「意味着什么、机构会问什么、口径怎么说」")
    st.warning(DISCLAIMER)

    stats = db_stats()
    with st.sidebar:
        st.markdown("### 数据概况")
        st.metric("公告总数", stats["total"])
        st.metric("有正文", stats["with_text"])
        st.metric("已分析", stats["analyzed"])
        if stats["range"][0]:
            st.caption(f"覆盖日期：{stats['range'][0]} ~ {stats['range'][1]}")
        st.divider()
        st.markdown("### 合规红线（模型只做判定）")
        st.caption("预测性表述 / 承诺性表述 / 绝对化用语 / 比较性表述 / "
                   "投资建议 / 未披露信息暗示")

    c1, c2, c3 = st.columns([2, 1, 1])
    keyword = c1.text_input("股票代码或公司简称", placeholder="例如 300750 或 宁德时代")
    days = c2.selectbox("时间范围", [3, 7, 14, 30, 90], index=1,
                        format_func=lambda d: f"近 {d} 天")
    high_only = c3.checkbox("只看高价值类型", value=True)

    if not keyword:
        st.info("输入股票代码或公司简称后开始查询。")
        return

    rows = query_notices(keyword.strip(), days, high_only)
    if not rows:
        st.warning("没有查到公告。可能是：① 代码/名称不对；② 该时间段内没有公告；"
                   "③ 数据还没抓取（先跑 fetch_notices.py）。")
        return

    name = rows[0]["sec_name"]
    st.subheader(f"{name}（{rows[0]['sec_code']}）　共 {len(rows)} 条")

    for row in rows:
        aid = row["announcement_id"]
        flag = "★ " if row["is_high_value"] else ""
        label = (f"{flag}{row['notice_date']}　{row['category']}　"
                 f"{row['title'][:46]}")
        with st.expander(label):
            st.caption(f"公告编号 {aid}　｜　类型：{row['category']}　｜　"
                       f"PDF 大小 {row['adjunct_size']} KB")

            res = load_analysis(aid) or st.session_state.get(f"res_{aid}")

            if res:
                render_analysis(res)
            elif not row.get("content_text"):
                st.warning("这条公告没有正文（PDF 未抓取或为扫描件），无法生成解读。")
            else:
                st.caption("尚未生成解读。点击下方按钮，约 5–10 秒。")
                if st.button("生成 AI 解读", key=f"btn_{aid}"):
                    with st.spinner("正在抽取事实并生成解读……"):
                        try:
                            result = analyze_notice(row)
                            save_analysis(result)
                            st.session_state[f"res_{aid}"] = {
                                "extracted": result["extracted"],
                                "interpreted": result["interpreted"],
                                "score": result["score"],
                                "total": result["score"]["total"],
                            }
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"分析失败：{exc}")


if __name__ == "__main__":
    main()
