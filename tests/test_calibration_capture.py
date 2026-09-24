from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

from calibration import capture
from calibration.capture import (
    MODEL,
    PreparedScenario,
    Scenario,
    _batches,
    _capture_budget,
    _capture_live,
    _plan,
    _prepare,
)
from jevscan.core.config import Config
from jevscan.core.context import Evidence
from jevscan.core.models import Kind, ParsedFile, Target, Unit
from jevscan.core.planning import RequestBudget
from jevscan.core.protocol import Check, JevResponse, encode

SOURCE = b"def work():\n    return 1\n"


def _unit(source: bytes, path: str = "same.py") -> Unit:
    return Unit(
        f"{path}:0:function",
        path,
        "python",
        Kind.FUNCTION,
        "work",
        "work",
        None,
        0,
        len(source),
        1,
        2,
        "def work():",
        True,
    )


def _scenario(
    scenario_id: str,
    rule: Any,
    *,
    parsed: ParsedFile | None = None,
    target: Target | None = None,
    group: str = "group",
) -> Scenario:
    parsed = parsed or ParsedFile("same.py", "python", SOURCE, (_unit(SOURCE),))
    target = target or Target.from_unit(parsed.units[0])
    return Scenario(
        scenario_id,
        group,
        "development",
        parsed.path,
        parsed.language,
        parsed.language,
        parsed.source,
        hashlib.sha256(parsed.source).hexdigest(),
        parsed,
        target,
        "cohesion",
        rule,
        {},
        {},
        (),
    )


def _prepared(
    scenario_id: str,
    rule: Any,
    evidence: Evidence,
    *,
    parsed: ParsedFile | None = None,
    target: Target | None = None,
    group: str = "group",
) -> PreparedScenario:
    scenario = _scenario(scenario_id, rule, parsed=parsed, target=target, group=group)
    return PreparedScenario(
        scenario,
        Check(scenario_id, scenario.target, scenario.rule_id, rule),
        evidence,
        True,
        True,
    )


def _evidence(content: str) -> Evidence:
    state = {
        "documents": [
            {
                "path": "same.py",
                "language": "python",
                "start_byte": 0,
                "end_byte": len(content.encode()),
                "start_line": 1,
                "end_line": 1,
                "content": content,
            }
        ],
        "coverage": {"file_complete": True},
    }
    return Evidence(state, encode(state))


def _evaluation_config(config: Config, **updates: Any) -> Config:
    return config.model_copy(update={"evaluation": config.evaluation.model_copy(update=updates)})


def test_prepare_separates_same_relative_path_from_distinct_parsed_sources(config, basic_rule) -> None:
    first_source = b"def work():\n    return 1\n"
    second_source = b"def work():\n    return 2\n"
    first_parsed = ParsedFile("same.py", "python", first_source, (_unit(first_source),))
    second_parsed = ParsedFile("same.py", "python", second_source, (_unit(second_source),))

    prepared = _prepare(
        [
            _scenario("first", basic_rule, parsed=first_parsed),
            _scenario("second", basic_rule, parsed=second_parsed),
        ],
        config,
    )

    assert prepared[0].evidence.state["documents"][0]["content"] == "def work():\n    return 1\n"
    assert prepared[1].evidence.state["documents"][0]["content"] == "def work():\n    return 2\n"
    assert prepared[0].evidence.key != prepared[1].evidence.key


def test_batches_split_before_request_budget_violation_and_plan_live_parity(
    config, basic_rule, monkeypatch, tmp_path
) -> None:
    evidence = _evidence("x" * 2_000)
    first = _prepared("first", basic_rule, evidence)
    second = _prepared("second", basic_rule, evidence)
    unconstrained_budget = RequestBudget(config.evaluation, MODEL)
    singleton = unconstrained_budget.body(evidence.encoded, {first.check.id: encode(first.check.question())})
    bounded = _evaluation_config(config, max_questions=10, max_request_bytes=len(singleton) + 1)
    budget = RequestBudget(bounded.evaluation, MODEL)

    batches = _batches([first, second], bounded)
    plan = _plan([first, second], bounded)

    assert len(batches) == 2
    assert plan["requests"] == len(batches)
    assert [record["question_ids"] for record in plan["batches"]] == [sorted(batch.questions) for batch in batches]
    for batch in batches:
        body = budget.body(batch.evidence.encoded, batch.wires)
        assert budget.fits(batch.evidence.encoded, batch.wires)
        assert len(body) <= bounded.evaluation.max_request_bytes

    class FakeClient:
        instances: ClassVar[list[FakeClient]] = []

        def __init__(self, _jev, _api_key, *, budget, **_kwargs) -> None:
            self.budget = budget
            self.calls: list[tuple[bytes, dict[str, Any]]] = []
            self.__class__.instances.append(self)

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None: ...

        async def evaluate(self, body, questions, *, reservation):
            self.calls.append((body, questions))
            reservation.input_tokens = 1
            return JevResponse.model_validate({
                "model": MODEL,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "answers": {question_id: {"type": "noul", "noul": 0.5} for question_id in questions},
            })

    monkeypatch.setattr(capture, "JevClient", FakeClient)
    asyncio.run(
        _capture_live(
            batches,
            bounded,
            "https://api.typesafe.ai",
            "test-key",
            0.01,
            {"manifest_sha256": "manifest"},
            tmp_path / "answers.jsonl",
            tmp_path / "receipts.jsonl",
        )
    )

    calls = FakeClient.instances[0].calls
    assert [sorted(questions) for _, questions in calls] == [record["question_ids"] for record in plan["batches"]]
    assert [body for body, _ in calls] == [budget.body(batch.evidence.encoded, batch.wires) for batch in batches]


