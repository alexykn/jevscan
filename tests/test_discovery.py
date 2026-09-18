from pathlib import Path

from jevscan.core.config import ScanConfig
from jevscan.core.discovery import discover
from jevscan.core.models import Diagnostic, FileJob


def test_filters_nested_gitignore_and_overlapping_targets(tmp_path: Path) -> None:
    paths = ["main.py", "ignore.py", "src/keep.rs", "src/skip.rs", "node_modules/lib.js", "lib.pm", "view.tsx"]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (tmp_path / ".gitignore").write_text("ignore.py\n*.rs\n")
    (tmp_path / "src/.gitignore").write_text("!keep.rs\n")
    items = list(discover([tmp_path, tmp_path / "main.py"], tmp_path, ScanConfig()))
    found = {item.display_path for item in items if isinstance(item, FileJob)}
    assert found == {"main.py", "src/keep.rs", "lib.pm", "view.tsx"}
    assert len(items) == len(found)


def test_explicit_missing_and_excluded_files_are_visible(tmp_path: Path) -> None:
    hidden = tmp_path / "a.min.js"
    hidden.write_text("")
    items = list(discover([hidden, tmp_path / "missing.py"], tmp_path, ScanConfig()))
    assert {item.code for item in items if isinstance(item, Diagnostic)} == {"excluded-target", "missing-target"}


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    source = tmp_path / "real.py"
    source.write_text("")
    (tmp_path / "link.py").symlink_to(source)
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    jobs = [item for item in discover([tmp_path], tmp_path, ScanConfig()) if isinstance(item, FileJob)]
    assert [job.display_path for job in jobs] == ["real.py"]


def test_parent_ignores_apply_when_scanning_a_subdirectory(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.py\n")
    child = tmp_path / "child"
    child.mkdir()
    (child / "x.py").write_text("")
    assert list(discover([child], tmp_path, ScanConfig())) == []


def test_wide_walk_is_lazy_and_closes_iterators(tmp_path: Path, monkeypatch) -> None:
    import os

    for index in range(30):
        folder = tmp_path / str(index)
        folder.mkdir()
        (folder / "sample.py").write_text("")
    original = os.scandir
    reads = 0
    opened = []

    class TrackedEntries:
        def __init__(self, path):
            self.entries = original(path)
            self.closed = False
            opened.append(self)

        def __next__(self):
            nonlocal reads
            reads += 1
            return next(self.entries)

        def close(self):
            self.closed = True
            self.entries.close()

    monkeypatch.setattr(os, "scandir", TrackedEntries)
    iterator = discover([tmp_path], tmp_path, ScanConfig())
    assert isinstance(next(iterator), FileJob)
    assert reads == 2  # One directory entry, then one file; not all thirty siblings.
    iterator.close()
    assert all(item.closed for item in opened)
