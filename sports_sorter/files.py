from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from .core import Database


def protected_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    banned = Path.home() / "Downloads"
    if path == banned or banned in path.parents:
        raise ValueError("Access to Downloads is forbidden")
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise ValueError("Symbolic links are forbidden")
    return path


def _outside_source(path: Path, source: Path) -> None:
    if path == source or source in path.parents or path in source.parents:
        raise ValueError("State/output and source must not overlap")


def _read_file(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Expected a regular file")
    return os.fdopen(fd, "rb")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _read_file(protected_path(path)) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_source_file(db: Database, row: dict | sqlite3.Row) -> Path:
    relpath = Path(row["relpath"])
    if relpath.is_absolute() or ".." in relpath.parts:
        raise ValueError("Invalid source relative path")
    path = protected_path(db.source / relpath)
    if not path.is_file():
        raise FileNotFoundError("Source file unavailable")
    return path
