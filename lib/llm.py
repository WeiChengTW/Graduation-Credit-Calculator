from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from lib.report import render_markdown_report
from lib.utils import ensure_parent, extract_json

REQUIRED_REPORT_KEYS = [
    "status",
    "recognized_credits",
    "required_credits",
    "missing_credits",
    "one_sentence_summary",
    "requirements",
    "missing_items",
    "detailed_checks",
    "limited_or_excluded_courses",
    "manual_review_items",
    "next_semester_recommendations",
]


def validate_report_schema(report_data: dict) -> list[str]:
    errors = []
    if not isinstance(report_data, dict):
        return ["LLM 回傳內容不是 JSON object。"]
    for key in REQUIRED_REPORT_KEYS:
        if key not in report_data:
            errors.append(f"缺少必要欄位：{key}")
    list_keys = [
        "requirements",
        "missing_items",
        "limited_or_excluded_courses",
        "manual_review_items",
        "next_semester_recommendations",
    ]
    for key in list_keys:
        if key in report_data and not isinstance(report_data[key], list):
            errors.append(f"欄位 {key} 必須是 list。")
    if "detailed_checks" in report_data and not isinstance(
        report_data["detailed_checks"], dict
    ):
        errors.append("欄位 detailed_checks 必須是 object。")
    return errors


def load_course_records(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def append_manual_review(
    report_data: dict, item: str, reason: str, evidence: str
) -> None:
    report_data.setdefault("manual_review_items", [])
    report_data["manual_review_items"].append(
        {"item": item, "reason": reason, "evidence": evidence}
    )


def extract_grounding_candidate(item: dict) -> str:
    for key in ("course", "item"):
        value = str(item.get(key, "")).strip()
        if value:
            return value
    return ""


def looks_like_course_name(candidate: str) -> bool:
    candidate = candidate.strip()
    if not candidate:
        return False
    if any(
        keyword in candidate
        for keyword in ("領域", "門檻", "學分", "選修", "必修", "學程", "摘要", "統計")
    ):
        return False
    return bool(re.search(r"[0-9A-Za-z(（)]", candidate))


def validate_report_grounding(report_data: dict, csv_path: Path) -> None:
    course_names = {
        row.get("課程名稱", "").strip() for row in load_course_records(csv_path)
    }
    course_names.discard("")
    if not course_names:
        append_manual_review(
            report_data,
            "修課資料",
            "找不到可比對的 courses_detail.csv 課程名稱",
            str(csv_path),
        )
        return

    containers = []
    containers.extend(report_data.get("limited_or_excluded_courses", []))
    containers.extend(report_data.get("completed_items", []))

    for item in containers:
        if not isinstance(item, dict):
            continue
        candidate = extract_grounding_candidate(item)
        if not looks_like_course_name(candidate):
            continue
        if candidate not in course_names and not any(
            candidate in name or name in candidate for name in course_names
        ):
            append_manual_review(
                report_data,
                candidate,
                "報告將此項目描述為已完成/已修，但在 courses_detail.csv 找不到完全對應課程名稱。",
                "CSV grounding validation",
            )


def summarize_rule(content: str, model: str, base_url: str, api_key: str) -> str:
    if base_url.endswith("/chat/completions"):
        endpoint = base_url
    else:
        endpoint = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是一個文件總結助手。請務必使用繁體中文（zh-TW）輸出。嚴禁輸出任何 JSON 格式的內容，只能使用 Markdown 條列式純文字。",
            },
            {
                "role": "user",
                "content": f"請總結以下手冊中有關「畢業學分抵免與修課規定」的重點，保留具體的學分數字與修課條件，忽略與修課無關的行政細節。請務必使用純文字 Markdown 條列式輸出，絕對不要輸出 JSON 格式：\n\n{content}",
            },
        ],
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        endpoint, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST"
    )
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                raw_response = response.read().decode("utf-8")
                if not raw_response.strip():
                    raise ValueError("API 回傳空白內容 (Empty Response)")
                result = json.loads(raw_response)
                summary = result["choices"][0]["message"]["content"]
                summary = re.sub(
                    r"<think>.*?</think>", "", summary, flags=re.DOTALL
                ).strip()
                summary = summary.replace("{", "").replace("}", "").replace('"', "")
                return summary
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"總結失敗 ({e})，正在重試 ({attempt + 1}/{max_retries})...")
                time.sleep(2)
            else:
                print(f"總結失敗 ({e})，將使用原始文本...")
                return content


