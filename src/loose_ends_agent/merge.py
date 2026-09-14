from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from .models import LooseEnd, ObligationCandidate


STOP_WORDS = {
    "a", "about", "and", "at", "before", "by", "for", "from", "i", "if", "in", "is", "it",
    "my", "need", "needs", "of", "on", "or", "the", "to", "todo", "with", "next", "this",
    "ask", "whether", "remember", "waiting", "still", "works",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {word for word in words if word not in STOP_WORDS and not word.isdigit() and len(word) > 2}


def candidate_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared_tokens = left_tokens & right_tokens
    if not shared_tokens:
        return 0.0
    overlap = len(shared_tokens) / min(len(left_tokens), len(right_tokens))
    sequence = SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio()
    shared_anchor_boost = 0.75 if len(shared_tokens) >= 2 else 0.0
    return max(overlap, sequence, shared_anchor_boost)


def _action_for(item: LooseEnd) -> str:
    if item.needs_human_judgment:
        return "Review the conflicting or ambiguous evidence, choose the intended commitment, and confirm its deadline."
    if item.due_date:
        return f"Complete or schedule this obligation by {item.due_date.isoformat()}."
    return "Choose the next concrete step and add it to the personal task list."


def merge_candidates(candidates: list[ObligationCandidate]) -> list[LooseEnd]:
    groups: list[list[ObligationCandidate]] = []
    for candidate in candidates:
        matching = next((group for group in groups if any(candidate_similarity(candidate.title, existing.title) >= 0.58 for existing in group)), None)
        if matching is None:
            groups.append([candidate])
        else:
            matching.append(candidate)

    loose_ends: list[LooseEnd] = []
    for group in groups:
        representative = max(group, key=lambda candidate: (candidate.confidence, len(candidate.title)))
        due_dates = sorted({candidate.due_date for candidate in group if candidate.due_date})
        reasons: list[str] = []
        if len(due_dates) > 1:
            reasons.append("Related sources disagree on the deadline: " + ", ".join(value.isoformat() for value in due_dates) + ".")
        if any(candidate.decision_language for candidate in group):
            reasons.append("The notes describe an unresolved choice rather than a settled action.")
        digest = hashlib.sha1("|".join(sorted(candidate.evidence.lower() for candidate in group)).encode("utf-8")).hexdigest()[:10]
        item = LooseEnd(
            id=f"le-{digest}",
            title=representative.title,
            evidence=list(dict.fromkeys(candidate.evidence for candidate in group)),
            sources=list(dict.fromkeys(
                f"{candidate.source_path}:{candidate.line_number}" if candidate.line_number else candidate.source_path
                for candidate in group
            )),
            due_dates=due_dates,
            due_date=due_dates[0] if len(due_dates) == 1 else None,
            confidence=round(sum(candidate.confidence for candidate in group) / len(group), 2),
            needs_human_judgment=bool(reasons),
            judgment_reasons=reasons,
        )
        item.actions = [_action_for(item)]
        loose_ends.append(item)

    return sorted(loose_ends, key=lambda item: (not item.needs_human_judgment, item.due_date or max(item.due_dates, default=None) or __import__("datetime").date.max, item.title.lower()))
