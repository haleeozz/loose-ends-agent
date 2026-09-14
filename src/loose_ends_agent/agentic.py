from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from strands import Agent

from .agent_state import AgentWorkspace
from .agent_tools import LooseEndsTools
from .agent_trace import AgentLoopTracer


AGENT_SYSTEM_PROMPT = """You are Loose Ends Agent, a careful personal-obligation reconciler.

Your orchestration loop is explicit:
1. OBSERVE: call list_sources, then dynamically choose which source scanners are relevant.
2. REASON: use find_deadlines and find_related_items as evidence, never as automatic verdicts.
3. TOOL CALL: gather only the information needed for the current decision.
4. OBSERVE RESULT: cite evidence IDs and source provenance; never invent either.
5. DECIDE: use create_loose_end to group evidence that truly represents one obligation.
6. ACT OR ESCALATE: call mark_ready_action only when a concrete next step is safe. Call
   request_human_decision when deadlines conflict, intent is unresolved, or acting would require
   a personal choice. Human escalation is a workflow state and a Strands interrupt, not a label.

Process every plausible unfinished obligation you observe. Preserve all supporting evidence. Do not
send messages, edit source documents, purchase anything, or perform external actions. Before ending
a fully resolved run, call write_action_log. Keep final prose brief because structured tool state is
the source of truth. Do not reveal private chain-of-thought; report only concise decision rationales."""


class ProviderUnavailableError(RuntimeError):
    pass


@dataclass
class AgentRuntime:
    agent: Agent
    workspace: AgentWorkspace
    tools: LooseEndsTools
    provider: str
    model_id: str


def ollama_model_info(host: str, model_id: str) -> dict[str, Any]:
    request = urllib.request.Request(
        host.rstrip("/") + "/api/show",
        data=json.dumps({"model": model_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ProviderUnavailableError(
            f"Ollama is not reachable at {host}. Start Ollama and install a tool-capable model. Details: {exc}"
        ) from exc


def _ollama_model(host: str, model_id: str, require_tools: bool) -> Any:
    info = ollama_model_info(host, model_id)
    capabilities = set(info.get("capabilities", []))
    if require_tools and "tools" not in capabilities:
        shown = ", ".join(sorted(capabilities)) or "none reported"
        raise ProviderUnavailableError(
            f"Local Ollama model '{model_id}' does not advertise tool calling (capabilities: {shown}). "
            "Install a tool-capable model and pass --model MODEL_NAME; no model call was made."
        )
    try:
        from strands.models.ollama import OllamaModel
    except ImportError as exc:
        raise ProviderUnavailableError(
            "The Ollama provider dependency is missing. Install the project with .[local-model]."
        ) from exc
    return OllamaModel(host=host, model_id=model_id, temperature=0.1, additional_args={"think": False})


def _bedrock_model(model_id: str) -> Any:
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    credential_hint = any(
        os.getenv(name)
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_PROFILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        )
    )
    missing = []
    if not region:
        missing.append("AWS_REGION or AWS_DEFAULT_REGION")
    if not credential_hint:
        missing.append("an AWS credential source such as AWS_PROFILE, SSO, or access-key environment variables")
    if missing:
        raise ProviderUnavailableError(
            "Bedrock preflight stopped before agent construction or invocation. Missing: " + "; ".join(missing) + ". "
            "The identity also needs enabled model access and bedrock:InvokeModel / "
            "bedrock:InvokeModelWithResponseStream permissions."
        )
    from strands.models import BedrockModel

    return BedrockModel(model_id=model_id, region_name=region)


def build_agent(
    data_dir: Path,
    as_of: date,
    report_dir: Path,
    *,
    provider: str = "ollama",
    model_id: str = "qwen3:14b",
    ollama_host: str = "http://127.0.0.1:11434",
    demo_trace: bool = False,
    require_tool_capability: bool = True,
    focused_decision: bool = False,
) -> AgentRuntime:
    workspace = AgentWorkspace(data_dir, as_of, report_dir)
    toolset = LooseEndsTools(workspace)
    tracer = AgentLoopTracer(workspace, emit=(lambda message: print(message, flush=True)) if demo_trace else None)
    if provider == "ollama":
        model = _ollama_model(ollama_host, model_id, require_tool_capability)
    elif provider == "bedrock":
        model = _bedrock_model(model_id)
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    enabled_tools = (
        [toolset.mark_ready_action, toolset.request_human_decision]
        if focused_decision
        else toolset.all_tools()
    )
    system_prompt = AGENT_SYSTEM_PROMPT
    if focused_decision:
        system_prompt += (
            "\n\nThis invocation is a focused decision over pre-inspected state. Do not perform discovery. "
            "Use mark_ready_action only if the existing evidence is unambiguous; otherwise call "
            "request_human_decision."
        )

    agent = Agent(
        model=model,
        tools=enabled_tools,
        system_prompt=system_prompt,
        callback_handler=None,
        hooks=[tracer],
        name="Loose Ends Agent",
        description="Reconciles unfinished personal obligations and escalates genuine ambiguity.",
        agent_id="loose-ends-agent",
        state={"workflow": "observe_reason_tool_observe_decide_act_or_escalate"},
    )
    workspace.log("initialize", "Instantiated a real Strands Agent.", provider=provider, model_id=model_id)
    return AgentRuntime(agent, workspace, toolset, provider, model_id)


def run_agent(runtime: AgentRuntime) -> Any:
    prompt = (
        f"Reconcile loose ends in {runtime.workspace.data_dir}. "
        f"Resolve relative deadlines as of {runtime.workspace.as_of.isoformat()}. "
        "Inspect sources as needed, build evidence-backed loose ends, and either mark each ready or request a human decision."
    )
    try:
        return runtime.agent(prompt, invocation_state={"data_dir": str(runtime.workspace.data_dir)})
    finally:
        runtime.workspace.report_dir.mkdir(parents=True, exist_ok=True)
        path = runtime.workspace.report_dir / "agent_run_state.json"
        path.write_text(json.dumps(runtime.workspace.snapshot(), indent=2), encoding="utf-8")


def format_runtime_result(runtime: AgentRuntime, result: Any) -> str:
    snapshot = runtime.workspace.snapshot()
    lines = [
        "# Agentic Loose Ends Run",
        "",
        f"Provider: {runtime.provider} / {runtime.model_id}",
        f"Stop reason: {getattr(result, 'stop_reason', 'unknown')}",
        "",
        "## Ready Actions",
        "",
    ]
    ready = [item for item in snapshot["loose_ends"] if item["state"] == "ready_action"]
    pending = [item for item in snapshot["loose_ends"] if item["state"] == "human_decision_required"]
    lines.extend(
        f"- {item['title']}: {item['next_action']}" + (f" (due {item['due_date']})" if item["due_date"] else "")
        for item in ready
    )
    if not ready:
        lines.append("- None recorded before the loop stopped.")
    lines.extend(["", "## Human Judgment", ""])
    lines.extend(f"- {item['title']}: {item['human_decision']['question']}" for item in pending)
    if not pending:
        lines.append("- None pending.")
    lines.extend(["", "## Provenance", "", "See `agent_run_state.json` for evidence and the complete high-level event log.", ""])
    return "\n".join(lines)
