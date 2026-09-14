from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from strands import tool
from strands.types.tools import ToolContext

from .agent_state import AgentWorkspace, HumanDecision, ISO_DATE_CLAIM_RE, ResolutionState, _grounding_tokens
from .extract import DECISION_RE, extract_candidates, parse_deadline
from .ingest import SUPPORTED_SUFFIXES, ingest_directory
from .merge import candidate_similarity


NEUTRAL_DECISION_TERMS = {
    "approve",
    "choose",
    "decline",
    "decide",
    "keep",
    "later",
    "proceed",
    "unresolved",
}
DECISION_CONTEXT_TERMS = NEUTRAL_DECISION_TERMS | {
    "action",
    "aligns",
    "approved",
    "appropriate",
    "available",
    "choice",
    "clarification",
    "conflict",
    "conflicting",
    "correct",
    "current",
    "deadline",
    "deadlines",
    "evidence",
    "explicitly",
    "grounded",
    "human",
    "happen",
    "includes",
    "needed",
    "needs",
    "option",
    "options",
    "proposed",
    "provenance",
    "required",
    "requires",
    "resolve",
    "resolved",
    "resolution",
    "should",
    "source",
    "step",
    "terms",
    "this",
    "judgment",
    "leaves",
    "what",
    "which",
}
DECISION_TOKEN_ALIASES = {
    "applies": "apply",
    "associated": "associate",
    "decision": "decide",
    "linked": "link",
    "proceeding": "proceed",
    "renewal": "renew",
    "required": "require",
    "requires": "require",
    "resolving": "resolve",
    "used": "use",
}
DECISION_CONTEXT_TERMS |= {
    "apply",
    "associate",
    "date",
    "determine",
    "identify",
    "link",
    "obligation",
    "precedence",
    "require",
    "take",
    "use",
}
SAFE_HUMAN_REVIEW_OPTIONS = ["Approve", "Decline", "Keep unresolved"]


