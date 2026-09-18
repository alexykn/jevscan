"""CLI grammar only; configuration validation is owned by core.config."""

import argparse
from pathlib import Path

from jevscan import __version__


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="jevscan", description="Parallel Tree-sitter + Jev code quality scanner")
    result.add_argument("paths", nargs="*", type=Path, help="files or directories (default: current directory)")
    result.add_argument("--version", action="version", version=f"jevscan {__version__}")
    result.add_argument("--config", type=Path, help="explicit YAML config; otherwise discover nearest jevscan.yaml")
    result.add_argument(
        "--init-config",
        nargs="?",
        const="jevscan.yaml",
        type=Path,
        metavar="PATH",
        help="write the packaged config without overwriting an existing file",
    )
    result.add_argument("--show-config", action="store_true", help="print resolved config as YAML and exit")
    result.add_argument("--list-rules", action="store_true", help="show configured rules and exit")
    result.add_argument(
        "--rule", action="append", default=[], metavar="ID", help="limit analysis to this rule (repeatable)"
    )
    result.add_argument(
        "--offline", action="store_true", help="parse and list units only; no API calls or cache writes"
    )
    result.add_argument("--jobs", type=int, help="parser processes; 0 selects automatically")
    result.add_argument("--concurrency", type=int, help="maximum concurrent Jev evaluations")
    result.add_argument("--rpm", type=float, help="paced request attempts/minute; 0 disables pacing")
    result.add_argument("--model", help="Jev model ID (overrides environment and YAML)")
    result.add_argument("--no-cache", action="store_true", help="neither read nor write the response cache")
    result.add_argument(
        "--format", choices=("text", "json", "jsonl"), default="text", help="report format (default: text)"
    )
    result.add_argument(
        "-v", "--verbose", action="store_true", help="show all evaluated answers, not just warnings/errors"
    )
    result.add_argument("-o", "--output", type=Path, help="write report to this file instead of stdout")
    result.add_argument(
        "--fail-on",
        choices=("info", "warning", "error", "never"),
        default="warning",
        help="minimum finding severity for exit 1; operational failures always exit 2",
    )
    result.add_argument(
        "--max-display",
        type=int,
        default=0,
        help="text target limit after filtering; 0 means unlimited (JSON is never limited)",
    )
    result.add_argument(
        "--no-enrichment", action="store_true", help="disable uncertainty routing and supplemental source retrieval"
    )
    return result
