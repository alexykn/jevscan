"""Small validation primitives for configuration and wire contracts.

These helpers keep validation code declarative: callers state an invariant and
its error message instead of repeatedly spelling control-flow plumbing.
"""

from collections.abc import Collection, Iterable, Sequence
from typing import TypeVar

T = TypeVar("T")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def require_unique(values: Sequence[T], message: str) -> None:
    require(len(values) == len(set(values)), message)


def require_nonempty(values: Collection[object], message: str) -> None:
    require(bool(values), message)


def require_subset(values: Collection[T], allowed: Collection[T], message: str) -> None:
    require(not (set(values) - set(allowed)), message)


def require_disjoint(groups: Iterable[Collection[T]], message: str) -> None:
    seen: set[T] = set()
    for group in groups:
        current = set(group)
        require(not (seen & current), message)
        seen.update(current)


def require_none(values: Iterable[object | None], message: str) -> None:
    require(all(value is None for value in values), message)


def require_exactly_one(values: Iterable[object | None], message: str) -> None:
    require(sum(value is not None for value in values) == 1, message)
