from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

from lib.utils import resolve_existing_path


def convert_rule_pdfs(rule_paths: list[Path], output_dir: Path) -> list[Path]:
    try:
        import pymupdf4llm
    except ImportError:
        pymupdf4llm = None
    try:
        import fitz
    except ImportError:
        fitz = None

    if pymupdf4llm is None and fitz is None:
        raise RuntimeError(
            "需要安裝 PDF 轉換套件：python3 -m pip install pymupdf pymupdf4llm"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    generated_mds = []

    for pdf_path in rule_paths:
        resolved_pdf_path = resolve_existing_path(pdf_path)
        if resolved_pdf_path is None:
            print(f"警告：找不到規則檔案 {pdf_path}", file=sys.stderr)
            continue

        if resolved_pdf_path != pdf_path:
            print(f"警告：改用同名規則檔 {resolved_pdf_path} 代替 {pdf_path}")

        pdf_path = resolved_pdf_path

        md_path = output_dir / f"{pdf_path.stem}.md"
        generated_mds.append(md_path)

        header = f"<!-- Source: {pdf_path.name} | Generated: {datetime.now().isoformat()} -->\n\n"

        print(f"正在轉換規則檔：{pdf_path.name}", flush=True)

        try:
            started_at = time.time()
            if pymupdf4llm is not None:
                md_text = pymupdf4llm.to_markdown(str(pdf_path))
                md_path.write_text(header + md_text, encoding="utf-8")
                print(f"成功將 {pdf_path.name} 轉換為 {md_path.name} (pymupdf4llm)")
            elif fitz is not None:
                doc = fitz.open(pdf_path)
                text_blocks = []
                for page in doc:
                    text_blocks.append(page.get_text("text"))
                doc.close()
                md_path.write_text(header + "\n\n".join(text_blocks), encoding="utf-8")
                print(
                    f"成功將 {pdf_path.name} 轉換為 {md_path.name} (pymupdf/fitz 基礎文字萃取)"
                )
            print(
                f"{pdf_path.name} 轉換完成，耗時 {time.time() - started_at:.1f} 秒",
                flush=True,
            )
        except Exception as e:
            print(f"轉換 {pdf_path.name} 失敗：{e}", file=sys.stderr)

    return generated_mds
