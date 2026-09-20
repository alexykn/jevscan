"""Command-line entry point for offline semantic calibration replay."""

import argparse
import json
import os
import stat
import sys
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

from jevscan.core.calibration_selection import (
    SelectionAudit,
    SelectionObjective,
    select_policies,
    selected_policy_document,
)
from jevscan.core.config import load_config
from jevscan.core.rules import Rule
from jevscan.core.semantic_calibration import load_cases, replay_cases, write_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jevscan-calibrate",
        description="Replay versioned semantic-calibration JSONL cases through jevscan assessment.",
    )
    parser.add_argument("input", type=Path, help="strict semantic-calibration JSONL file")
    parser.add_argument("-o", "--output", type=Path, help="write the JSON report here instead of stdout")
    parser.add_argument(
        "--report-policy",
        type=Path,
        help="optional YAML/JSON report policy, or a mapping of arbitrary rule IDs to policies",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="select reporting thresholds from development labels instead of replaying only",
    )
    parser.add_argument(
        "--selected-policy",
        type=Path,
        help="write selected policies in the existing --report-policy YAML format",
    )
    parser.add_argument(
        "--rules",
        "--config",
        dest="rules_path",
        type=Path,
        help="supplied YAML ruleset authoritative for --select (built-ins are never imported)",
    )
    parser.add_argument(
        "--rule-id",
        "--rule",
        dest="rule_ids",
        action="append",
        default=[],
        metavar="ID",
        help="select this supplied rule ID (repeatable); without it, every supplied rule is selected",
    )
    parser.add_argument(
        "--development-split",
        default="development",
        metavar="NAME",
        help="case split used for selection (default: development)",
    )
    parser.add_argument("--heldout-split", metavar="NAME", help="optional frozen split reported after selection")
    parser.add_argument("--objective", type=Path, help="optional YAML/JSON selection utility configuration")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically write selected threshold fields back to --rules after validation",
    )
    return parser


def _load_policy(path: Path) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]] | None]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise TypeError("report policy must be a YAML mapping")
    if "levels" in value:
        return value, None
    if "rules" in value:
        value = value["rules"]
    if not isinstance(value, dict) or any(not isinstance(policy, dict) for policy in value.values()):
        raise ValueError("report policy must be one policy or a rule-ID-to-policy mapping")
    return None, value


def _roundtrip_yaml() -> YAML:
    result = YAML(typ="rt")
    result.preserve_quotes = True
    result.allow_duplicate_keys = False
    return result


def _reject_aliases(value: Any, seen: set[int] | None = None) -> None:
    """Aliases make targeted write-back ambiguous, so reject them explicitly."""
    seen = set() if seen is None else seen
    if not isinstance(value, (dict, list)):
        return
    identity = id(value)
    if identity in seen:
        raise ValueError("rules YAML aliases are not supported for selection write-back")
    seen.add(identity)
    values = value.values() if isinstance(value, dict) else value
    for child in values:
        _reject_aliases(child, seen)


def _rule_entries(document: Any) -> list[tuple[str, CommentedMap]]:
    if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
        raise TypeError("supplied rules YAML must contain a rules list")
    entries: list[tuple[str, CommentedMap]] = []
    names: set[str] = set()
    for entry in document["rules"]:
        if not isinstance(entry, CommentedMap) or not isinstance(entry.get("name"), str):
            raise TypeError("each supplied rule must be a mapping with a name")
        name = entry["name"]
        if not name or name in names:
            raise ValueError(f"duplicate or empty supplied rule ID: {name!r}")
        names.add(name)
        entries.append((name, entry))
    return entries


def _load_supplied_rules(path: Path, requested: list[str]) -> tuple[dict[str, Rule], Any, bytes]:
    if path.is_symlink():
        raise ValueError("supplied rules path must not be a symlink")
    original = path.read_bytes()
    parser = _roundtrip_yaml()
    try:
        document = parser.load(original.decode("utf-8"))
    except (UnicodeError, DuplicateKeyError, YAMLError) as exc:
        raise ValueError(f"cannot read supplied rules YAML: {exc}") from exc
    _reject_aliases(document)
    rules: dict[str, Rule] = {}
    requested_ids = set(requested)
    for name, entry in _rule_entries(document):
        candidate = Rule.model_validate({key: value for key, value in entry.items() if key != "name"})
        if not requested_ids or name in requested_ids:
            rules[name] = candidate
    unknown = requested_ids - set(rules)
    if unknown:
        raise ValueError(f"requested rule IDs are not present in supplied YAML: {', '.join(sorted(unknown))}")
    if not rules:
        raise ValueError("supplied YAML contains no selected rules")
    return rules, document, original


_THRESHOLD_FIELDS = ("min_probability", "min_confidence", "min_score", "max_score", "score_levels")


def _apply_policy_values(level: CommentedMap, before: dict[str, Any], after: dict[str, Any]) -> bool:
    changed = False
    for field in _THRESHOLD_FIELDS:
        old, new = before.get(field), after.get(field)
        if old == new:
            continue
        changed = True
        if new is None:
            level.pop(field, None)
        elif field == "score_levels" and isinstance(level.get(field), CommentedSeq):
            level[field][:] = list(new)
        else:
            level[field] = new
    return changed


def _apply_to_document(document: Any, audit: SelectionAudit, rules: dict[str, Rule]) -> bool:
    entries = dict(_rule_entries(document))
    changed = False
    for rule_id, selected in audit.selected_policies.items():
        entry = entries.get(rule_id)
        if entry is None:
            raise ValueError(f"selected rule {rule_id!r} disappeared from supplied YAML")
        report = entry.get("report")
        if not isinstance(report, CommentedMap) or not isinstance(report.get("levels"), CommentedMap):
            raise TypeError(f"rule {rule_id!r} has no report levels to update")
        levels = report["levels"]
        before = rules[rule_id].report
        before_levels = before.levels.model_dump(mode="python")
        after_levels = selected.levels.model_dump(mode="python")
        for severity in ("warning", "error"):
            level = levels.get(severity)
            if not isinstance(level, CommentedMap):
                raise TypeError(f"rule {rule_id!r} has no {severity} report threshold")
            changed |= _apply_policy_values(level, before_levels[severity], after_levels[severity])
    return changed


