from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from lib.utils import markdown_cell


def summarize_courses(csv_path: Path) -> str:
    courses = []
    if not csv_path.exists():
        return "找不到修課紀錄 CSV 檔案。"

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            courses.append(row)

    total_credits = 0.0
    zero_credit_courses = []
    category_counts = {}
    department_counts = {}
    name_counts = {}

    for row in courses:
        credits_str = row.get("學分", "0")
        try:
            c = float(credits_str)
        except ValueError:
            c = 0.0

        total_credits += c
        if c == 0:
            zero_credit_courses.append(row.get("課程名稱", ""))

        cat = row.get("課程類別", "未知")
        category_counts[cat] = category_counts.get(cat, 0.0) + c

        dept = row.get("開課單位", "未知")
        department_counts[dept] = department_counts.get(dept, 0.0) + c

        name = row.get("課程名稱", "未知")
        name_counts[name] = name_counts.get(name, 0) + 1

    summary = [
        f"### 修課資料摘要",
        f"- 總學分: {total_credits:g}",
        f"",
        f"#### 依課程類別加總",
    ]
    for cat, val in category_counts.items():
        summary.append(f"- {cat}: {val:g} 學分")

    summary.append("")
    summary.append(f"#### 依開課單位加總")
    for dept, val in department_counts.items():
        summary.append(f"- {dept}: {val:g} 學分")

    summary.append("")
    summary.append(f"#### 0 學分課程")
    if zero_credit_courses:
        for z in zero_credit_courses:
            summary.append(f"- {z}")
    else:
        summary.append("無")

    summary.append("")
    summary.append(f"#### 重複修課 (重修或同名)")
    duplicates = {name: count for name, count in name_counts.items() if count > 1}
    if duplicates:
        for name, count in duplicates.items():
            summary.append(f"- {name}: {count} 次")
    else:
        summary.append("無")

    return "\n".join(summary)


def render_markdown_report(
    data: dict,
    *,
    csv_path: Path | None = None,
    rules_md_paths: list[Path] | None = None,
    model: str | None = None,
) -> str:
    lines = ["# 畢業學分檢查報告\n"]
    lines.append(f"> 產生時間：{datetime.now().isoformat(timespec='seconds')}")
    if model:
        lines.append(f"> 使用模型：{markdown_cell(model)}")
    if csv_path:
        lines.append(f"> 修課資料：{markdown_cell(csv_path)}")
    if rules_md_paths:
        lines.append(
            f"> 規則來源：{markdown_cell(', '.join(p.name for p in rules_md_paths))}"
        )
    lines.append(
        "> 注意：本報告由 LLM 根據提供資料分析，正式畢業資格仍以系辦/教務處認定為準。\n"
    )
    lines.append("## 1. 一句話結論")
    lines.append(
        f"目前{data.get('status', '未知')}，已認列 {data.get('recognized_credits', 0)} / {data.get('required_credits', 0)} 學分，尚缺 {data.get('missing_credits', 0)} 學分。"
    )
    lines.append(f"{data.get('one_sentence_summary', '')}\n")

    lines.append("## 2. 缺口總表")
    lines.append("| 優先級 | 類別 | 缺少項目 | 學分 | 建議動作 |")
    lines.append("|---|---|---:|---:|---|")
    for item in data.get("missing_items", []):
        lines.append(
            f"| {markdown_cell(item.get('priority', ''))} | {markdown_cell(item.get('category', ''))} | {markdown_cell(item.get('item', ''))} | {markdown_cell(item.get('credits', ''))} | {markdown_cell(item.get('recommended_action', ''))} |"
        )
    lines.append("")

    lines.append("## 3. 完成狀況總表")
    lines.append("| 類別 | 要求 | 已完成 | 缺少 | 狀態 |")
    lines.append("|---|---|---|---|---|")
    for req in data.get("requirements", []):
        lines.append(
            f"| {markdown_cell(req.get('category', ''))} | {markdown_cell(req.get('required', ''))} | {markdown_cell(req.get('completed', ''))} | {markdown_cell(req.get('missing', ''))} | {markdown_cell(req.get('status', ''))} |"
        )
    lines.append("")

    lines.append("## 4. 詳細檢查")
    detailed = data.get("detailed_checks", {})

    def render_detail_table(items):
        if not items:
            lines.append("無相關紀錄\n")
            return
        lines.append("| 類別/課程 | 判斷 | 依據 |")
        lines.append("|---|---|---|")
        for i in items:
            lines.append(
                f"| {markdown_cell(i.get('category', ''))} | {markdown_cell(i.get('status', ''))} | {markdown_cell(i.get('evidence', ''))} |"
            )
        lines.append("")

    lines.append("### 4.1 必修")
    render_detail_table(detailed.get("compulsory", []))
    lines.append("### 4.2 系選修 / 專業選修")
    render_detail_table(detailed.get("elective", []))
    lines.append("### 4.3 通識")
    render_detail_table(detailed.get("general_education", []))
    lines.append("### 4.4 榮譽學程 / 特殊抵免")
    render_detail_table(detailed.get("honors_or_special", []))

    lines.append("## 5. 不採認或只能部分採認的課程")
    if data.get("limited_or_excluded_courses"):
        lines.append("| 課程 | 原因 | 依據 |")
        lines.append("|---|---|---|")
        for i in data.get("limited_or_excluded_courses", []):
            lines.append(
                f"| {markdown_cell(i.get('course', ''))} | {markdown_cell(i.get('reason', ''))} | {markdown_cell(i.get('evidence', ''))} |"
            )
    else:
        lines.append("無")
    lines.append("")

    lines.append("## 6. 需要人工確認")
    if data.get("manual_review_items"):
        lines.append("| 項目 | 原因 | 依據 |")
        lines.append("|---|---|---|")
        for i in data.get("manual_review_items", []):
            lines.append(
                f"| {markdown_cell(i.get('item', ''))} | {markdown_cell(i.get('reason', ''))} | {markdown_cell(i.get('evidence', ''))} |"
            )
    else:
        lines.append("無")
    lines.append("")

    lines.append("## 7. 建議下學期選課")
    if data.get("next_semester_recommendations"):
        for r in data.get("next_semester_recommendations", []):
            lines.append(f"- {markdown_cell(r)}")
    else:
        lines.append("無建議")
    lines.append("")

    lines.append("## 8. 判斷依據")
    evidence_rows = []
    for req in data.get("requirements", []):
        if isinstance(req, dict) and req.get("evidence"):
            evidence_rows.append((req.get("category", ""), req.get("evidence", "")))
    if evidence_rows:
        lines.append("| 類別 | 依據 |")
        lines.append("|---|---|")
        for category, evidence in evidence_rows:
            lines.append(f"| {markdown_cell(category)} | {markdown_cell(evidence)} |")
    else:
        lines.append("無")
    lines.append("")

    return "\n".join(lines)
