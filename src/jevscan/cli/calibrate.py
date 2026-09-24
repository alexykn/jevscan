"""Command-line composition for offline replay and policy selection."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from jevscan.cli.path_identity import paths_alias as _paths_alias
from jevscan.core.calibration_selection import (
    SelectionAudit,
    SelectionObjective,
    select_policies,
    selected_policy_document,
)
from jevscan.core.rule_writeback import SuppliedRules
from jevscan.core.semantic_calibration import CalibrationCase, load_cases, replay_cases, write_report


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
    parser.add_argument(
        "--allow-incompatible-model-prompt",
        action="store_true",
        help=(
            "allow mixed returned models and prompt contracts during selection; for audited historical experiments only"
        ),
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


def _reject_selection_output_collisions(args: argparse.Namespace) -> None:
    destinations = [path for path in (args.output, args.selected_policy) if path is not None]
    protected = [args.input, args.rules_path, args.objective]
    for index, destination in enumerate(destinations):
        for other in destinations[index + 1 :] + protected:
            if other is not None and _paths_alias(destination, other):
                raise ValueError(f"selection output {destination} aliases {other}; choose distinct paths")


def _reject_replay_output_collisions(args: argparse.Namespace) -> None:
    if args.output is None:
        return
    protected = [args.input, args.report_policy]
    for other in protected:
        if other is not None and _paths_alias(args.output, other):
            raise ValueError(f"replay output {args.output} aliases {other}; choose distinct paths")


def _validate_selection_arguments(args: argparse.Namespace) -> None:
    _reject_selection_output_collisions(args)
    if args.report_policy:
        raise ValueError("--report-policy is replay-only; use --select with supplied rules")
    if args.apply and args.rules_path is None:
        raise ValueError("--apply requires --rules")
    if args.rule_ids and args.rules_path is None:
        raise ValueError("--rule-id requires --rules")


def _select(args: argparse.Namespace, cases: list[CalibrationCase]) -> None:
    _validate_selection_arguments(args)
    objective = _load_objective(args.objective)
    supplied = SuppliedRules.load(args.rules_path, args.rule_ids) if args.rules_path else None
    audit = select_policies(
        cases,
        development_split=args.development_split,
        heldout_split=args.heldout_split,
        objective=objective,
        rules=supplied.rules if supplied is not None else None,
        rule_ids=args.rule_ids or None,
        allow_incompatible_model_prompt=args.allow_incompatible_model_prompt,
    )
    if args.selected_policy:
        _write_selected_policy(audit, args.selected_policy)
    applied = False
    if args.apply:
        assert supplied is not None
        applied = supplied.apply(audit)
    _write_selection_audit(
        audit,
        args.output,
        writeback={
            "path": str(args.rules_path) if args.rules_path else None,
            "requested": args.apply,
            "applied": applied,
        },
    )


def _replay_overrides(args: argparse.Namespace, cases: list[CalibrationCase]) -> dict[str, dict[str, Any]] | None:
    if args.report_policy is None:
        return None
    global_policy, per_rule = _load_policy(args.report_policy)
    if global_policy is not None:
        return {case.rule_id: global_policy for case in cases}
    return per_rule


def _replay(args: argparse.Namespace, cases: list[CalibrationCase]) -> None:
    if args.allow_incompatible_model_prompt:
        raise ValueError("--allow-incompatible-model-prompt requires --select")
    report = replay_cases(cases, _replay_overrides(args, cases))
    if args.output:
        with args.output.open("w", encoding="utf-8") as stream:
            write_report(report, stream)
    else:
        write_report(report, sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.select:
            _reject_replay_output_collisions(args)
        cases = load_cases(args.input)
        if args.select:
            _select(args, cases)
        else:
            _replay(args, cases)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"jevscan-calibrate: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
