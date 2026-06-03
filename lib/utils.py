from __future__ import annotations

import os
import re
from pathlib import Path


def normalize_key(key: str) -> str:
    return key.lstrip("﻿").strip()


def mask_identifier(value: str) -> str:
    if not value:
        return "已遮蔽"
    if len(value) <= 4:
        return "****"
    return f"{value[:3]}{'*' * max(4, len(value) - 3)}"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def resolve_existing_path(path: Path) -> Path | None:
    if path.exists():
        return path


def get_llm_api_key() -> str | None:
    return (
        os.getenv("CGU_LLM_API_KEY")
        or os.getenv("MINIMAX_API_KEY")
        or os.getenv("MINNIMAX_API_KEY")
    )


def markdown_cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def strip_thinking_tags(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def extract_json(content: str) -> str:
    content = strip_thinking_tags(content)
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()
