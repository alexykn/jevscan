"""Descriptor ownership begins before a source stream can be constructed."""

import errno
import os

import pytest

from jevscan.core import source_snapshot


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_failed_stream_adoption_closes_raw_descriptor(tmp_path, monkeypatch, failure):
    (tmp_path / "source.py").write_bytes(b"VALUE = 1\n")
    opened: list[int] = []

    def fail_adoption(descriptor, *_args, **_kwargs):
        opened.append(descriptor)
        raise failure("stream could not adopt descriptor")

    monkeypatch.setattr(source_snapshot.os, "fdopen", fail_adoption)
    with pytest.raises(failure, match="could not adopt descriptor"):
        source_snapshot.read_source(tmp_path, "source.py", 1024)

    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF
