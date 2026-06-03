from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from lib.indexer import query_rules
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
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            err_code = ""
            try:
                err_code = json.loads(error_body).get("code", "")
            except Exception:
                pass
            if err_code == "INSUFFICIENT_BALANCE":
                print(f"總結失敗：帳戶餘額不足（INSUFFICIENT_BALANCE）。請加值後再試。")
                raise
            if attempt < max_retries - 1:
                print(f"總結失敗 (HTTP {e.code})，正在重試 ({attempt + 1}/{max_retries})...")
                time.sleep(2)
            else:
                print(f"總結失敗 (HTTP {e.code})，將使用原始文本...")
                return content
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"總結失敗 ({e})，正在重試 ({attempt + 1}/{max_retries})...")
                time.sleep(2)
            else:
                print(f"總結失敗 ({e})，將使用原始文本...")
                return content


_ELECTIVE_DOMAINS: dict[str, list[str]] = {
    "資訊系統設計": [
        "Unix程式設計", "物件導向軟體設計", "作業系統實務", "平行程式設計", "編譯器設計",
        "通訊系統", "嵌入式軟體設計", "資料庫設計", "校外實習", "軟硬體協同設計", "雲端系統",
    ],
    "資訊應用技術": [
        "訊號與系統", "資料庫系統設計", "網頁程式設計", "計算機圖學", "多媒體資訊概論",
        "生物統計", "人工智慧", "校外實習", "樣型識別", "資料庫設計", "生醫資訊概論",
        "機器學習", "雲端系統", "機器學習與其醫學應用", "深度學習與Python實作", "醫學影像處理",
    ],
    "計算機網路技術": [
        "資料庫系統設計", "網頁程式設計", "網路應用軟體設計", "Unix程式設計", "通訊系統",
        "平行程式設計", "校外實習", "高等計算機網路", "物聯網", "網路安全與管理", "雲端系統",
    ],
    "人工智慧": [
        "智慧感測與識別", "大數據應用", "人工智慧應用於工業4.0", "深度學習概論",
        "人工智慧專題", "自然語言技術與實作", "Unix程式設計",
    ],
}

