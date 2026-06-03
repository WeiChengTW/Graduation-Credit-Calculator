from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

from lib.utils import ensure_parent, mask_identifier, normalize_key

MOOCS_LOGIN_URL = "https://moocs.cgu.edu.tw/learn/index.php"
MOOCS_COURSES_URL = "https://moocs.cgu.edu.tw/learn/mycourse/index.php"
MOOCS_GRADES_URL = "https://moocs.cgu.edu.tw/learn/co_student_record.php"

_GRADE_COURSE_RE = re.compile(r"^(\d{3})([123])-(.+)$")

TEXT_COURSE_RE = re.compile(
    r"^\s*(?:\d+\.\s*)?(?P<year>\d{3})-(?P<term>[123])-(?P<name>.+)-(?P<section>[A-Z]?\d+)(?:-.+)?\s*$"
)

SUBMIT_PAGE_JS = """
(targetPage) => {
  const form = document.querySelector('form#actFm') || Array.from(document.forms).find(f => f.page);
  if (!form || !form.page) return false;
  form.page.value = String(targetPage);
  form.submit();
  return true;
}
"""

USERNAME_SELECTORS = [
    'input[name="username"]',
    'input[name="user"]',
    'input[name="account"]',
    'input[name="login"]',
    'input[id*="user" i]',
    'input[id*="account" i]',
    'input[type="text"]',
    'input[type="email"]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[name="passwd"]',
    'input[name="pwd"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("登入")',
    'input[value*="登入"]',
    'button:has-text("Login")',
    'input[value*="Login"]',
]


def parse_course_text(text: str) -> dict[str, str] | None:
    if "E-Learning" in text or "操作說明" in text:
        return None
    match = TEXT_COURSE_RE.match(text)
    if not match:
        return None
    return {
        "year": match.group("year"),
        "term": match.group("term"),
        "name": match.group("name"),
        "sectionid": match.group("section"),
    }