def call_llm_analyzer(
    csv_path: Path,
    rules_md_paths: list[Path],
    summary: str,
    base_url: str,
    api_key: str,
    model: str,
    report_md_path: Path,
    report_json_path: Path,
    report_raw_path: Path,
    debug: bool = False,
):
    rules_text = []
    for p in rules_md_paths:
        content = p.read_text(encoding="utf-8")
        if len(content) > 10000:
            print(
                f"檔案 {p.name} 長度為 {len(content)} 字元，自動啟動 LLM 摘要以避免 Token 過長..."
            )
            content = summarize_rule(content, model, base_url, api_key)
        rules_text.append(f"--- 規則文件：{p.name} ---\n{content}")

    csv_text = csv_path.read_text(encoding="utf-8-sig") if csv_path.exists() else ""

    prompt = f"""請根據以下提供的修課紀錄 CSV 資料、修課統計摘要，以及多份畢業規則 Markdown 檔案，為這位學生進行畢業學分分析。

【要求】
1. 只能根據提供的 CSV 與 Markdown 規則判斷，不確定時請列在「需要人工確認」，不要亂猜。
2. 課程如果可抵免或只能部分採認，要列出原因與來源規則。
3. 對不同科系請動態判讀規則，不要帶有預設立場。
4. **必須**回傳一個純 JSON 物件，不要有任何其他 Markdown 文字或解釋。你唯一能輸出的只有符合下方格式的最終畢業學分分析 JSON，請務必嚴格遵守此 JSON 結構，絕對不要改變欄位名稱或新增其他欄位。
5. 輸出的 JSON 格式如下所示：

```json
{{
  "status": "尚不可畢業",
  "recognized_credits": 122,
  "required_credits": 128,
  "missing_credits": 6,
  "one_sentence_summary": "目前尚缺系定必修1學分、系選修3學分、通識多元2學分。",
  "requirements": [
    {{
      "category": "系定必修",
      "status": "未完成",
      "required": "63學分",
      "completed": "62學分",
      "missing": "軟硬體專題(3) 1學分",
      "evidence": "畢業學分.md：系定必修63學分"
    }}
  ],
  "missing_items": [
    {{
      "priority": "high",
      "category": "系定必修",
      "item": "軟硬體專題(3)",
      "credits": 1,
      "recommended_action": "大四修習或依規定確認是否可抵修"
    }}
  ],
  "detailed_checks": {{
    "compulsory": [
      {{"category": "計算機概論", "status": "已完成", "evidence": "修課紀錄中已包含計算機概論(1)(2)"}}
    ],
    "elective": [],
    "general_education": [],
    "honors_or_special": []
  }},
  "limited_or_excluded_courses": [
    {{"course": "某課程", "reason": "重複修習", "evidence": "規定第X條"}}
  ],
  "manual_review_items": [
    {{"item": "抵免課程", "reason": "需要系辦確認", "evidence": "規則Y"}}
  ],
  "next_semester_recommendations": [
    "建議修習系選修3學分"
  ]
}}
```

---
【輸入資料】

<修課紀錄 CSV>
{csv_text}
</修課紀錄 CSV>

<修課統計摘要>
{summary}
</修課統計摘要>

<畢業規則>
{chr(10).join(rules_text)}
</畢業規則>
"""

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    data = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是一個專業的大學畢業學分分析助手。請只輸出 JSON 格式資料，並請務必使用繁體中文（zh-TW）撰寫內容。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }

    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        endpoint = base_url
    else:
        endpoint = f"{base_url}/chat/completions"

    print(f"正在呼叫 LLM 分析 ({model}) 於 {endpoint} ...")
    if debug:
        print(
            f"DEBUG: CSV 字元數={len(csv_text)}，規則檔數={len(rules_text)}，模型={model}"
        )
    req = urllib.request.Request(
        endpoint, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST"
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(
                f"等待 LLM 回應中（第 {attempt + 1}/{max_retries} 次嘗試）...",
                flush=True,
            )
            with urllib.request.urlopen(req, timeout=300) as response:
                raw_response = response.read().decode("utf-8")
                if not raw_response.strip():
                    raise ValueError("API 回傳空白內容 (Empty Response)")
                result = json.loads(raw_response)
                content = result["choices"][0]["message"]["content"]

                json_str = extract_json(content)
                break
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""

            if error_body:
                print(
                    f"LLM 呼叫失敗：HTTP {e.code} {e.reason}。回應內容：{error_body[:800].strip()}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"LLM 呼叫失敗：HTTP {e.code} {e.reason}",
                    file=sys.stderr,
                )

            if e.code in {401, 403, 404}:
                raise
            if attempt < max_retries - 1:
                print(
                    f"將在 2 秒後重試 ({attempt + 1}/{max_retries})...",
                    file=sys.stderr,
                )
                time.sleep(2)
            else:
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                print(
                    f"LLM 呼叫失敗：{e}，正在重試 ({attempt + 1}/{max_retries})...",
                    file=sys.stderr,
                )
                time.sleep(2)
            else:
                print(f"LLM 呼叫失敗：{e}", file=sys.stderr)
                raise
    try:
        report_data = json.loads(json_str)
        schema_errors = validate_report_schema(report_data)
        if schema_errors:
            ensure_parent(report_raw_path)
            report_raw_path.write_text(content, encoding="utf-8")
            raise ValueError(
                "LLM 回傳 JSON schema 不符合預期："
                + "；".join(schema_errors)
                + f"。原始輸出已儲存至 {report_raw_path}"
            )

        validate_report_grounding(report_data, csv_path)

        ensure_parent(report_json_path)
        report_json_path.write_text(
            json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"成功解析 JSON！已儲存至 {report_json_path}")

        md_content = render_markdown_report(
            report_data, csv_path=csv_path, rules_md_paths=rules_md_paths, model=model
        )
        ensure_parent(report_md_path)
        report_md_path.write_text(md_content, encoding="utf-8")
        print(f"成功生成 Markdown 報告！已儲存至 {report_md_path}")

    except json.JSONDecodeError as e:
        print(
            f"解析 JSON 失敗：{e}，原始輸出已儲存至 {report_raw_path}", file=sys.stderr
        )
        ensure_parent(report_raw_path)
        report_raw_path.write_text(content, encoding="utf-8")
        raise
