"""Internal scan contracts. Source offsets are UTF-8 byte offsets; lines are one-based."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLOSURE = "closure"
    CLASS = "class"
    STRUCT = "struct"
    ENUM = "enum"
    TRAIT = "trait"
    IMPL = "impl"
    INTERFACE = "interface"
    TYPE = "type"
    MODULE = "module"
    PACKAGE = "package"


CALLABLE_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CLOSURE})
TYPE_KINDS = frozenset({Kind.CLASS, Kind.STRUCT, Kind.ENUM, Kind.TRAIT, Kind.INTERFACE, Kind.TYPE})


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}


@dataclass(frozen=True, slots=True)
class Unit:
    id: str
    path: str
    language: str
    kind: Kind
    name: str
    qualified_name: str
    parent_id: str | None
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    signature: str
    has_body: bool
    member_count: int = 0
    branch_nodes: int = 0

    def metadata(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    path: str
    code: str
    message: str
    severity: Severity = Severity.WARNING
    line: int | None = None
    incomplete: bool = True


@dataclass(frozen=True, slots=True)
class ParsedFile:
    path: str
    language: str
    source: bytes
    units: tuple[Unit, ...]
    declarations: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    failed: bool = False


@dataclass(frozen=True, slots=True)
class FileJob:
    absolute_path: str
    display_path: str
    grammar: str
    language: str


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: Severity
    message: str
    unit: Unit
    value: float | str
    probability: float | None = None
    confidence: float | None = None


@dataclass(slots=True)
class Summary:
    mode: str
    files_discovered: int = 0
    files_parsed: int = 0
    files_failed: int = 0
    units_found: int = 0
    units_evaluated: int = 0
    units_cached: int = 0
    units_skipped: int = 0
    units_failed: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    diagnostics: int = 0
    incomplete: bool = False
    findings: dict[str, int] = field(default_factory=lambda: {str(s): 0 for s in Severity})
    elapsed_seconds: float = 0.0

    def exit_code(self, fail_on: str) -> int:
        if self.incomplete:
            return 2
        if fail_on == "never" or self.mode == "offline":
            return 0
        threshold = SEVERITY_RANK[Severity(fail_on)]
        return int(any(count and SEVERITY_RANK[Severity(level)] >= threshold for level, count in self.findings.items()))
