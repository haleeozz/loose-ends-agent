from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from dateutil import parser as date_parser

from .models import ObligationCandidate, SourceItem


ACTION_RE = re.compile(
    r"(?:\b(?:todo|to-do|need to|needs to|remember to|waiting on|owe|follow[ -]?up|"
    r"send|schedule|book|call|renew|pay|finish|confirm|ask|reply|return|submit|sign|"
    r"decide|buy|pick up|check)\b|^\s*[-*]\s*\[\s\])",
    re.IGNORECASE,
)
COMPLETE_RE = re.compile(r"(?:^\s*[-*]\s*\[[xX]\]|\b(?:done|completed?|cancelled|canceled)\b)", re.IGNORECASE)
DECISION_RE = re.compile(r"\b(?:decide|figure out|choose|not sure|maybe|which one)\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
MONTH_DATE_RE = re.compile(
    r"\b(?:by|before|due|on|expires?)?\s*"
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:,\s*20\d{2})?)\b",
    re.IGNORECASE,
)
WEEKDAYS = {name.lower(): index for index, name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}


def parse_deadline(text: str, as_of: date, supplied: date | None = None) -> date | None:
    if supplied:
        return supplied
    match = ISO_DATE_RE.search(text)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None

    lowered = text.lower()
    if "tomorrow" in lowered:
        return as_of + timedelta(days=1)
    if "next week" in lowered:
        return as_of + timedelta(days=7)

    month_match = MONTH_DATE_RE.search(text)
    if month_match:
        try:
            default = datetime.combine(as_of.replace(month=1, day=1), time.min)
            parsed = date_parser.parse(month_match.group(1), default=default).date()
            if parsed < as_of:
                parsed = parsed.replace(year=parsed.year + 1)
            return parsed
        except (ValueError, OverflowError):
            pass

    for weekday, target in WEEKDAYS.items():
        if re.search(rf"\b{weekday}\b", lowered):
            days = (target - as_of.weekday()) % 7
            return as_of + timedelta(days=days or 7)
    return None


def _clean_title(text: str) -> str:
    text = re.sub(r"^\s*[-*]\s*\[[ xX]\]\s*", "", text)
    text = re.sub(r"^\s*(?:todo|to-do)\s*[:\-]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -.;")
    return text[:1].upper() + text[1:] if text else text


def extract_candidates(items: list[SourceItem], as_of: date) -> list[ObligationCandidate]:
    candidates: list[ObligationCandidate] = []
    for item in items:
        if COMPLETE_RE.search(item.text) or not ACTION_RE.search(item.text):
            continue
        due_date = parse_deadline(item.text, as_of, item.supplied_due)
        confidence = 0.58
        if item.source_type == "todo" or re.search(r"\b(?:todo|need to|must|due)\b", item.text, re.IGNORECASE):
            confidence += 0.17
        if due_date:
            confidence += 0.15
        candidates.append(
            ObligationCandidate(
                title=_clean_title(item.text),
                evidence=item.text,
                source_path=str(item.path),
                source_type=item.source_type,
                line_number=item.line_number,
                due_date=due_date,
                confidence=min(confidence, 0.98),
                decision_language=bool(DECISION_RE.search(item.text)),
            )
        )
    return candidates
