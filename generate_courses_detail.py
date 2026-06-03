#!/usr/bin/env python3
"""Generate courses_detail.csv from CGU MOOCS by browser automation or an exported course list,
and analyze graduation rules using an LLM.

Install dependencies:
  python3 -m venv venv
  source venv/bin/activate
  python3 -m pip install -r requirements.txt
  python3 -m playwright install chromium

Usage:
  python3 generate_courses_detail.py --scrape-moocs --moocs-user bxxxxxxx \\
    --rules 畢業學分.pdf \\
    --analyze \\
    --llm-api-key "$MINNIMAX_API_KEY"
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from lib.catalog import generate, write_details
from lib.indexer import build_rules_index
from lib.llm import call_llm_analyzer
from lib.report import summarize_courses
from lib.rules import convert_rule_pdfs
from lib.scraper import scrape_moocs_courses, scrape_moocs_grades
from lib.utils import get_llm_api_key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate courses_detail.csv from CGU MOOCS or a course list and analyze graduation rules."
    )
    parser.add_argument(
        "--scrape-moocs",
        action="store_true",
        help="Login to MOOCS, scrape course list, then query catalog API.",
    )
    parser.add_argument(
        "--moocs-user",
        default=os.getenv("CGU_MOOCS_USER"),
        help="MOOCS username. Can also use CGU_MOOCS_USER.",
    )
    parser.add_argument(
        "--moocs-password",
        default=os.getenv("CGU_MOOCS_PASSWORD"),
        help="MOOCS password. Can also use CGU_MOOCS_PASSWORD.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show browser window while scraping MOOCS.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=30,
        help="Maximum MOOCS course pages to scrape. Default: 30",
    )
    parser.add_argument(
        "--output-dir",
        default="generated",
        help="Directory for generated files. Default: generated",
    )
    parser.add_argument(
        "--save-moocs",
        default=None,
        help="Where to save scraped MOOCS list. Default: <output-dir>/moocs_courses.txt. Use empty string to skip.",
    )
    parser.add_argument(
        "--save-taken-courses",
        default=None,
        help="Where to save grade records CSV. Default: <output-dir>/taken_courses.csv. Use empty string to skip.",
    )

    parser.add_argument(
        "--input",
        "-i",
        default="moocs_courses.txt",
        help="Input file path for non-scrape mode. Default: moocs_courses.txt",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output CSV path. Default: <output-dir>/courses_detail.csv",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Seconds to wait between catalog API calls. Default: 0.1",
    )
    parser.add_argument(
        "--term-map", help="JSON string or file path for missing term IDs map."
    )

    parser.add_argument(
        "--rules", nargs="+", help="Paths to graduation rule PDF files."
    )
    parser.add_argument(
        "--convert-rules-only",
        action="store_true",
        help="Only convert PDF rules to Markdown and exit.",
    )
    parser.add_argument(
        "--build-rules-index",
        action="store_true",
        help="Extract structured rules from PDFs into rules_index.json, then exit.",
    )
    parser.add_argument(
        "--merge-rules-index",
        action="store_true",
        help="When building the index, merge into existing rules_index.json instead of overwriting. Useful for updating only 畢業學分.pdf without re-extracting shared PDFs.",
    )
    parser.add_argument(
        "--rules-index",
        default=None,
        help="Path to rules_index.json. Default: <output-dir>/rules_index.json",
    )

    parser.add_argument(
        "--analyze", action="store_true", help="Run LLM graduation rules analysis."
    )
    parser.add_argument(
        "--llm-base-url",
        default=os.getenv("LLM_BASE_URL", "https://minnimax.chat/v1"),
        help="LLM API base URL. Can also use LLM_BASE_URL.",
    )
    parser.add_argument(
        "--llm-api-key",
        default=get_llm_api_key(),
        help="LLM API key. Can also use MINIMAX_API_KEY or MINNIMAX_API_KEY.",
    )
    parser.add_argument(
        "--llm-model",
        default=os.getenv("LLM_MODEL", "MiniMax-M2.7"),
        help="LLM model name. Can also use LLM_MODEL.",
    )
    parser.add_argument(
        "--report-output",
        default=None,
        help="Path to save the generated Markdown report. Default: <output-dir>/graduation_report.md",
    )
    parser.add_argument(
        "--report-json-output",
        default=None,
        help="Path to save the generated JSON. Default: <output-dir>/graduation_report.json",
    )
    parser.add_argument(
        "--raw-report-output",
        default=None,
        help="Path to save raw LLM output if JSON parsing fails. Default: <output-dir>/graduation_report.raw.txt",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print extra non-sensitive diagnostic information.",
    )
    parser.add_argument(
        "--reuse-existing-csv",
        action="store_true",
        help="Allow --analyze to reuse an existing output CSV instead of regenerating it.",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = (
        Path(args.output) if args.output else output_dir / "courses_detail.csv"
    )
    save_moocs_path = (
        None
        if args.save_moocs == ""
        else (
            Path(args.save_moocs)
            if args.save_moocs
            else output_dir / "moocs_courses.txt"
        )
    )
    save_taken_courses_path = (
        None
        if args.save_taken_courses == ""
        else (
            Path(args.save_taken_courses)
            if args.save_taken_courses
            else output_dir / "taken_courses.csv"
        )
    )
    rules_md_dir = output_dir / "rules_md"
    rules_index_path = (
        Path(args.rules_index) if args.rules_index else output_dir / "rules_index.json"
    )
    report_md_path = (
        Path(args.report_output)
        if args.report_output
        else output_dir / "graduation_report.md"
    )
    report_json_path = (
        Path(args.report_json_output)
        if args.report_json_output
        else output_dir / "graduation_report.json"
    )
    report_raw_path = (
        Path(args.raw_report_output)
        if args.raw_report_output
        else output_dir / "graduation_report.raw.txt"
    )

    term_map = None
    if args.term_map:
        try:
            if args.term_map.startswith("{"):
                term_map = json.loads(args.term_map)
            else:
                term_map = json.loads(Path(args.term_map).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"解析 --term-map 失敗：{e}", file=sys.stderr)
            return 1

    if args.reuse_existing_csv and not output_path.exists():
        print(
            f"指定要重用的 CSV 不存在：{output_path}。請改用存在的 --output 路徑，或移除 --reuse-existing-csv 讓程式重新產生。",
            file=sys.stderr,
        )
        return 1

    # Only convert rules
    if args.convert_rules_only:
        if not args.rules:
            print("請提供 --rules <pdf_paths>", file=sys.stderr)
            return 1
        rule_paths = [Path(p) for p in args.rules]
        convert_rule_pdfs(rule_paths, rules_md_dir)
        return 0

    # Build rules index
    if args.build_rules_index:
        if not args.llm_api_key:
            print(
                "請提供 --llm-api-key 或設定 MINIMAX_API_KEY / MINNIMAX_API_KEY 環境變數。",
                file=sys.stderr,
            )
            return 1
        md_paths: list[Path] = []
        if args.rules:
            rule_paths = [Path(p) for p in args.rules]
            print(f"開始轉換 {len(rule_paths)} 份規則 PDF...", flush=True)
            md_paths = convert_rule_pdfs(rule_paths, rules_md_dir)
        elif rules_md_dir.exists():
            md_paths = list(rules_md_dir.glob("*.md"))
        if not md_paths:
            print("找不到規則 Markdown 檔案。請提供 --rules <pdf_paths>。", file=sys.stderr)
            return 1
        build_rules_index(
            md_paths, rules_index_path, args.llm_model, args.llm_base_url, args.llm_api_key,
            merge=args.merge_rules_index,
        )
        return 0

    generated_course_details = False
    reused_existing_csv = False

    try:
        if args.scrape_moocs:
            username = args.moocs_user or input("MOOCS 帳號：").strip()
            password = args.moocs_password or getpass.getpass("MOOCS 密碼：")
            courses = scrape_moocs_courses(
                username,
                password,
                headless=not args.headful,
                max_pages=args.max_pages,
                save_path=save_moocs_path,
                debug=args.debug,
            )
            grades = None
            try:
                print("開始抓取 MOOCS 成績記錄...", flush=True)
                grades = scrape_moocs_grades(
                    username,
                    password,
                    headless=not args.headful,
                    save_path=save_taken_courses_path,
                    debug=args.debug,
                )
            except Exception as e:
                print(f"警告：無法抓取成績記錄 ({e})，將略過成績欄位。", file=sys.stderr)
            count, total_credits, errors = write_details(
                courses, output_path, args.delay, term_map, grades
            )
            generated_course_details = True
        elif args.analyze and output_path.exists():
            if not args.reuse_existing_csv and args.output is None:
                print(
                    f"偵測到既有修課資料 {output_path}。若要重用它進行分析，請加上 --reuse-existing-csv；若要重新產生，請使用 --scrape-moocs 或指定 --input。",
                    file=sys.stderr,
                )
                return 1
            reused_existing_csv = True
            print(f"重用既有修課資料：{output_path}")
        else:
            count, total_credits, errors = generate(
                Path(args.input), output_path, args.delay, term_map
            )
            generated_course_details = True
            print(f"\n輸出：{output_path}")
            print(f"成功：{count} 門，總學分：{total_credits:g}")
            if errors:
                print("\n以下課程查詢失敗：", file=sys.stderr)
                for error in errors:
                    print(f"- {error}", file=sys.stderr)
    except Exception as exc:
        print(f"執行失敗：{exc}", file=sys.stderr)
        return 1

    if args.analyze:
        if not args.llm_api_key:
            print(
                "請提供 --llm-api-key 或設定 MINIMAX_API_KEY / MINNIMAX_API_KEY 環境變數。",
                file=sys.stderr,
            )
            return 1

        rules_md_paths = []
        if args.rules:
            rule_paths = [Path(p) for p in args.rules]
            print(f"開始轉換 {len(rule_paths)} 份規則 PDF...", flush=True)
            rules_md_paths = convert_rule_pdfs(rule_paths, rules_md_dir)
        else:
            print(
                "未提供 --rules，LLM 分析將只基於現有的 rules_md/*.md (如果有) 或無規則分析。"
            )
            if rules_md_dir.exists():
                rules_md_paths = list(rules_md_dir.glob("*.md"))

        source = (
            "重用既有 CSV"
            if reused_existing_csv
            else "本次產生 CSV" if generated_course_details else "指定 CSV"
        )
        print(f"使用修課資料：{output_path}")
        print(f"資料來源：{source}")
        print("開始彙整修課摘要...", flush=True)
        summary = summarize_courses(output_path)
        try:
            print("開始進行畢業規則分析...", flush=True)
            call_llm_analyzer(
                csv_path=output_path,
                rules_md_paths=rules_md_paths,
                summary=summary,
                base_url=args.llm_base_url,
                api_key=args.llm_api_key,
                model=args.llm_model,
                report_md_path=report_md_path,
                report_json_path=report_json_path,
                report_raw_path=report_raw_path,
                debug=args.debug,
                rules_index_path=rules_index_path,
            )
        except Exception as e:
            print(f"分析過程發生錯誤：{e}", file=sys.stderr)
            return 1

    print("\n完成。")
    generated_files = [output_path]
    if save_moocs_path and save_moocs_path.exists():
        generated_files.insert(0, save_moocs_path)
    if save_taken_courses_path and save_taken_courses_path.exists():
        generated_files.insert(1, save_taken_courses_path)
    if args.analyze:
        generated_files.extend([report_json_path, report_md_path])
        if report_raw_path.exists():
            generated_files.append(report_raw_path)
    print("產生/使用檔案：")
    for path in generated_files:
        if path.exists():
            print(f"- {path}")
    if args.analyze and report_md_path.exists():
        print(f"下一步：請打開 {report_md_path} 查看結果。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
