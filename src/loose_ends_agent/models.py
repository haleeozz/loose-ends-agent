from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceItem:
    path: Path
    source_type: str
    text: str
    line_number: int | None = None
    supplied_due: date | None = None


@dataclass
class ObligationCandidate:
    title: str
    evidence: str
    source_path: str
    source_type: str
    line_number: int | None = None
    due_date: date | None = None
    confidence: float = 0.0
    decision_language: bool = False


@dataclass
class LooseEnd:
    id: str
    title: str
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    due_dates: list[date] = field(default_factory=list)
    due_date: date | None = None
    confidence: float = 0.0
    needs_human_judgment: bool = False
    judgment_reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["due_date"] = self.due_date.isoformat() if self.due_date else None
        payload["due_dates"] = [value.isoformat() for value in self.due_dates]
        return payload


@dataclass
class PipelineResult:
    as_of: date
    files_scanned: int
    source_items: int
    candidates: int
    loose_ends: list[LooseEnd]
    strands_available: bool
    strands_version: str | None

    @property
    def judgment_items(self) -> list[LooseEnd]:
        return [item for item in self.loose_ends if item.needs_human_judgment]

    @property
    def ready_items(self) -> list[LooseEnd]:
        return [item for item in self.loose_ends if not item.needs_human_judgment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "files_scanned": self.files_scanned,
            "source_items": self.source_items,
            "candidates": self.candidates,
            "strands": {
                "available": self.strands_available,
                "version": self.strands_version,
                "model_invoked": False,
            },
            "summary": {
                "loose_ends": len(self.loose_ends),
                "needs_human_judgment": len(self.judgment_items),
                "ready_actions": len(self.ready_items),
            },
            "loose_ends": [item.to_dict() for item in self.loose_ends],
        }

