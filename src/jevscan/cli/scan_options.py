"""CLI-only configuration overrides, path selection, and safety checks."""

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jevscan.cli.terminal import safe_text
from jevscan.core.config import Config, ConfigError, LoadedConfig
from jevscan.core.languages import language_for


def _set_if(document: dict[str, Any], section: str, key: str, value: Any) -> None:
    if value is not None:
        document[section][key] = value


_OVERRIDES = (
    ("scan", "jobs", "jobs"),
    ("scan", "max_full_file_lines", "max_full_file_lines"),
    ("budget", "max_requests", "max_requests"),
    ("budget", "max_input_tokens", "max_input_tokens"),
    ("budget", "max_cost", "max_cost"),
    ("jev", "concurrency", "concurrency"),
    ("jev", "requests_per_minute", "rpm"),
)


def _enrichment_overrides(document: dict[str, Any], args: Any) -> None:
    if args.no_enrichment:
        document["enrichment"].update({"enabled": False, "mode": "off"})
    if args.enrichment_mode is not None:
        document["enrichment"].update({
            "mode": args.enrichment_mode,
            "enabled": args.enrichment_mode != "off",
        })


def _lint_overrides(document: dict[str, Any], args: Any) -> None:
    if args.rule:
        document["lint"]["select"] = args.rule
    if args.ignore:
        document["lint"]["ignore"] = [*document["lint"]["ignore"], *args.ignore]


def _value_overrides(document: dict[str, Any], args: Any) -> None:
    for section, key, attribute in _OVERRIDES:
        _set_if(document, section, key, getattr(args, attribute))
    model = args.model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or None
    _set_if(document, "jev", "model", model)


def apply_overrides(loaded: LoadedConfig, args: Any) -> LoadedConfig:
    document = loaded.config.model_dump(mode="json")
    _enrichment_overrides(document, args)
    _value_overrides(document, args)
    _lint_overrides(document, args)
    try:
        config = Config.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    return replace(loaded, config=config)


def list_rules(config: Config) -> None:
    selected = config.selected_rules()
    for name, rule in config.rules.items():
        warning = rule.report.levels.warning.model_dump(exclude_none=True)
        error = rule.report.levels.error.model_dump(exclude_none=True)
        print(
            f"{name}\ttitle={safe_text(rule.title).replace(chr(10), ' ')}\truleset={rule.ruleset}"
            f"\tenabled={name in selected}\ttype={rule.question.type}\t"
            f"target={rule.target} context={rule.context} applies_to={','.join(rule.applies_to)}"
            f"\twarning={warning}\terror={error}"
        )


def _source_overlap(path: Path, target: Path) -> bool:
    resolved = target.resolve()
    return path == resolved or (resolved.is_dir() and path.is_relative_to(resolved))


def validate_report_output(output: Path | None, paths: list[Path], config_source: str) -> None:
    if output is None:
        return
    absolute = output.resolve()
    if str(absolute) == config_source:
        raise ConfigError("output path would overwrite the active configuration")
    overlaps_source = any(_source_overlap(absolute, target) and language_for(absolute) for target in paths)
    if overlaps_source:
        raise ConfigError("output path overlaps source being scanned; choose a .json, .jsonl, or .txt report path")


def _calibration_collisions(
    capture: Path,
    sidecar: Path,
    report: Path | None,
    paths: list[Path],
    config_source: str,
) -> tuple[tuple[bool, str], ...]:
    aliases = {capture, sidecar}
    return (
        (
            report is not None and report.resolve() in aliases,
            "--calibration-output collides with the normal report output or its sidecar",
        ),
        (
            config_source != "packaged default" and Path(config_source).resolve() in aliases,
            "--calibration-output collides with the active configuration",
        ),
        (
            any(any(_source_overlap(alias, target) for alias in aliases) for target in paths),
            "--calibration-output collides with a scanned source",
        ),
        (
            capture.exists() or sidecar.exists(),
            "--calibration-output or its metadata sidecar already exists; choose a new path",
        ),
    )


def validate_calibration_output(
    output: Path | None,
    report: Path | None,
    paths: list[Path],
    config_source: str,
) -> None:
    if output is None:
        return
    capture = output.resolve()
    sidecar = output.with_suffix(".meta.json").resolve()
    for collision, message in _calibration_collisions(capture, sidecar, report, paths, config_source):
        if collision:
            raise ConfigError(message)


def validate_scan_options(config: Config, args: Any) -> None:
    invalid = (
        (args.max_display < 0, "--max-display must be nonnegative"),
        (args.plan and args.offline, "--plan and --offline are separate modes; choose one"),
        (
            not args.offline and not config.selected_rules(),
            "there are no enabled rules; use --offline for an inventory or enable a rule",
        ),
        (
            args.calibration_output is not None and (args.offline or args.plan),
            "--calibration-output requires a live scan",
        ),
    )
    for failed, message in invalid:
        if failed:
            raise ConfigError(message)


def _run_git(git: str, root: Path, *args: str) -> list[str]:
    try:
        result = subprocess.run(  # noqa: S603 -- fixed Git executable and fixed command families below
            [git, "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"cannot select Git-changed files: {exc}") from exc
    return [line for line in result.stdout.splitlines() if line]


def _changed_names(git: str, root: Path, staged: bool) -> list[str]:
    diff_base = ["--cached"] if staged else ["HEAD"]
    names = _run_git(git, root, "diff", "--name-only", "--diff-filter=ACMR", *diff_base)
    if not staged:
        names.extend(_run_git(git, root, "ls-files", "--others", "--exclude-standard"))
    return list(dict.fromkeys(names))


def _candidate_path(root: Path, name: str) -> Path | None:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return Path(os.path.abspath(root / relative))  # noqa: PTH100 -- do not resolve symlinks here


def _requested_path(candidate: Path, requested: list[Path]) -> bool:
    return any(candidate == target or (target.is_dir() and candidate.is_relative_to(target)) for target in requested)


def git_selected_paths(root: Path, requested: list[Path], *, staged: bool) -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise ConfigError("cannot select Git-changed files: git executable not found")
    candidates = (_candidate_path(root, name) for name in _changed_names(git, root, staged))
    return [candidate for candidate in candidates if candidate is not None and _requested_path(candidate, requested)]
