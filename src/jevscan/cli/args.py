"""CLI grammar only; configuration validation is owned by core.config."""

import argparse
from pathlib import Path

from jevscan import __version__


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="jevscan", description="Parallel Tree-sitter + Jev code quality scanner")
    result.add_argument("paths", nargs="*", type=Path, help="files or directories (default: current directory)")
    result.add_argument("--version", action="version", version=f"jevscan {__version__}")
    result.add_argument("--config", type=Path, help="explicit TOML config; otherwise discover nearest jevscan.toml")
    result.add_argument(
        "--init-config",
        nargs="?",
        const="jevscan.toml",
        type=Path,
        metavar="PATH",
        help="write a minimal additive config without overwriting an existing file",
    )
    result.add_argument(
        "--ignore", action="append", default=[], metavar="NAME", help="ignore a rule or ruleset (repeatable)"
    )
    result.add_argument("--show-config", action="store_true", help="print resolved config as TOML and exit")
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
    result.add_argument("--model", help="Jev model ID (overrides environment and TOML)")
    result.add_argument("--no-cache", action="store_true", help="neither read nor write the response cache")
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
        "--fail-on",
        choices=("info", "warning", "error", "never"),
        default="warning",
        help="minimum confirmed finding severity for exit 1; uncertain findings do not fail; operational failures exit 2",
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
