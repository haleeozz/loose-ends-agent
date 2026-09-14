from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .models import SourceItem


SUPPORTED_SUFFIXES = {".md", ".txt", ".json"}


@dataclass(frozen=True)
class IngestResult:
    files_scanned: int
    items: list[SourceItem]


def _as_text(record: dict[str, Any]) -> str:
    for key in ("task", "title", "text", "name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            context = record.get("context")
            return f"{value.strip()} - {context.strip()}" if isinstance(context, str) and context.strip() else value.strip()
    return ""


def _as_due(record: dict[str, Any]) -> date | None:
    value = record.get("due") or record.get("due_date")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _is_complete(record: dict[str, Any]) -> bool:
    status = str(record.get("status", "")).strip().lower()
    return bool(record.get("completed")) or status in {"done", "complete", "completed", "cancelled", "canceled"}


def _read_json(path: Path) -> list[SourceItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        records = payload.get("todos", payload.get("tasks", []))
    else:
        records = payload
    if not isinstance(records, list):
        raise ValueError("JSON input must be a list or contain a 'todos'/'tasks' list")

    items: list[SourceItem] = []
    for index, record in enumerate(records, start=1):
        if isinstance(record, str):
            text, supplied_due = record.strip(), None
        elif isinstance(record, dict) and not _is_complete(record):
            text, supplied_due = _as_text(record), _as_due(record)
        else:
            continue
        if text:
            items.append(SourceItem(path, "todo", text, index, supplied_due))
    return items


def _read_text(path: Path) -> list[SourceItem]:
    parts = {part.lower() for part in path.parts}
    source_type = "note" if parts & {"note", "notes"} else "document"
    return [
        SourceItem(path, source_type, line.strip(), line_number)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip()
    ]


def ingest_directory(data_dir: Path) -> IngestResult:
    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory does not exist: {data_dir}")
    paths = sorted(path for path in data_dir.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES)
    items: list[SourceItem] = []
    for path in paths:
        try:
            items.extend(_read_json(path) if path.suffix.lower() == ".json" else _read_text(path))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Could not read {path}: {exc}") from exc
    return IngestResult(len(paths), items)
