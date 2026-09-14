from __future__ import annotations

from importlib import metadata
from typing import Any


SYSTEM_PROMPT = """You are Loose Ends Agent. Help a person reconcile unfinished obligations.
Prefer evidence from local sources, merge duplicates, preserve conflicting deadlines, and request
human judgment only when ambiguity prevents a safe concrete action. Never send messages or mutate
external systems without explicit authorization."""


def sdk_status() -> tuple[bool, str | None]:
    try:
        __import__("strands")
        return True, metadata.version("strands-agents")
    except (ImportError, metadata.PackageNotFoundError):
        return False, None


def local_tools() -> list[Any]:
    """Expose the deterministic scanner as a Strands tool without invoking a model."""
    from strands import tool

    @tool
    def scan_local_loose_ends(data_dir: str = "data", as_of: str | None = None) -> dict[str, Any]:
        """Scan local files for unresolved personal obligations.

        Args:
            data_dir: Directory containing Markdown, text, and JSON source files.
            as_of: Optional ISO date used to resolve relative deadlines reproducibly.
        """
        from datetime import date
        from pathlib import Path

        from .pipeline import run_pipeline

        effective_date = date.fromisoformat(as_of) if as_of else None
        return run_pipeline(Path(data_dir), effective_date).to_dict()

    return [scan_local_loose_ends]


def create_strands_agent(*, model: Any, tools: list[Any] | None = None) -> Any:
    """Create a Strands agent only when the caller supplies an explicit model provider."""
    if model is None:
        raise ValueError(
            "An explicit Strands model is required. The SDK default is Amazon Bedrock, "
            "which needs AWS credentials, region configuration, model access, and invoke permission."
        )
    from strands import Agent

    return Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools if tools is not None else local_tools())