def _validate_roundtripped_rules(path: Path, document: Any) -> bytes:
    output = StringIO()
    writer = _roundtrip_yaml()
    try:
        writer.dump(document, output)
    except (TypeError, ValueError, YAMLError) as exc:
        raise ValueError(f"cannot serialize selected rules YAML: {exc}") from exc
    encoded = output.getvalue().encode("utf-8")
    parser = _roundtrip_yaml()
    try:
        reparsed = parser.load(encoded.decode("utf-8"))
    except (UnicodeError, DuplicateKeyError, YAMLError) as exc:
        raise ValueError(f"cannot validate selected rules YAML: {exc}") from exc
    _reject_aliases(reparsed)
    for _, entry in _rule_entries(reparsed):
        Rule.model_validate({key: value for key, value in entry.items() if key != "name"})
    # Validate the complete existing configuration through the normal loader;
    # this only validates the file and never supplies rules to selection.
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=path.suffix, prefix=f".{path.name}.validate-", dir=path.parent, delete=False
    ) as stream:
        validation_path = Path(stream.name)
        stream.write(encoded)
    try:
        load_config([path.parent], explicit=validation_path)
    finally:
        validation_path.unlink(missing_ok=True)
    return encoded


def _apply_selected_rules(
    path: Path, document: Any, original: bytes, audit: SelectionAudit, rules: dict[str, Rule]
) -> bool:
    original_stat = path.stat()
    changed = _apply_to_document(document, audit, rules)
    if not changed:
        return False
    encoded = _validate_roundtripped_rules(path, document)
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=path.suffix, prefix=f".{path.name}.selection-", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
        temporary.chmod(stat.S_IMODE(original_stat.st_mode))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        current_stat = path.stat()
        if (
            current_stat.st_dev != original_stat.st_dev
            or current_stat.st_ino != original_stat.st_ino
            or path.read_bytes() != original
        ):
            raise ValueError("supplied rules YAML changed while selection was running; refusing to overwrite it")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _load_objective(path: Path | None) -> SelectionObjective:
    if path is None:
        return SelectionObjective.default()
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise TypeError("selection objective must be a YAML mapping")
    return SelectionObjective.from_mapping(value)


def _write_selection_audit(audit: SelectionAudit, destination: Path | None, *, writeback: dict[str, Any]) -> None:
    value = audit.as_dict()
    value["writeback"] = writeback
    if destination:
        with destination.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    else:
        json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        sys.stdout.write("\n")


def _write_selected_policy(audit: SelectionAudit, destination: Path) -> None:
    with destination.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(selected_policy_document(audit), stream, sort_keys=False, allow_unicode=True)


def _paths_alias(first: Path, second: Path) -> bool:
    try:
        if first.exists() and second.exists() and first.samefile(second):
            return True
    except OSError:
        pass
    return first.resolve(strict=False) == second.resolve(strict=False)


def _reject_selection_output_collisions(args: Any) -> None:
    destinations = [path for path in (args.output, args.selected_policy) if path is not None]
    protected = [args.input, args.rules_path, args.objective]
    for index, destination in enumerate(destinations):
        for other in destinations[index + 1 :] + protected:
            if other is not None and _paths_alias(destination, other):
                raise ValueError(f"selection output {destination} aliases {other}; choose distinct paths")


def _selection_argument_error(message: str) -> None:
    raise ValueError(message)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cases = load_cases(args.input)
        if args.select:
            _reject_selection_output_collisions(args)
            if args.report_policy:
                _selection_argument_error("--report-policy is replay-only; use --select with supplied rules")
            if args.apply and args.rules_path is None:
                _selection_argument_error("--apply requires --rules")
            objective = _load_objective(args.objective)
            rules = None
            supplied_document = None
            supplied_bytes = None
            if args.rules_path:
                rules, supplied_document, supplied_bytes = _load_supplied_rules(args.rules_path, args.rule_ids)
            elif args.rule_ids:
                _selection_argument_error("--rule-id requires --rules")
            audit = select_policies(
                cases,
                development_split=args.development_split,
                heldout_split=args.heldout_split,
                objective=objective,
                rules=rules,
                rule_ids=args.rule_ids or None,
            )
            if args.selected_policy:
                _write_selected_policy(audit, args.selected_policy)
            applied = False
            if args.apply:
                assert args.rules_path is not None and supplied_document is not None and supplied_bytes is not None
                applied = _apply_selected_rules(
                    args.rules_path,
                    supplied_document,
                    supplied_bytes,
                    audit,
                    rules or {},
                )
            _write_selection_audit(
                audit,
                args.output,
                writeback={
                    "path": str(args.rules_path) if args.rules_path else None,
                    "requested": args.apply,
                    "applied": applied,
                },
            )
            return 0
        global_policy, per_rule = _load_policy(args.report_policy) if args.report_policy else (None, None)
        overrides = None
        if global_policy is not None:
            overrides = {case.rule_id: global_policy for case in cases}
        elif per_rule is not None:
            overrides = per_rule
        report = replay_cases(cases, overrides)
        if args.output:
            with args.output.open("w", encoding="utf-8") as stream:
                write_report(report, stream)
        else:
            write_report(report, sys.stdout)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"jevscan-calibrate: {exc}", file=sys.stderr)
        return 2
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
