from __future__ import annotations

from datetime import date
from pathlib import Path

from loose_ends_agent.agentic import build_agent
from loose_ends_agent.extract import extract_candidates
from loose_ends_agent.ingest import ingest_directory


def main() -> int:
    runtime = build_agent(
        Path("data"),
        date(2026, 9, 12),
        Path("reports"),
        provider="ollama",
        model_id="qwen3:14b",
        demo_trace=True,
        focused_decision=True,
    )
    candidates = extract_candidates(ingest_directory(Path("data")).items, date(2026, 9, 12))
    passport_records = [
        runtime.workspace.add_evidence(candidate)
        for candidate in candidates
        if "passport" in candidate.title.lower()
    ]
    loose_end = runtime.workspace.create_loose_end(
        "Renew passport",
        [record.id for record in passport_records],
        "Both records describe the same passport renewal and contain conflicting deadlines.",
    )
    runtime.workspace.inspected_source_types = runtime.workspace.expected_source_types()
    runtime.workspace.related_analysis_count = 1
    runtime.workspace.focus_loose_end_ids = {loose_end.id}
    result = runtime.agent(
        "Review the existing passport loose end "
        f"{loose_end.id}. Its provenance has two conflicting deadlines. Decide whether it is safe to act; "
        "use request_human_decision if a person must resolve the conflict. Do not invent a deadline."
    )
    print(f"stop_reason={result.stop_reason}")
    interrupts = result.interrupts or []
    for interrupt in interrupts:
        print(f"interrupt={interrupt.reason}")

    called_tools = [
        event.data.get("tool")
        for event in runtime.workspace.events
        if event.phase == "tool_call"
    ]
    if "request_human_decision" not in called_tools:
        raise AssertionError(f"Expected request_human_decision tool call; observed {called_tools}")
    if result.stop_reason != "interrupt":
        raise AssertionError(f"Expected stop_reason=interrupt, got {result.stop_reason}")
    if len(interrupts) != 1:
        raise AssertionError(f"Expected one interrupt, got {len(interrupts)}")

    provenance = interrupts[0].reason.get("provenance", [])
    observed = {(item["source_path"], item["due_date"]) for item in provenance}
    expected = {
        ("data\\notes\\weekend_notes.md", "2026-10-01"),
        ("data\\todos\\personal.json", "2026-09-30"),
    }
    if observed != expected:
        raise AssertionError(f"Interrupt provenance mismatch: expected {expected}, got {observed}")
    print(f"tools_called={called_tools}")
    print("focused_hitl_verification=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
