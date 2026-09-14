from __future__ import annotations

import json
from pathlib import Path

from .models import LooseEnd, PipelineResult


def _section_item(item: LooseEnd) -> list[str]:
    due = item.due_date.isoformat() if item.due_date else "needs review" if item.due_dates else "not found"
    lines = [f"### {item.title}", "", f"- ID: `{item.id}`", f"- Due: {due}", f"- Confidence: {item.confidence:.0%}"]
    if item.judgment_reasons:
        lines.append("- Why review is needed: " + " ".join(item.judgment_reasons))
    lines.extend(["- Next action: " + item.actions[0], "- Evidence:"])
    lines.extend(f"  - {evidence}" for evidence in item.evidence)
    lines.append("- Sources: " + ", ".join(f"`{source}`" for source in item.sources))
    lines.append("")
    return lines


def markdown_report(result: PipelineResult) -> str:
    sdk = f"available ({result.strands_version})" if result.strands_available else "not available"
    lines = [
        "# Loose Ends Report", "", f"As of: {result.as_of.isoformat()}", "",
        f"Scanned {result.files_scanned} files and {result.source_items} source items. "
        f"Found {len(result.loose_ends)} loose ends: {len(result.judgment_items)} need human judgment and "
        f"{len(result.ready_items)} have ready actions.", "", f"Strands SDK: {sdk}; no model was invoked.", "",
        "## Needs Human Judgment", "",
    ]
    if not result.judgment_items:
        lines.extend(["Nothing requires human judgment.", ""])
    for item in result.judgment_items:
        lines.extend(_section_item(item))
    lines.extend(["## Ready Actions", ""])
    if not result.ready_items:
        lines.extend(["No ready actions found.", ""])
    for item in result.ready_items:
        lines.extend(_section_item(item))
    return "\n".join(lines).rstrip() + "\n"


def write_reports(result: PipelineResult, markdown_path: Path, json_path: Path) -> None:
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown_report(result), encoding="utf-8")
    json_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")