_MULTI_ELECTIVE_DOMAINS: dict[str, list[str]] = {
    "人文藝術": [
        "現代詩", "詩詞選讀", "中文寫作", "古典短篇小說選讀", "張愛玲小說選讀", "文學與人生",
        "紅樓夢之詩詞品賞", "臺灣古典文學選讀", "史學名著選讀", "自然生態文學", "音樂與文化",
        "電影與音樂", "室內樂作品欣賞與實習", "殿堂之外：現代音樂與流行樂文化", "交響樂的世界",
        "弦樂欣賞", "音樂與情緒", "基礎素描", "中國繪畫入門", "電影與醫學的對話",
        "道家思想導論與導讀", "聖經與科學", "英美小說選讀", "商務英語溝通", "新聞英語",
        "國際時事閱讀與會話", "英文簡報技巧", "旅遊英文", "學術英文寫作", "職場英語檢定",
        "環境與生態", "雅思閱讀與寫作", "美國大眾文化與世界", "托福英文", "社會秩序與犯罪",
        "地域與文化", "思考與爭辯的藝術",
        "日文（1）", "日文（2）", "日文（3）", "德文（1）", "德文（2）", "德文（3）",
        "初階法文", "進階法文",
    ],
    "社會科學": [
        "中國近、現代史", "歷史與人物", "醫療人權與案例討論", "1960 年代專題",
        "經典閱讀:<羅爾斯正義論>", "醫療與社會", "多元文化、媒體與社會", "賽局理論",
        "危機調適理論與實務", "科技倫理", "醫學、疾病與現代東亞社會", "傳染病史概論",
        "影像中的社會與文化", "管理經濟學", "財務管理與分析", "個人理財與投資", "法律與生活",
        "網路社會學", "兩性關係", "親職教育", "自我探索", "人際溝通", "特殊教育導論",
        "生涯發展與規劃", "認識銀髮族", "瘟癘瘴蠱", "生命的科學", "生物科技產業之認識與分析",
        "全球化思維的領導與決策", "媒體素養", "企業組織與工作倫理", "智慧財產權",
        "團體動力在人際互動的應用", "溝通技巧與領導統御", "情境模擬的創新法則",
    ],
    "自然科學": [
        "營養與保健", "生命科學導論", "針灸學概論", "草藥的認識與應用", "中藥的養生保健與美容",
        "中醫概論", "人類性學概論", "生物技術概論", "自然科技與永續發展應用",
        "生物技術及生物資訊之發展現況", "趣味數學與量子計算", "大數據之旅-挑戰Kaggle",
        "網頁設計美學", "部落格數位生活", "程式寫作邏輯導論", "資料處理與應用",
        "借力使力人工智慧", "舉一反三人工智慧", "生命科學與工程", "生醫材料概論",
        "新興能源技術概論", "能源需求與供給的全球衝擊",
    ],
    "運算思維": [
        "人工智慧概論", "程式語言及其醫學應用", "健康應用之程式語言",
        "R 程式語言入門", "Python 程式語言", "Python 入門與實作", "Python 程式入門",
    ],
    "跨域學習與實踐": [
        "從急救案例到人生省思", "量子諧振設計", "創新設計思考", "創業知能", "能源與文明永續",
        "環境變遷與永續發展策略", "環境教育與永續發展", "創新、創意、創業開發課程",
        "正念減壓與情緒管理", "西洋文學與醫學-經典選讀",
        "服務學習與營隊活動導論（一）USR 與 SDGs", "服務學習與營隊活動導論（二）活動規劃",
        "服務學習與營隊活動導論（三）企劃撰寫與文宣", "服務學習與營隊活動導論（四）法律與團隊經營",
        "服務學習與營隊活動導論（五）口語表達與溝通技巧", "服務學習與營隊活動導論（六）影像紀錄",
        "服務學習與營隊活動導論（七）簡報實戰技巧", "臺灣戰後經濟發展與台塑企業的成長",
        "環境、社會、治理－台塑企業的實踐", "田野調查：跨領域應用與實作",
        "團體領導實作課程(1)", "團體領導實作課程(2)",
        "利他行為與生死迷思（1）：基礎知識", "利他行為與生死迷思（2）：場域實作",
        "大學與社區連結", "龜山區稻米產業多元化-清酒釀造", "龜山區稻米產業多元化-清酒釀造(場域)",
        "身心障礙者的社區共融與永續", "身心障礙者的社區共融與永續之場域實作",
        "長庚大學周遭的自然生態與生態調查方法", "長庚大學周遭人文景觀踏查與林地生態調查",
        "食安永續桃竹苗無抗生素雞場之共創實踐", "無抗生素雞場之場域實作",
    ],
}

_CORE_DOMAINS: dict[str, list[str]] = {
    "藝術與人文思維": [
        "文學中的現代軌跡", "傳記文學選讀及寫作", "現代詩與當代文化", "寓言-經典與多元思維",
        "臺灣詩．鄉土情", "哲學與文化", "音樂的語言", "歌劇與歌劇院",
        "倫理與美學：電影與戲劇中的莎士比亞", "西洋文學概論: 古代作品選讀", "歐美戲劇",
        "北美小說、技藝與性別角色",
    ],
    "公民與社會探究": [
        "政治學與現代公民", "法學緒論", "現代公民的社會學想像", "經濟學與現代社會",
        "近代東亞的歷史變遷與發展", "社會心理學", "科技法律", "自由主義",
        "全球與兩岸政治經濟", "管理與現代社會", "腦與認知",
    ],
}


