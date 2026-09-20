from os import fsync, replace
from pathlib import Path


def publish(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        fsync(stream.fileno())
    replace(temporary, path)
