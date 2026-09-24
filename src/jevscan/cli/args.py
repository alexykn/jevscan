"""CLI grammar only; configuration validation is owned by core.config."""

import argparse
from pathlib import Path

from jevscan import __version__


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="jevscan", description="Parallel Tree-sitter + Jev code quality scanner")
    result.add_argument("paths", nargs="*", type=Path, help="files or directories (default: current directory)")
    result.add_argument("--version", action="version", version=f"jevscan {__version__}")
    result.add_argument(
        "--config", type=Path, help="explicit YAML config; otherwise discover nearest jevscan.yaml/jevscan.yml"
    )
    result.add_argument(
        "--init-config",
        nargs="?",
        const="jevscan.yaml",
        type=Path,
        metavar="PATH",
        help="write a minimal additive config without overwriting an existing file",
    )
    result.add_argument(
        "--ignore", action="append", default=[], metavar="NAME", help="ignore a rule or ruleset (repeatable)"
    )
    result.add_argument("--show-config", action="store_true", help="print resolved config as YAML and exit")
    result.add_argument("--list-rules", action="store_true", help="show configured rules and exit")
    result.add_argument(
        "--rule", "--select", action="append", default=[], metavar="NAME", help="select a rule or ruleset (repeatable)"
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
        "--plan", action="store_true", help="parse and estimate live request packing/cost without making API calls"
    )
    result.add_argument("--max-requests", type=int, help="hard live request-attempt budget")
    result.add_argument("--max-input-tokens", type=int, help="conservative estimated input-token budget")
    result.add_argument("--max-cost", type=float, help="conservative estimated input-cost budget")
    result.add_argument(
        "--enrichment-mode", choices=("off", "targeted", "full"), help="cross-source enrichment breadth"
    )
    result.add_argument("--max-full-file-lines", type=int, help="omit checks that require a larger complete file")
    vcs = result.add_mutually_exclusive_group()
    vcs.add_argument("--changed", action="store_true", help="scan only tracked/untracked files changed from HEAD")
    vcs.add_argument("--staged", action="store_true", help="scan only files staged in Git")
    result.add_argument(
        "--format", choices=("text", "json", "jsonl"), default="text", help="report format (default: text)"
    )
    result.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show all answers, including below-threshold uncertainty and OK results",
    )
    result.add_argument("-o", "--output", type=Path, help="write report to this file instead of stdout")
    result.add_argument(
        "--calibration-output",
        type=Path,
        help="opt-in final-judgment capture JSONL; source snapshots are included only in this file",
    )
    result.add_argument(
        "--fail-on",
        choices=("info", "warning", "error", "never"),
        default="warning",
        help="minimum exit-blocking confirmed finding severity for exit 1; uncertain/advisory findings do not fail",
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