def _extract_domains_from_index(index: dict) -> dict:
    """Parse rules_index.json into domain maps and credit thresholds."""
    result: dict = {
        "elective_domains": {},
        "elective_min_credits": 30,
        "elective_min_domain_credits": 12,
        "elective_required_domains": 2,
        "multi_elective_domains": {},
        "multi_elective_min_credits": 11,
        "multi_elective_min_domains": 3,
        "core_domains": {},
        "core_min_credits": 12,
        "total_required_credits": 128,
    }

    # 系選修
    sys_el = index.get("系選修", {})
    if isinstance(sys_el, dict):
        result["elective_min_credits"] = int(sys_el.get("最低學分", 30))
        regulation = str(sys_el.get("規定", ""))
        m = re.search(r"每領域至少(\d+)學分", regulation)
        if m:
            result["elective_min_domain_credits"] = int(m.group(1))
        m = re.search(r"[二2]個?領域", regulation)
        if m:
            result["elective_required_domains"] = 2
        domain_map = sys_el.get("領域課程對照", {})
        if isinstance(domain_map, dict):
            for domain, courses in domain_map.items():
                if isinstance(courses, list):
                    clean = [re.sub(r"\([^)]*\)$", "", c).strip().rstrip("*") for c in courses]
                    result["elective_domains"][domain] = [c for c in clean if c]

    # 多元選修
    multi = index.get("多元選修課程", {})
    if isinstance(multi, dict):
        result["multi_elective_min_credits"] = int(multi.get("學分要求", 11))
        min_d = str(multi.get("必選領域數", ""))
        m = re.search(r"(\d+)", min_d)
        if m:
            result["multi_elective_min_domains"] = int(m.group(1))
        for sub in multi.get("子領域", []):
            if not isinstance(sub, dict):
                continue
            dname = sub.get("領域名稱", "")
            if not dname:
                continue
            courses: list[str] = []
            for section in ("可選課程", "必選課程"):
                for c in sub.get(section, []):
                    n = c.get("名稱", "") if isinstance(c, dict) else str(c)
                    if n:
                        courses.append(n)
            result["multi_elective_domains"][dname] = courses

    # 核心課程
    core = index.get("核心課程", {})
    if isinstance(core, dict):
        result["core_min_credits"] = int(core.get("學分要求", 12))
        for sub in core.get("子領域", []):
            if not isinstance(sub, dict):
                continue
            dname = sub.get("領域名稱", "")
            if not dname:
                continue
            courses = []
            for c in sub.get("可選課程", []):
                n = c.get("名稱", "") if isinstance(c, dict) else str(c)
                if n:
                    courses.append(n)
            result["core_domains"][dname] = courses

    # 畢業總學分
    total = index.get("畢業總學分", {})
    if isinstance(total, dict) and "總學分" in total:
        result["total_required_credits"] = int(total["總學分"])

    return result


