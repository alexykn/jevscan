"""Small path-identity checks for CLI inputs and outputs."""

from pathlib import Path


def paths_alias(first: Path, second: Path) -> bool:
    """Return whether two paths identify the same filesystem object or name."""
    try:
        if first.exists() and second.exists() and first.samefile(second):
            return True
    except OSError:
        pass
    return first.resolve(strict=False) == second.resolve(strict=False)
