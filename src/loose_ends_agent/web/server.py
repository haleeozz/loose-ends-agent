from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..agentic import AgentRuntime, build_agent, ollama_model_info, run_agent


PROJECT_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).resolve().parent / "static"
MODEL_ID = "qwen3:14b"
OLLAMA_HOST = "http://127.0.0.1:11434"
STATUS_LABELS = {
    "running": "Running",
    "paused": "Paused — human decision required",
    "completed": "Completed",
    "incomplete": "Stopped — reconciliation incomplete",
    "failed": "Run failed",
    "historical": "Historical snapshot — not resumable",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunSession:
    id: str
    runtime: AgentRuntime
    status: str = "running"
    result: Any | None = None
    error: str | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    invocation_count: int = 0
    resume_count: int = 0
    resume_event_sequence: int | None = None
    resumed_interrupt_ids: list[str] = field(default_factory=list)


class RunManager:
    """Own one resumable in-memory Strands session and serialize model work."""

    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        runtime_factory: Callable[..., AgentRuntime] = build_agent,
    ) -> None:
        self.project_root = project_root.resolve()
        self.data_dir = self.project_root / "data"
        self.report_dir = self.project_root / "reports"
        self.runtime_factory = runtime_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="loose-ends-agent")
        self._lock = threading.RLock()
        self._session: RunSession | None = None

    def start(self, as_of: date) -> dict[str, Any]:
        with self._lock:
            if self._session and self._session.status in {"running", "paused"}:
                raise ValueError("A live run is already active; finish or restart the server before starting another.")
            runtime = self.runtime_factory(
                self.data_dir,
                as_of,
                self.report_dir,
                provider="ollama",
                model_id=MODEL_ID,
                ollama_host=OLLAMA_HOST,
                demo_trace=False,
            )
            session = RunSession(id=str(uuid.uuid4()), runtime=runtime)
            self._session = session
            self._executor.submit(self._execute, session, None)
            return self._payload(session)

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            if self._session:
                return self._payload(self._session)
        return self._historical_payload()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            if self._session and self._session.id == run_id:
                return self._payload(self._session)
        return None

    def resume(self, run_id: str, responses: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            session = self._require_session(run_id)
            if session.status != "paused" or session.result is None:
                raise ValueError("This run is not paused for a human decision.")
            interrupts = list(session.result.interrupts or [])
            pending = {interrupt.id: interrupt for interrupt in interrupts}
            supplied = {item.get("interrupt_id"): item.get("response") for item in responses}
            if set(supplied) != set(pending):
                raise ValueError("Responses must include every current interrupt ID exactly once.")
            for interrupt_id, response in supplied.items():
                options = (pending[interrupt_id].reason or {}).get("options", [])
                if options and response not in options:
                    raise ValueError(f"Response for {interrupt_id} must be one of the offered options.")

            strands_input = [
                {"interruptResponse": {"interruptId": interrupt_id, "response": supplied[interrupt_id]}}
                for interrupt_id in pending
            ]
            session.status = "running"
            session.error = None
            session.updated_at = _utc_now()
            session.resume_count += 1
            session.resume_event_sequence = len(session.runtime.workspace.events)
            session.resumed_interrupt_ids.extend(pending)
            self._executor.submit(self._execute, session, strands_input)
            return self._payload(session)

    def _require_session(self, run_id: str) -> RunSession:
        if not self._session or self._session.id != run_id:
            raise ValueError("Live run not found. Historical snapshots cannot be resumed.")
        return self._session

    def _execute(self, session: RunSession, strands_input: list[dict[str, Any]] | None) -> None:
        try:
            session.invocation_count += 1
            result = run_agent(session.runtime) if strands_input is None else session.runtime.agent(strands_input)
            with self._lock:
                session.result = result
                session.status = self._terminal_status(session)
                session.updated_at = _utc_now()
                self._write_demo_snapshot(session)
        except Exception as exc:
            with self._lock:
                session.status = "failed"
                session.error = f"{type(exc).__name__}: {exc}"
                session.updated_at = _utc_now()
                self._write_demo_snapshot(session)

    def _terminal_status(self, session: RunSession) -> str:
        if session.result.stop_reason == "interrupt":
            return "paused"
        issues = session.runtime.workspace.completion_issues(require_log=True)
        return "completed" if not issues else "incomplete"

    def _payload(self, session: RunSession) -> dict[str, Any]:
        workspace = session.runtime.workspace
        snapshot = workspace.snapshot()
        evidence = [asdict(record) for record in workspace.evidence.values()]
        assigned = {evidence_id for item in workspace.loose_ends.values() for evidence_id in item.evidence_ids}
        expected_sources = sorted(workspace.expected_source_types())
        inspected_sources = sorted(workspace.inspected_source_types)
        issues = workspace.completion_issues(require_log=session.status != "running")
        interrupts = []
        if session.result is not None:
            interrupts = [
                {"id": interrupt.id, "name": interrupt.name, "reason": interrupt.reason}
                for interrupt in (session.result.interrupts or [])
            ]
        return {
            "id": session.id,
            "status": session.status,
            "status_label": STATUS_LABELS[session.status],
            "resumable": session.status == "paused",
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "stop_reason": getattr(session.result, "stop_reason", None),
            "error": session.error,
            "model": {"provider": session.runtime.provider, "id": session.runtime.model_id},
            "summary": snapshot["summary"],
            "loose_ends": snapshot["loose_ends"],
            "evidence": evidence,
            "unassigned_evidence": [record for record in evidence if record["id"] not in assigned],
            "events": snapshot["events"],
            "interrupts": interrupts,
            "reconciliation": {
                "complete": session.status == "completed",
                "issues": issues,
                "expected_source_types": expected_sources,
                "inspected_source_types": inspected_sources,
                "source_coverage_complete": set(expected_sources) <= set(inspected_sources),
            },
            "continuity": {
                "agent_instance_id": hex(id(session.runtime.agent)),
                "invocation_count": session.invocation_count,
                "resume_count": session.resume_count,
                "resume_event_sequence": session.resume_event_sequence,
                "resumed_interrupt_ids": session.resumed_interrupt_ids,
            },
        }

    def _write_demo_snapshot(self, session: RunSession) -> None:
        path = self.report_dir / "demo_runs" / f"{session.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._payload(session), indent=2), encoding="utf-8")

    def _historical_payload(self) -> dict[str, Any] | None:
        path = self.report_dir / "agent_run_state.json"
        if not path.exists():
            return None
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return {
            "id": "historical-latest",
            "status": "historical",
            "status_label": STATUS_LABELS["historical"],
            "resumable": False,
            "stop_reason": None,
            "error": None,
            "model": {"provider": "unknown", "id": "report snapshot"},
            "summary": snapshot.get("summary", {}),
            "loose_ends": snapshot.get("loose_ends", []),
            "evidence": [
                record
                for item in snapshot.get("loose_ends", [])
                for record in item.get("provenance", [])
            ],
            "unassigned_evidence": [],
            "events": snapshot.get("events", []),
            "interrupts": [],
            "reconciliation": {
                "complete": False,
                "issues": ["This report is read-only and its live Strands session is unavailable."],
                "expected_source_types": [],
                "inspected_source_types": snapshot.get("summary", {}).get("inspected_source_types", []),
                "source_coverage_complete": False,
            },
            "continuity": None,
        }


manager = RunManager()


async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def health(request: Request) -> JSONResponse:
    try:
        info = ollama_model_info(OLLAMA_HOST, MODEL_ID)
        capabilities = info.get("capabilities", [])
        return JSONResponse({"ok": "tools" in capabilities, "model": MODEL_ID, "capabilities": capabilities})
    except Exception as exc:
        return JSONResponse({"ok": False, "model": MODEL_ID, "error": str(exc)}, status_code=503)


async def start_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    try:
        as_of = date.fromisoformat(body.get("as_of", "2026-09-12"))
        return JSONResponse({"run": manager.start(as_of)}, status_code=202)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": {"code": "run_not_started", "message": str(exc)}}, status_code=409)


async def current_run(request: Request) -> JSONResponse:
    return JSONResponse({"run": manager.current()})


async def get_run(request: Request) -> JSONResponse:
    run = manager.get(request.path_params["run_id"])
    if run is None:
        return JSONResponse({"error": {"code": "run_not_found", "message": "Live run not found."}}, status_code=404)
    return JSONResponse({"run": run})


async def resume_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        responses = body.get("responses", [])
        if not isinstance(responses, list):
            raise ValueError("responses must be a list")
        run = manager.resume(request.path_params["run_id"], responses)
        return JSONResponse({"run": run}, status_code=202)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return JSONResponse({"error": {"code": "run_not_resumed", "message": str(exc)}}, status_code=409)


def create_app() -> Starlette:
    return Starlette(
        debug=False,
        routes=[
            Route("/", index),
            Route("/api/health", health),
            Route("/api/runs", start_run, methods=["POST"]),
            Route("/api/runs/current", current_run),
            Route("/api/runs/{run_id}", get_run),
            Route("/api/runs/{run_id}/resume", resume_run, methods=["POST"]),
            Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
        ],
    )


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

