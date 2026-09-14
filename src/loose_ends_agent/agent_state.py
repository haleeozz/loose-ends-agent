from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .models import ObligationCandidate
from .merge import candidate_similarity
ISO_DATE_CLAIM_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
GROUNDING_STOP_WORDS = {
    "ask", "high", "similarity", "pair", "score", "loose", "end", "item", "task",
    "whether", "remember", "waiting", "still", "works",
}


def _grounding_tokens(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if len(word) > 3 and word not in GROUNDING_STOP_WORDS and not word.isdigit()
    }


class ResolutionState(str, Enum):
    OBSERVED = "observed"
    READY_ACTION = "ready_action"
    HUMAN_DECISION_REQUIRED = "human_decision_required"
    HUMAN_DECISION_RECEIVED = "human_decision_received"


@dataclass
class EvidenceRecord:
    id: str
    title: str
    evidence: str
    source_path: str
    source_type: str
    line_number: int | None
    due_date: str | None
    confidence: float


@dataclass
class HumanDecision:
    question: str
    options: list[str]
    rationale: str
    response: Any | None = None


@dataclass
class AgentLooseEnd:
    id: str
    title: str
    evidence_ids: list[str]
    grouping_rationale: str
    state: ResolutionState = ResolutionState.OBSERVED
    next_action: str | None = None
    due_date: str | None = None
    decision_rationale: str | None = None
    human_decision: HumanDecision | None = None


@dataclass
class AgentEvent:
    sequence: int
    timestamp: str
    phase: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


