from pathlib import Path

import pytest

from jevscan.core.config import Config, JevConfig, Rule, ScanConfig
from jevscan.core.models import Kind, Unit


@pytest.fixture
def basic_rule() -> Rule:
    return Rule.model_validate({
        "applies_to": ["function", "method", "class"],
        "question": {"type": "noul", "instructions": "Is this operation incohesive?"},
        "report": {
            "message": "Mixed responsibilities.",
            "levels": {
                "warning": {"min_probability": 0.60},
                "error": {"min_probability": 0.85},
            },
        },
    })


@pytest.fixture
def config(basic_rule: Rule) -> Config:
    return Config(rules={"cohesion": basic_rule}, scan=ScanConfig(jobs=2, batch_size=2, queue_size=3),
                  jev=JevConfig(concurrency=3, requests_per_minute=0, retries=0))


@pytest.fixture
def unit() -> Unit:
    return Unit("sample.py:0:function", "sample.py", "python", Kind.FUNCTION, "work", "work", None,
                0, 30, 1, 2, "def work():", True)


@pytest.fixture
def grammar_runtime() -> None:
    pytest.importorskip("tree_sitter", reason="real Tree-sitter runtime is not installed")
    pytest.importorskip("tree_sitter_language_pack", reason="real bundled grammars are not installed")


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"
