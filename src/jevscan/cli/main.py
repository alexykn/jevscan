"""CLI entry point. All scan behavior is delegated to the core."""

import asyncio
import os
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jevscan import __version__
from jevscan.cli.args import parser
from jevscan.cli.render import Reporter
from jevscan.cli.terminal import safe_text
from jevscan.core.config import (
    Config,
    ConfigError,
    LoadedConfig,
    initial_yaml,
    load_config,
    resolved_yaml,
    validate_config_path,
)
from jevscan.core.languages import language_for
from jevscan.core.scanner import run_scan, worker_count


def _overrides(loaded: LoadedConfig, args: Any) -> LoadedConfig:
    document = loaded.config.model_dump(mode="json")
    if args.no_enrichment:
        document["enrichment"]["enabled"] = False
    if args.jobs is not None:
        document["scan"]["jobs"] = args.jobs
    for key, value in (
        ("concurrency", args.concurrency),
        ("requests_per_minute", args.rpm),
        ("model", args.model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or None),
    ):
        if value is not None:
            document["jev"][key] = value
    if args.rule:
        document["lint"]["select"] = args.rule
    if args.ignore:
        document["lint"]["ignore"] = [*document["lint"]["ignore"], *args.ignore]
    try:
        config = Config.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    return replace(loaded, config=config)


def _list_rules(config: Config) -> None:
    selected = config.selected_rules()
    for name, rule in config.rules.items():
        warning = rule.report.levels.warning.model_dump(exclude_none=True)
        error = rule.report.levels.error.model_dump(exclude_none=True)
        print(
            f"{name}\ttitle={safe_text(rule.title).replace(chr(10), ' ')}\truleset={rule.ruleset}\tenabled={name in selected}\ttype={rule.question.type}\t"
            f"target={rule.target} context={rule.context} applies_to={','.join(rule.applies_to)}\twarning={warning}\terror={error}"
        )


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


def _validate_scan_options(config: Config, args: Any) -> None:
    if args.max_display < 0:
        raise ConfigError("--max-display must be nonnegative")
    if not args.offline and not config.selected_rules():
        raise ConfigError("there are no enabled rules; use --offline for an inventory or enable a rule")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.init_config:
            validate_config_path(args.init_config)
            with args.init_config.open("x", encoding="utf-8") as stream:
                stream.write(initial_yaml())
            print(f"Created {args.init_config}")
            return 0
        paths = [path.absolute() for path in (args.paths or [Path.cwd()])]
        loaded = _overrides(load_config(paths, args.config), args)
        if args.show_config:
            sys.stdout.write(resolved_yaml(loaded.config))
            return 0
        if args.list_rules:
            _list_rules(loaded.config)
            return 0
        _validate_scan_options(loaded.config, args)
        _validate_output(args.output, paths, loaded.source)
        metadata = {
            "version": __version__,
            "root": str(loaded.root),
            "config": loaded.source,
            "mode": "offline" if args.offline else "live",
            "model": loaded.config.jev.model,
            "parser_processes": worker_count(loaded.config),
            "concurrency": 0 if args.offline else loaded.config.jev.concurrency,
            "targets": [str(path) for path in paths],
        }
        context = args.output.open("w", encoding="utf-8") if args.output else nullcontext(sys.stdout)
        with context as output:
            reporter = Reporter(output, args.format, metadata, max_display=args.max_display, verbose=args.verbose)
            summary = asyncio.run(
                run_scan(
                    paths,
                    loaded,
                    reporter,
                    offline=args.offline,
                    no_cache=args.no_cache,
                    api_key=os.environ.get("TYPESAFE_API_KEY", ""),
                    base_url=os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.typesafe.ai",
                )
            )
        return summary.exit_code(args.fail_on)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (ConfigError, OSError, ValueError) as exc:
        print(f"jevscan: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