class AgentWorkspace:
    """Authoritative local state shared by the Strands tools for one run."""

    def __init__(self, data_dir: Path, as_of: date, report_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.as_of = as_of
        self.report_dir = report_dir.resolve()
        self.evidence: dict[str, EvidenceRecord] = {}
        self.loose_ends: dict[str, AgentLooseEnd] = {}
        self.events: list[AgentEvent] = []
        self.inspected_source_types: set[str] = set()
        self.related_analysis_count = 0
        self.resume_count = 0
        self.focus_loose_end_ids: set[str] | None = None

    def log(self, phase: str, detail: str, **data: Any) -> None:
        self.events.append(
            AgentEvent(
                sequence=len(self.events) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                phase=phase,
                detail=detail,
                data=data,
            )
        )

    def add_evidence(self, candidate: ObligationCandidate) -> EvidenceRecord:
        source = Path(candidate.source_path).resolve()
        try:
            source_path = str(source.relative_to(self.data_dir.parent))
        except ValueError:
            source_path = str(source)
        key = f"{source}|{candidate.line_number}|{candidate.evidence}"
        evidence_id = "ev-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        record = EvidenceRecord(
            id=evidence_id,
            title=candidate.title,
            evidence=candidate.evidence,
            source_path=source_path,
            source_type=candidate.source_type,
            line_number=candidate.line_number,
            due_date=candidate.due_date.isoformat() if candidate.due_date else None,
            confidence=candidate.confidence,
        )
        self.evidence[evidence_id] = record
        return record

    def create_loose_end(self, title: str, evidence_ids: list[str], rationale: str) -> AgentLooseEnd:
        missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in self.evidence]
        if missing:
            raise ValueError(f"Unknown evidence IDs: {', '.join(missing)}")
        if not evidence_ids:
            raise ValueError("A loose end must preserve at least one evidence item")
        records = [self.evidence[evidence_id] for evidence_id in evidence_ids]
        if len(records) > 1:
            connected = {0}
            pending = set(range(1, len(records)))
            while pending:
                newly_connected = {
                    index
                    for index in pending
                    if any(candidate_similarity(records[index].title, records[known].title) >= 0.58 for known in connected)
                }
                if not newly_connected:
                    unrelated = ", ".join(records[index].id for index in sorted(pending))
                    raise ValueError("Evidence items are not meaningfully related to the proposed group: " + unrelated)
                connected.update(newly_connected)
                pending.difference_update(newly_connected)
        evidence_tokens = set().union(*(_grounding_tokens(record.title) for record in records))
        if not (_grounding_tokens(title) & evidence_tokens):
            raise ValueError("Loose-end title is not grounded in the selected evidence titles")
        supported_dates = {record.due_date for record in records if record.due_date}
        claimed_dates = set(ISO_DATE_CLAIM_RE.findall(title + " " + rationale))
        unsupported_dates = sorted(claimed_dates - supported_dates)
        if unsupported_dates:
            raise ValueError("Unsupported date claims in title or rationale: " + ", ".join(unsupported_dates))
        already_assigned = {value for item in self.loose_ends.values() for value in item.evidence_ids}
        omitted_related = []
        for other_id, other in self.evidence.items():
            if other_id in evidence_ids or other_id in already_assigned:
                continue
            if any(candidate_similarity(record.title, other.title) >= 0.74 for record in records):
                omitted_related.append(other_id)
        if omitted_related and "intentionally separate" not in rationale.lower():
            raise ValueError(
                "Likely related evidence was omitted: " + ", ".join(sorted(omitted_related)) + ". Include it, or explain "
                "why it is intentionally separate in grouping_rationale."
            )
        canonical = "|".join(sorted(set(evidence_ids)))
        loose_end_id = "le-agent-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
        existing = self.loose_ends.get(loose_end_id)
        if existing:
            return existing
        item = AgentLooseEnd(loose_end_id, title.strip(), list(dict.fromkeys(evidence_ids)), rationale.strip())
        self.loose_ends[loose_end_id] = item
        self.log("decide", "Created a loose end from selected evidence.", loose_end_id=loose_end_id, evidence_ids=evidence_ids)
        return item

    def snapshot(self) -> dict[str, Any]:
        def loose_end_payload(item: AgentLooseEnd) -> dict[str, Any]:
            payload = asdict(item)
            payload["state"] = item.state.value
            payload["provenance"] = [asdict(self.evidence[evidence_id]) for evidence_id in item.evidence_ids]
            return payload

        items = [loose_end_payload(item) for item in self.loose_ends.values()]
        return {
            "mode": "strands_agent",
            "as_of": self.as_of.isoformat(),
            "data_dir": str(self.data_dir),
            "summary": {
                "observed_evidence": len(self.evidence),
                "loose_ends": len(items),
                "ready_actions": sum(item["state"] == ResolutionState.READY_ACTION.value for item in items),
                "human_decisions_required": sum(
                    item["state"] == ResolutionState.HUMAN_DECISION_REQUIRED.value for item in items
                ),
                "inspected_source_types": sorted(self.inspected_source_types),
                "relation_analyses": self.related_analysis_count,
            },
            "loose_ends": items,
            "events": [asdict(event) for event in self.events],
        }

    def expected_source_types(self) -> set[str]:
        expected: set[str] = set()
        for path in self.data_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".json"}:
                continue
            parts = {part.lower() for part in path.parts}
            expected.add(
                "todo" if path.suffix.lower() == ".json" else "note" if parts & {"note", "notes"} else "document"
            )
        return expected

    def completion_issues(self, require_log: bool = False) -> list[str]:
        issues: list[str] = []
        if self.focus_loose_end_ids is not None:
            unresolved = sorted(
                item.id
                for item in self.loose_ends.values()
                if item.id in self.focus_loose_end_ids
                and item.state in {ResolutionState.OBSERVED, ResolutionState.HUMAN_DECISION_RECEIVED}
            )
            if unresolved:
                issues.append("focused loose ends without a ready action or pending escalation: " + ", ".join(unresolved))
            return issues
        missing_sources = sorted(self.expected_source_types() - self.inspected_source_types)
        if missing_sources:
            issues.append("uninspected source types: " + ", ".join(missing_sources))
        assigned = {evidence_id for item in self.loose_ends.values() for evidence_id in item.evidence_ids}
        unassigned = sorted(set(self.evidence) - assigned)
        if unassigned:
            issues.append("evidence not assigned to a loose end: " + ", ".join(unassigned))
        unresolved = sorted(
            item.id
            for item in self.loose_ends.values()
            if item.state in {ResolutionState.OBSERVED, ResolutionState.HUMAN_DECISION_RECEIVED}
        )
        if unresolved:
            issues.append("loose ends without a ready action or pending escalation: " + ", ".join(unresolved))
        if require_log and not any(event.detail == "Wrote the local action log." for event in self.events):
            issues.append("the action log has not been written")
        return issues