def test_batches_reject_singleton_that_exceeds_request_budget(config, basic_rule) -> None:
    evidence = _evidence("x" * 2_000)
    bounded = _evaluation_config(config, max_request_bytes=1024)

    with pytest.raises(ValueError, match=r"single question/evidence request exceeds request budget.*bytes"):
        _batches([_prepared("oversized", basic_rule, evidence)], bounded)


def _import_fixture(basic_rule) -> tuple[list[PreparedScenario], dict[str, Any]]:
    first = _prepared("z-case", basic_rule, _evidence("first"))
    second = _prepared("a-case", basic_rule, _evidence("second"))
    return [first, second], {"manifest_sha256": "manifest"}


def _write_import_inputs(
    tmp_path: Path,
    prepared: list[PreparedScenario],
    rows: list[dict[str, Any]],
) -> argparse.Namespace:
    answers = tmp_path / "answers.jsonl"
    answers.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    answers.with_suffix(".meta.json").write_text(json.dumps({"manifest_sha256": "manifest"}), encoding="utf-8")
    adjudications = tmp_path / "adjudications.json"
    adjudications.write_text(
        json.dumps({
            "cases": [
                {"scenario_id": item.scenario.scenario_id, "expected_answer": False, "reason": "reviewed"}
                for item in prepared
            ]
        }),
        encoding="utf-8",
    )
    return argparse.Namespace(
        manifest=tmp_path / "manifest.yaml",
        config=None,
        snapshot_sha256=None,
        answers=answers,
        adjudications=adjudications,
        output=tmp_path / "cases.jsonl",
        summary=tmp_path / "summary.json",
    )


class _Case:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id

    def model_dump(self, *, mode: str) -> dict[str, str]:
        assert mode == "json"
        return {"case_id": self.case_id}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row, _prepared: row.update({"scenario_id": "unknown"}), "unknown or unexpected"),
        (lambda row, _prepared: row.update({"rule_id": "wrong-rule"}), "captured rule_id"),
        (lambda row, _prepared: row.update({"evidence_sha256": "wrong-evidence"}), "evidence_sha256"),
    ],
)
def test_cmd_import_rejects_capture_identity_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config,
    basic_rule,
    mutation,
    message: str,
) -> None:
    prepared, metadata = _import_fixture(basic_rule)
    row = {
        "scenario_id": prepared[0].scenario.scenario_id,
        "rule_id": prepared[0].scenario.rule_id,
        "answer": {"type": "noul", "noul": 0.5},
        "returned_model": MODEL,
        "evidence_sha256": prepared[0].evidence.key,
    }
    mutation(row, prepared)
    args = _write_import_inputs(tmp_path, prepared, [row])
    monkeypatch.setattr(capture, "_load_manifest", lambda *_args: (metadata, [], config))
    monkeypatch.setattr(capture, "_prepare", lambda *_args: prepared)

    with pytest.raises(ValueError, match=message):
        capture.cmd_import(args)


def test_cmd_import_emits_cases_in_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config,
    basic_rule,
) -> None:
    prepared, metadata = _import_fixture(basic_rule)
    rows = [
        {
            "scenario_id": item.scenario.scenario_id,
            "rule_id": item.scenario.rule_id,
            "answer": {"type": "noul", "noul": 0.5},
            "returned_model": MODEL,
            "evidence_sha256": item.evidence.key,
        }
        for item in reversed(prepared)
    ]
    args = _write_import_inputs(tmp_path, prepared, rows)
    monkeypatch.setattr(capture, "_load_manifest", lambda *_args: (metadata, [], config))
    monkeypatch.setattr(capture, "_prepare", lambda *_args: prepared)
    monkeypatch.setattr(
        capture,
        "_make_case",
        lambda item, *_args: _Case(item.scenario.scenario_id),
    )

    assert capture.cmd_import(args) == 0
    emitted = [json.loads(line)["case_id"] for line in args.output.read_text(encoding="utf-8").splitlines()]
    assert emitted == [item.scenario.scenario_id for item in prepared]


def test_capture_budget_accounts_for_configured_retries() -> None:
    assert _capture_budget(3, 0.01).max_requests == 3
    assert _capture_budget(3, 0.01, retries=2).max_requests == 9
