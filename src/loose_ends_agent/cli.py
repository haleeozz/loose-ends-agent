from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .agentic import ProviderUnavailableError, build_agent, format_runtime_result, run_agent
from .pipeline import run_pipeline
from .report import write_reports
from .strands_bridge import sdk_status


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find and reconcile unfinished personal obligations.")
    parser.add_argument("--mode", choices=("agent", "fallback"), default="agent")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--markdown-out", type=Path, default=Path("reports/loose_ends_report.md"))
    parser.add_argument("--json-out", type=Path, default=Path("reports/loose_ends_report.json"))
    parser.add_argument("--as-of", type=_date_value, default=None)
    parser.add_argument("--show-all", action="store_true", help="Print ready actions after review items.")
    parser.add_argument("--demo", action="store_true", help="Show high-level agent loop and tool-call events.")
    parser.add_argument("--provider", choices=("ollama", "bedrock"), default="ollama")
    parser.add_argument("--model", default=None, help="Provider model ID.")
    parser.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    parser.add_argument("--check-sdk", action="store_true", help="Verify Strands and instantiate an Agent without invoking it.")
    return parser


def _run_fallback(args: argparse.Namespace, as_of: date) -> int:
    try:
        result = run_pipeline(args.data_dir, as_of)
        write_reports(result, args.markdown_out, args.json_out)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 2

    print(f"Scanned {result.files_scanned} files; found {len(result.loose_ends)} loose ends.")
    print(f"Needs human judgment: {len(result.judgment_items)}")
    for item in result.judgment_items:
        print(f"  REVIEW  {item.title}")
        for reason in item.judgment_reasons:
            print(f"          {reason}")
    if args.show_all:
        print(f"Ready actions: {len(result.ready_items)}")
        for item in result.ready_items:
            due = item.due_date.isoformat() if item.due_date else "no deadline"
            print(f"  ACTION  {item.title} [{due}]")
    print(f"Markdown report: {args.markdown_out.resolve()}")
    print(f"JSON report: {args.json_out.resolve()}")
    return 0


def _model_id(args: argparse.Namespace) -> str:
    if args.model:
        return args.model
    return "qwen3:14b" if args.provider == "ollama" else "global.anthropic.claude-sonnet-4-6"


def _build_runtime(args: argparse.Namespace, as_of: date, require_tools: bool = True):
    return build_agent(
        args.data_dir,
        as_of,
        args.report_dir,
        provider=args.provider,
        model_id=_model_id(args),
        ollama_host=args.ollama_host,
        demo_trace=args.demo,
        require_tool_capability=require_tools,
    )


def _run_agent(args: argparse.Namespace, as_of: date) -> int:
    try:
        runtime = _build_runtime(args, as_of)
    except (ProviderUnavailableError, ValueError) as exc:
        print(f"Agent preflight stopped safely: {exc}")
        print("Use --mode fallback to run the deterministic testing path.")
        return 3

    print(f"Instantiated Strands Agent with {runtime.provider}/{runtime.model_id} and {len(runtime.tools.all_tools())} tools.")
    try:
        result = run_agent(runtime)
    except Exception as exc:
        print(f"Agent execution failed without falling back or fabricating results: {type(exc).__name__}: {exc}")
        return 4

    report_path = args.report_dir / "agent_result.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(format_runtime_result(runtime, result), encoding="utf-8")
    print(f"Agent stop reason: {result.stop_reason}")
    if result.stop_reason == "interrupt":
        print("Human judgment required:")
        for interrupt in result.interrupts:
            print(json.dumps(interrupt.reason, indent=2))
    summary = runtime.workspace.snapshot()["summary"]
    print(f"Resolved state: {summary}")
    print(f"Agent report: {report_path.resolve()}")
    print(f"Agent state: {(args.report_dir / 'agent_run_state.json').resolve()}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    as_of = args.as_of or date.today()
    if args.check_sdk:
        available, version = sdk_status()
        if not available:
            print("Strands Agents SDK is not importable in this environment.")
            return 1
        try:
            runtime = _build_runtime(args, as_of, require_tools=False)
        except (ProviderUnavailableError, ValueError) as exc:
            print(f"Strands Agents SDK {version} imports, but Agent construction preflight failed: {exc}")
            return 3
        print(
            f"Strands Agents SDK {version}: instantiated {runtime.agent.name} with "
            f"{len(runtime.tools.all_tools())} tools. No model was invoked."
        )
        if args.mode == "agent":
            return 0
    return _run_agent(args, as_of) if args.mode == "agent" else _run_fallback(args, as_of)


if __name__ == "__main__":
    raise SystemExit(main())
