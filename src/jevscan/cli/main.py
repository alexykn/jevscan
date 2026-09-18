"""CLI entry point. All scan behavior is delegated to the core."""

import asyncio
import os
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from jevscan import __version__
from jevscan.cli.args import parser
from jevscan.cli.render import Reporter
from jevscan.core.config import Config, ConfigError, LoadedConfig, default_yaml, load_config
from jevscan.core.languages import language_for
from jevscan.core.scanner import run_scan, worker_count


def _overrides(loaded: LoadedConfig, args: Any) -> LoadedConfig:
    document = loaded.config.model_dump(mode="json")
    if args.jobs is not None:
        document["scan"]["jobs"] = args.jobs
    for key, value in (("concurrency", args.concurrency), ("requests_per_minute", args.rpm),
                       ("model", args.model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or None)):
        if value is not None:
            document["jev"][key] = value
    if args.rule:
        unknown = set(args.rule) - document["rules"].keys()
        if unknown:
            raise ConfigError(f"unknown rule IDs: {', '.join(sorted(unknown))}")
        document["rules"] = {name: rule for name, rule in document["rules"].items() if name in args.rule}
    try:
        config = Config.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    return replace(loaded, config=config)


def _list_rules(config: Config, no_color: bool) -> None:
    table = Table(title="jevscan rules", header_style="bold cyan")
    for column in ("Rule", "Enabled", "Question", "Applies to", "Severity"):
        table.add_column(column)
    for name, rule in config.rules.items():
        table.add_row(Text(name), "yes" if rule.enabled else "no", rule.question.type,
                      Text(", ".join(rule.applies_to)), str(rule.report.severity))
    Console(no_color=no_color).print(table)


def _validate_output(output: Path | None, paths: list[Path], config_source: str) -> None:
    if output is None:
        return
    absolute = output.resolve()
    if str(absolute) == config_source:
        raise ConfigError("output path would overwrite the active configuration")
    for target in paths:
        target = target.resolve()
        if absolute == target or (target.is_dir() and absolute.is_relative_to(target) and language_for(absolute)):
            raise ConfigError("output path overlaps source being scanned; choose a .json, .jsonl, or .txt report path")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    console = Console(stderr=True, no_color=args.no_color, highlight=False)
    try:
        if args.init_config:
            with args.init_config.open("x", encoding="utf-8") as stream:
                stream.write(default_yaml())
            console.print(Text(f"Created {args.init_config}"))
            return 0
        paths = [path.absolute() for path in (args.paths or [Path.cwd()])]
        loaded = _overrides(load_config(paths, args.config), args)
        if args.show_config:
            sys.stdout.write(yaml.safe_dump(loaded.config.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
            return 0
        if args.list_rules:
            _list_rules(loaded.config, args.no_color)
            return 0
        if args.max_display < 0:
            raise ConfigError("--max-display must be nonnegative")
        if not args.offline and not any(rule.enabled for rule in loaded.config.rules.values()):
            raise ConfigError("there are no enabled rules; use --offline for an inventory or enable a rule")
        _validate_output(args.output, paths, loaded.source)
        metadata = {
            "version": __version__, "root": str(loaded.root), "config": loaded.source,
            "mode": "offline" if args.offline else "live", "model": loaded.config.jev.model,
            "parser_processes": worker_count(loaded.config), "concurrency": 0 if args.offline else loaded.config.jev.concurrency,
            "targets": [str(path) for path in paths],
        }
        context = args.output.open("w", encoding="utf-8") if args.output else nullcontext(sys.stdout)
        with context as output:
            reporter = Reporter(output, args.format, metadata, no_color=args.no_color,
                                quiet=args.quiet, max_display=args.max_display)
            summary = asyncio.run(run_scan(paths, loaded, reporter, offline=args.offline, no_cache=args.no_cache,
                api_key=os.environ.get("TYPESAFE_API_KEY", ""),
                base_url=os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.typesafe.ai"))
        return summary.exit_code(args.fail_on)
    except KeyboardInterrupt:
        console.print("Interrupted.", style="yellow")
        return 130
    except (ConfigError, OSError, ValueError) as exc:
        console.print(Text(f"jevscan: {exc}", style="red"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