class LooseEndsTools:
    """Stateful tools through which the Strands agent observes and acts."""

    def __init__(self, workspace: AgentWorkspace) -> None:
        self.workspace = workspace

    def all_tools(self) -> list[Any]:
        return [
            self.list_sources,
            self.scan_notes,
            self.scan_tasks,
            self.inspect_documents,
            self.find_deadlines,
            self.find_related_items,
            self.create_loose_end,
            self.mark_ready_action,
            self.request_human_decision,
            self.write_action_log,
        ]

    def _invalid_evidence_error(self, evidence_ids: list[str]) -> dict[str, Any] | None:
        invalid = []
        for evidence_id in evidence_ids:
            if evidence_id in self.workspace.evidence:
                continue
            reason = (
                "loose_end_id_not_evidence_id"
                if evidence_id in self.workspace.loose_ends
                else "unknown_evidence_id"
            )
            invalid.append({"id": evidence_id, "reason": reason})
        if not invalid:
            return None
        return {
            "ok": False,
            "error": {
                "code": "invalid_evidence_ids",
                "message": "Every evidence_ids value must be an existing evidence record ID.",
                "invalid": invalid,
            },
        }

    def _grounding_result(
        self,
        item: Any,
        text: str,
        *,
        neutral_terms: set[str] | None = None,
        token_aliases: dict[str, str] | None = None,
    ) -> tuple[bool, list[str]]:
        provenance_text = " ".join(
            [
                item.title,
                *(
                    self.workspace.evidence[evidence_id].title
                    + " "
                    + self.workspace.evidence[evidence_id].evidence
                    for evidence_id in item.evidence_ids
                ),
            ]
        )
        def normalize(tokens: set[str]) -> set[str]:
            return {(token_aliases or {}).get(token, token) for token in tokens}

        text_tokens = normalize(_grounding_tokens(text))
        substantive_tokens = text_tokens - normalize(neutral_terms or set())
        provenance_tokens = normalize(_grounding_tokens(provenance_text))
        supported_dates = {
            self.workspace.evidence[evidence_id].due_date
            for evidence_id in item.evidence_ids
            if self.workspace.evidence[evidence_id].due_date
        }
        claimed_dates = set(ISO_DATE_CLAIM_RE.findall(text))
        unsupported_dates = sorted(claimed_dates - supported_dates)
        unsupported_terms = sorted(substantive_tokens - provenance_tokens)
        grounded_terms = substantive_tokens & provenance_tokens
        grounded_ratio = len(grounded_terms) / len(substantive_tokens) if substantive_tokens else 1.0
        grounded = not unsupported_dates and grounded_ratio >= 0.5 and len(unsupported_terms) <= 2
        return grounded, unsupported_dates + unsupported_terms

    def _remaining_work(self) -> dict[str, Any]:
        assigned = {
            evidence_id
            for item in self.workspace.loose_ends.values()
            for evidence_id in item.evidence_ids
        }
        unresolved_states = {
            ResolutionState.OBSERVED,
            ResolutionState.HUMAN_DECISION_RECEIVED,
        }
        return {
            "unassigned_evidence": [
                asdict(self.workspace.evidence[evidence_id])
                for evidence_id in sorted(set(self.workspace.evidence) - assigned)
            ],
            "unresolved_loose_ends": [
                {
                    "id": item.id,
                    "title": item.title,
                    "state": item.state.value,
                    "evidence_ids": item.evidence_ids,
                }
                for item in self.workspace.loose_ends.values()
                if item.state in unresolved_states
            ],
            "instruction": (
                "Work only on unassigned evidence and unresolved loose ends. "
                "Do not recreate loose ends or reprocess ready actions."
            ),
        }

    def _scan(self, source_type: str) -> dict[str, Any]:
        if source_type in self.workspace.inspected_source_types:
            existing_ids = sorted(
                evidence_id
                for evidence_id, record in self.workspace.evidence.items()
                if record.source_type == source_type
            )
            self.workspace.log(
                "observe_result",
                f"Skipped already-inspected {source_type} sources.",
                source_type=source_type,
                new_evidence=0,
            )
            return {
                "status": "already_inspected",
                "no_op": True,
                "source_type": source_type,
                "new_evidence_count": 0,
                "existing_evidence_ids": existing_ids,
                "remaining_work": self._remaining_work(),
            }
        ingested = ingest_directory(self.workspace.data_dir)
        selected = [item for item in ingested.items if item.source_type == source_type]
        candidates = extract_candidates(selected, self.workspace.as_of)
        records = [asdict(self.workspace.add_evidence(candidate)) for candidate in candidates]
        self.workspace.inspected_source_types.add(source_type)
        self.workspace.log(
            "observe_result",
            f"Scanned {source_type} sources.",
            source_type=source_type,
            candidates=len(records),
        )
        return {"source_type": source_type, "candidate_count": len(records), "items": records}

    @tool
    def list_sources(self) -> dict[str, Any]:
        """List available local source files so the agent can choose what to inspect."""
        files = []
        for path in sorted(self.workspace.data_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                parts = {part.lower() for part in path.parts}
                source_type = (
                    "todo"
                    if path.suffix.lower() == ".json"
                    else "note"
                    if parts & {"note", "notes"}
                    else "document"
                )
                files.append({"path": str(path.relative_to(self.workspace.data_dir)), "source_type": source_type})
        self.workspace.log("observe_result", "Listed available local sources.", files=len(files))
        return {"data_dir": str(self.workspace.data_dir), "files": files}

    @tool
    def scan_notes(self) -> dict[str, Any]:
        """Extract possible unfinished obligations from local note files, preserving provenance."""
        return self._scan("note")

    @tool
    def scan_tasks(self) -> dict[str, Any]:
        """Extract open obligations from local JSON task exports, ignoring completed tasks."""
        return self._scan("todo")

    @tool
    def inspect_documents(self) -> dict[str, Any]:
        """Inspect local Markdown and text documents for unfinished obligations and evidence."""
        return self._scan("document")

    @tool
    def find_deadlines(self, evidence_ids: list[str]) -> dict[str, Any]:
        """Resolve deadline candidates for selected evidence using explicit and relative dates.

        Args:
            evidence_ids: Evidence IDs returned by the source inspection tools.
        """
        invalid = self._invalid_evidence_error(evidence_ids)
        if invalid:
            return invalid
        results = []
        for evidence_id in evidence_ids:
            record = self.workspace.evidence[evidence_id]
            parsed = parse_deadline(record.evidence, self.workspace.as_of)
            results.append({
                "evidence_id": evidence_id,
                "deadline": record.due_date or (parsed.isoformat() if parsed else None),
                "source_path": record.source_path,
                "evidence": record.evidence,
            })
        self.workspace.log("observe_result", "Resolved deadline candidates.", evidence_ids=evidence_ids)
        return {"as_of": self.workspace.as_of.isoformat(), "deadlines": results}

    @tool
    def find_related_items(self, evidence_ids: list[str], minimum_score: float = 0.4) -> dict[str, Any]:
        """Suggest possibly related evidence pairs; the agent must decide whether to merge them.

        Args:
            evidence_ids: Evidence IDs to compare.
            minimum_score: Minimum deterministic similarity score to return, from 0 to 1.
        """
        invalid = self._invalid_evidence_error(evidence_ids)
        if invalid:
            return invalid
        known = [self.workspace.evidence[evidence_id] for evidence_id in evidence_ids]
        pairs = []
        for index, left in enumerate(known):
            for right in known[index + 1 :]:
                score = round(candidate_similarity(left.title, right.title), 3)
                if score >= minimum_score:
                    pairs.append({"left_id": left.id, "right_id": right.id, "score": score})
        pairs.sort(key=lambda pair: pair["score"], reverse=True)
        self.workspace.related_analysis_count += 1
        self.workspace.log("observe_result", "Suggested related evidence pairs.", pairs=len(pairs))
        return {"pairs": pairs, "note": "Similarity is evidence, not a merge decision."}

    @tool
    def create_loose_end(self, title: str, evidence_ids: list[str], grouping_rationale: str) -> dict[str, Any]:
        """Create one provisional loose end from evidence the agent judges to belong together.

        Args:
            title: Clear description of the unfinished obligation.
            evidence_ids: One or more provenance IDs supporting this loose end.
            grouping_rationale: Why these records represent the same obligation.
        """
        invalid = self._invalid_evidence_error(evidence_ids)
        if invalid:
            return invalid
        normalized_ids = list(dict.fromkeys(evidence_ids))
        existing = next(
            (
                item
                for item in self.workspace.loose_ends.values()
                if set(item.evidence_ids) == set(normalized_ids)
            ),
            None,
        )
        if existing is not None:
            return {
                "status": "already_exists",
                "no_op": True,
                "created": False,
                "loose_end": asdict(existing),
                "remaining_work": self._remaining_work(),
            }
        try:
            item = self.workspace.create_loose_end(title, normalized_ids, grouping_rationale)
        except ValueError as exc:
            message = str(exc)
            code = "unrelated_evidence_merge" if message.startswith("Evidence items are not meaningfully related") else "invalid_loose_end"
            return {"ok": False, "error": {"code": code, "message": message}}
        return {"loose_end": asdict(item), "next": "Mark ready or request a human decision."}

    @tool
    def mark_ready_action(
        self,
        loose_end_id: str,
        next_action: str,
        decision_rationale: str,
        due_date: str | None = None,
    ) -> dict[str, Any]:
        """Mark a loose end safe to act on and record its next concrete action.

        Args:
            loose_end_id: ID returned by create_loose_end.
            next_action: Specific, non-destructive action grounded in the assigned evidence. After a
                grounding rejection, retry once using the evidence wording without adding details;
                do not cycle through alternative unsupported people, entities, plans, or relationships.
            decision_rationale: Evidence-based explanation of why escalation is unnecessary.
            due_date: Optional ISO deadline supported by the evidence.
        """
        item = self.workspace.loose_ends.get(loose_end_id)
        if item is None:
            raise ValueError(f"Unknown loose end: {loose_end_id}")
        if item.state is ResolutionState.READY_ACTION:
            return {
                "status": "already_ready",
                "no_op": True,
                "updated": False,
                "loose_end": asdict(item),
                "remaining_work": self._remaining_work(),
            }
        grounded, unsupported_terms = self._grounding_result(item, next_action)
        if not _grounding_tokens(next_action) or not grounded:
            raise ValueError(
                "Action is not grounded in this loose end's provenance; unsupported terms: "
                + (", ".join(unsupported_terms) if unsupported_terms else "none")
                + ". Do not retry with alternative unsupported details. Retry once using only the "
                "assigned evidence wording; if that cannot produce a grounded action, leave this item "
                "unresolved and continue with another unresolved loose end."
            )
        evidence_text = " ".join(
            self.workspace.evidence[evidence_id].evidence for evidence_id in item.evidence_ids
        )
        if DECISION_RE.search(evidence_text) and item.state is not ResolutionState.HUMAN_DECISION_RECEIVED:
            raise ValueError("Unresolved decision language requires request_human_decision before a ready action")
        supported_dates = {
            self.workspace.evidence[evidence_id].due_date
            for evidence_id in item.evidence_ids
            if self.workspace.evidence[evidence_id].due_date
        }
        accepted_dates = supported_dates
        if len(supported_dates) > 1:
            human_response = item.human_decision.response if item.human_decision else None
            if item.state is not ResolutionState.HUMAN_DECISION_RECEIVED or human_response not in supported_dates:
                raise ValueError(
                    "Conflicting evidence deadlines require request_human_decision: "
                    + ", ".join(sorted(supported_dates))
                )
            accepted_dates = {human_response}
        claimed_dates = set(ISO_DATE_CLAIM_RE.findall(next_action + " " + decision_rationale))
        unsupported_claims = sorted(claimed_dates - accepted_dates)
        if unsupported_claims:
            raise ValueError("Action contains dates unsupported by provenance: " + ", ".join(unsupported_claims))
        if due_date:
            try:
                date.fromisoformat(due_date)
            except ValueError as exc:
                raise ValueError("due_date must use YYYY-MM-DD") from exc
            if due_date not in accepted_dates:
                raise ValueError(f"Deadline {due_date} is not supported by this loose end's evidence")
        item.state = ResolutionState.READY_ACTION
        item.next_action = next_action.strip()
        item.due_date = due_date or (next(iter(accepted_dates)) if len(accepted_dates) == 1 else None)
        item.decision_rationale = decision_rationale.strip()
        self.workspace.log("act", "Marked a loose end as a ready action.", loose_end_id=loose_end_id)
        return {"loose_end": asdict(item)}

    @tool(context=True)
    def request_human_decision(
        self,
        tool_context: ToolContext,
        loose_end_id: str,
        question: str,
        options: list[str],
        rationale: str,
    ) -> dict[str, Any]:
        """Escalate an ambiguous loose end through a real Strands interrupt.

        Args:
            loose_end_id: ID returned by create_loose_end.
            question: Focused question the human must decide.
            options: Plausible choices supported by the source evidence.
            rationale: Why proceeding without judgment would be unsafe or incorrect.
        """
        item = self.workspace.loose_ends.get(loose_end_id)
        if item is None:
            raise ValueError(f"Unknown loose end: {loose_end_id}")
        invalid_fields = []
        for field_name, value in (("question", question), ("rationale", rationale)):
            grounded, unsupported_terms = self._grounding_result(
                item,
                value,
                neutral_terms=DECISION_CONTEXT_TERMS,
                token_aliases=DECISION_TOKEN_ALIASES,
            )
            if not grounded:
                invalid_fields.append({"field": field_name, "unsupported_terms": unsupported_terms})
        if invalid_fields:
            return {
                "ok": False,
                "error": {
                    "code": "human_decision_association_mismatch",
                    "message": "Question and rationale must refer to the supplied loose end and its provenance.",
                    "loose_end_id": loose_end_id,
                    "invalid_fields": invalid_fields,
                },
            }
        accepted_options = []
        rejected_options = []
        for option in dict.fromkeys(value.strip() for value in options if value.strip()):
            grounded, unsupported_terms = self._grounding_result(
                item,
                option,
                neutral_terms=NEUTRAL_DECISION_TERMS,
            )
            if grounded:
                accepted_options.append(option)
            else:
                rejected_options.append({"option": option, "unsupported_terms": unsupported_terms})
        fallback_used = len(accepted_options) < 2
        if fallback_used:
            accepted_options = list(SAFE_HUMAN_REVIEW_OPTIONS)
        if rejected_options:
            self.workspace.log(
                "decide",
                "Rejected unsupported human-decision options.",
                loose_end_id=loose_end_id,
                rejected_options=rejected_options,
                fallback_used=fallback_used,
            )
        item.state = ResolutionState.HUMAN_DECISION_REQUIRED
        item.human_decision = HumanDecision(question.strip(), accepted_options, rationale.strip())
        self.workspace.log("escalate", "Requested a human decision.", loose_end_id=loose_end_id, question=question)

        response = tool_context.interrupt(
            "loose-ends-human-decision",
            reason={
                "loose_end_id": loose_end_id,
                "question": question,
                "options": accepted_options,
                "rationale": rationale,
                "provenance": [asdict(self.workspace.evidence[value]) for value in item.evidence_ids],
                "option_validation": {
                    "fallback_used": fallback_used,
                    "rejected_options": rejected_options,
                },
            },
        )
        item.human_decision.response = response
        item.state = ResolutionState.HUMAN_DECISION_RECEIVED
        self.workspace.log("observe_result", "Received the human decision.", loose_end_id=loose_end_id)
        return {"loose_end_id": loose_end_id, "human_response": response, "next": "Decide the safe next action."}

    @tool
    def write_action_log(self) -> dict[str, Any]:
        """Write the structured agent state, provenance, decisions, and action log locally."""
        issues = self.workspace.completion_issues()
        if issues:
            return {
                "written": False,
                "status": "incomplete",
                "blocking_issues": issues,
                "remaining_work": self._remaining_work(),
                "next": "Continue with remaining_work only; do not revisit ready actions or inspected sources.",
            }
        self.workspace.report_dir.mkdir(parents=True, exist_ok=True)
        path = self.workspace.report_dir / "agent_action_log.json"
        self.workspace.log("act", "Wrote the local action log.", path=str(path))
        path.write_text(json.dumps(self.workspace.snapshot(), indent=2), encoding="utf-8")
        return {"written": True, "path": str(path), "summary": self.workspace.snapshot()["summary"]}