def read_csv_courses(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []
        rows = []
        for row in reader:
            fixed = {
                normalize_key(k): (v or "").strip()
                for k, v in row.items()
                if k is not None
            }
            section = (
                fixed.get("開課序號")
                or fixed.get("sectionid")
                or fixed.get("SECTIONID")
            )
            if not section:
                continue
            rows.append(
                {
                    "year": fixed.get("學年", ""),
                    "term": fixed.get("學期", ""),
                    "name": fixed.get("課程名稱", ""),
                    "sectionid": section,
                }
            )
        return rows


def read_text_courses(path: Path) -> list[dict[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        course = parse_course_text(line)
        if course:
            rows.append(course)
    return rows


def read_courses(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".csv":
        rows = read_csv_courses(path)
        if rows:
            return rows
        raise ValueError(
            f"{path} 缺少開課序號欄位，無法準確查詢。請確保 CSV 有『開課序號』欄位。"
        )
    return read_text_courses(path)


def locate_first(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            pass
    for frame in page.frames:
        for selector in selectors:
            locator = frame.locator(selector).first
            try:
                if locator.count() > 0:
                    return locator
            except Exception:
                pass
    return None


def extract_course_texts(page) -> list[str]:
    try:
        page.locator("table tr td:first-child").first.wait_for(
            state="attached", timeout=10000
        )
    except Exception:
        pass

    EXTRACT_COURSES_JS = """
    () => {
      const rows = [];
      const anchors = Array.from(document.querySelectorAll('table tr td:first-child a'));
      if (anchors.length > 0) {
        for (const a of anchors) {
          const t = (a.textContent || '').trim();
          if (!t) continue;
          if (/^課程名稱[:：]?$/i.test(t)) continue;
          rows.push(t);
        }
        return rows;
      }
      const tds = Array.from(document.querySelectorAll('table tr td:first-child'));
      for (const td of tds) {
        const t = (td.textContent || '').trim();
        if (!t) continue;
        if (/^課程名稱[:：]?$/i.test(t)) continue;
        if (/輸入課程名稱關鍵字|搜尋|報名說明/i.test(t)) continue;
        rows.push(t);
      }
      return rows;
    }
    """

    try:
        return page.evaluate(EXTRACT_COURSES_JS)
    except Exception as e:
        print(f"Extraction error: {e}")
        return []


def submit_course_page(page, target_page: int) -> bool:
    try:
        try:
            prev_texts = extract_course_texts(page)
        except Exception:
            prev_texts = []

        submitted = False
        try:
            if page.evaluate(SUBMIT_PAGE_JS, target_page):
                submitted = True
        except Exception:
            submitted = False

        if not submitted:
            for frame in page.frames:
                try:
                    if frame.evaluate(SUBMIT_PAGE_JS, target_page):
                        submitted = True
                        break
                except Exception:
                    pass

        if not submitted:
            return False

        waited = 0.0
        timeout = 10.0
        interval = 0.4
        while waited < timeout:
            try:
                page.wait_for_timeout(int(interval * 1000))
            except Exception:
                pass
            try:
                cur_texts = extract_course_texts(page)
                if cur_texts and cur_texts != prev_texts:
                    return True
            except Exception:
                pass
            waited += interval

        return True
    except Exception:
        return False


def scrape_moocs_courses(
    username: str,
    password: str,
    *,
    headless: bool,
    max_pages: int,
    save_path: Path | None,
    debug: bool = False,
) -> list[dict[str, str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "需要先安裝 Playwright：python3 -m pip install playwright && python3 -m playwright install chromium"
        ) from exc

    seen = set()
    courses = []
    raw_lines = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(MOOCS_LOGIN_URL, wait_until="domcontentloaded")

        username_input = locate_first(page, USERNAME_SELECTORS)
        password_input = locate_first(page, PASSWORD_SELECTORS)
        if username_input is None or password_input is None:
            browser.close()
            raise RuntimeError(
                "找不到 MOOCS 登入欄位；可改用 --headful 觀察頁面是否改版或有驗證碼。"
            )

        username_input.fill(username)
        password_input.fill(password)

        submit = locate_first(page, SUBMIT_SELECTORS)
        if submit is not None:
            submit.click()
        else:
            password_input.press("Enter")

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            page.wait_for_timeout(1500)

        page.goto(MOOCS_COURSES_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            page.wait_for_timeout(1500)

        try:
            locator = page.locator('a:has-text("全校課程")').first
            try:
                if locator.count() > 0:
                    locator.click()
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        page.wait_for_timeout(800)
            except Exception:
                pass
        except Exception:
            pass

        GET_TOTAL_PAGES_JS = """
        () => {
          if (typeof total_page === 'number' && total_page > 0) return total_page;
          const afterText = document.querySelector('.paginate-number-after');
          if (afterText) {
            const m = (afterText.textContent || '').match(/(\\d+)/);
            if (m) return parseInt(m[1], 10);
          }
          return null;
        }
        """

        detected_total = None
        try:
            detected = page.evaluate(GET_TOTAL_PAGES_JS)
            if isinstance(detected, (int, float)) and int(detected) > 0:
                detected_total = int(detected)
        except Exception:
            detected_total = None

        pages_to_scrape = (
            max_pages if max_pages and max_pages > 0 else (detected_total or 1)
        )
        if detected_total:
            pages_to_scrape = (
                min(max_pages, detected_total)
                if max_pages and max_pages > 0
                else detected_total
            )

        if not submit_course_page(page, 1):
            print("警告：無法跳回第 1 頁，從當前頁面開始抓取。", file=sys.stderr)
        else:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(700)

        for page_number in range(1, pages_to_scrape + 1):
            if page_number > 1:
                if not submit_course_page(page, page_number):
                    print(
                        f"警告：無法送出 MOOCS 第 {page_number} 頁分頁表單，停止翻頁。",
                        file=sys.stderr,
                    )
                    break
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                page.wait_for_timeout(700)

            texts = extract_course_texts(page)
            if not texts and page_number > 1:
                print(
                    f"警告：MOOCS 第 {page_number} 頁未抓到文字，重試一次。",
                    file=sys.stderr,
                )
                page.wait_for_timeout(1000)
                texts = extract_course_texts(page)

            fresh = 0
            parsed = 0
            for text in texts:
                course = parse_course_text(text)
                if course is None:
                    if debug and text.strip():
                        print(f"DEBUG: 忽略非課程項目：{text.strip()}")
                    continue
                parsed += 1
                key = (course["year"], course["term"], course["sectionid"])
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                courses.append(course)
                raw_lines.append(
                    f"{len(raw_lines) + 1}. {course['year']}-{course['term']}-{course['name']}-{course['sectionid']}"
                )

            print(f"MOOCS 第 {page_number} 頁：抓到 {parsed} 門，新增 {fresh} 門")
            if page_number > 1 and fresh == 0:
                print(
                    f"警告：第 {page_number} 頁沒有新增課程，可能已到最後一頁或分頁失敗。",
                    file=sys.stderr,
                )
                break

        browser.close()

    if not courses:
        raise RuntimeError(
            "沒有抓到任何 MOOCS 課程；請確認帳密正確，或用 --headful 檢查是否需要人工驗證。"
        )

    if save_path:
        ensure_parent(save_path)
        lines = [
            "# 長庚大學 MOOCS 課程清單",
            f"# 帳號：{mask_identifier(username)}",
            f"# 總計：{len(courses)} 門課程",
            "",
            *raw_lines,
        ]
        save_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"MOOCS 清單輸出：{save_path}")

    return courses


def _is_passed(score: str) -> bool:
    score = score.strip()
    if not score:
        return False
    if score == "S":
        return False
    if score in ("P", "通過", "及格"):
        return True
    try:
        return float(score) >= 60
    except ValueError:
        return False


def parse_grade_rows(lines: list[str]) -> list[dict[str, str]]:
    """Parse co_student_record.php innerText format.

    Each course appears as 6 lines:
        1142-物件導向軟體設計   ← YYYTT-name (year 3 digits + term 1 digit)
        \\t
        3                       ← credits
        \\t
        89                      ← grade (may be empty if not yet graded)
        (blank)
    """
    results = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _GRADE_COURSE_RE.match(line)
        if m:
            year = m.group(1)
            term = m.group(2)
            name = m.group(3)
            # skip \t, read credits, skip \t, read grade
            credits = lines[i + 2].strip() if i + 2 < len(lines) else ""
            grade = lines[i + 4].strip() if i + 4 < len(lines) else ""
            if re.match(r"^\d+\.?\d*$", credits):
                results.append({
                    "year": year,
                    "term": term,
                    "name": name,
                    "credits": credits,
                    "grade": grade,
                    "passed": "True" if _is_passed(grade) else "False",
                })
                i += 6
                continue
        i += 1
    return results


def scrape_moocs_grades(
    username: str,
    password: str,
    *,
    headless: bool,
    save_path: Path | None = None,
    debug: bool = False,
) -> list[dict[str, str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "需要先安裝 Playwright：python3 -m pip install playwright && python3 -m playwright install chromium"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(MOOCS_LOGIN_URL, wait_until="domcontentloaded")

        username_input = locate_first(page, USERNAME_SELECTORS)
        password_input = locate_first(page, PASSWORD_SELECTORS)
        if username_input is None or password_input is None:
            browser.close()
            raise RuntimeError("找不到 MOOCS 登入欄位。")

        username_input.fill(username)
        password_input.fill(password)
        submit = locate_first(page, SUBMIT_SELECTORS)
        if submit is not None:
            submit.click()
        else:
            password_input.press("Enter")

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            page.wait_for_timeout(1500)

        page.goto(MOOCS_GRADES_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            page.wait_for_timeout(2000)

        raw_text = page.evaluate("() => document.body.innerText")
        browser.close()

    raw_lines = raw_text.splitlines()
    if debug:
        print(f"DEBUG: grades page raw lines={len(raw_lines)}")

    records = parse_grade_rows(raw_lines)
    print(f"成績資料：解析到 {len(records)} 門課程")

    if save_path:
        ensure_parent(save_path)
        with save_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["學年", "學期", "課程名稱", "學分", "成績", "是否通過"])
            for r in records:
                writer.writerow([
                    r["year"], r["term"], r["name"],
                    r["credits"], r["grade"], r["passed"],
                ])
        print(f"成績資料輸出：{save_path}")

    return records
