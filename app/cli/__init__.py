"""CLI helpers for running packaged entrypoints."""

from __future__ import annotations

from typing import Iterable, Optional

from ..ingesta_unificada import main as ingest_main


def run_ingesta(args: Optional[Iterable[str]] = None) -> int:
    """Execute the unified ingest routine with optional CLI args."""
    cli_args = list(args) if args is not None else None
    ingest_main(cli_args)
    return 0
