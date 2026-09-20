"""Internal scan contracts. Source offsets are UTF-8 byte offsets; lines are one-based."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


class Kind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLOSURE = "closure"
    CLASS = "class"
    STRUCT = "struct"
    UNION = "union"
    ENUM = "enum"
    TRAIT = "trait"
    IMPL = "impl"
    INTERFACE = "interface"
    TYPE = "type"
    MODULE = "module"
    PACKAGE = "package"


CALLABLE_KINDS = frozenset({Kind.FUNCTION, Kind.METHOD, Kind.CLOSURE})
TYPE_KINDS = frozenset({Kind.CLASS, Kind.STRUCT, Kind.UNION, Kind.ENUM, Kind.TRAIT, Kind.INTERFACE, Kind.TYPE})
SyntaxFact = Literal[
    "validation_candidate",
    "fallback_candidate",
    "state_transition_candidate",
    "helper_relationship",
    "executable_behavior",
]


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
    has_implementation: bool = True
    display_name: str = ""
    body_start_byte: int | None = None
    body_end_byte: int | None = None
    syntax_facts: tuple[SyntaxFact, ...] = ()

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
class Reference:
    """A syntax occurrence, not a resolved symbol or data-flow edge."""

    name: str
    start_byte: int
    end_byte: int
    kind: Literal["call", "name"]


@dataclass(frozen=True, slots=True)
class SourceBlock:
    """Exact AST declaration/member span for source selection, not a new scored target."""

    start_byte: int
    end_byte: int
    names: tuple[str, ...]
    kind: str


@dataclass(frozen=True, slots=True)
class ParsedFile:
    path: str
    language: str
    source: bytes
    units: tuple[Unit, ...]
    declarations: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    failed: bool = False
    references: tuple[Reference, ...] = ()
    blocks: tuple[SourceBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class FileJob:
    absolute_path: str
    display_path: str
    grammar: str
    language: str


@dataclass(frozen=True, slots=True)
class Target:
    id: str
    scope: Literal["unit", "file"]
    path: str
    language: str
    qualified_name: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    kind: Kind | None = None
    display_name: str = ""

    @classmethod
    def from_unit(cls, unit: Unit) -> "Target":
        return cls(
            unit.id,
            "unit",
            unit.path,
            unit.language,
            unit.qualified_name,
            unit.start_byte,
            unit.end_byte,
            unit.start_line,
            unit.end_line,
            unit.kind,
            unit.display_name,
        )

    @classmethod
    def from_file(cls, parsed: ParsedFile) -> "Target":
        lines = parsed.source.count(b"\n") + (not parsed.source.endswith(b"\n"))
        return cls(
            f"{parsed.path}:file",
            "file",
            parsed.path,
            parsed.language,
            parsed.path,
            0,
            len(parsed.source),
            1,
            max(1, lines),
        )

    def metadata(self) -> dict[str, Any]:
        return asdict(self)

    def model_metadata(self) -> dict[str, Any]:
        """Small target locator for Jev; exact attribution remains local."""
        result = {
            "scope": self.scope,
            "path": self.path,
            "language": self.language,
            "qualified_name": self.qualified_name,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }
        if self.kind is not None:
            result["kind"] = self.kind
        return result


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: Severity
    message: str
    target: Target
    value: float | str
    probability: float | None = None
    confidence: float | None = None


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


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
    file_targets_evaluated: int = 0
    file_targets_skipped: int = 0
    checks_evaluated: int = 0
    checks_skipped: int = 0
    uncertain: int = 0
    not_applicable: int = 0
    enrichment_reviewed: int = 0
    enrichment_reruns: int = 0
    enrichment_resolved: int = 0
    enrichment_calls: int = 0
    enrichment_cache_hits: int = 0
    applicability_skips: int = 0
    context_reduced: int = 0
    size_rejections: int = 0
    request_rejections: int = 0
    compaction_calls: int = 0
    compaction_cache_hits: int = 0
    cache_hits: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    evaluation_input_tokens: int = 0
    compaction_input_tokens: int = 0
    enrichment_input_tokens: int = 0
    evaluation_reserved_input_tokens: int = 0
    compaction_reserved_input_tokens: int = 0
    enrichment_reserved_input_tokens: int = 0
    retry_attempts: int = 0
    estimated_input_tokens: int = 0
    estimated_cost: float = 0.0
    reported_cost: float = 0.0
    planned_checks: int = 0
    planned_requests: int = 0
    planned_input_tokens: int = 0
    planned_state_bytes: int = 0
    diagnostics: int = 0
    incomplete: bool = False
    findings: dict[str, int] = field(default_factory=lambda: {str(s): 0 for s in Severity})
    tentative_findings: dict[str, int] = field(default_factory=lambda: {"warning": 0, "error": 0})
    elapsed_seconds: float = 0.0

    def exit_code(self, fail_on: str) -> int:
        if self.incomplete:
            return 2
        if fail_on == "never" or self.mode == "offline":
            return 0
        threshold = SEVERITY_RANK[Severity(fail_on)]
        return int(any(count and SEVERITY_RANK[Severity(level)] >= threshold for level, count in self.findings.items()))


def emit_diagnostic(sink: EventSink, summary: Summary, diagnostic: Diagnostic) -> None:
    summary.diagnostics += 1
    summary.incomplete |= diagnostic.incomplete
    sink.emit({"event": "diagnostic", **asdict(diagnostic)})
