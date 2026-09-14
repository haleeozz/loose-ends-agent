from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookRegistry,
)

from .agent_state import AgentWorkspace


class AgentLoopTracer:
    """Expose high-level loop phases without revealing private chain-of-thought."""

    def __init__(self, workspace: AgentWorkspace, emit: Callable[[str], None] | None = None) -> None:
        self.workspace = workspace
        self.emit = emit

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(AfterModelCallEvent, self.after_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def _record(self, phase: str, detail: str, **data: Any) -> None:
        self.workspace.log(phase, detail, **data)
        if self.emit:
            self.emit(f"[{phase.upper()}] {detail}")

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        detail = (
            "Agent received a focused decision goal for pre-inspected evidence."
            if self.workspace.focus_loose_end_ids is not None
            else "Agent received the reconciliation goal and will inspect available sources."
        )
        self._record("observe", detail)

    def before_model(self, event: BeforeModelCallEvent) -> None:
        self._record("reason", "Model evaluates current observations and chooses the next step.")

    def after_model(self, event: AfterModelCallEvent) -> None:
        self._record("decide", "Model response received; the loop will act, inspect further, or escalate.")

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        tool_use = getattr(event, "tool_use", {}) or {}
        name = tool_use.get("name", "unknown")
        inputs = tool_use.get("input", {})
        self._record("tool_call", f"Calling {name} with {inputs}.", tool=name, inputs=inputs)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        tool_use = getattr(event, "tool_use", {}) or {}
        name = tool_use.get("name", "unknown")
        exception = getattr(event, "exception", None)
        result = getattr(event, "result", {}) or {}
        validation_rejected = False
        for content in result.get("content", []):
            text = content.get("text") if isinstance(content, dict) else None
            if not isinstance(text, str):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("ok") is False:
                validation_rejected = True
                break
        status = "error" if exception or result.get("status") == "error" or validation_rejected else "success"
        self._record(
            "observe_result",
            f"{name} returned with status {status}.",
            tool=name,
            status=status,
            validation_rejected=validation_rejected,
        )

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        summary = self.workspace.snapshot()["summary"]
        stop_reason = getattr(getattr(event, "result", None), "stop_reason", None)
        if stop_reason == "interrupt":
            self._record("escalate", f"Agent loop paused for first-class human input with state summary {summary}.")
            return

        issues = self.workspace.completion_issues(require_log=True)
        if issues and self.workspace.resume_count < 20:
            self.workspace.resume_count += 1
            missing_sources = sorted(self.workspace.expected_source_types() - self.workspace.inspected_source_types)
            assigned = {evidence_id for item in self.workspace.loose_ends.values() for evidence_id in item.evidence_ids}
            unassigned = sorted(set(self.workspace.evidence) - assigned)
            unresolved = sorted(
                item.id
                for item in self.workspace.loose_ends.values()
                if (self.workspace.focus_loose_end_ids is None or item.id in self.workspace.focus_loose_end_ids)
                and item.state.value in {"observed", "human_decision_received"}
            )
            directives = []
            scan_tools = {"note": "scan_notes", "todo": "scan_tasks", "document": "inspect_documents"}
            if missing_sources:
                directives.append("Call " + ", ".join(scan_tools[value] for value in missing_sources) + " now.")
            elif unassigned:
                compact = "; ".join(
                    f"{evidence_id}={self.workspace.evidence[evidence_id].title}"
                    + (
                        f" [due {self.workspace.evidence[evidence_id].due_date}]"
                        if self.workspace.evidence[evidence_id].due_date
                        else ""
                    )
                    for evidence_id in unassigned
                )
                if self.workspace.related_analysis_count == 0:
                    directives.append(
                        "Call find_related_items with this exact evidence_ids list: " + str(unassigned) + ". "
                        "Do not call any scanner again."
                    )
                else:
                    directives.append(
                        "Relation analysis is already complete. Call create_loose_end for every obligation now, grouping "
                        "true duplicates into one call. Do not call find_related_items or any scanner again. Use multiple "
                        "independent tool calls in one response when possible. Compact evidence: " + compact
                    )
            elif unresolved:
                compact = []
                for loose_end_id in unresolved:
                    item = self.workspace.loose_ends[loose_end_id]
                    deadlines = sorted(
                        {
                            self.workspace.evidence[evidence_id].due_date
                            for evidence_id in item.evidence_ids
                            if self.workspace.evidence[evidence_id].due_date
                        }
                    )
                    compact.append(f"{loose_end_id}={item.title} [evidence deadlines: {deadlines or 'none'}]")
                directives.append(
                    "Call mark_ready_action or request_human_decision for every unresolved loose end. "
                    "Conflicting deadlines or unresolved personal choices require request_human_decision; other items require "
                    "mark_ready_action. Use multiple independent tool calls in one response when possible. " + "; ".join(compact)
                )
            else:
                directives.append("Call write_action_log now.")
            event.resume = (
                "You ended before the reconciliation contract was complete. Your next response MUST contain tool calls; "
                "do not answer with prose and do not summarize. " + " ".join(directives) + " Outstanding state: " + " | ".join(issues)
            )
            self._record(
                "reason",
                f"Completion guard resumed the agent loop (pass {self.workspace.resume_count}) because: " + " | ".join(issues),
            )
            return
        phase = "act" if not issues else "incomplete"
        detail = f"Agent loop stopped with state summary {summary}."
        if issues:
            detail += " Remaining issues: " + " | ".join(issues)
        self._record(phase, detail)
