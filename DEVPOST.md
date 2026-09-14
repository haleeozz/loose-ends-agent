# Loose Ends Agent - Devpost Draft

## Inspiration

Personal obligations are scattered across notes, todo exports, and documents.
The hard part is not storing another task. It is recognizing that several
records refer to one commitment, detecting when their details conflict, and
knowing when software should stop and ask a person.

## What It Does

Loose Ends Agent scans local personal sources and builds evidence-backed
"loose ends." It merges genuinely related records, detects deadlines, proposes
concrete next actions, and escalates ambiguous cases through a real
human-in-the-loop workflow.

The submission demo contains two passport records with conflicting deadlines.
The Strands agent discovers both sources, groups them into one obligation, and
calls `request_human_decision`. The browser displays the original records,
paths, and dates while the run is paused. After the user chooses
`2026-09-30`, the same Strands Agent session resumes and creates a grounded
passport action using that date.

## How We Built It

The orchestration brain is a real Strands `Agent` backed locally by the
tool-capable `qwen3:14b` model through Ollama. The agent dynamically chooses
between ten production tools for discovery, deadline analysis, relationship
analysis, loose-end creation, action creation, escalation, and logging.

Deterministic Python utilities provide strong boundaries around the model.
They parse local files, preserve source provenance, validate evidence IDs,
reject unrelated merges, reject unsupported dates and invented entities, and
maintain structured workflow state. Strands still decides which tools to call,
whether evidence belongs together, whether action is safe, and whether a
person must decide.

The product surface is a restrained Starlette dashboard. A `RunManager`
allows only one model run at a time and preserves the same live
`AgentRuntime` across interrupt and resume. A Strands
`context.interrupt(...)` produces `stop_reason=interrupt`; the browser
submits an `interruptResponse` to continue that exact session.

## Why The HITL Is Real

Human escalation is not model prose or a classification tag. It is a
first-class state:

1. The accepted tool call validates its question, options, rationale, and
   provenance as one atomic decision.
2. Strands stops the invocation with `stop_reason=interrupt`.
3. The UI says **Paused - human decision required**, never completed.
4. The user sees both conflicting source records and dates.
5. The response resumes the same Agent instance and advances the invocation
   and resume counters.
6. The final action must be grounded in the selected date and source evidence.

## Challenges

Small local models often produced syntactically valid but semantically
ungrounded tool calls. We addressed that without replacing the agent with a
script: tool contracts return explicit validation outcomes, preserve
provenance, reject cross-item contamination, and make interrupts atomic.

We also learned that truthful UI state matters as much as tool correctness. An
interrupted run must be visibly paused and incomplete, and historical reports
must never look resumable after a server restart.

## Accomplishments

- Genuine Strands orchestration with a local tool-capable model
- Real interrupt and same-session browser resume
- Evidence-level provenance for every decision
- Safety guards for IDs, dates, merges, actions, and escalation options
- Deterministic fallback pipeline for offline tests and comparison
- Local-first operation with no cloud credentials required
- A focused, repeatable judge-facing passport conflict demo

## What We Learned

Agentic software benefits from a clear division of responsibility: the model
handles semantic choices and tool planning, while deterministic contracts
enforce identity, provenance, safety, and workflow truth. HITL is strongest
when it is part of execution state rather than an explanatory message added
afterward.

## What's Next

Future work would add durable session storage, broader mixed-source
convergence, user-approved external actions, and an optional hosted Bedrock
deployment. Those are intentionally outside this local-first hackathon scope.

## Demo

Start the dashboard, click **Start new run**, inspect the passport conflict,
choose `2026-09-30`, and watch the same Strands session produce the grounded
ready action and complete naturally.

## Technology

Python, Strands Agents SDK, Ollama, `qwen3:14b`, Starlette, Uvicorn,
HTML/CSS/JavaScript, and pytest.
