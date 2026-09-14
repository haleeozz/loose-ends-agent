from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.interrupt import InterruptException, _InterruptState
from strands.types.tools import ToolContext

from loose_ends_agent.pipeline import run_pipeline
from loose_ends_agent.agent_state import AgentWorkspace, ResolutionState
from loose_ends_agent.agent_tools import LooseEndsTools
from loose_ends_agent.agent_trace import AgentLoopTracer
from loose_ends_agent.extract import extract_candidates
from loose_ends_agent.ingest import ingest_directory
from loose_ends_agent.merge import candidate_similarity
from loose_ends_agent.models import ObligationCandidate
from loose_ends_agent.strands_bridge import local_tools


def test_sample_pipeline_merges_and_triages() -> None:
    result = run_pipeline(Path("data"), as_of=date(2026, 9, 12))

    assert result.files_scanned == 2
    assert result.candidates == 2
    assert len(result.loose_ends) == 1
    assert len(result.judgment_items) == 1
    assert "passport" in result.judgment_items[0].title.lower()
    assert "2026-09-30" in " ".join(result.judgment_items[0].judgment_reasons)
    assert result.ready_items == []


def test_completed_items_are_ignored() -> None:
    result = run_pipeline(Path("data"), as_of=date(2026, 9, 12))
    all_evidence = " ".join(evidence for item in result.loose_ends for evidence in item.evidence).lower()

    assert "tax return" not in all_evidence
    assert "library books" not in all_evidence


def test_strands_tool_builds_without_a_model() -> None:
    tools = local_tools()

    assert len(tools) == 1
    assert tools[0].tool_name == "scan_local_loose_ends"


def test_agent_tool_surface_and_provenance(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    toolset = LooseEndsTools(workspace)
    names = {agent_tool.tool_name for agent_tool in toolset.all_tools()}

    assert names == {
        "list_sources",
        "scan_notes",
        "scan_tasks",
        "inspect_documents",
        "find_deadlines",
        "find_related_items",
        "create_loose_end",
        "mark_ready_action",
        "request_human_decision",
        "write_action_log",
    }

    evidence = workspace.add_evidence(
        ObligationCandidate(
            title="Renew passport",
            evidence="Renew passport by 2026-10-01",
            source_path="data/notes/weekend_notes.md",
            source_type="note",
            line_number=3,
            due_date=date(2026, 10, 1),
            confidence=0.9,
        )
    )
    loose_end = workspace.create_loose_end("Renew passport", [evidence.id], "Same explicit obligation.")

    assert loose_end.state is ResolutionState.OBSERVED
    assert workspace.snapshot()["loose_ends"][0]["provenance"][0]["id"] == evidence.id

    with pytest.raises(ValueError, match="not grounded"):
        workspace.create_loose_end("High-similarity pair", [evidence.id], "Generic model claim")

    with pytest.raises(ValueError, match="Unsupported date"):
        workspace.create_loose_end("Renew passport", [evidence.id], "Complete by 2023-12-31")

    related_workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    first = related_workspace.add_evidence(
        ObligationCandidate("Renew passport", "Renew passport", "data/a.md", "note", 1, date(2026, 9, 30), 0.9)
    )
    related_workspace.add_evidence(
        ObligationCandidate(
            "Renew my passport and prepare photo",
            "Renew my passport and prepare photo",
            "data/b.md",
            "note",
            1,
            date(2026, 10, 1),
            0.9,
        )
    )
    with pytest.raises(ValueError, match="Likely related evidence was omitted"):
        related_workspace.create_loose_end("Renew passport", [first.id], "Same obligation")


def test_sample_passport_records_are_strongly_related() -> None:
    candidates = extract_candidates(ingest_directory(Path("data")).items, date(2026, 9, 12))
    passports = [candidate for candidate in candidates if "passport" in candidate.title.lower()]

    assert len(passports) == 2
    assert {candidate.source_type for candidate in passports} == {"note", "todo"}
    assert candidate_similarity(passports[0].title, passports[1].title) >= 0.74

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), Path("reports"))
    records = [workspace.add_evidence(candidate) for candidate in candidates]
    passport_records = [record for record in records if "passport" in record.title.lower()]
    with pytest.raises(ValueError, match="Likely related evidence was omitted"):
        workspace.create_loose_end("Renew passport", [passport_records[0].id], "Single source")


