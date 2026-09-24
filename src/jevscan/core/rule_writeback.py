"""Round-trip supplied rule documents and publish validated threshold changes.

This module owns YAML mutation and filesystem publication. Selection still belongs
exclusively to calibration_selection; packaged rules never enter its input here.
"""

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, BinaryIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

from jevscan.core.calibration_selection import SelectionAudit
from jevscan.core.config import load_config
from jevscan.core.rules import Rule

_THRESHOLD_FIELDS = ("min_probability", "min_confidence", "min_score", "max_score", "score_levels")


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


def _rule_entries(document: Any) -> dict[str, CommentedMap]:
    if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
        raise TypeError("supplied rules YAML must contain a rules list")
    entries: dict[str, CommentedMap] = {}
    for entry in document["rules"]:
        if not isinstance(entry, CommentedMap) or not isinstance(entry.get("name"), str):
            raise TypeError("each supplied rule must be a mapping with a name")
        name = entry["name"]
        if not name or name in entries:
            raise ValueError(f"duplicate or empty supplied rule ID: {name!r}")
        entries[name] = entry
    return entries


def _decode_document(original: bytes, operation: str) -> Any:
    try:
        document = _roundtrip_yaml().load(original.decode("utf-8"))
    except (UnicodeError, DuplicateKeyError, YAMLError) as exc:
        raise ValueError(f"cannot {operation} rules YAML: {exc}") from exc
    _reject_aliases(document)
    return document


def _validated_rules(document: Any) -> dict[str, Rule]:
    return {
        name: Rule.model_validate({key: value for key, value in entry.items() if key != "name"})
        for name, entry in _rule_entries(document).items()
    }


def _select_supplied_rules(document: Any, requested: list[str]) -> dict[str, Rule]:
    # Validate even unselected definitions: this file is the authoritative input.
    rules = _validated_rules(document)
    requested_ids = set(requested)
    unknown = requested_ids - rules.keys()
    if unknown:
        raise ValueError(f"requested rule IDs are not present in supplied YAML: {', '.join(sorted(unknown))}")
    selected = {name: rule for name, rule in rules.items() if not requested_ids or name in requested_ids}
    if not selected:
        raise ValueError("supplied YAML contains no selected rules")
    return selected


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
    entries = _rule_entries(document)
    changed = False
    for rule_id, selected in audit.selected_policies.items():
        entry = entries.get(rule_id)
        if entry is None:
            raise ValueError(f"selected rule {rule_id!r} disappeared from supplied YAML")
        report = entry.get("report")
        if not isinstance(report, CommentedMap) or not isinstance(report.get("levels"), CommentedMap):
            raise TypeError(f"rule {rule_id!r} has no report levels to update")
        levels = report["levels"]
        before_levels = rules[rule_id].report.levels.model_dump(mode="python")
        after_levels = selected.levels.model_dump(mode="python")
        for severity in ("warning", "error"):
            level = levels.get(severity)
            if not isinstance(level, CommentedMap):
                raise TypeError(f"rule {rule_id!r} has no {severity} report threshold")
            changed |= _apply_policy_values(level, before_levels[severity], after_levels[severity])
    return changed


def _roundtrip_bytes(document: Any) -> bytes:
    output = StringIO()
    try:
        _roundtrip_yaml().dump(document, output)
    except (TypeError, ValueError, YAMLError) as exc:
        raise ValueError(f"cannot serialize selected rules YAML: {exc}") from exc
    encoded = output.getvalue().encode("utf-8")
    _validated_rules(_decode_document(encoded, "validate selected"))
    return encoded


@contextmanager
def _temporary_yaml(path: Path, purpose: str) -> Iterator[tuple[Path, BinaryIO]]:
    """Own cleanup from creation, including failed writes and interrupted validation."""
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=path.suffix, prefix=f".{path.name}.{purpose}-", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            yield temporary, stream
        finally:
            temporary.unlink(missing_ok=True)


def _validate_configuration(path: Path, encoded: bytes) -> None:
    # The normal loader validates the complete configuration, but its additive
    # packaged rules are never returned to the selector.
    with _temporary_yaml(path, "validate") as (temporary, stream):
        stream.write(encoded)
        stream.flush()
        load_config([path.parent], explicit=temporary)


def _require_unchanged(path: Path, original: bytes, original_stat: os.stat_result) -> None:
    current_stat = path.stat()
    if (
        current_stat.st_dev != original_stat.st_dev
        or current_stat.st_ino != original_stat.st_ino
        or path.read_bytes() != original
    ):
        raise ValueError("supplied rules YAML changed while selection was running; refusing to overwrite it")


def _publish(path: Path, encoded: bytes, original: bytes, original_stat: os.stat_result) -> None:
    with _temporary_yaml(path, "selection") as (temporary, stream):
        stream.write(encoded)
        temporary.chmod(stat.S_IMODE(original_stat.st_mode))
        stream.flush()
        os.fsync(stream.fileno())
        _require_unchanged(path, original, original_stat)
        temporary.replace(path)


@dataclass(frozen=True)
class SuppliedRules:
    """One supplied YAML snapshot, its selected definitions, and its write-back boundary."""

    path: Path
    rules: dict[str, Rule]
    document: Any
    original: bytes

    @classmethod
    def load(cls, path: Path, requested: list[str]) -> "SuppliedRules":
        if path.is_symlink():
            raise ValueError("supplied rules path must not be a symlink")
        original = path.read_bytes()
        document = _decode_document(original, "read supplied")
        return cls(path, _select_supplied_rules(document, requested), document, original)

    def apply(self, audit: SelectionAudit) -> bool:
        original_stat = self.path.stat()
        if not _apply_to_document(self.document, audit, self.rules):
            return False
        encoded = _roundtrip_bytes(self.document)
        _validate_configuration(self.path, encoded)
        _publish(self.path, encoded, self.original, original_stat)
        return True
