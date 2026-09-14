# Loose Ends Agent

Loose Ends Agent is a local-first Strands Agents application for the AWS Agents for Humans Hackathon. A real Strands `Agent` chooses which notes, task exports, and documents to inspect; calls evidence-preserving tools; decides which records describe the same unfinished obligation; and either creates a safe next action or pauses through a Strands human-in-the-loop interrupt.

No tool sends messages, purchases anything, or mutates source documents.

## Architecture

The default `agent` mode uses Strands as the orchestration brain. Its explicit loop is:

```text
observe -> reason -> tool call -> observe result -> decide -> act or escalate
```

The model controls source selection, tool order, semantic grouping, safety decisions, escalation, and next-action wording. Deterministic Python utilities still handle file parsing, candidate extraction, date parsing, and similarity suggestions. Structured workspace state, not model prose, is authoritative for evidence and decisions.

The agent has these tools:

- `list_sources`
- `scan_notes`
- `scan_tasks`
- `inspect_documents`
- `find_deadlines`
- `find_related_items`
- `create_loose_end`
- `mark_ready_action`
- `request_human_decision`
- `write_action_log`

`request_human_decision` records a `human_decision_required` state and raises a real Strands interrupt. A caller can present the interrupt and resume the same agent with an interrupt response.

## Environment

Prerequisites: Python 3.10 or newer and [Ollama](https://ollama.com/) running locally.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,local-model]"
ollama pull qwen3:14b
```

Verify that Strands can construct the real Agent and all tools without invoking a model:

```powershell
loose-ends --check-sdk --as-of 2026-09-12
```

## Local Agent Demo

Agent mode defaults to Ollama. It refuses to invoke models that do not advertise tool calling.

```powershell
loose-ends --demo --provider ollama --model qwen3:14b --as-of 2026-09-12
```

`--demo` prints high-level loop phases and tool calls, never private chain-of-thought. Outputs are written to:

- `reports/agent_result.md`
- `reports/agent_run_state.json`
- `reports/agent_action_log.json` when the agent completes and calls the logging tool

The validated local model is `qwen3:14b`. Model downloads are separate and can be several gigabytes. The preflight refuses to invoke an Ollama model that does not advertise tool support.

## Local Product Demo

Run the judge-facing local dashboard with the validated `qwen3:14b` model:

```powershell
loose-ends-web
```

Open `http://127.0.0.1:8000`. The dashboard starts one model run at a time, shows source coverage and the live high-level tool trail, and presents Strands interrupts as **Paused — human decision required**. Submitting a choice resumes the same in-memory Agent and Strands session.

For the shortest judge-facing path, start a fresh run, wait for the conflicting passport evidence to pause the session, select `2026-09-30`, and submit the decision. The dashboard then shows the same Agent instance and incremented resume counter, followed by the grounded passport ready action. Because source selection and tool order are controlled by the live model, the exact number and order of preceding tool calls can vary.

The checked-in demo corpus is intentionally narrow: one passport note and one passport todo with conflicting deadlines. This keeps the recorded flow reliable while all scanning, grouping, validation, interruption, and resumption still run through the production Strands agent and tools.

The live session intentionally exists only inside the local server process. JSON reports loaded after a restart are labeled historical and read-only; they are never presented as resumable.

Run the focused real-model human-interrupt verification with:

```powershell
.venv\Scripts\python.exe scripts\verify_hitl_agent.py
```

This integration scenario deterministically seeds the already-extracted conflicting passport evidence, then leaves the act-or-escalate decision to a real Strands Agent. It succeeds only when Strands returns `stop_reason=interrupt`.

## Bedrock Demo

```powershell
$env:AWS_PROFILE = "your-profile"
$env:AWS_REGION = "us-east-1"
loose-ends --demo --provider bedrock --model global.anthropic.claude-sonnet-4-6 --as-of 2026-09-12
```

Bedrock requires a valid credential source, a region, access to the selected model, and IAM permissions for `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`. The CLI checks basic configuration and stops before constructing or invoking Bedrock when it is missing.

## Deterministic Fallback

The original local pipeline remains available for tests, offline operation, and comparisons:

```powershell
loose-ends --mode fallback --as-of 2026-09-12 --show-all
```

It writes `reports/loose_ends_report.md` and `reports/loose_ends_report.json`.

## Inputs

- Markdown and text files (`.md`, `.txt`)
- JSON todo exports containing a list, a `todos` list, or a `tasks` list
- Completed lines (`[x]`, `done`, `completed`, `cancelled`) are ignored

JSON records may use `task`, `title`, `text`, or `name`; optional fields include `due`, `status`, and `context`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

## Windows Python Repair

`scripts/repair_python_discovery.ps1` repairs user-level PATH ordering and stale `python.cmd` / `py.cmd` compatibility shims without changing `.venv`. Existing shims are backed up as `*.pre-loose-ends.bak` before the script is run.
