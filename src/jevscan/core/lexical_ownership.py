"""Associate syntax references with their innermost lexical unit.

This is lexical containment only. It deliberately does not resolve names, calls,
types, imports, or data flow.
"""

from collections.abc import Iterable, Iterator

from jevscan.core.models import Reference, Unit


def owned_references(
    units: Iterable[Unit],
    references: Iterable[Reference],
) -> Iterator[tuple[Reference, Unit | None]]:
    """Yield each reference with the innermost unit that contains its full span."""
    following_units = iter(units)
    following = next(following_units, None)
    stack: list[Unit] = []

    for reference in sorted(references, key=lambda item: item.start_byte):
        while following is not None and following.start_byte <= reference.start_byte:
            while stack and following.start_byte >= stack[-1].end_byte:
                stack.pop()
            stack.append(following)
            following = next(following_units, None)

        while stack and reference.end_byte > stack[-1].end_byte:
            stack.pop()

        yield reference, stack[-1] if stack else None
