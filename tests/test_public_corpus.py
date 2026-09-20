"""The public examples are checked through the real parser and production planner."""

import runpy
import shutil
import subprocess
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from jevscan.core.config import load_config
from jevscan.core.context import ContextBuilder
from jevscan.core.languages import language_for
from jevscan.core.models import FileJob
from jevscan.core.parser import parse_source
from jevscan.core.planning import Planner

pytestmark = [pytest.mark.parser, pytest.mark.usefixtures("grammar_runtime")]

CORPUS = Path(__file__).parents[1] / "examples" / "calibration"
EXPANDED = CORPUS / "expanded"
FOCUSED = CORPUS / "focused"
EVOLUTION = CORPUS / "evolution"


def _manifest() -> dict:
    with (CORPUS / "MANIFEST.yaml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _expanded_manifest() -> dict:
    with (EXPANDED / "MANIFEST.yaml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _focused_manifest() -> dict:
    with (FOCUSED / "MANIFEST.yaml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _evolution_manifest() -> dict:
    with (EVOLUTION / "MANIFEST.yaml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _evolution_source_paths(manifest: dict) -> list[Path]:
    source_root = EVOLUTION / manifest["source_root"]
    declared = {entry["source"] for entry in manifest["cases"]}
    declared_relative = {path.removeprefix(f"{manifest['source_root']}/") for path in declared}
    actual = {path.relative_to(EVOLUTION).as_posix() for path in source_root.rglob("*") if path.is_file()}
    cache_files = {path for path in actual if Path(path).suffix == ".pyc" and "__pycache__" in Path(path).parts}
    assert declared <= actual
    assert actual - declared - cache_files == set()
    return [source_root / relative for relative in sorted(declared_relative)]


def _evolution_digest(manifest: dict) -> str:
    source_root = EVOLUTION / manifest["source_root"]
    digest = sha256()
    for path in _evolution_source_paths(manifest):
        digest.update(path.relative_to(source_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_manifest_covers_every_builtin_rule() -> None:
    manifest = _manifest()
    contracts = {entry["name"] for entry in manifest["rule_contracts"]}
    scenarios = {entry["rule"] for entry in manifest["scenarios"]}
    declared_sources = {entry["source"].removeprefix("sources/") for entry in manifest["scenarios"]}
    actual_sources = {path.name for path in (CORPUS / "sources").iterdir() if path.is_file()}
    assert contracts == {f"JEV{index:02d}" for index in range(1, 10)}
    assert scenarios == contracts
    assert declared_sources == actual_sources
    assert all(entry["generator_provenance"] == manifest["provenance"] for entry in manifest["scenarios"])


def test_manifest_source_snapshot_matches_sources() -> None:
    manifest = _manifest()
    digest = sha256()
    for path in sorted((CORPUS / manifest["source_root"]).iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    assert manifest["source_snapshot"]["algorithm"] == "sha256"
    assert manifest["source_snapshot"]["digest"] == digest.hexdigest()


def test_manifest_targets_and_applicability_match_parser_and_planner() -> None:
    manifest = _manifest()
    source_root = CORPUS / manifest["source_root"]
    config = load_config([source_root], cwd=source_root).config

    for scenario in manifest["scenarios"]:
        source = CORPUS / scenario["source"]
        spec = language_for(source)
        assert spec is not None, scenario
        job = FileJob(str(source), source.name, spec.grammar, spec.language)
        parsed = parse_source(source.read_bytes(), job)
        assert not parsed.failed, (scenario, parsed.diagnostics)
        planner = Planner(ContextBuilder(parsed), config)

        target = scenario["target"]
        matches = [
            check
            for check in planner.checks
            if check.rule_id == scenario["rule"]
            and check.target.scope == target["scope"]
            and check.target.qualified_name == target["name"]
        ]
        applicability = scenario["applicability"]
        if applicability["status"] == "applicable":
            assert len(matches) == 1, (scenario, [check.target.metadata() for check in planner.checks])
            actual = matches[0].target
            if target["scope"] == "unit":
                assert actual.kind is not None and actual.kind.value == target["kind"]
            else:
                assert target["kind"] is None and actual.kind is None
            if scenario["rule"] == "JEV05" and scenario["scenario_group"] in {"g28", "g29"}:
                evidence = planner.requested_evidence(matches[0])
                assert evidence.state["coverage"]["file_complete"], scenario
                assert evidence.state["documents"][0]["start_line"] == 1, scenario
        else:
            assert not matches, scenario
            target_id = (
                next(unit.id for unit in parsed.units if unit.qualified_name == target["name"])
                if target["scope"] == "unit"
                else planner.context.file.id
            )
            assert scenario["rule"] in planner.applicability_skips[target_id], scenario

        if target["scope"] == "unit":
            unit = next(unit for unit in parsed.units if unit.qualified_name == target["name"])
            facts = set(unit.syntax_facts)
        else:
            facts = {fact for unit in parsed.units for fact in unit.syntax_facts}
        required_facts = set(applicability["required_facts"])
        if applicability["status"] == "applicable":
            assert required_facts <= facts, scenario
        else:
            assert not required_facts & facts, scenario
        assert set(applicability["observed_facts"]) <= facts, scenario


def test_source_names_do_not_encode_provisional_labels() -> None:
    source_root = CORPUS / "sources"
    forbidden = ("positive", "negative", "clean", "bad", "good", "defect", "violation")
    assert all(not any(token in path.name.lower() for token in forbidden) for path in source_root.iterdir())


def test_expanded_manifest_preserves_independence_groups_and_split() -> None:
    manifest = _expanded_manifest()
    scenarios = manifest["scenarios"]
    rules = {entry["name"] for entry in manifest["rules"]}
    assert rules == {f"JEV{index:02d}" for index in range(1, 10)}
    assert len(scenarios) == 108
    assert manifest["coverage"]["total_independent_groups"] == 52
    assert len(manifest["coverage"]["development_groups"]) == 34
    assert len(manifest["coverage"]["heldout_groups"]) == 18

    groups = {}
    for scenario in scenarios:
        groups.setdefault((scenario["rule"], scenario["scenario_group"]), []).append(scenario)
        assert scenario["partition"] in {"development", "heldout"}
        assert scenario["source"].startswith("sources/")
        assert scenario["provisional_label"] is not None
        assert scenario["rationale"]
    for rule in rules:
        rule_groups = {key[1] for key in groups if key[0] == rule}
        expected_groups = 4 if rule == "JEV01" else 6
        assert len(rule_groups) == expected_groups
        assert manifest["coverage"]["groups_per_rule"][rule] == expected_groups
        assert (
            sum(groups[(rule, group)][0]["partition"] == "development" for group in rule_groups) == expected_groups - 2
        )
        assert sum(groups[(rule, group)][0]["partition"] == "heldout" for group in rule_groups) == 2
    for members in groups.values():
        assert len(members) in {2, 4}
        roles = {member["pair_role"] for member in members}
        assert len(roles) == 2
        assert roles <= {"counterpart", "positive", "ambiguous"}
        assert len({member["partition"] for member in members}) == 1
    for partition in ("development", "heldout"):
        actual_groups = {scenario["scenario_group"] for scenario in scenarios if scenario["partition"] == partition}
        assert actual_groups == set(manifest["coverage"][f"{partition}_groups"])


def test_expanded_manifest_source_snapshot_matches_sources() -> None:
    manifest = _expanded_manifest()
    source_root = EXPANDED / manifest["source_root"]
    digest = sha256()
    for path in sorted(source_root.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    declared_sources = {entry["source"].removeprefix("sources/") for entry in manifest["scenarios"]}
    actual_sources = {path.name for path in source_root.iterdir() if path.is_file()}
    assert declared_sources == actual_sources
    assert manifest["source_snapshot"]["algorithm"] == "sha256"
    assert manifest["source_snapshot"]["digest"] == digest.hexdigest()


def test_expanded_manifest_targets_and_applicability_match_parser_and_planner() -> None:
    manifest = _expanded_manifest()
    source_root = EXPANDED / manifest["source_root"]
    config = load_config([source_root], cwd=source_root).config
    parsed_files = {}
    planners = {}
    for scenario in manifest["scenarios"]:
        source = EXPANDED / scenario["source"]
        if source not in parsed_files:
            spec = language_for(source)
            assert spec is not None, scenario
            job = FileJob(str(source), source.name, spec.grammar, spec.language)
            parsed = parse_source(source.read_bytes(), job)
            assert not parsed.failed, (scenario, parsed.diagnostics)
            parsed_files[source] = parsed
            planners[source] = Planner(ContextBuilder(parsed), config)

        parsed = parsed_files[source]
        planner = planners[source]
        target = scenario["target"]
        matches = [
            check
            for check in planner.checks
            if check.rule_id == scenario["rule"]
            and check.target.scope == target["scope"]
            and check.target.qualified_name == target["name"]
        ]
        assert scenario["applicability"]["status"] == "applicable"
        assert len(matches) == 1, (scenario, [check.target.metadata() for check in planner.checks])
        actual = matches[0].target
        if target["scope"] == "unit":
            assert actual.kind is not None and actual.kind.value == target["kind"]
            unit = next(unit for unit in parsed.units if unit.qualified_name == target["name"])
            facts = set(unit.syntax_facts)
        else:
            assert target["kind"] is None and actual.kind is None
            facts = {fact for unit in parsed.units for fact in unit.syntax_facts}
        applicability = scenario["applicability"]
        assert set(applicability["required_facts"]) <= facts, scenario
        assert set(applicability["observed_facts"]) <= facts, scenario


def test_expanded_sources_do_not_contain_mapping_labels() -> None:
    source_root = EXPANDED / "sources"
    forbidden_names = ("positive", "negative", "clean", "bad", "good", "defect", "violation")
    for path in source_root.iterdir():
        assert not any(token in path.name.lower() for token in forbidden_names)
        text = path.read_text(encoding="utf-8")
        assert "provisional_label" not in text
        assert "scenario_group" not in text


def test_focused_manifest_and_adjudications_cover_neutral_sources() -> None:
    manifest = _focused_manifest()
    with (FOCUSED / "ADJUDICATIONS.yaml").open(encoding="utf-8") as stream:
        adjudications = yaml.safe_load(stream)

    source_root = FOCUSED / manifest["source_root"]
    digest = sha256()
    for path in sorted(path for path in source_root.rglob("*") if path.is_file()):
        digest.update(path.relative_to(source_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())

    cases = manifest["heldout"]
    case_ids = {entry["id"] for entry in cases}
    actual_sources = {path.relative_to(FOCUSED).as_posix() for path in source_root.iterdir() if path.is_file()}
    assert len(cases) == 18
    assert {entry["rule"] for entry in cases} == {"JEV01", "JEV02", "JEV04"}
    assert {entry["source"] for entry in cases} == actual_sources
    assert {entry["case_id"] for entry in adjudications["cases"]} == case_ids
    assert adjudications["source_snapshot_sha256"] == digest.hexdigest()
    assert manifest["source_snapshot"]["digest"] == digest.hexdigest()

    forbidden = ("positive", "negative", "clean", "bad", "good", "defect", "violation")
    for path in source_root.iterdir():
        assert not any(token in path.name.lower() for token in forbidden)
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in ("provisional_label", "scenario_group", "expected_score"))


def test_focused_targets_match_production_parser_and_planner() -> None:
    manifest = _focused_manifest()
    source_root = FOCUSED / manifest["source_root"]
    config = load_config([source_root], cwd=source_root).config

    for scenario in manifest["heldout"]:
        source = FOCUSED / scenario["source"]
        spec = language_for(source)
        assert spec is not None, scenario
        parsed = parse_source(source.read_bytes(), FileJob(str(source), source.name, spec.grammar, spec.language))
        assert not parsed.failed, (scenario, parsed.diagnostics)
        planner = Planner(ContextBuilder(parsed), config)
        target = scenario["target"]
        matches = [
            check
            for check in planner.checks
            if check.rule_id == scenario["rule"]
            and check.target.scope == target["scope"]
            and check.target.qualified_name == target["name"]
        ]
        assert len(matches) == 1, (scenario, [check.target.metadata() for check in planner.checks])
        assert matches[0].target.kind is not None
        assert matches[0].target.kind.value == target["kind"]


def test_evolution_manifest_and_adjudications_cover_neutral_sources() -> None:
    manifest = _evolution_manifest()
    with (EVOLUTION / "ADJUDICATIONS.yaml").open(encoding="utf-8") as stream:
        adjudications = yaml.safe_load(stream)

    source_root = EVOLUTION / manifest["source_root"]
    cases = manifest["cases"]
    case_ids = {entry["id"] for entry in cases}
    adjudicated_cases = {entry["case_id"]: entry for entry in adjudications["cases"]}
    actual_sources = {path.relative_to(EVOLUTION).as_posix() for path in _evolution_source_paths(manifest)}
    assert len(cases) == 22
    assert manifest["coverage"]["total_support_groups"] == 15
    assert {entry["language"] for entry in cases} == {"python", "rust", "javascript", "typescript", "perl"}
    assert {entry["source"] for entry in cases} == actual_sources
    assert set(adjudicated_cases) == case_ids
    assert manifest["source_snapshot"]["digest"] == _evolution_digest(manifest)
    assert adjudications["source_snapshot_sha256"] == _evolution_digest(manifest)

    groups = {entry["support_group"] for entry in cases}
    assert len(groups) == 15
    assert dict(Counter(entry["fixture_class"] for entry in cases)) == manifest["coverage"]["fixture_classes"]
    assert dict(Counter(entry["comparison_bucket"] for entry in cases)) == manifest["coverage"]["comparison_buckets"]
    assert {entry["id"] for entry in adjudications["support_groups"]} == groups
    assert {member for group in adjudications["support_groups"] for member in group["members"]} == case_ids
    assert {entry["comparison_bucket"] for entry in cases} == {
        "jev05_only",
        "jev07_only",
        "both_overlap",
        "neither",
    }
    assert {entry["label"] for entry in adjudications["cases"]} == {"Agree", "Partial", "Disagree"}
    assert {entry["label"] for entry in adjudications["cases"]} == {entry["provisional_label"] for entry in cases}
    for case in cases:
        adjudication = adjudicated_cases[case["id"]]
        source_lines = (EVOLUTION / case["source"]).read_text(encoding="utf-8").splitlines()
        assert adjudication["support_group"] == case["support_group"]
        assert adjudication["comparison_bucket"] == case["comparison_bucket"]
        assert adjudication["label"] == case["provisional_label"]
        assert {
            "JEV05": adjudication["jev05"],
            "JEV07": adjudication["jev07"],
        } == case["baseline_comparison"]
        assert adjudication.get("compared_rule_scope", {}) == case.get("compared_rule_scope", {})
        for rule, scope in case.get("compared_rule_scope", {}).items():
            assert rule in {"JEV05", "JEV07"}
            assert scope in {"unit", "file"}
        supporting_lines = adjudication.get("supporting_lines", [])
        if supporting_lines:
            assert adjudication["supporting_source"] == case["source"]
            assert all(1 <= line <= len(source_lines) for line in supporting_lines)

    forbidden = ("positive", "negative", "clean", "bad", "good", "defect", "violation")
    for path in source_root.iterdir():
        assert not any(token in path.name.lower() for token in forbidden)
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in ("provisional_label", "scenario_group", "expected_score"))


def test_evolution_targets_match_real_parser() -> None:
    manifest = _evolution_manifest()
    with (EVOLUTION / "ADJUDICATIONS.yaml").open(encoding="utf-8") as stream:
        adjudications = yaml.safe_load(stream)
    adjudicated_cases = {entry["case_id"]: entry for entry in adjudications["cases"]}

    for case in manifest["cases"]:
        source = EVOLUTION / case["source"]
        spec = language_for(source)
        assert spec is not None, case
        assert spec.language == case["language"], case
        parsed = parse_source(source.read_bytes(), FileJob(str(source), source.name, spec.grammar, spec.language))
        assert not parsed.failed, (case, parsed.diagnostics)

        target = case["target"]
        matches = [unit for unit in parsed.units if unit.qualified_name == target["name"]]
        assert len(matches) == 1, (case, [unit.qualified_name for unit in parsed.units])
        assert matches[0].kind is not None
        assert matches[0].kind.value == target["kind"]
        adjudication = adjudicated_cases[case["id"]]
        assert all(matches[0].start_line <= line <= matches[0].end_line for line in adjudication["decisive_lines"]), (
            case,
            matches[0].start_line,
            matches[0].end_line,
        )


def test_evolution_snapshot_is_stable_after_runtime_fixture_load() -> None:
    manifest = _evolution_manifest()
    before = _evolution_digest(manifest)
    runpy.run_path(str(EVOLUTION / "sources" / "copper.py"))
    assert _evolution_digest(manifest) == before


def test_evolution_python_contracts_execute() -> None:
    copper = runpy.run_path(str(EVOLUTION / "sources" / "copper.py"))
    store = copper["Store"]()
    assert copper["apply_change"](store, {"key": "one", "value": "first"}) is None
    assert store.values == {"one": "first"}
    assert store.history == ["one"]

    failed_store = copper["Store"](fail_history=True)
    with pytest.raises(OSError):
        copper["apply_change"](failed_store, {"key": "one", "value": "first"})
    assert failed_store.values == {"one": "first"}
    assert failed_store.history == []

    harvest = runpy.run_path(str(EVOLUTION / "sources" / "harvest.py"))
    ledger = harvest["Ledger"]()
    assert harvest["advance_record"](ledger, "one", "first") == 1
    assert ledger.current == ("one", "first")
    assert ledger.index == {"one": 0}
    assert ledger.history == ["one"]
    assert ledger.cursor == 1

    failed_ledger = harvest["Ledger"](fail_history=True)
    with pytest.raises(OSError):
        harvest["advance_record"](failed_ledger, "one", "first")
    assert failed_ledger.current == ("one", "first")
    assert failed_ledger.index == {"one": 0}
    assert failed_ledger.history == []
    assert failed_ledger.cursor == 0

    ripple = runpy.run_path(str(EVOLUTION / "sources" / "ripple.py"))
    entry = {"key": "one", "value": "first"}
    audited_store = ripple["Store"]()
    failed_audit = ripple["Audit"](fail=True)
    assert ripple["publish"](audited_store, failed_audit, entry) == entry
    assert audited_store.entries == [entry]
    assert failed_audit.records == []


@pytest.mark.skipif(shutil.which("perl") is None, reason="Perl runtime is unavailable")
def test_evolution_perl_cursor_contract_executes() -> None:
    perl = shutil.which("perl")
    assert perl is not None
    source = (EVOLUTION / "sources" / "harvest.pl").resolve().as_posix()
    escaped_source = source.replace("\\", "\\\\").replace("'", "\\'")
    script = """
require '__SOURCE__';
my $state = Marker->new();
my $result = $state->apply('one', 'first');
die 'success' unless $result == 1 && $state->cursor() == 1;
die 'current' unless $state->{current}->{key} eq 'one';
die 'history' unless @{$state->{history}} == 1;
my $failed = Marker->new(fail_history => 1);
my $ok = eval { $failed->apply('one', 'first'); 1 };
die 'failure' if $ok;
die 'partial' unless defined $failed->{current}
    && @{$failed->{history}} == 0
    && $failed->cursor() == 0;
""".replace("__SOURCE__", escaped_source)
    subprocess.run([perl, "-e", script], check=True, capture_output=True, text=True)  # noqa: S603
