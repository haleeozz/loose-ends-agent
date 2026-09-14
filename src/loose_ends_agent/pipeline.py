from __future__ import annotations

from datetime import date
from pathlib import Path

from .extract import extract_candidates
from .ingest import ingest_directory
from .merge import merge_candidates
from .models import PipelineResult
from .strands_bridge import sdk_status


def run_pipeline(data_dir: Path, as_of: date | None = None) -> PipelineResult:
    effective_date = as_of or date.today()
    ingested = ingest_directory(data_dir)
    candidates = extract_candidates(ingested.items, effective_date)
    available, version = sdk_status()
    return PipelineResult(
        as_of=effective_date,
        files_scanned=ingested.files_scanned,
        source_items=len(ingested.items),
        candidates=len(candidates),
        loose_ends=merge_candidates(candidates),
        strands_available=available,
        strands_version=version,
    )

