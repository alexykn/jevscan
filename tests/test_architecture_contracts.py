"""Behavioral contracts at refactored ownership boundaries."""

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml
from test_calibration_selection import _cases, _score_rule

from jevscan.core.calibration_selection import select_policies
from jevscan.core.client import JevClient, ReservationUsage
from jevscan.core.config import BudgetConfig, JevConfig
from jevscan.core.protocol import BudgetExhaustedError
from jevscan.core.rule_writeback import SuppliedRules
from jevscan.core.rules import NoulQuestion


@pytest.fixture
def supplied_rules(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump({"version": 4, "rules": [{"name": "custom-rule", **_score_rule().model_dump(mode="json")}]}),
        encoding="utf-8",
    )
    return SuppliedRules.load(path, [])


def test_writeback_preserves_concurrent_edits_and_cleans_temporary_files(supplied_rules):
    audit = select_policies(_cases(["Agree", "Disagree", "Disagree"], scores=[3, 2, 2]))
    concurrent = supplied_rules.original + b"\n# An edit made after loading.\n"
    supplied_rules.path.write_bytes(concurrent)

    with pytest.raises(ValueError, match="changed while selection was running"):
        supplied_rules.apply(audit)

    assert supplied_rules.path.read_bytes() == concurrent
    assert not list(supplied_rules.path.parent.glob(".rules.yaml.*"))


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_writeback_validation_failure_never_publishes_or_leaks_temporary_files(supplied_rules, monkeypatch, failure):
    from jevscan.core import rule_writeback

    audit = select_policies(_cases(["Agree", "Disagree", "Disagree"], scores=[3, 2, 2]))

    def reject(*_args, **_kwargs):
        raise failure("validation interrupted")

    monkeypatch.setattr(rule_writeback, "load_config", reject)
    with pytest.raises(failure, match="validation interrupted"):
        supplied_rules.apply(audit)

    assert supplied_rules.path.read_bytes() == supplied_rules.original
    assert not list(supplied_rules.path.parent.glob(".rules.yaml.*"))


async def test_cancelled_transport_finishes_accounting_without_refunding_paid_attempt():
    started = asyncio.Event()
    release = asyncio.Event()

    async def transport(_request):
        started.set()
        await release.wait()
        return httpx.Response(500)

    config = JevConfig(requests_per_minute=0, retries=0)
    reservation = ReservationUsage()
    questions = {"q": NoulQuestion(type="noul", instructions="Is the source inconsistent?")}
    async with JevClient(
        config,
        "test-key",
        transport=httpx.MockTransport(transport),
        budget=BudgetConfig(max_requests=1),
    ) as client:
        task = asyncio.create_task(client.evaluate(b"{}", questions, reservation=reservation))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.requests == client.completed_requests == 1
            assert reservation.input_tokens == client.estimated_input_tokens > 0
            before = (client.requests, client.completed_requests, client.estimated_input_tokens, client.estimated_cost)
            with pytest.raises(BudgetExhaustedError):
                await client.evaluate(b"{}", questions)
            assert (client.requests, client.completed_requests, client.estimated_input_tokens, client.estimated_cost) == before
        finally:
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)


def test_core_has_no_cli_dependency():
    import ast

    root = Path(__file__).parents[1] / "src" / "jevscan" / "core"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("jevscan.cli"), path
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("jevscan.cli") for alias in node.names), path
