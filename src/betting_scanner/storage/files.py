"""Small atomic JSON writes and a cross-platform single-writer directory lock."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from betting_scanner.core.models import DomainModel


def atomic_write(path: Path, model: DomainModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def directory_lock(output: Path, name: str = ".download.lock") -> Iterator[None]:
    """The OS releases the lock on interruption or process death."""
    output.mkdir(parents=True, exist_ok=True)
    if Path(name).name != name:
        raise ValueError("Lock name must be a filename")
    with (output / name).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError(
                "Dataset is already in use by another collection"
            ) from None
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
