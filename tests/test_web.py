import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from loose_ends_agent.agent_state import AgentWorkspace, HumanDecision, ResolutionState
from loose_ends_agent.agent_tools import LooseEndsTools
from loose_ends_agent.models import ObligationCandidate
from loose_ends_agent.web import server


class FakeInterrupt:
    def __init__(self, interrupt_id: str, reason: dict) -> None:
        self.id = interrupt_id
        self.name = "loose-ends-human-decision"
        self.reason = reason


class FakeAgent:
    def __init__(self, workspace: AgentWorkspace) -> None:
        self.workspace = workspace
        self.calls = []
        self.interrupt = None

    def __call__(self, value, **kwargs):
        self.calls.append(value)
        passport = next(iter(self.workspace.loose_ends.values()))
        if len(self.calls) == 1:
            passport.state = ResolutionState.HUMAN_DECISION_REQUIRED
            passport.human_decision = HumanDecision(
                "Which passport deadline is correct?",
                ["2026-09-30", "2026-10-01"],
                "The source deadlines conflict.",
            )
            reason = {
                "loose_end_id": passport.id,
                "question": passport.human_decision.question,
                "options": passport.human_decision.options,
                "rationale": passport.human_decision.rationale,
                "provenance": [asdict_record(self.workspace.evidence[value]) for value in passport.evidence_ids],
            }
            self.interrupt = FakeInterrupt("interrupt-1", reason)
            return SimpleNamespace(stop_reason="interrupt", interrupts=[self.interrupt])

        passport.state = ResolutionState.READY_ACTION
        passport.next_action = "Use the selected deadline."
        passport.due_date = "2026-09-30"
        self.workspace.log("act", "Wrote the local action log.")
        return SimpleNamespace(stop_reason="end_turn", interrupts=[])


def asdict_record(record):
    return {
        "id": record.id,
        "title": record.title,
        "evidence": record.evidence,
        "source_path": record.source_path,
        "source_type": record.source_type,
        "line_number": record.line_number,
        "due_date": record.due_date,
        "confidence": record.confidence,
    }


def fake_runtime_factory(data_dir, as_of, report_dir, **kwargs):
    workspace = AgentWorkspace(data_dir, as_of, report_dir)
    first = workspace.add_evidence(
        ObligationCandidate("Renew passport", "Renew by 2026-10-01", str(data_dir / "notes.md"), "note", 3, date(2026, 10, 1), 0.9)
    )
    second = workspace.add_evidence(
        ObligationCandidate("Renew passport", "Renew by 2026-09-30", str(data_dir / "tasks.json"), "todo", 1, date(2026, 9, 30), 0.9)
    )
    workspace.inspected_source_types = workspace.expected_source_types()
    workspace.create_loose_end("Renew passport", [first.id, second.id], "Same obligation with conflicting dates.")
    agent = FakeAgent(workspace)
    return SimpleNamespace(
        agent=agent,
        workspace=workspace,
        tools=LooseEndsTools(workspace),
        provider="ollama",
        model_id="qwen3:14b",
    )


def wait_for(client: TestClient, run_id: str, status: str) -> dict:
    for _ in range(100):
        run = client.get(f"/api/runs/{run_id}").json()["run"]
        if run["status"] == status:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {status}")


def test_hitl_web_lifecycle_preserves_agent_and_session(tmp_path, monkeypatch) -> None:
    manager = server.RunManager(project_root=tmp_path, runtime_factory=fake_runtime_factory)
    monkeypatch.setattr(server, "manager", manager)
    client = TestClient(server.create_app())

    started = client.post("/api/runs", json={"as_of": "2026-09-12"}).json()["run"]
    paused = wait_for(client, started["id"], "paused")

    assert paused["status_label"] == "Paused — human decision required"
    assert paused["resumable"] is True
    assert paused["reconciliation"]["complete"] is False
    assert {item["due_date"] for item in paused["interrupts"][0]["reason"]["provenance"]} == {
        "2026-09-30",
        "2026-10-01",
    }
    agent_id = paused["continuity"]["agent_instance_id"]

    response = client.post(
        f"/api/runs/{started['id']}/resume",
        json={"responses": [{"interrupt_id": "interrupt-1", "response": "2026-09-30"}]},
    )
    assert response.status_code == 202
    completed = wait_for(client, started["id"], "completed")

    assert completed["continuity"]["agent_instance_id"] == agent_id
    assert completed["continuity"]["invocation_count"] == 2
    assert completed["continuity"]["resume_count"] == 1
    assert manager._session.runtime.agent.calls[1] == [
        {"interruptResponse": {"interruptId": "interrupt-1", "response": "2026-09-30"}}
    ]


def test_historical_report_is_never_resumable(tmp_path, monkeypatch) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "agent_run_state.json").write_text(
        '{"summary":{"loose_ends":1},"loose_ends":[],"events":[]}', encoding="utf-8"
    )
    monkeypatch.setattr(server, "manager", server.RunManager(project_root=tmp_path, runtime_factory=fake_runtime_factory))
    client = TestClient(server.create_app())

    run = client.get("/api/runs/current").json()["run"]

    assert run["status"] == "historical"
    assert run["resumable"] is False
    assert run["reconciliation"]["complete"] is False
    assert client.post(
        "/api/runs/historical-latest/resume",
        json={"responses": [{"interrupt_id": "old", "response": "yes"}]},
    ).status_code == 409


def test_dashboard_contains_truthful_hitl_surface() -> None:
    html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "Human decision" in html
    assert "Conflicting evidence" in html
    assert "Ready actions" in html
    assert "Completed" in html