def _build_precomputed_context(
    records: list[dict[str, str]],
    rules_index: dict | None = None,
) -> str:
    """Pre-compute key analysis facts to inject into the LLM prompt."""
    # Domain maps and thresholds: prefer rules_index, fallback to hardcoded
    if rules_index:
        dyn = _extract_domains_from_index(rules_index)
        elective_domains = dyn["elective_domains"] or _ELECTIVE_DOMAINS
        elective_min_credits = dyn["elective_min_credits"]
        elective_min_domain_credits = dyn["elective_min_domain_credits"]
        elective_required_domains = dyn["elective_required_domains"]
        multi_elective_domains = dyn["multi_elective_domains"] or _MULTI_ELECTIVE_DOMAINS
        multi_elective_min_credits = dyn["multi_elective_min_credits"]
        multi_elective_min_domains = dyn["multi_elective_min_domains"]
        core_domains = dyn["core_domains"] or _CORE_DOMAINS
        core_min_credits = dyn["core_min_credits"]
        REQUIRED_TOTAL = float(dyn["total_required_credits"])
    else:
        elective_domains = _ELECTIVE_DOMAINS
        elective_min_credits = 30
        elective_min_domain_credits = 12
        elective_required_domains = 2
        multi_elective_domains = _MULTI_ELECTIVE_DOMAINS
        multi_elective_min_credits = 11
        multi_elective_min_domains = 3
        core_domains = _CORE_DOMAINS
        core_min_credits = 12
        REQUIRED_TOTAL = 128.0

    in_progress = [
        r for r in records
        if r.get("成績", "").strip() == "" and r.get("是否通過", "") == "False"
    ]
    english_intensive = [r for r in records if "英文專修學習" in r.get("課程名稱", "")]
    honors_courses = {
        "批判性思考：品德與幸福", "青年領袖論壇",
    }
    is_honors = len(english_intensive) > 0 or any(
        r.get("課程名稱", "") in honors_courses for r in records
    )

    system_electives_all = [
        r for r in records if r.get("課程類別", "") == "系定選修"
    ]
    domain_credits: dict[str, float] = {d: 0.0 for d in elective_domains}
    domain_credits_inprogress: dict[str, float] = {d: 0.0 for d in elective_domains}
    domain_courses: dict[str, list[str]] = {d: [] for d in elective_domains}
    assigned: set[str] = set()

    for r in system_electives_all:
        name = r.get("課程名稱", "").strip()
        credits = float(r.get("學分", 0) or 0)
        passed = r.get("是否通過", "") == "True"
        inprogress = r.get("成績", "").strip() == "" and r.get("是否通過", "") == "False"
        if not passed and not inprogress:
            continue
        matched_domains = [
            d for d, courses in elective_domains.items()
            if any(name == c or c in name or name in c for c in courses)
        ]
        for d in matched_domains:
            key = f"{d}:{name}"
            if key not in assigned:
                assigned.add(key)
                if passed:
                    domain_credits[d] += credits
                    domain_courses[d].append(f"{name}({credits:g})")
                else:
                    domain_credits_inprogress[d] += credits
                    domain_courses[d].append(f"{name}({credits:g},進行中)")

    domain_total = {d: domain_credits[d] + domain_credits_inprogress[d] for d in elective_domains}
    domains_over_12 = {d: c for d, c in domain_total.items() if c >= elective_min_domain_credits}
    domains_partial = {d: c for d, c in domain_total.items() if 0 < c < elective_min_domain_credits}
    domain_meets_requirement = len(domains_over_12) >= elective_required_domains

    multi_elective_passed = [
        r for r in records
        if r.get("是否通過", "") == "True"
        and r.get("課程名稱", "") not in {"英文專修學習"}
        and (
            r.get("課程類別", "") == "校定選修"
            or (
                r.get("課程類別", "") == "校定必修"
                and any(
                    r.get("課程名稱", "") == c or c in r.get("課程名稱", "")
                    for courses in list(core_domains.values()) + list(multi_elective_domains.values())
                    for c in courses
                )
            )
        )
    ]
    multi_domain_map: dict[str, list[tuple[str, float]]] = {}
    honors_courses_taken: set[str] = {
        r.get("課程名稱", "") for r in records
        if r.get("課程名稱", "") in {"批判性思考：品德與幸福", "青年領袖論壇"}
        and r.get("是否通過", "") == "True"
    }

    core_domain_map: dict[str, list[tuple[str, float]]] = {}
    for r in multi_elective_passed:
        name = r.get("課程名稱", "").strip()
        credits = float(r.get("學分", 0) or 0)
        for domain, courses in core_domains.items():
            if any(name == c or c in name or name in c for c in courses):
                core_domain_map.setdefault(domain, []).append((name, credits))
        for domain, courses in multi_elective_domains.items():
            if any(name == c or c in name or name in c for c in courses):
                multi_domain_map.setdefault(domain, []).append((name, credits))

    if "批判性思考：品德與幸福" in honors_courses_taken:
        core_domain_map.setdefault("藝術與人文思維", []).append(("批判性思考：品德與幸福(榮譽學程抵免)", 3.0))
    if "青年領袖論壇" in honors_courses_taken:
        multi_domain_map.setdefault("跨域學習與實踐(榮譽學程抵免)", []).append(("青年領袖論壇", 2.0))

    core_total = sum(c for courses in core_domain_map.values() for _, c in courses)
    multi_total = sum(c for courses in multi_domain_map.values() for _, c in courses)
    multi_domains_count = len(multi_domain_map)

    lines = ["【預計算分析（請直接採用，不需重新推斷）】"]

    lines.append(f"\n■ 學生身份：{'榮譽學程學生（有英文專修學習/批判性思考/青年領袖論壇修課記錄）' if is_honors else '一般學生'}")

    lines.append(f"\n■ 英文領域（通識）：")
    lines.append(f"  - 英文專修學習 共 {len(english_intensive)} 筆（其中{sum(1 for r in english_intensive if r.get('是否通過') == 'True')}筆已通過，{sum(1 for r in english_intensive if r.get('是否通過') == 'False' and r.get('成績','').strip() == '')}筆進行中）")
    if len(english_intensive) >= 6:
        lines.append("  - 結論：榮譽學程規定6次英文專修學習即完整抵免通識英文領域6學分。此規定確定，無需另行確認。基礎英文A/B不計入英文領域（已由英文專修學習取代）。")
    else:
        lines.append(f"  - 結論：目前只有 {len(english_intensive)} 次，榮譽學程要求6次，尚不足。")

    lines.append(f"\n■ 進行中課程（成績欄位空白，本學期尚未結束）：")
    if in_progress:
        for r in in_progress:
            lines.append(f"  - {r.get('學年學期','')} {r.get('課程名稱','')} {r.get('學分','')}學分（{r.get('課程類別','')}）")
        lines.append("  → 以上課程不應標記為「已完成」，應標記為「進行中」。總學分計算時，進行中課程視為「預期完成」並另行說明。")
    else:
        lines.append("  - 無進行中課程。")

    lines.append(f"\n■ 系定選修領域分析（規則：最低{elective_min_credits}學分，至少{elective_required_domains}個領域各≥{elective_min_domain_credits}學分，一門課只能計入一個領域）：")
    for d, courses in domain_courses.items():
        cr_done = domain_credits[d]
        cr_prog = domain_credits_inprogress[d]
        cr_total = cr_done + cr_prog
        if cr_total == 0:
            status = "✗ 未修"
        elif cr_total >= elective_min_domain_credits:
            status = f"✓ 達標(≥{elective_min_domain_credits})"
        else:
            status = "△ 部分"
        prog_note = f"（含進行中{cr_prog:g}）" if cr_prog > 0 else ""
        lines.append(f"  {d}：{cr_done:g}已通過 + {cr_prog:g}進行中 = {cr_total:g}學分{prog_note} {status}  |  {', '.join(courses) if courses else '無'}")
    if domain_meets_requirement:
        met = ", ".join(f"{d}({c:g}cr)" for d, c in domains_over_12.items())
        lines.append(f"  → 結論：本學期結束後預計滿足{elective_required_domains}領域≥{elective_min_domain_credits}學分要求（{met}）。但若進行中課程有任何未通過，需補修。")
    else:
        lines.append(f"  → 結論：本學期結束後預計只有{len(domains_over_12)}個領域達標，尚未滿足{elective_required_domains}領域≥{elective_min_domain_credits}學分要求。")
        if domains_partial:
            for d, c in domains_partial.items():
                lines.append(f"     {d} 僅 {c:g} 學分，差 {elective_min_domain_credits-c:g} 學分才達標。")

    lines.append(f"\n■ 通識核心課程（規則：{core_min_credits}學分，藝術與人文思維≥6含至少1門中文類，公民與社會探究≥6）：")
    for domain, courses in core_domain_map.items():
        total = sum(c for _, c in courses)
        lines.append(f"  {domain}：{total:g}學分  |  {', '.join(f'{n}({c:g})' for n,c in courses)}")
    if "批判性思考：品德與幸福" in honors_courses_taken:
        lines.append("  注意：批判性思考：品德與幸福依榮譽學程決議，已滿足「藝術與人文思維」中文類必修，無需另修。")
    lines.append(f"  核心課程目前合計：{core_total:g}學分（需{core_min_credits}學分）")

    lines.append(f"\n■ 通識多元選修（規則：{multi_elective_min_credits}學分，至少{multi_elective_min_domains}個領域）：")
    for domain, courses in multi_domain_map.items():
        total = sum(c for _, c in courses)
        lines.append(f"  {domain}：{total:g}學分  |  {', '.join(f'{n}({c:g})' for n,c in courses)}")
    lines.append(f"  多元選修目前合計：{multi_total:g}學分 / 已覆蓋 {multi_domains_count} 個領域（需{multi_elective_min_credits}學分、至少{multi_elective_min_domains}領域）")
    if multi_total < multi_elective_min_credits:
        lines.append(f"  → 結論：多元選修尚缺 {multi_elective_min_credits - multi_total:g} 學分。")
    elif multi_domains_count < multi_elective_min_domains:
        lines.append(f"  → 結論：多元選修學分足夠但領域數不足（需{multi_elective_min_domains}，目前{multi_domains_count}）。")
    else:
        lines.append(f"  → 結論：多元選修學分已達標。")

    # 計算實際學分
    passed_credits = sum(
        float(r.get("學分", 0) or 0)
        for r in records
        if r.get("是否通過", "") == "True"
    )
    inprogress_credits = sum(
        float(r.get("學分", 0) or 0)
        for r in in_progress
    )
    total_expected_credits = passed_credits + inprogress_credits
    REQUIRED_TOTAL = 128.0

    # 軟硬體專題(3) 狀態
    proj3_records = [r for r in records if "軟硬體專題(3)" in r.get("課程名稱", "")]
    proj3_taken = any(
        r.get("是否通過", "") == "True" or
        (r.get("成績", "").strip() == "" and r.get("是否通過", "") == "False")
        for r in proj3_records
    )

    lines.append("\n■ 軟硬體專題說明：")
    lines.append("  - 軟硬體專題(1)：0學分，已完成（無缺口）")
    lines.append("  - 軟硬體專題(2)：1學分，進行中（114-2，本學期結束後才算完成）")
    if proj3_taken:
        lines.append("  - 軟硬體專題(3)：已修或進行中（無缺口）")
    else:
        lines.append("  - 軟硬體專題(3)：1學分，尚未修習 → 確定缺口，需另行修習。")
        lines.append("    注意：「未通過可用(2)抵(3)」是重修規定（重修失敗才能抵），不等於修過(2)就能跳過(3)。")

    # 確認缺口總結
    confirmed_gaps: list[str] = []
    if not proj3_taken:
        confirmed_gaps.append("系定必修：軟硬體專題(3) 差1學分")
    if not domain_meets_requirement:
        worst = sorted(domains_partial.items(), key=lambda x: -x[1])
        best_partial = worst[0] if worst else None
        if best_partial:
            confirmed_gaps.append(f"系選修：第二達標領域不足，{best_partial[0]} 目前{best_partial[1]:g}學分差{12-best_partial[1]:g}學分")
    if multi_total < multi_elective_min_credits:
        confirmed_gaps.append(f"通識多元選修：差 {multi_elective_min_credits - multi_total:g} 學分")
    elif multi_domains_count < multi_elective_min_domains:
        confirmed_gaps.append(f"通識多元選修：學分足夠但領域數不足（需{multi_elective_min_domains}，目前{multi_domains_count}）")
    total_gap = REQUIRED_TOTAL - total_expected_credits
    credits_gap_note = f"目前 {total_expected_credits:g} / {REQUIRED_TOTAL:g}，差 {total_gap:g} 學分"
    if total_gap <= 0:
        credits_gap_note = f"總學分已達標（{total_expected_credits:g} ≥ {REQUIRED_TOTAL:g}），但仍有以下專項缺口"

    lines.append(f"\n■ 確認缺口總結（{credits_gap_note}）：")
    lines.append("  ★ 以下是確定的缺口，請全部列入 missing_items，不要放進 manual_review_items：")
    if confirmed_gaps:
        for g in confirmed_gaps:
            lines.append(f"  - {g}")
    else:
        lines.append("  - 無確定缺口（所有項目均已達標或進行中）")
    lines.append("  ★ manual_review_items 只放「資料不足無法判斷」的項目，例如英文畢業門檻校訂標準。")

    return "\n".join(lines)


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
    rules_index_path: Path | None = None,
):
    csv_text = csv_path.read_text(encoding="utf-8-sig") if csv_path.exists() else ""
    course_records = load_course_records(csv_path)

    # Load rules index first so precomputed context can use dynamic domain maps
    rules_index: dict = {}
    if rules_index_path and rules_index_path.exists():
        import json as _json
        rules_index = _json.loads(rules_index_path.read_text(encoding="utf-8"))

    precomputed_context = _build_precomputed_context(course_records, rules_index=rules_index or None)

    if rules_index:
        categories = {
            row.get("課程類別", "").strip()
            for row in course_records
        }
        categories.discard("")
        rules_section = query_rules(rules_index, categories)
        if debug:
            print(f"DEBUG: 使用規則索引模式，類別={categories}，索引 keys={list(rules_index.keys())}")
            print(f"DEBUG: 規則節錄字元數={len(rules_section)}")
    else:
        if rules_index_path:
            print("規則索引不存在，使用全文模式（高 token）。執行 --build-rules-index 可大幅降低 token 用量。")
        rules_text = []
        for p in rules_md_paths:
            content = p.read_text(encoding="utf-8")
            if len(content) > 10000:
                print(
                    f"檔案 {p.name} 長度為 {len(content)} 字元，自動啟動 LLM 摘要以避免 Token 過長..."
                )
                content = summarize_rule(content, model, base_url, api_key)
            rules_text.append(f"--- 規則文件：{p.name} ---\n{content}")
        rules_section = chr(10).join(rules_text)

    prompt = f"""請根據以下提供的修課紀錄 CSV 資料、修課統計摘要，以及多份畢業規則 Markdown 檔案，為這位學生進行畢業學分分析。

【要求】
1. 只能根據提供的 CSV 與 Markdown 規則判斷，不確定時請列在「需要人工確認」，不要亂猜。
2. 課程如果可抵免或只能部分採認，要列出原因與來源規則。
3. 對不同科系請動態判讀規則，不要帶有預設立場。
4. **「預計算分析」區塊中的結論請直接採用，不要與其相矛盾。** 預計算結論已根據課程清單與規則精確計算。
5. 進行中課程（預計算分析已列出）不算「已完成」，要在 requirements 中說明「含進行中X學分」。
6. **預計算分析中「確認缺口總結」列出的所有缺口，必須放進 missing_items，不能放進 manual_review_items。** manual_review_items 只放「資料不足、規則模糊、無法從 CSV 判斷」的情況（例如英文畢業門檻的具體分數）。確認缺口有明確規則依據，不是「需要人工確認」。
7. **必須**回傳一個純 JSON 物件，不要有任何其他 Markdown 文字或解釋。你唯一能輸出的只有符合下方格式的最終畢業學分分析 JSON，請務必嚴格遵守此 JSON 結構，絕對不要改變欄位名稱或新增其他欄位。
8. 輸出的 JSON 格式如下所示：

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
      "completed": "62學分（含進行中1學分）",
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

<預計算分析>
{precomputed_context}
</預計算分析>

<修課紀錄 CSV>
{csv_text}
</修課紀錄 CSV>

<修課統計摘要>
{summary}
</修課統計摘要>

<畢業規則>
{rules_section}
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
        mode = "索引模式" if (rules_index_path and rules_index_path.exists()) else "全文模式"
        print(
            f"DEBUG: CSV 字元數={len(csv_text)}，規則字元數={len(rules_section)}，模式={mode}，模型={model}"
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
                try:
                    err_json = json.loads(error_body)
                    err_code = err_json.get("code", "")
                except Exception:
                    err_code = ""

                if err_code == "INSUFFICIENT_BALANCE":
                    print(
                        f"LLM 呼叫失敗：帳戶餘額不足（INSUFFICIENT_BALANCE）。"
                        f"請至 {base_url.split('/v1')[0]} 加值後再試。",
                        file=sys.stderr,
                    )
                else:
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