def test_evidence_tools_reject_loose_end_and_unknown_ids(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    evidence = workspace.add_evidence(
        ObligationCandidate("Renew passport", "Renew passport", "data/a.md", "note", 1, None, 0.9)
    )
    loose_end = workspace.create_loose_end("Renew passport", [evidence.id], "Same obligation")
    toolset = LooseEndsTools(workspace)

    for result in (
        toolset.find_deadlines([loose_end.id]),
        toolset.find_related_items([loose_end.id]),
        toolset.create_loose_end("Renew passport", [loose_end.id], "Wrong identifier kind"),
    ):
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_evidence_ids"
        assert result["error"]["invalid"] == [
            {"id": loose_end.id, "reason": "loose_end_id_not_evidence_id"}
        ]


def test_carpool_and_filter_evidence_cannot_be_merged(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    carpool = workspace.add_evidence(
        ObligationCandidate(
            "Waiting on Alex about the October carpool; ask whether Tuesday still works",
            "Waiting on Alex about the October carpool; ask whether Tuesday still works",
            "data/notes/weekend_notes.md",
            "note",
            6,
            date(2026, 9, 15),
            0.73,
        )
    )
    filter_quote = workspace.add_evidence(
        ObligationCandidate(
            "Remember to ask whether the quoted price includes filter replacement",
            "Remember to ask whether the quoted price includes filter replacement",
            "data/documents/home_quotes.md",
            "document",
            5,
            None,
            0.58,
        )
    )
    toolset = LooseEndsTools(workspace)

    related = toolset.find_related_items([carpool.id, filter_quote.id])
    merged = toolset.create_loose_end(
        "Carpool and filter follow-up",
        [carpool.id, filter_quote.id],
        "Treat both follow-ups as one obligation.",
    )

    assert related["pairs"] == []
    assert merged["ok"] is False
    assert merged["error"]["code"] == "unrelated_evidence_merge"


def test_misleading_title_is_not_grounded_by_generic_words(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    bus = workspace.add_evidence(
        ObligationCandidate(
            "Need to decide whether Sam can take the late bus after robotics on Thursday",
            "Need to decide whether Sam can take the late bus after robotics on Thursday",
            "data/documents/family_schedule.txt",
            "document",
            4,
            date(2026, 9, 17),
            0.9,
        )
    )

    with pytest.raises(ValueError, match="not grounded"):
        workspace.create_loose_end(
            "Ask whether Tuesday still works for the October carpool",
            [bus.id],
            "Incorrectly relabeled obligation.",
        )


def test_passport_human_decision_preserves_conflicting_provenance(tmp_path: Path) -> None:
    class CapturedInterrupt(RuntimeError):
        pass

    class FakeToolContext:
        def __init__(self) -> None:
            self.reason = None

        def interrupt(self, name: str, reason: dict) -> None:
            assert name == "loose-ends-human-decision"
            self.reason = reason
            raise CapturedInterrupt

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    candidates = extract_candidates(ingest_directory(Path("data")).items, date(2026, 9, 12))
    records = [workspace.add_evidence(candidate) for candidate in candidates if "passport" in candidate.title.lower()]
    loose_end = workspace.create_loose_end(
        "Renew passport",
        [record.id for record in records],
        "Both records describe the same passport renewal with conflicting deadlines.",
    )
    context = FakeToolContext()

    with pytest.raises(CapturedInterrupt):
        LooseEndsTools(workspace).request_human_decision(
            context,
            loose_end.id,
            "Which passport deadline is correct?",
            ["2026-09-30", "2026-10-01"],
            "The source deadlines conflict.",
        )

    assert workspace.loose_ends[loose_end.id].state is ResolutionState.HUMAN_DECISION_REQUIRED
    assert {(item["source_path"], item["due_date"]) for item in context.reason["provenance"]} == {
        ("data\\notes\\weekend_notes.md", "2026-10-01"),
        ("data\\todos\\personal.json", "2026-09-30"),
    }
    assert context.reason["options"] == ["2026-09-30", "2026-10-01"]
    assert context.reason["option_validation"] == {"fallback_used": False, "rejected_options": []}


def test_human_deadline_choice_unblocks_only_the_selected_date(tmp_path: Path) -> None:
    class FakeToolContext:
        def interrupt(self, name: str, reason: dict) -> str:
            assert name == "loose-ends-human-decision"
            return "2026-09-30"

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    first = workspace.add_evidence(
        ObligationCandidate("Renew passport", "Renew passport", "data/a.md", "note", 1, date(2026, 9, 30), 0.9)
    )
    second = workspace.add_evidence(
        ObligationCandidate(
            "Renew my passport and prepare photo",
            "Renew my passport and prepare photo",
            "data/b.md",
            "todo",
            1,
            date(2026, 10, 1),
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Renew passport",
        [first.id, second.id],
        "Both records describe the same passport renewal with conflicting deadlines.",
    )
    tools = LooseEndsTools(workspace)

    tools.request_human_decision(
        FakeToolContext(),
        loose_end.id,
        "Which passport deadline is correct?",
        ["2026-09-30", "2026-10-01"],
        "The source deadlines conflict.",
    )

    with pytest.raises(ValueError, match="unsupported by provenance"):
        tools.mark_ready_action(
            loose_end.id,
            "Renew the passport by 2026-10-01.",
            "The human selected 2026-09-30.",
            "2026-10-01",
        )

    result = tools.mark_ready_action(
        loose_end.id,
        "Gather a compliant photo and the old passport by 2026-09-30.",
        "The human selected 2026-09-30.",
        "2026-09-30",
    )

    assert result["loose_end"]["state"] is ResolutionState.READY_ACTION
    assert result["loose_end"]["due_date"] == "2026-09-30"


def test_ready_action_rejects_entities_and_plans_missing_from_provenance(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    evidence = workspace.add_evidence(
        ObligationCandidate(
            "Need to decide whether Sam can take the late bus after robotics on Thursday",
            "Need to decide whether Sam can take the late bus after robotics on Thursday.",
            "data/documents/family_schedule.txt",
            "document",
            4,
            date(2026, 9, 17),
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Decide whether Sam can take the late bus after robotics on Thursday",
        [evidence.id],
        "This evidence describes the unresolved late-bus decision.",
    )
    tools = LooseEndsTools(workspace)

    with pytest.raises(ValueError, match="not grounded.*alex, allow, carpool"):
        tools.mark_ready_action(
            loose_end.id,
            "Decide whether to allow Sam to carpool with Alex.",
            "The obligation is clear and actionable.",
            "2026-09-17",
        )

    assert loose_end.state is ResolutionState.OBSERVED

    with pytest.raises(ValueError, match="Unresolved decision language"):
        tools.mark_ready_action(
            loose_end.id,
            "Decide whether Sam can take the late bus after robotics on Thursday.",
            "The action repeats the evidenced decision.",
            "2026-09-17",
        )


def test_ready_action_allows_grounded_paraphrase(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    evidence = workspace.add_evidence(
        ObligationCandidate(
            "Buy Maya's birthday gift - She mentioned the ceramic workshop",
            "Buy Maya's birthday gift - She mentioned the ceramic workshop.",
            "data/todos/personal.json",
            "todo",
            2,
            date(2026, 9, 20),
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Buy Maya's birthday gift",
        [evidence.id],
        "This evidence describes Maya's birthday gift.",
    )

    result = LooseEndsTools(workspace).mark_ready_action(
        loose_end.id,
        "Purchase a gift for the birthday party.",
        "The gift obligation is explicit.",
        "2026-09-20",
    )

    assert result["loose_end"]["state"] is ResolutionState.READY_ACTION


def test_hvac_grounding_rejection_converges_on_evidence_wording(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    evidence = workspace.add_evidence(
        ObligationCandidate(
            "Book HVAC maintenance before September 25",
            "Need to book HVAC maintenance before the quote expires on September 25.",
            "data/notes/home.md",
            "note",
            1,
            date(2026, 9, 25),
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Book HVAC maintenance before the quote expires",
        [evidence.id],
        "The evidence describes one HVAC maintenance obligation.",
    )
    tools = LooseEndsTools(workspace)

    with pytest.raises(ValueError) as rejected:
        tools.mark_ready_action(
            loose_end.id,
            "Contact the HVAC provider to schedule maintenance before the quote expires.",
            "The obligation is ready to act on.",
            "2026-09-25",
        )

    assert "Retry once using only the assigned evidence wording" in str(rejected.value)
    assert loose_end.state is ResolutionState.OBSERVED

    result = tools.mark_ready_action(
        loose_end.id,
        "Book HVAC maintenance before the quote expires on September 25.",
        "The evidence explicitly requires booking HVAC maintenance before the quote expires.",
        "2026-09-25",
    )

    assert result["loose_end"]["state"] is ResolutionState.READY_ACTION
    assert loose_end.state is ResolutionState.READY_ACTION
    assert loose_end.next_action == "Book HVAC maintenance before the quote expires on September 25."


def test_human_decision_replaces_unsupported_options_with_safe_review(tmp_path: Path) -> None:
    class CapturedInterrupt(RuntimeError):
        pass

    class FakeToolContext:
        reason = None

        def interrupt(self, name: str, reason: dict) -> None:
            self.reason = reason
            raise CapturedInterrupt

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    evidence = workspace.add_evidence(
        ObligationCandidate(
            "Need to decide whether a family member can take the late bus",
            "Need to decide whether a family member can take the late bus.",
            "data/documents/schedule.txt",
            "document",
            1,
            None,
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Decide whether a family member can take the late bus",
        [evidence.id],
        "The late-bus decision is unresolved.",
    )
    context = FakeToolContext()

    with pytest.raises(CapturedInterrupt):
        LooseEndsTools(workspace).request_human_decision(
            context,
            loose_end.id,
            "How should this be resolved?",
            ["Ask the school principal", "Arrange a ride with a neighbor"],
            "The evidence leaves the choice unresolved.",
        )

    assert context.reason["options"] == ["Approve", "Decline", "Keep unresolved"]
    assert context.reason["option_validation"]["fallback_used"] is True
    assert {value["option"] for value in context.reason["option_validation"]["rejected_options"]} == {
        "Ask the school principal",
        "Arrange a ride with a neighbor",
    }


def test_human_decision_accepts_simple_grounded_options(tmp_path: Path) -> None:
    class CapturedInterrupt(RuntimeError):
        pass

    class FakeToolContext:
        reason = None

        def interrupt(self, name: str, reason: dict) -> None:
            self.reason = reason
            raise CapturedInterrupt

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    evidence = workspace.add_evidence(
        ObligationCandidate("Approve the trip", "Approve or decline the trip.", "data/a.md", "note", 1, None, 0.9)
    )
    loose_end = workspace.create_loose_end("Approve the trip", [evidence.id], "The trip requires a decision.")
    context = FakeToolContext()

    with pytest.raises(CapturedInterrupt):
        LooseEndsTools(workspace).request_human_decision(
            context,
            loose_end.id,
            "Should the trip be approved?",
            ["Approve", "Decline", "Decide later / keep unresolved"],
            "A human choice is required.",
        )

    assert context.reason["options"] == ["Approve", "Decline", "Decide later / keep unresolved"]
    assert context.reason["option_validation"] == {"fallback_used": False, "rejected_options": []}


def test_late_bus_escalation_contains_no_invented_option_details(tmp_path: Path) -> None:
    class CapturedInterrupt(RuntimeError):
        pass

    class FakeToolContext:
        reason = None

        def interrupt(self, name: str, reason: dict) -> None:
            self.reason = reason
            raise CapturedInterrupt

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    evidence = workspace.add_evidence(
        ObligationCandidate(
            "Need to decide whether Sam can take the late bus after robotics on Thursday",
            "Need to decide whether Sam can take the late bus after robotics on Thursday.",
            "data/documents/family_schedule.txt",
            "document",
            4,
            date(2026, 9, 17),
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Decide whether Sam can take the late bus after robotics on Thursday",
        [evidence.id],
        "The late-bus decision is unresolved.",
    )
    context = FakeToolContext()

    with pytest.raises(CapturedInterrupt):
        LooseEndsTools(workspace).request_human_decision(
            context,
            loose_end.id,
            "What should happen?",
            ["Discuss it with a school administrator", "Arrange another transport plan"],
            "The action needs human judgment.",
        )

    assert context.reason["options"] == ["Approve", "Decline", "Keep unresolved"]
    assert context.reason["provenance"][0]["id"] == evidence.id


def test_cross_item_human_decision_request_is_atomic(tmp_path: Path) -> None:
    class FakeToolContext:
        called = False

        def interrupt(self, name: str, reason: dict) -> None:
            self.called = True

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    first = workspace.add_evidence(
        ObligationCandidate("Renew passport", "Renew passport", "data/a.md", "note", 1, date(2026, 9, 30), 0.9)
    )
    second = workspace.add_evidence(
        ObligationCandidate(
            "Renew my passport and prepare photo",
            "Renew my passport and prepare photo",
            "data/b.md",
            "todo",
            1,
            date(2026, 10, 1),
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Renew passport",
        [first.id, second.id],
        "Both records describe the same passport renewal with conflicting deadlines.",
    )
    context = FakeToolContext()

    result = LooseEndsTools(workspace).request_human_decision(
        context,
        loose_end.id,
        "What is the next action required for rescheduling the dental cleaning by Friday?",
        ["Contact the dental office", "Reschedule the cleaning directly"],
        "The dental cleaning needs a human decision.",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "human_decision_association_mismatch"
    assert {value["field"] for value in result["error"]["invalid_fields"]} == {"question", "rationale"}
    assert context.called is False
    assert loose_end.state is ResolutionState.OBSERVED
    assert loose_end.human_decision is None


def test_post_resume_repeated_work_is_no_op_and_sam_remains_actionable(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    filter_evidence = workspace.add_evidence(
        ObligationCandidate(
            "Remember to ask whether the quoted price includes filter replacement",
            "Remember to ask whether the quoted price includes filter replacement.",
            "data/documents/home_quotes.md",
            "document",
            5,
            None,
            0.58,
        )
    )
    sam_evidence = workspace.add_evidence(
        ObligationCandidate(
            "Need to decide whether Sam can take the late bus after robotics on Thursday",
            "Need to decide whether Sam can take the late bus after robotics on Thursday.",
            "data/documents/family_schedule.txt",
            "document",
            4,
            date(2026, 9, 17),
            0.9,
        )
    )
    filter_item = workspace.create_loose_end(
        "Ask whether the quoted price includes filter replacement",
        [filter_evidence.id],
        "This evidence describes the HVAC quote question.",
    )
    sam_item = workspace.create_loose_end(
        "Decide whether Sam can take the late bus after robotics on Thursday",
        [sam_evidence.id],
        "The late-bus decision is unresolved.",
    )
    tools = LooseEndsTools(workspace)
    tools.mark_ready_action(
        filter_item.id,
        "Ask whether the quoted price includes filter replacement.",
        "The quote question is explicit.",
    )
    workspace.inspected_source_types.add("document")

    duplicate_create = tools.create_loose_end(
        "Ask whether the quoted price includes filter replacement",
        [filter_evidence.id],
        "This evidence describes the HVAC quote question.",
    )
    duplicate_action = tools.mark_ready_action(
        filter_item.id,
        "Ask whether the quoted price includes filter replacement.",
        "The quote question is explicit.",
    )
    repeated_scan = tools.inspect_documents()
    incomplete_log = tools.write_action_log()

    assert duplicate_create["status"] == "already_exists"
    assert duplicate_create["no_op"] is True
    assert duplicate_create["created"] is False
    assert duplicate_action["status"] == "already_ready"
    assert duplicate_action["no_op"] is True
    assert duplicate_action["updated"] is False
    assert repeated_scan["status"] == "already_inspected"
    assert repeated_scan["no_op"] is True
    assert repeated_scan["new_evidence_count"] == 0
    assert len(workspace.evidence) == 2
    assert filter_item.state is ResolutionState.READY_ACTION
    assert sam_item.state is ResolutionState.OBSERVED
    assert incomplete_log["status"] == "incomplete"
    assert incomplete_log["remaining_work"]["unassigned_evidence"] == []
    assert incomplete_log["remaining_work"]["unresolved_loose_ends"] == [
        {
            "id": sam_item.id,
            "title": sam_item.title,
            "state": ResolutionState.OBSERVED.value,
            "evidence_ids": [sam_evidence.id],
        }
    ]


@pytest.mark.parametrize(
    ("question", "rationale"),
    [
        (
            "Which passport date should take precedence?",
            "Determine which passport deadline should take precedence.",
        ),
        (
            "Which deadline is linked to the passport renewal?",
            "Identify which date is linked to the passport renewal obligation.",
        ),
        (
            "Which passport deadline should be used?",
            "Determine which date should be used for the passport renewal.",
        ),
    ],
)
def test_grounded_passport_paraphrases_raise_strands_interrupt(
    tmp_path: Path,
    question: str,
    rationale: str,
) -> None:
    class ContextAgent:
        def __init__(self) -> None:
            self._interrupt_state = _InterruptState()

    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    first = workspace.add_evidence(
        ObligationCandidate("Renew passport", "Renew passport", "data/a.md", "note", 1, date(2026, 9, 30), 0.9)
    )
    second = workspace.add_evidence(
        ObligationCandidate(
            "Renew my passport and prepare photo",
            "Renew my passport and prepare photo",
            "data/b.md",
            "todo",
            1,
            date(2026, 10, 1),
            0.9,
        )
    )
    loose_end = workspace.create_loose_end(
        "Renew passport",
        [first.id, second.id],
        "Both records describe the same passport renewal with conflicting deadlines.",
    )
    context = ToolContext(
        tool_use={"toolUseId": "passport-paraphrase", "name": "request_human_decision", "input": {}},
        agent=ContextAgent(),
        invocation_state={},
    )

    with pytest.raises(InterruptException) as raised:
        LooseEndsTools(workspace).request_human_decision(
            context,
            loose_end.id,
            question,
            ["2026-09-30", "2026-10-01"],
            rationale,
        )

    assert raised.value.interrupt.reason["loose_end_id"] == loose_end.id
    assert raised.value.interrupt.reason["options"] == ["2026-09-30", "2026-10-01"]
    assert loose_end.state is ResolutionState.HUMAN_DECISION_REQUIRED


def test_trace_marks_structured_validation_rejection_as_error(tmp_path: Path) -> None:
    workspace = AgentWorkspace(Path("data"), date(2026, 9, 12), tmp_path)
    tracer = AgentLoopTracer(workspace)
    event = SimpleNamespace(
        tool_use={"name": "request_human_decision"},
        exception=None,
        result={
            "status": "success",
            "content": [{"text": '{"ok": false, "error": {"code": "association_mismatch"}}'}],
        },
    )

    tracer.after_tool(event)

    recorded = workspace.events[-1]
    assert recorded.data["status"] == "error"
    assert recorded.data["validation_rejected"] is True
    assert "status error" in recorded.detail
